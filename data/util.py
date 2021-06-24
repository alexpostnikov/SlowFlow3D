import time

import tensorflow as tf
import torch

from waymo_open_dataset.utils import frame_utils
from waymo_open_dataset import dataset_pb2

from utils.pillars import create_pillars_matrix, remove_out_of_bounds_points
from torch.utils.data._utils.collate import default_collate
import numpy as np
import os, glob
import pickle

from waymo_open_dataset import dataset_pb2 as open_dataset
import tensorflow as tf

def convert_range_image_to_point_cloud(frame,
                                       range_images,
                                       camera_projections,
                                       point_flows,
                                       range_image_top_pose,
                                       ri_index=0,
                                       keep_polar_features=False):
    """Convert range images to point cloud.

  Args:
    frame: open dataset frame
    range_images: A dict of {laser_name, [range_image_first_return,
      range_image_second_return]}.
    camera_projections: A dict of {laser_name,
      [camera_projection_from_first_return,
      camera_projection_from_second_return]}.
    range_image_top_pose: range image pixel pose for top lidar.
    ri_index: 0 for the first return, 1 for the second return.
    keep_polar_features: If true, keep the features from the polar range image
      (i.e. range, intensity, and elongation) as the first features in the
      output range image.

  Returns:
    points: {[N, 3]} list of 3d lidar points of length 5 (number of lidars).
      (NOTE: Will be {[N, 6]} if keep_polar_features is true.
    cp_points: {[N, 6]} list of camera projections of length 5
      (number of lidars).
  """
    calibrations = sorted(frame.context.laser_calibrations, key=lambda c: c.name)
    points = []
    cp_points = []
    flows = []

    cartesian_range_images = frame_utils.convert_range_image_to_cartesian(
        frame, range_images, range_image_top_pose, ri_index, keep_polar_features)

    for c in calibrations:
        range_image = range_images[c.name][ri_index]
        range_image_tensor = tf.reshape(
            tf.convert_to_tensor(value=range_image.data), range_image.shape.dims)
        range_image_mask = range_image_tensor[..., 0] > 0

        range_image_cartesian = cartesian_range_images[c.name]
        points_tensor = tf.gather_nd(range_image_cartesian,
                                     tf.compat.v1.where(range_image_mask))

        flow = point_flows[c.name][ri_index]
        flow_tensor = tf.reshape(tf.convert_to_tensor(value=flow.data), flow.shape.dims)
        flow_points_tensor = tf.gather_nd(flow_tensor,
                                          tf.compat.v1.where(range_image_mask))

        cp = camera_projections[c.name][ri_index]
        cp_tensor = tf.reshape(tf.convert_to_tensor(value=cp.data), cp.shape.dims)
        cp_points_tensor = tf.gather_nd(cp_tensor,
                                        tf.compat.v1.where(range_image_mask))

        points.append(points_tensor.numpy())
        cp_points.append(cp_points_tensor.numpy())
        flows.append(flow_points_tensor.numpy())

    return points, cp_points, flows


