# Copyright (c) 2017-2020, NVIDIA CORPORATION.  All rights reserved.

"""Load MakeNet experiment spec .txt files and return an experiment_pb2."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import os

from google.protobuf.text_format import Merge as merge_text_proto

from iva.common.spec_validator import SpecValidator
import iva.makenet.proto.experiment_pb2 as experiment_pb2
from iva.makenet.spec_handling.constants import (
    TRAINVAL_EXP_REQUIRED_MSG,
    TRAINVAL_OPTIONAL_CHECK_DICT,
    TRAINVAL_REQUIRED_MSG_DICT,
    TRAINVAL_VALUE_CHECK_DICT,
    VALIDATION_EXP_REQUIRED_MSG
)

VALIDATION_SCHEMA = {
    "train_val": {
        "required_msg_dict": TRAINVAL_REQUIRED_MSG_DICT,
        "value_checker_dict": TRAINVAL_VALUE_CHECK_DICT,
        "required_msg": TRAINVAL_EXP_REQUIRED_MSG,
        "optional_check_dict": TRAINVAL_OPTIONAL_CHECK_DICT,
        "proto": experiment_pb2.Experiment(),
        "default_spec": "experiment_specs/default_spec.txt"
    },
    "validation": {
        "required_msg_dict": TRAINVAL_REQUIRED_MSG_DICT,
        "value_checker_dict": TRAINVAL_VALUE_CHECK_DICT,
        "required_msg": VALIDATION_EXP_REQUIRED_MSG,
        "optional_check_dict": TRAINVAL_OPTIONAL_CHECK_DICT,
        "proto": experiment_pb2.Experiment(),
        "default_spec": "experiment_specs/default_spec.txt"
    }
}

logger = logging.getLogger(__name__)


def validate_spec(spec, validation_schema="train_val"):
    """Validate the loaded experiment spec file."""
    assert validation_schema in list(VALIDATION_SCHEMA.keys()), (
        "Invalidation specification file schema: {}".format(validation_schema)
    )
    schema = VALIDATION_SCHEMA[validation_schema]
    if schema["required_msg"] is None:
        schema["required_msg"] = []
    spec_validator = SpecValidator(required_msg_dict=schema["required_msg_dict"],
                                   value_checker_dict=schema["value_checker_dict"])
    spec_validator.validate(spec, schema["required_msg"])


def load_proto(spec_path, proto_buffer, default_spec_path,
               merge_from_default=True):
    """Load spec from file and merge with given proto_buffer instance.

    Args:
        spec_path (str): location of a file containing the custom spec proto.
        proto_buffer(pb2): protocal buffer instance to be loaded.
        default_spec_path(str): location of default spec to use if
                                merge_from_default is True.
        merge_from_default (bool): disable default spec, if False, spec_path
                                   must be set.

    Returns:
        proto_buffer(pb2): protocol buffer instance updated with spec.
    """
    def _load_from_file(filename, pb2):
        with open(filename, "r") as f:
            merge_text_proto(f.read(), pb2)

    # Setting this flag false prevents concatenating repeated-fields
    if merge_from_default:
        assert default_spec_path, \
                "default spec path has to be defined if" \
                "merge_from_default is enabled"
        # Load the default spec
        _load_from_file(default_spec_path, proto_buffer)
    else:
        assert spec_path, "spec_path has to be defined," \
                           "if merge_from_default is disabled"

    # Merge a custom proto on top of the default spec, if given
    if spec_path:
        _load_from_file(spec_path, proto_buffer)

    return proto_buffer


def load_experiment_spec(spec_path=None, merge_from_default=True, validation_schema="train_val"):
    """Load experiment spec from a .txt file.

    Args:
        spec_path (str): location of a file containing the custom experiment
                         spec proto.
        merge_from_default (bool):
                         disable default spec, if False, spec_path
                         must be set.

    Returns:
        experiment_spec: protocol buffer instance of type
                         experiment_pb2.Experiment.
    """
    experiment_spec = VALIDATION_SCHEMA[validation_schema]["proto"]
    file_path = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    default_spec_path = os.path.join(file_path,
                                     VALIDATION_SCHEMA[validation_schema]["default_spec"])
    experiment_spec = load_proto(spec_path, experiment_spec, default_spec_path,
                                 merge_from_default)
    validate_spec(experiment_spec, validation_schema=validation_schema)

    return experiment_spec
