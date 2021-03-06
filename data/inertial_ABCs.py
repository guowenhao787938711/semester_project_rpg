import numpy as np
import matplotlib.pyplot as plt

from pyquaternion import Quaternion
from abc import ABC, abstractmethod
from sklearn.preprocessing import MinMaxScaler
from sklearn.externals import joblib
from scipy.signal import butter as butterworth_filter

from data.utils.data_utils import filter_with_coeffs, interpolate_ts


class IMU:
    def __init__(self):
        # Timestamp in ns!!
        self.timestamp = 0.0
        self.gyro = np.array([0.0, 0.0, 0.0])
        self.acc = np.array([0.0, 0.0, 0.0])

    @abstractmethod
    def read(self, data):
        ...

    def unroll(self):
        return self.gyro, self.acc, self.timestamp


class GT:
    def __init__(self):
        # Timestamp expected in ns!!

        self.timestamp = 0.0
        self.pos = np.array([0.0, 0.0, 0.0])
        self.att = np.array([0.0, 0.0, 0.0, 0.0])
        self.vel = np.array([0.0, 0.0, 0.0])
        self.ang_vel = np.array([0.0, 0.0, 0.0])
        self.acc = np.array([0.0, 0.0, 0.0])

    @abstractmethod
    def read(self, data):
        ...

    def read_from_tuple(self, data):
        self.pos = data[0]
        self.vel = data[1]
        self.att = data[2]
        self.ang_vel = data[3]
        self.acc = data[4]
        self.timestamp = data[5]
        return self

    def unroll(self):
        return self.pos, self.vel, self.att, self.ang_vel, self.acc, self.timestamp

    def integrate(self, gt_old, int_pos=True, int_att=True):
        """
        Integrates position and attitude. Saves integrated values to current GT object

        :param gt_old: GT from previous timestamp
        :param int_pos: whether position should be integrated, or velocity is already available instead
        :param int_att: whether attitude should be integrated, or angular velocity is already available instead
        """

        # TODO: implement angular velocity integration

        dt = (self.timestamp - gt_old.timestamp) * 10e-6
        if int_pos:
            self.vel = (self.pos - gt_old.pos) / dt
        if int_att:
            att_q = Quaternion(self.att[0], self.att[1], self.att[2], self.att[3])
            self.ang_vel = self.ang_vel


