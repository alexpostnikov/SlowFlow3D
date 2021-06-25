import torch
from argparse import ArgumentParser
import pytorch_lightning as pl
import wandb

from pathlib import Path

from pytorch_lightning.loggers import WandbLogger

from data import WaymoDataModule
from models import FastFlow3DModel, FastFlow3DModelScatter


def cli():
    parser = ArgumentParser()
    parser.add_argument('data_directory', type=str)
    parser.add_argument('experiment_name', type=str)
    parser.add_argument('--batch_size', default=2, type=int)
    parser.add_argument('--x_max', default=85, type=float)
    parser.add_argument('--x_min', default=-85, type=float)
    parser.add_argument('--y_max', default=85, type=float)
    parser.add_argument('--y_min', default=-85, type=float)
    parser.add_argument('--z_max', default=3, type=float)
    parser.add_argument('--z_min', default=-3, type=float)
    parser.add_argument('--grid_size', default=512, type=float)
    parser.add_argument('--test_data_available', default=False, type=bool)
    parser.add_argument('--fast_dev_run', default=False, type=bool)
    parser.add_argument('--num_workers', default=1, type=int)
    parser.add_argument('--wandb_api_key', default=None, type=str)
    parser.add_argument('--wandb_project', default="fastflow3d", type=str)
    parser.add_argument('--wandb_entity', default='dllab21fastflow3d', type=str)
    parser.add_argument('--use_sparse_lookup', default=False, type=bool)

    # Set default dtype to float and not double
    torch.set_default_dtype(torch.float)

    # NOTE: Readd this to see all parameters of the trainer
    # parser = pl.Trainer.add_argparse_args(parser)  # Add arguments for the trainer
    # Add model specific arguments here
    parser = FastFlow3DModel.add_model_specific_args(parser)
    args = parser.parse_args()

    dataset_path = Path(args.data_directory)
    # Check if the dataset exists
    if not dataset_path.is_dir() or not dataset_path.exists():
        print(f"Dataset directory not found: {dataset_path}")
        exit(1)

    # We assume, that the length of the grid is the same in x and y direction.
    # Otherwise, we have to implement different grid_cell_sizes for x and y direction
    if args.x_max + abs(args.x_min) != args.y_max + abs(args.y_min):
        raise ValueError("Grid must have same length in x and y direction but has a length of {0} in "
                         "x direction and {1} in y direction".format(args.x_max + abs(args.x_min),
                                                                     args.y_max + abs(args.y_min)))

    grid_cell_size = (args.x_max + abs(args.x_min)) / args.grid_size

    n_pillars_x = int(((args.x_max - args.x_min) / grid_cell_size))
    n_pillars_y = int(((args.y_max - args.y_min) / grid_cell_size))

    if args.use_sparse_lookup:
        # Tested GPU memory increase from batch size 1 to 2 is 2350MiB
        model = FastFlow3DModel(n_pillars_x=n_pillars_x, n_pillars_y=n_pillars_y, point_features=8,
                                learning_rate=args.learning_rate)
    else:
        # Tested GPU memory increase from batch size 1 to 2 is 1824MiB
        model = FastFlow3DModelScatter(n_pillars_x=n_pillars_x, n_pillars_y=n_pillars_y, point_features=8,
                                       learning_rate=args.learning_rate)
    waymo_data_module = WaymoDataModule(dataset_path, grid_cell_size=grid_cell_size, x_min=args.x_min,
                                        x_max=args.x_max, y_min=args.y_min,
                                        y_max=args.y_max, z_min=args.z_min, z_max=args.z_max,
                                        batch_size=args.batch_size,
                                        has_test=args.test_data_available,
                                        num_workers=args.num_workers,
                                        scatter_collate=not args.use_sparse_lookup)

    # Initialize the weights and biases logger.
    # Name is the name of this run
    # Project is the name of the project
    # Entity is the name of the team
    logger = True  # Not set a logger defaulting to tensorboard
    if args.wandb_api_key is not None:
        wandb.login(key=args.wandb_api_key)
        logger = WandbLogger(name=args.experiment_name, project=args.wandb_project, entity=args.wandb_entity)
        additional_hyperparameters = {'grid_cell_size': grid_cell_size,
                                      'x_min': args.x_min,
                                      'x_max': args.x_max,
                                      'y_max': args.y_max,
                                      'y_min': args.y_min,
                                      'z_min': args.z_min,
                                      'z_max': args.z_max,
                                      'batch_size': args.batch_size,
                                      'has_test': args.test_data_available,
                                      'num_workers': args.num_workers,
                                      'scatter_collate': args.use_sparse_lookup}
        logger.log_hyperparams(additional_hyperparameters)
    else:
        print("No weights and biases API key set. Using tensorboard instead!")

    # Max epochs can be configured here to, early stopping is also configurable.
    # Some things are definable as callback from pytorch_lightning.callback
    trainer = pl.Trainer.from_argparse_args(args,
                                            progress_bar_refresh_rate=25,  # Prevents Google Colab crashes
                                            gpus=1 if torch.cuda.is_available() else 0,
                                            logger=logger
                                            )  # Add Trainer hparams if desired
    # The actual train loop
    trainer.fit(model, waymo_data_module)

    # Run also the testing
    if args.test_data_available and not args.fast_dev_run:
        trainer.test()  # Also loads the best checkpoint automatically


if __name__ == '__main__':
    cli()