def parse_range_image_and_camera_projection(frame):
    """Parse range images and camera projections given a frame.

  Args:
     frame: open dataset frame proto

  Returns:
     range_images: A dict of {laser_name,
       [range_image_first_return, range_image_second_return]}.
     camera_projections: A dict of {laser_name,
       [camera_projection_from_first_return,
        camera_projection_from_second_return]}.
    range_image_top_pose: range image pixel pose for top lidar.
  """
    range_images = {}
    camera_projections = {}
    point_flows = {}
    range_image_top_pose = None
    for laser in frame.lasers:
        if len(laser.ri_return1.range_image_compressed) > 0:  # pylint: disable=g-explicit-length-test
            range_image_str_tensor = tf.io.decode_compressed(
                laser.ri_return1.range_image_compressed, 'ZLIB')
            ri = dataset_pb2.MatrixFloat()
            ri.ParseFromString(bytearray(range_image_str_tensor.numpy()))
            range_images[laser.name] = [ri]

            if len(laser.ri_return1.range_image_flow_compressed) > 0:
                range_image_flow_str_tensor = tf.io.decode_compressed(
                    laser.ri_return1.range_image_flow_compressed, 'ZLIB')
                ri = dataset_pb2.MatrixFloat()
                ri.ParseFromString(bytearray(range_image_flow_str_tensor.numpy()))
                point_flows[laser.name] = [ri]

            if laser.name == dataset_pb2.LaserName.TOP:
                range_image_top_pose_str_tensor = tf.io.decode_compressed(
                    laser.ri_return1.range_image_pose_compressed, 'ZLIB')
                range_image_top_pose = dataset_pb2.MatrixFloat()
                range_image_top_pose.ParseFromString(
                    bytearray(range_image_top_pose_str_tensor.numpy()))

            camera_projection_str_tensor = tf.io.decode_compressed(
                laser.ri_return1.camera_projection_compressed, 'ZLIB')
            cp = dataset_pb2.MatrixInt32()
            cp.ParseFromString(bytearray(camera_projection_str_tensor.numpy()))
            camera_projections[laser.name] = [cp]
        if len(laser.ri_return2.range_image_compressed) > 0:  # pylint: disable=g-explicit-length-test
            range_image_str_tensor = tf.io.decode_compressed(
                laser.ri_return2.range_image_compressed, 'ZLIB')
            ri = dataset_pb2.MatrixFloat()
            ri.ParseFromString(bytearray(range_image_str_tensor.numpy()))
            range_images[laser.name].append(ri)

            if len(laser.ri_return2.range_image_flow_compressed) > 0:
                range_image_flow_str_tensor = tf.io.decode_compressed(
                    laser.ri_return2.range_image_flow_compressed, 'ZLIB')
                ri = dataset_pb2.MatrixFloat()
                ri.ParseFromString(bytearray(range_image_flow_str_tensor.numpy()))
                point_flows[laser.name].append(ri)

            camera_projection_str_tensor = tf.io.decode_compressed(
                laser.ri_return2.camera_projection_compressed, 'ZLIB')
            cp = dataset_pb2.MatrixInt32()
            cp.ParseFromString(bytearray(camera_projection_str_tensor.numpy()))
            camera_projections[laser.name].append(cp)
    return range_images, camera_projections, point_flows, range_image_top_pose


class ApplyPillarization:
    def __init__(self, grid_cell_size, x_min, y_min, z_min, z_max):
        self._grid_cell_size = grid_cell_size
        self._z_max = z_max
        self._z_min = z_min
        self._y_min = y_min
        self._x_min = x_min

    """ Transforms an point cloud to the augmented pointcloud depending on Pillarization """

    def __call__(self, x):
        point_cloud, grid_indices = create_pillars_matrix(x,
                                                          grid_cell_size=self._grid_cell_size,
                                                          x_min=self._x_min,
                                                          y_min=self._y_min,
                                                          z_min=self._z_min, z_max=self._z_max)
        return point_cloud, grid_indices


def drop_points_function(x_min, x_max, y_min, y_max, z_min, z_max):
    def inner(x, y):
        return remove_out_of_bounds_points(x, y,
                                           x_min=x_min,
                                           y_min=y_min,
                                           z_min=z_min,
                                           z_max=z_max,
                                           x_max=x_max,
                                           y_max=y_max
                                           )

    return inner


def custom_collate(batch):
    """
    We need this custom collate because of the structure of our data.
    :param batch:
    :return:
    """
    # Only convert the points clouds from numpy arrays to tensors
    batch_previous = [
        [torch.as_tensor(e) for e in entry[0][0]] for entry in batch
    ]
    batch_current = [
        [torch.as_tensor(e) for e in entry[0][1]] for entry in batch
    ]

    # For the targets we can only transform each entry to a tensor and not stack them
    batch_targets = [
        torch.as_tensor(entry[1]) for entry in batch
    ]

    return (batch_previous, batch_current), batch_targets




# ------------- Preprocessing Functions ---------------

def save_point_cloud(compressed_frame, file_path):
    """
    Compute the point cloud from a frame and stores it into disk.
    :param compressed_frame: compressed frame from a TFRecord
    :param file_path: name path that will have the stored point cloud
    :returns:
        - points - [N, 5] matrix which stores the [x, y, z, intensity, elongation] in the frame reference
        - flows - [N, 4] matrix where each row is the flow for each point in the form [vx, vy, vz, label]
                  in the reference frame
        - transform - [,16] flattened transformation matrix
    """
    frame = get_uncompressed_frame(compressed_frame)
    points, flows = compute_features(frame)
    point_cloud = np.hstack((points, flows))
    np.save(file_path, point_cloud)
    transform = list(frame.pose.transform)
    return points, flows, transform


