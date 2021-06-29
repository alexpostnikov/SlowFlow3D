import pytorch_lightning as pl
import torch
from torch.nn import functional as F
from pytorch_lightning.metrics import functional as FM
from argparse import ArgumentParser

from networks import PillarFeatureNetScatter, ConvEncoder, ConvDecoder, UnpillarNetworkScatter, PointFeatureNet
from .utils import init_weights


class FastFlow3DModelScatter(pl.LightningModule):
    def __init__(self, n_pillars_x, n_pillars_y,
                 background_weight=0.1,
                 point_features=8,
                 learning_rate=1e-6,
                 adam_beta_1=0.9,
                 adam_beta_2=0.999):
        super(FastFlow3DModelScatter, self).__init__()
        self.save_hyperparameters()  # Store the constructor parameters into self.hparams

        self._point_feature_net = PointFeatureNet(in_features=point_features, out_features=64)
        self._point_feature_net.apply(init_weights)

        self._pillar_feature_net = PillarFeatureNetScatter(n_pillars_x=n_pillars_x, n_pillars_y=n_pillars_y,
                                                           out_features=64)
        self._pillar_feature_net.apply(init_weights)

        self._conv_encoder_net = ConvEncoder(in_channels=64, out_channels=256)
        self._conv_encoder_net.apply(init_weights)

        self._conv_decoder_net = ConvDecoder()
        self._conv_decoder_net.apply(init_weights)

        self._unpillar_network = UnpillarNetworkScatter(n_pillars_x=n_pillars_x, n_pillars_y=n_pillars_y)
        self._unpillar_network.apply(init_weights)

        self._n_pillars_x = n_pillars_x
        self._background_weight = background_weight

        # ----- Metrics information -----
        # TODO delete no flow class
        self._classes = [(0, 'background'), (1, 'vehicle'), (2, 'pedestrian'), (3, 'sign'), (4, 'cyclist')]
        self._thresholds = [(1, '1_1'), (0.1, '1_10')]  # 1/1 = 1, 1/10 = 0.1

    def _transform_point_cloud_to_embeddings(self, pc, mask):
        # Flatten the first two dimensions to get the points as batch dimension
        previous_batch_pc_embedding = self._point_feature_net(pc.flatten(0, 1))
        # Output is (batch_size * points, embedding_features)
        # Set the points to 0 that are just there for padding
        previous_batch_pc_embedding[mask.flatten(0, 1), :] = 0
        # Retransform into batch dimension (batch_size, max_points, embedding_features)
        previous_batch_pc_embedding = previous_batch_pc_embedding.unflatten(0,
                                                                            (pc.size(0),
                                                                             pc.size(1)))
        return previous_batch_pc_embedding

    def forward(self, x):
        """
        The usual forward pass function of a torch module
        :param x:
        :return:
        """
        # 1. Do scene encoding of each point cloud to get the grid with pillar embeddings
        # Input is a point cloud each with shape (N_points, point_features)

        # The input here is more complex as we deal with a batch of point clouds
        # that do not have a fixed amount of points
        # x is a tuple of two lists representing the batches
        previous_batch, current_batch = x
        previous_batch_pc, previous_batch_grid, previous_batch_mask = previous_batch
        current_batch_pc, current_batch_grid, current_batch_mask = current_batch

        # batch_pc = (batch_size, N, 8) | batch_grid = (n_batch, N, 2) | batch_mask = (n_batch, N)
        # The grid indices are (batch_size, max_points) long. But we need them as
        # (batch_size, max_points, feature_dims) to work. Features are in all necessary cases 64.
        # Expand does only create multiple views on the same datapoint and not allocate extra memory
        current_batch_grid = current_batch_grid.unsqueeze(-1).expand(-1, -1, 64)
        previous_batch_grid = previous_batch_grid .unsqueeze(-1).expand(-1, -1, 64)

        # Pass the whole batch of point clouds to get the embedding for each point in the cloud
        # Input pc is (batch_size, max_n_points, features_in)
        # per each point, there are 8 features: [cx, cy, cz,  Δx, Δy, Δz, l0, l1], as stated in the paper
        previous_batch_pc_embedding = self._transform_point_cloud_to_embeddings(previous_batch_pc.float(),
                                                                                previous_batch_mask)
        # previous_batch_pc_embedding = [n_batch, N, 64]
        # Output pc is (batch_size, max_n_points, embedding_features)
        current_batch_pc_embedding = self._transform_point_cloud_to_embeddings(current_batch_pc.float(),
                                                                               current_batch_mask)

        # Now we need to scatter the points into their 2D matrix
        # batch_pc_embeddings -> (batch_size, N, 64)
        # batch_grid -> (batch_size, N, 64)
        previous_pillar_embeddings = self._pillar_feature_net(previous_batch_pc_embedding, previous_batch_grid)
        current_pillar_embeddings = self._pillar_feature_net(current_batch_pc_embedding, current_batch_grid)
        # pillar_embeddings = (batch_size, 64, 512, 512)

        # 2. Apply the U-net encoder step
        # Note that weight sharing is used here. The same U-net is used for both point clouds.
        # Corresponds to F, L and R from the paper
        prev_64_conv, prev_128_conv, prev_256_conv = self._conv_encoder_net(previous_pillar_embeddings)
        cur_64_conv, cur_128_conv, cur_256_conv = self._conv_encoder_net(current_pillar_embeddings)

        # 3. Apply the U-net decoder with skip connections
        grid_flow_embeddings = self._conv_decoder_net(previous_pillar_embeddings, prev_64_conv,
                                                      prev_128_conv, prev_256_conv,
                                                      current_pillar_embeddings, cur_64_conv,
                                                      cur_128_conv, cur_256_conv)

        # grid_flow_embeddings -> [batch_size, 64, 512, 512]

        # 4. Apply the unpillar and flow prediction operation
        predictions = self._unpillar_network(grid_flow_embeddings, current_batch_pc_embedding, current_batch_grid)

        # List of batch size many output with each being a (N_points, 3) flow prediction.
        return predictions

    def compute_metrics(self, y, y_hat, labels):
        """

        :param y: predicted data (batch_size, max_points, 3)
        :param y_hat: ground truth (batch_size, max_points, 3)
        :param labels:
        :return:
        """
        # y, y_hat = (batch_size, N, 3)
        # mask = (batch_size, N)
        # weights = (batch_size, N, 1)
        # labels = (batch_size, N)
        # background_weight = float

        # First we flatten the batch dimension since it will make computations easier
        # For computing the metrics it is not needed to distinguish between batches
        # y = y.flatten(0, 1)
        # y_hat = y_hat.flatten(0, 1)
        # labels = labels.flatten(0, 1)
        # Flattened versions -> Second dimension is batch_size * N

        squared_root_difference = torch.sqrt(torch.sum((y - y_hat) ** 2, dim=1))
        # We compute the weighting vector for background_points
        # weights is a mask which background_weight value for backgrounds and 1 for no backgrounds, in order to
        # downweight the background points
        weights = torch.ones((squared_root_difference.shape[0]),
                             device=squared_root_difference.device,
                             dtype=squared_root_difference.dtype)  # weights -> (batch_size * N)
        weights[labels == -1] = 0
        weights[labels == 0] = self._background_weight

        loss = torch.sum(weights * squared_root_difference) / torch.sum(weights)
        # ---------------- Computing rest of metrics -----------------
        metrics = {}
        # --- Computing L2 with threshold ---
        for threshold, name in self._thresholds:
            L2_list = {}
            for label, class_name in self._classes:
                correct_prediction = (squared_root_difference < threshold).sum()
                metric = correct_prediction / squared_root_difference.shape[0]
                L2_list[class_name] = metric
            metrics[name] = L2_list
        return loss, metrics

    # def compute_metrics(self, y, y_hat, mask, label):
    #    # L2 with threshold 1 m/s
    #    (mask * (y - y_hat) ** 2))
    # https://stackoverflow.com/questions/53906380/average-calculation-in-python
    # https://www.geeksforgeeks.org/numpy-maskedarray-mean-function-python/

    def general_step(self, batch, batch_idx, mode):
        """
        A function to share code between all different steps.
        :param batch: the batch to perform on
        :param batch_idx: the id of the batch
        :param mode: str of "train", "val", "test". Useful if specific things are required.
        :return:
        """
        x, y = batch
        y_hat = self(x)
        # x is a list of input batches with the necessary data
        # For loss calculation we need to know which elements are actually present and not padding
        # Therefore we need the mast of the current frame as batch tensor
        # It is True for all points that just are padded and of size (batch_size, max_points)
        # Invert the matrix for our purpose to get all points that are NOT padded
        current_frame_masks = ~x[1][2]

        # Remove all points that are padded
        y = y[current_frame_masks]
        y_hat = y_hat[current_frame_masks]
        # This will yield a (n_real_points, 3) tensor with the batch size being included already

        # The first 3 dimensions are the actual flow. The last dimension is the class id.
        y_flow = y[:, :3]
        # Loss computation
        labels = y[:, -1]
        loss, metrics = self.compute_metrics(y_flow, y_hat, labels)

        return loss, metrics

    def log_metrics(self, loss, metrics, phase):
        # phase should be training, validation or test
        metrics_dict = {}
        for metric in metrics:
            for label in metrics[metric]:
                metrics_dict[f'{phase}/{metric}/{label}'] = metrics[metric][label]

        # Do not log the in depth metrics in the progress bar
        self.log(f'{phase}/loss', loss, on_step=True, on_epoch=True, prog_bar=True, logger=True)
        self.log_dict(metrics_dict, on_step=True, on_epoch=True, prog_bar=False, logger=True)

    def training_step(self, batch, batch_idx):
        """
        This method is specific to pytorch lightning.
        It is called for each minibatch that the model should be trained for.
        Basically a part of the normal training loop is just moved here.

        model.train() is already set!
        :param batch: (data, target) of batch size
        :param batch_idx: the id of this batch e.g. for discounting?
        :return:
        """
        phase = "train"
        loss, metrics = self.general_step(batch, batch_idx, phase)
        # Automatically reduces this metric after each epoch
        self.log_metrics(loss, metrics, phase)
        # Return loss for backpropagation
        return loss

    def validation_step(self, batch, batch_idx):
        """
        Similar to the train step.
        Already has model.eval() and torch.nograd() set!
        :param batch:
        :param batch_idx:
        :return:
        """
        phase = "val"
        loss, metrics = self.general_step(batch, batch_idx, phase)
        # Automatically reduces this metric after each epoch
        self.log_metrics(loss, metrics, phase)

    def test_step(self, batch, batch_idx):
        """
        Similar to the train step.
        Already has model.eval() and torch.nograd() set!
        :param batch:
        :param batch_idx:
        :return:
        """
        phase = "test"
        loss, metrics = self.general_step(batch, batch_idx, phase)
        # Automatically reduces this metric after each epoch
        self.log_metrics(loss, metrics, phase)

    def configure_optimizers(self):
        """
        Also pytorch lightning specific.
        Define the optimizers in here this will return the optimizer that is used to train this module.
        Also define learning rate scheduler in here. Not sure how this works...
        :return: The optimizer to use
        """
        # Defaults are the same as for pytorch
        betas = (
            self.hparams.adam_beta_1 if self.hparams.adam_beta_1 is not None else 0.9,
            self.hparams.adam_beta_1 if self.hparams.adam_beta_2 is not None else 0.999)

        return torch.optim.Adam(self.parameters(), lr=self.hparams.learning_rate, betas=betas, weight_decay=0)

    @staticmethod
    def add_model_specific_args(parent_parser):
        """
        Method to add all command line arguments specific to this module.
        Used to dynamically add the correct arguments required.
        :param parent_parser: The current argparser to add the options to
        :return: the new argparser with the new options
        """
        parser = ArgumentParser(parents=[parent_parser], add_help=False)
        parser.add_argument('--learning_rate', type=float, default=1e-6)
        return parser