class InertialDataset(ABC):
    @abstractmethod
    def __init__(self):
        # The four variables should be set by overriding class
        self.imu_data = None
        self.gt_data = None
        self.sampling_freq = None
        self.ds_local_dir = None

        self.plot_stft = False
        ...

    @abstractmethod
    def get_raw_ds(self):
        ...
    
    def get_ds_directory(self):
        assert self.ds_local_dir is not None, "Directory has not yet been set"
        return self.ds_local_dir

    def basic_preprocessing(self, gyro_scale_file, acc_scale_file, filter_freq):
        """
        Pre-process dataset (apply low-pass filter and minmax scaling)

        :param gyro_scale_file: file to save pre-processing functions for gyroscope
        :param acc_scale_file: file to save pre-processing functions for accelerometer
        :param filter_freq: frequency used for low-pass filter
        :return: the filtered datasets in compressed (numpy) format
        """
        
        assert self.imu_data is not None and self.gt_data is not None and self.sampling_freq is not None, \
            "Data cannot be processed because there is no data yet."

        # Transform the data to numpy matrices
        imu_unroll = np.array([(imu_s.unroll()) for imu_s in self.imu_data])
        gt_unroll = np.array([(gt_meas.unroll()) for gt_meas in self.gt_data])

        # Get number of channels per data type (we subtract 1 because timestamp is not a channel we want to filter)
        imu_channels = np.shape(imu_unroll)[1] - 1

        # Design butterworth filter
        fs = self.sampling_freq  # Sample frequency (Hz)
        f0 = filter_freq  # Frequency to be removed from signal (Hz)
        w0 = f0 / (fs / 2)  # Normalized Frequency
        [b_bw, a_bw] = butterworth_filter(10, w0, output='ba')

        for i, tit in zip(range(imu_channels), ("log(STFT) gyro", "log(STFT) acc")):
            filt_res = filter_with_coeffs(a_bw, b_bw, np.stack(imu_unroll[:, i]), fs, self.plot_stft)
            if self.plot_stft:
                fig = filt_res[1]
                imu_unroll[:, i] = [tuple(j) for j in filt_res[0]]
                fig.suptitle(tit)
                fig.axes[0].set_title("x")
                fig.axes[1].set_title("y")
                fig.axes[2].set_title("z")
                fig.show()
            else:
                imu_unroll[:, i] = [tuple(j) for j in filt_res]

        scale_g = MinMaxScaler()
        scale_g.fit(np.stack(imu_unroll[:, 0]))
        scale_a = MinMaxScaler()
        scale_a.fit(np.stack(imu_unroll[:, 1]))

        joblib.dump(scale_g, self.get_ds_directory() + gyro_scale_file)
        joblib.dump(scale_a, self.get_ds_directory() + acc_scale_file)

        # Add back the timestamps to the data matrix and return
        # Careful -> data from now on is in numpy format, instead of GT and IMU format
        self.imu_data = imu_unroll
        self.gt_data = gt_unroll

        return self.imu_data, self.gt_data
    
    def interpolate_ground_truth(self):
        """
        Interpolates the data of the ground truth so that it matches the timestamps of the raw imu data

        :return: the original imu data, and the interpolated ground truth data
        """
        x_data = np.array(self.imu_data)

        imu_timestamps = np.array([imu_meas.timestamp for imu_meas in x_data])
        gt_unroll = np.array([(gt_meas.unroll()) for gt_meas in self.gt_data])

        gt_timestamps = gt_unroll[:, 5]

        # Only keep imu data that is within the ground truth time span
        x_data = x_data[(imu_timestamps > gt_timestamps[0]) * (imu_timestamps < gt_timestamps[-1])]
        imu_timestamps = np.array([imu_meas.timestamp for imu_meas in x_data])
        self.imu_data = x_data

        gt_pos = np.stack(gt_unroll[:, 0])
        gt_vel = np.stack(gt_unroll[:, 1])
        gt_att = np.stack(gt_unroll[:, 2])
        gt_ang_vel = np.stack(gt_unroll[:, 3])
        gt_acc = np.stack(gt_unroll[:, 4])

        # Interpolate Ground truth to match IMU time acquisitions
        gt_pos_interp = interpolate_ts(gt_timestamps, imu_timestamps, gt_pos)
        gt_vel_interp = interpolate_ts(gt_timestamps, imu_timestamps, gt_vel)
        gt_att_interp = interpolate_ts(gt_timestamps, imu_timestamps, gt_att, is_quaternion=True)
        gt_ang_vel_interp = interpolate_ts(gt_timestamps, imu_timestamps, gt_ang_vel)
        gt_acc_interp = interpolate_ts(gt_timestamps, imu_timestamps, gt_acc)
        
        gt_interp = (gt_pos_interp, gt_vel_interp, gt_att_interp, gt_ang_vel_interp, gt_acc_interp, imu_timestamps)
        
        # Re-make vector of interpolated GT measurements
        n_samples = len(gt_pos_interp)
        gt_interp = [GT().read_from_tuple(tuple([gt_interp[i][j] for i in range(6)])) for j in range(n_samples)]

        self.gt_data = gt_interp

    def plot_all_data(self, title="", from_numpy=False, show=False):
        """
        Plots the imu and ground truth data in two separate figures

        :param title: title of the plot
        :param from_numpy: format of the input data
        :param show: whether to show plot or not
        :return:
        """

        self.plot_stft = True
        x_axis = np.linspace(0, len(self.imu_data)/self.sampling_freq, len(self.imu_data))

        if from_numpy:
            fig = plt.figure()
            fig.tight_layout()
            ax = fig.add_subplot(2, 1, 1)
            ax.plot(x_axis, np.stack(self.imu_data[:, 0]))
            ax.set_title("IMU: gyroscope")
            ax.legend(['x', 'y', 'z'])
            ax.set_ylabel('rad/s')
            ax = fig.add_subplot(2, 1, 2)
            ax.plot(x_axis, np.stack(self.imu_data[:, 1]))
            ax.set_title("IMU: accelerometer")
            ax.legend(['x', 'y', 'z'])
            ax.set_ylabel(r'$m/s^{2}$')
            ax.set_xlabel('s')
            fig.suptitle(title)

            fig = plt.figure()
            fig.tight_layout()
            ax = fig.add_subplot(2, 2, 1)
            ax.plot(x_axis, np.stack(self.gt_data[:, 0]))
            ax.set_title("GT: position")
            ax.legend(['x', 'y', 'z'])
            ax = fig.add_subplot(2, 2, 2)
            ax.plot(x_axis, np.stack(self.gt_data[:, 1]))
            ax.set_title("GT: velocity")
            ax.legend(['x', 'y', 'z'])
            ax = fig.add_subplot(2, 2, 3)
            ax.plot(x_axis, np.stack(self.gt_data[:, 2]))
            ax.set_title("GT: attitude")
            ax.legend(['w', 'x', 'y', 'z'])
            ax = fig.add_subplot(2, 2, 4)
            ax.plot(x_axis, np.stack(self.gt_data[:, 3]))
            ax.set_title("GT: angular velocity")
            ax.legend(['x', 'y', 'z'])
            fig.suptitle(title)

        else:
            fig = plt.figure()
            fig.tight_layout()
            ax = fig.add_subplot(2, 1, 1)
            ax.plot(x_axis, [imu.gyro for imu in self.imu_data])
            ax.set_title("IMU: gyroscope")
            ax.legend(['x', 'y', 'z'])
            ax.set_ylabel('rad/s')
            ax = fig.add_subplot(2, 1, 2)
            ax.plot(x_axis, [imu.acc for imu in self.imu_data])
            ax.set_title("IMU: accelerometer")
            ax.legend(['x', 'y', 'z'])
            ax.set_ylabel(r'$m/s^{2}$')
            ax.set_xlabel('s')
            fig.suptitle(title)

            fig = plt.figure()
            fig.tight_layout()
            ax = fig.add_subplot(2, 2, 1)
            ax.plot(x_axis, [gt.pos for gt in self.gt_data])
            ax.set_title("GT: position")
            ax.legend(['x', 'y', 'z'])
            ax = fig.add_subplot(2, 2, 2)
            ax.plot(x_axis, [gt.vel for gt in self.gt_data])
            ax.set_title("GT: velocity")
            ax.legend(['x', 'y', 'z'])
            ax = fig.add_subplot(2, 2, 3)
            ax.plot(x_axis, [gt.att for gt in self.gt_data])
            ax.set_title("GT: attitude")
            ax.legend(['w', 'x', 'y', 'z'])
            ax = fig.add_subplot(2, 2, 4)
            ax.plot(x_axis, [gt.ang_vel for gt in self.gt_data])
            ax.set_title("GT: angular velocity")
            ax.legend(['x', 'y', 'z'])
            fig.suptitle(title)

        if show:
            plt.show()
