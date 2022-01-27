# Copyright (c) 2021-2022, NVIDIA CORPORATION.  All rights reserved.
"""EfficientDet standalone evaluation script."""
import os
import time
from mpi4py import MPI
from absl import logging
import tensorflow as tf
import horovod.tensorflow.keras as hvd

from cv.efficientdet.config.hydra_runner import hydra_runner
from cv.efficientdet.config.default_config import ExperimentConfig
from cv.efficientdet.dataloader import dataloader
from cv.efficientdet.model.efficientdet import efficientdet
from cv.efficientdet.processor.postprocessor import EfficientDetPostprocessor
from cv.efficientdet.utils import coco_metric, label_utils, keras_utils
from cv.efficientdet.utils import hparams_config
from cv.efficientdet.utils.config_utils import generate_params_from_cfg
from cv.efficientdet.utils.horovod_utils import is_main_process, get_world_size, get_rank


def run_experiment(cfg, results_dir, key):
    """Run evaluation."""
    hvd.init()
    gpus = tf.config.experimental.list_physical_devices('GPU')
    for gpu in gpus:
        tf.config.experimental.set_memory_growth(gpu, True)
    if gpus:
        tf.config.experimental.set_visible_devices(gpus[hvd.local_rank()], 'GPU')

    # Parse and update hparams
    config = hparams_config.get_detection_config(cfg['model_config']['model_name'])
    config.update(generate_params_from_cfg(config, cfg, mode='eval'))

    # Set up dataloader
    eval_dl = dataloader.CocoDataset(
        cfg['data_config']['validation_file_pattern'],
        is_training=False,
        max_instances_per_image=config.max_instances_per_image)
    eval_dataset = eval_dl(
        config.as_dict(),
        batch_size=cfg['eval_config']['eval_batch_size'])

    num_samples = (cfg['eval_config']['eval_samples'] + get_world_size() - 1) // get_world_size()
    num_samples = (num_samples + cfg['eval_config']['eval_batch_size'] - 1) // cfg['eval_config']['eval_batch_size']
    cfg['eval_config']['eval_samples'] = num_samples

    eval_dataset = eval_dataset.shard(get_world_size(), get_rank()).take(num_samples)
    # TODO(@yuw): make configurable
    input_shape = [512,512,3]
    outputs, model = efficientdet(input_shape, training=False, config=config)

    # evaluation
    postpc = EfficientDetPostprocessor(cfg)
    label_map = label_utils.get_label_map(cfg['eval_config']['label_map'])
    evaluator = coco_metric.EvaluationMetric(
        filename=cfg['data_config']['validation_json_file'], label_map=label_map)
    keras_utils.restore_ckpt(
        model, cfg['eval_config']['model_path'], config.moving_average_decay,
        steps_per_epoch=0, skip_mismatch=False, expect_partial=True)

    @tf.function
    def eval_model_fn(images, labels):
        cls_outputs, box_outputs = model(images, training=False)
        detections = postpc.generate_detections(
            cls_outputs, box_outputs,
            labels['image_scales'],
            labels['source_ids'])

        def transform_detections(detections):
            """A transforms detections in [id, x1, y1, x2, y2, score, class] 
               form to [id, x, y, w, h, score, class]."""
            return tf.stack([
                detections[:, :, 0],
                detections[:, :, 1],
                detections[:, :, 2],
                detections[:, :, 3] - detections[:, :, 1],
                detections[:, :, 4] - detections[:, :, 2],
                detections[:, :, 5],
                detections[:, :, 6],
            ], axis=-1)

        tf.numpy_function(
            evaluator.update_state,
            [labels['groundtruth_data'], transform_detections(detections)], [])

    evaluator.reset_states()
    # evaluate all images.
    pbar = tf.keras.utils.Progbar(num_samples)
    for i, (images, labels) in enumerate(eval_dataset):
        eval_model_fn(images, labels)
        if is_main_process():
            pbar.update(i)

    # gather detections from all ranks
    evaluator.gather()

    if is_main_process():
        # compute the final eval results.
        metrics = evaluator.result()
        metric_dict = {}
        for i, name in enumerate(evaluator.metric_names):
            metric_dict[name] = metrics[i]

        if label_map:
            for i, cid in enumerate(sorted(label_map.keys())):
                name = 'AP_/%s' % label_map[cid]
                metric_dict[name] = metrics[i + len(evaluator.metric_names)]
    MPI.COMM_WORLD.Barrier()


spec_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
@hydra_runner(
    config_path=os.path.join(spec_root, "experiment_specs"),
    config_name="eval", schema=ExperimentConfig
)
def main(cfg: ExperimentConfig) -> None:
    """Wrapper function for EfficientDet evaluation.
    """
    run_experiment(cfg=cfg,
                   results_dir=cfg.results_dir,
                   key=cfg.key)


if __name__ == '__main__':
    main()