def preprocess(tfrecord_files, output_path, frames_per_segment = None):
    """
    Preprocess a list of TFRecord files to store in a suitable form for training
    in disk. A point cloud in disk has dimensions [N, 9] where N is the number of points
    and per each point it stores [x, y, z, intensity, elongation, vx, vy, vz, label].
    It stores a look-up table: It has the form [[t_1, t_0], [t_2, t_1], ... , [t_n, t_(n-1)]], where t_i is
    (file_path, transform), where file_path is the file where the point cloud is stored and transform the transformation
    to apply to a point to change it reference frame from global to the car frame in that moment.

    :param tfrecord_files: list with paths of TFRecord files. They should have the flow extension.
                          They can be downloaded from https://console.cloud.google.com/storage/browser/waymo_open_dataset_scene_flow
    :param output_path: path where the processed point clouds will be saved.
    """
    tfrecord_files = [tfrecord_files]
    for data_file in tfrecord_files:
        tfrecord_filename = os.path.basename(data_file)
        tfrecord_filename = os.path.splitext(tfrecord_filename)[0]

        look_up_table = []
        look_up_table_path = os.path.join(output_path, f"look_up_table_{tfrecord_filename}")
        loaded_file = tf.data.TFRecordDataset(data_file, compression_type='')
        previous_frame = None
        for j, frame in enumerate(loaded_file):
            point_cloud_path = os.path.join(output_path, "pointCloud_file_" + tfrecord_filename + "_frame_" + str(j) + ".npy")
            # Process frame and store point clouds into disk
            _, _, pose_transform = save_point_cloud(frame, point_cloud_path)
            if j == 0:
                previous_frame = (point_cloud_path, pose_transform)
            else:
                current_frame = (point_cloud_path, pose_transform)
                look_up_table.append([current_frame, previous_frame])
                previous_frame = current_frame
            if frames_per_segment is not None and j == frames_per_segment:
                break

        # Save look-up-table into disk
        with open(look_up_table_path, 'wb') as look_up_table_file:
            pickle.dump(look_up_table, look_up_table_file)

def get_uncompressed_frame(compressed_frame):
    """
    :param compressed_frame: Compressed frame
    :return: Uncompressed frame
    """
    frame = open_dataset.Frame()
    frame.ParseFromString(bytearray(compressed_frame.numpy()))
    return frame


def compute_features(frame):
    """
    :param frame: Uncompressed frame
    :return: [N, F], [N, 4], where N is the number of points, F the number of features,
    which is [x, y, z, intensity, elongation] and 4 in the second results stands for [vx, vy, vz, label], which corresponds
    to the flow information
    """
    range_images, camera_projections, point_flows, range_image_top_pose = parse_range_image_and_camera_projection(
        frame)

    points, cp_points, flows = convert_range_image_to_point_cloud(
        frame,
        range_images,
        camera_projections,
        point_flows,
        range_image_top_pose,
        keep_polar_features=True)

    # 3D points in the vehicle reference frame
    points_all = np.concatenate(points, axis=0)
    flows_all = np.concatenate(flows, axis=0)
    # We skip the range feature since pillars will account for it
    # Note that first are features and then point coordinates
    points_features, points_coord = points_all[:, 1:3], points_all[:, 3:points_all.shape[1]]
    points_all = np.hstack((points_coord, points_features))
    return points_all, flows_all


def get_coordinates_and_features(point_cloud, transform=None):
    """
    Parse a point clound into coordinates and features.
    :param point_cloud: Full [N, 9] point cloud
    :param transform: Optional parameter. Transformation matrix to apply
    to the coordinates of the point cloud
    :return: [N, 5] where N is the number of points and 5 is [x, y, z, intensity, elongation]
    """
    points_coord, features, flows = point_cloud[:, 0:3], point_cloud[:, 3:5], point_cloud[:, 5:]
    if transform is not None:
        ones = np.ones((points_coord.shape[0], 1))
        points_coord = np.hstack((points_coord, ones))
        points_coord = transform @ points_coord.T
        points_coord = points_coord[0:-1, :]
        points_coord = points_coord.T
    point_cloud = np.hstack((points_coord, features))
    return point_cloud

def merge_look_up_tables(input_path):
    """
    Merge individual look-up table and store it in the input_path with the name look_up_table
    :param input_path: Path with the local look-up tables in the form look_up_table_[tfRecordName]
    """
    look_up_table = []
    os.chdir(input_path)
    for file in glob.glob("look_up_table_*"):
        file_name = os.path.abspath(file)
        try:
            with open(file_name, 'rb') as look_up_table_file:
                look_up_table_local = pickle.load(look_up_table_file)
                look_up_table.extend(look_up_table_local)
        except FileNotFoundError:
            raise FileNotFoundError(
                "Look-up table not found when merging individual look-up tables")

    # Save look-up-table into disk
    with open(os.path.join(input_path, "look_up_table"), 'wb') as look_up_table_file:
        pickle.dump(look_up_table, look_up_table_file)
