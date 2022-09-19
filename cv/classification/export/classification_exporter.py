# Copyright (c) 2022-2023, NVIDIA CORPORATION.  All rights reserved.

"""Base class to export trained .tlt models to etlt file."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import shutil
import logging
import tensorrt as trt
import tensorflow as tf

from tf2onnx import tf_loader, utils, convert

from cv.classification.utils.helper import decode_eff

TRT_VERSION = trt.__version__
TRT_MAJOR = float(".".join(TRT_VERSION.split(".")[:2]))
logger = logging.getLogger(__name__)


class Exporter:
    """Define an exporter for classification models."""

    def __init__(self,
                 config=None,
                 classmap_file=None,
                 min_batch_size=1,
                 opt_batch_size=4,
                 max_batch_size=8,
                 **kwargs):
        """Initialize the classification exporter.

        Args:
            key (str): Key to load the model.
        """
        self.config = config
        if config.export.dtype == "int8":
            self._dtype = trt.DataType.INT8
        elif config.export.dtype == "fp16":
            self._dtype = trt.DataType.HALF
        elif config.export.dtype == "fp32":
            self._dtype = trt.DataType.FLOAT
        else:
            raise ValueError("Unsupported data type: %s" % self._dtype)
        
        if config.train_config.qat and config.export_config.dtype != "int8":
            raise ValueError("QAT only supports int8 export")
        self.backend = "onnx"
        self.input_shape = None
        self.max_batch_size = max_batch_size
        self.min_batch_size = min_batch_size
        self.opt_batch_size = opt_batch_size

        if not self.config.key: # TODO(@yuw): for internal usage only
            self._saved_model = self.config.export.model_path
        else:
            self._saved_model = decode_eff(
                str(self.config.export.model_path),
                self.config.key)
    
    def _set_input_shape(self):
        model = tf.keras.models.load_model(self._saved_model, custom_objects=None)
        self.input_shape = tuple(model.layers[0].input_shape[0][1:4])

    def export_onnx(self) -> None:
        """ Convert Keras saved model into ONNX format.

        Args:
            root_dir: root directory containing the quantized Keras saved model. This is the same directory where the ONNX
                file will be saved.
            saved_model_dir: name of the quantized 'saved_model' directory.
            onnx_filename: desired name to save the converted ONNX file.
        """
        # 1. Let TensorRT optimize QDQ nodes instead of TF
        from tf2onnx.optimizer import _optimizers
        updated_optimizers = copy.deepcopy(_optimizers)
        del updated_optimizers["q_dq_optimizer"]
        del updated_optimizers["const_dequantize_optimizer"]

        # 2. Extract graph definition from SavedModel
        graph_def, inputs, outputs = tf_loader.from_saved_model(
            model_path=self._saved_model,
            input_names=None,
            output_names=None,
            tag="serve",
            signatures=["serving_default"]
        )

        # 3. Convert tf2onnx and save onnx file
        model_proto, _ = convert._convert_common(
            graph_def,
            name=self._saved_model,
            opset=13,
            input_names=inputs,
            output_names=outputs,
            output_path=self.config.export.output_path,
            optimizers=updated_optimizers
        )

        utils.save_protobuf(self.config.export.output_path, model_proto)
        print("ONNX conversion Done!")
    
    def export_engine(self, verbose=True) -> None:
        """Parse the model file through TensorRT and build TRT engine
        """
        # Create builder and network
        if verbose:
            TRT_LOGGER = trt.Logger(trt.Logger.VERBOSE)
        else:
            TRT_LOGGER = trt.Logger(trt.Logger.INFO)

        network_flags = 1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
        network_flags = network_flags | (
                1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_PRECISION)
        )

        with trt.Builder(TRT_LOGGER) as builder, builder.create_network(
                flags=network_flags
        ) as network, trt.OnnxParser(network, TRT_LOGGER) as parser:
            with open(self.config.export.output_path, "rb") as model:
                if not parser.parse(model.read()):
                    print("ERROR: Failed to parse the ONNX file.")
                    for error in range(parser.num_errors):
                        print(parser.get_error(error))
                    return None
                else:
                    print("Parsed ONNX model successfully")

            config = builder.create_builder_config()
            config.max_workspace_size = 1 << 30
            builder.max_batch_size = self.max_batch_size
            if TRT_MAJOR >= 8.2:
                config.profiling_verbosity = trt.ProfilingVerbosity.DETAILED
            else:
                config.profiling_verbosity = trt.ProfilingVerbosity.VERBOSE

            if self._dtype == trt.DataType.HALF:
                config.set_flag(trt.BuilderFlag.FP16)

            if self._dtype == trt.DataType.INT8:
                config.set_flag(trt.BuilderFlag.INT8)
            # config.flags = config.flags | 1 << int(trt.BuilderFlag.INT8)
            # Setting the (min, opt, max) batch sizes to be (1, 4, 8).
            #   The users need to configure this according to their requirements.
            config.add_optimization_profile(
                self._build_profile(
                    builder,
                    network,
                    profile_shapes={
                        "Input": [(self.min_batch_size,) + self.input_shape,
                                  (self.opt_batch_size,) + self.input_shape,
                                  (self.max_batch_size,) + self.input_shape]
                    },
                )
            )
            trt_engine = builder.build_engine(network, config)  # build_serialized_network
            if not trt_engine:
                logger.info("TensorRT engine failed.")
            if self.config.export.save_engine:
                engine_path = self.config.export.output_path + f'.{self.config.export.dtype}.engine'
                with open(engine_path, "wb") as engine_file:
                    engine_file.write(trt_engine.serialize())

    def _del(self):
        """Remove temp files."""
        shutil.rmtree(self._saved_model)

    def export(self):
        """Export to EFF and TensorRT engine."""
        self._set_input_shape()
        # TODO(@yuw): encrypt with EFF
        self.export_onnx()
        logger.info(f"ONNX is saved at {self.config.export.output_path}")
        self.export_engine()
        self._del()

    def _build_profile(self, builder, network, profile_shapes, default_shape_value=1):
        """
        Build optimization profile for the builder and configure the min, opt, max shapes appropriately.
        """

        def is_dimension_dynamic(dim):
            return dim is None or dim <= 0

        def override_shape(shape):
            return tuple([1 if is_dimension_dynamic(dim) else dim for dim in shape])

        profile = builder.create_optimization_profile()
        for idx in range(network.num_inputs):
            inp = network.get_input(idx)

            def get_profile_shape(name):
                # if name not in profile_shapes:
                # profile_shapes={'input': [(),(),()]} and name='input_1:0
                profile_name = None
                for k in profile_shapes.keys():
                    if k in name:
                        profile_name = k
                if profile_name is None:  # not any([k in name for k in profile_shapes.keys()]):
                    return None
                shapes = profile_shapes[profile_name]
                if not isinstance(shapes, list) or len(shapes) != 3:
                    logger.critical(
                        "Profile values must be a list containing exactly 3 shapes (tuples or Dims), but received shapes: {:} for input: {:}.\nNote: profile was: {:}.\nNote: Network inputs were: {:}".format(
                            shapes, name, profile_shapes, shapes,
                        )
                    )
                return shapes

            if inp.is_shape_tensor:
                shapes = get_profile_shape(inp.name)
                if not shapes:
                    rank = inp.shape[0]
                    shapes = [(default_shape_value,) * rank] * 3
                    print(
                        "Setting shape input to {:}. If this is incorrect, for shape input: {:}, please provide tuples for min, opt, and max shapes containing {:} elements".format(
                            shapes[0], inp.name, rank
                        )
                    )
                min, opt, max = shapes
                profile.set_shape_input(inp.name, min, opt, max)
                print(
                    "Setting shape input: {:} values to min: {:}, opt: {:}, max: {:}".format(
                        inp.name, min, opt, max
                    )
                )
            elif -1 in inp.shape:
                shapes = get_profile_shape(inp.name)
                if not shapes:
                    shapes = [override_shape(inp.shape)] * 3
                    print(
                        "Overriding dynamic input shape {:} to {:}. If this is incorrect, for input tensor: {:}, please provide tuples for min, opt, and max shapes containing values: {:} with dynamic dimensions replaced,".format(
                            inp.shape, shapes[0], inp.name, inp.shape
                        )
                    )
                min, opt, max = shapes
                profile.set_shape(inp.name, min, opt, max)
                print(
                    "Setting input: {:} shape to min: {:}, opt: {:}, max: {:}".format(
                        inp.name, min, opt, max
                    )
                )
        if not profile:
            print(
                "Profile is not valid, please provide profile data. Note: profile was: {:}".format(
                    profile_shapes
                )
            )
        return profile
