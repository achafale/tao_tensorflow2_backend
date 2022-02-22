#!/bin/bash
# Copyright (c) 2021-2022, NVIDIA CORPORATION.  All rights reserved.
# Parse args to find "-np <num GPUs>".
NUM_GPUS=0
PYTHON_ARGS=()
CURDIR=$(pwd)
BASEDIR=$(dirname "$0")
while [[ $# -gt 0 ]]
do
key="$1"

case $key in
    -np)
    NUM_GPUS="$2"
    shift # Skip argument.
    shift # Skip value.
    ;;
    *)    # Unknown option, pass these to python.
    PYTHON_ARGS+=("$1")
    shift # Skip argument.
    ;;
esac
done

if [ "$NUM_GPUS" -lt 1 ]; then
	echo "Usage: -np <num GPUs> [train.py arguments]"
else
	# Note: need to execute bazel created train script instead of train.py.
	mpirun -np $NUM_GPUS --allow-run-as-root --bind-to none -map-by slot -x LD_LIBRARY_PATH -x PATH -mca pml ob1 -mca btl ^openib python /workspace/tao-tf2/cv/efficientdet/scripts/train.py ${PYTHON_ARGS[*]}
fi

