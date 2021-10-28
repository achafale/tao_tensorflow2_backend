# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.

"""Base calibrator class for TensorRT INT8 Calibration."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import os

import numpy as np

import pycuda.autoinit  # noqa pylint: disable=unused-import
import pycuda.driver as cuda

# Simple helper class for calibration.
from iva.common.export.base_calibrator import BaseCalibrator
from iva.common.export.tensorfile import TensorFile

logger = logging.getLogger(__name__)


class TensorfileCalibrator(BaseCalibrator):
    """Calibrator class."""

    def __init__(self, data_filename, cache_filename,
                 n_batches, batch_size,
                 *args, **kwargs):
        """Init routine.

        This inherits from ``iva.common.export.base_calibrator.BaseCalibrator``
        to implement the calibration interface that TensorRT needs to
        calibrate the INT8 quantization factors. The data source here is assumed
        to be a Tensorfile as defined in modulus.export.data.Tensorfile(), which
        was pre-generated using the dataloader or iva.common.export.app.py

        Args:
            data_filename (str): ``TensorFile`` data file to use.
            cache_filename (str): name of calibration file to read/write to.
            n_batches (int): number of batches for calibrate for.
            batch_size (int): batch size to use for calibration data.
        """
        super(TensorfileCalibrator, self).__init__(
              cache_filename,
              n_batches, batch_size,
              *args, **kwargs
        )
        self.instantiate_data_source(data_filename)

    def instantiate_data_source(self, data_filename):
        """Simple function to instantiate the data_source of the dataloader.

        Args:
            data_filename (str): The path to the data file.

        Returns:
            No explicit returns.
        """
        if os.path.exists(data_filename):
            self._data_source = TensorFile(data_filename, "r")
        else:
            logger.info(
                "A valid data source wasn't provided to the calibrator. "
                "The calibrator will attempt to read from a cache file if provided."
            )

    def get_data_from_source(self):
        """Simple function to get data from the defined data_source."""
        batch = np.array(self._data_source.read())
        if batch is not None:
            # <@vpraveen>: Disabling pylint error check on line below
            # because of a python3 linting error. To be reverted when
            # pylint/issues/3139 gets fixed.
            batch_size = batch.shape[0]  # pylint: disable=E1136
            if batch_size < self._batch_size:
                raise ValueError(
                    "Batch size yielded from data source {} < requested batch size "
                    "from calibrator {}".format(batch_size, self._batch_size)
                )
            batch = batch[:self._batch_size]
        else:
            raise ValueError(
                "Batch wasn't yielded from the data source. You may have run "
                "out of batches. Please set the num batches accordingly")
        return batch

    def get_batch(self, names):
        """Return one batch.

        Args:
            names (list): list of memory bindings names.
        """
        if self._batch_count < self._n_batches:
            batch = self.get_data_from_source()
            if batch is not None:
                if self._data_mem is None:
                    # 4 bytes per float32.
                    self._data_mem = cuda.mem_alloc(batch.size * 4)

                self._batch_count += 1

                # Transfer input data to device.
                cuda.memcpy_htod(self._data_mem, np.ascontiguousarray(
                    batch, dtype=np.float32))
                return [int(self._data_mem)]

        if self._data_mem is not None:
            self._data_mem.free()
        return None
