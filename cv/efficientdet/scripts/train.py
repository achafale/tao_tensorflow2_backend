# Copyright (c) 2021-2022, NVIDIA CORPORATION.  All rights reserved.
"""The main training script."""
import os
import time
from absl import logging
import tensorflow as tf
import horovod.tensorflow.keras as hvd
from dllogger import StdOutBackend, JSONStreamBackend, Verbosity
import dllogger as DLLogger
from nv_tfqat_wrappers import quantize

from cv.efficientdet.config.hydra_runner import hydra_runner
from cv.efficientdet.config.default_config import ExperimentConfig
from cv.efficientdet.dataloader import dataloader
from cv.efficientdet.losses import losses
from cv.efficientdet.model.efficientdet import efficientdet
from cv.efficientdet.model import callback_builder
from cv.efficientdet.model import optimizer_builder
from cv.efficientdet.trainer.efficientdet_trainer import EfficientDetTrainer
from cv.efficientdet.utils import hparams_config, keras_utils
from cv.efficientdet.utils.config_utils import generate_params_from_cfg
from cv.efficientdet.utils.helper import dump_json, decode_eff, load_model
from cv.efficientdet.utils.horovod_utils import is_main_process, get_world_size, get_rank, initialize


def run_experiment(cfg, results_dir, key):

    # get e2e training time
    begin = time.time()
    logging.info("Training started at: {}".format(time.asctime()))

    hvd.init()

    # Parse and update hparams
    config = hparams_config.get_detection_config(cfg['model_config']['model_name'])
    config.update(generate_params_from_cfg(config, cfg, mode='train'))

    # initialize
    initialize(config, training=True)
    # dllogger setup
    backends = []
    if is_main_process():
        log_path = os.path.join(cfg['results_dir'], 'log.txt')
        backends += [
            JSONStreamBackend(verbosity=Verbosity.VERBOSE, filename=log_path),
            StdOutBackend(verbosity=Verbosity.DEFAULT)]
    DLLogger.init(backends=backends)

    steps_per_epoch = (cfg['train_config']['num_examples_per_epoch'] + 
        (cfg['train_config']['train_batch_size'] * get_world_size()) - 1) // \
            (cfg['train_config']['train_batch_size'] * get_world_size())

    # Set up dataloader
    train_dl= dataloader.CocoDataset(
        cfg['data_config']['training_file_pattern'],
        is_training=True,
        use_fake_data=cfg['data_config']['use_fake_data'],
        max_instances_per_image=config.max_instances_per_image)
    train_dataset = train_dl(
        config.as_dict(),
        batch_size=cfg['train_config']['train_batch_size'])

    eval_dl = dataloader.CocoDataset(
        cfg['data_config']['validation_file_pattern'],
        is_training=False,
        max_instances_per_image=config.max_instances_per_image)
    eval_dataset = eval_dl(
        config.as_dict(),
        batch_size=cfg['eval_config']['eval_batch_size'])

    # Build models
    if not cfg['train_config']['pruned_model_path']:
        # TODO(@yuw): verify channels_first training or force last
        input_shape = list(config.image_size) + [3] \
            if config.data_format == 'channels_last' else [3] + list(config.image_size)
        original_learning_phase = tf.keras.backend.learning_phase()
        model = efficientdet(input_shape, training=True, config=config)
        tf.keras.backend.set_learning_phase(0)
        eval_model = efficientdet(input_shape, training=False, config=config)
        tf.keras.backend.set_learning_phase(original_learning_phase)
    else:
        original_learning_phase = tf.keras.backend.learning_phase()
        model = load_model(cfg['train_config']['pruned_model_path'], cfg, mode='train')
        tf.keras.backend.set_learning_phase(0)
        eval_model = load_model(cfg['train_config']['pruned_model_path'], cfg, mode='eval')
        tf.keras.backend.set_learning_phase(original_learning_phase)
    # TODO(@yuw): check and enable nms_config?
    # TODO(@yuw): save to another eff file?
    if is_main_process():
        dump_json(model, os.path.join(cfg['results_dir'], 'train_graph.json'))
        dump_json(eval_model, os.path.join(cfg['results_dir'], 'eval_graph.json'))

    resume_ckpt_path = os.path.join(cfg['results_dir'], f'{config.name}.resume')
    if cfg['train_config']['checkpoint'] and not os.path.exists(resume_ckpt_path):
        if not cfg['train_config']['pruned_model_path']:
            print("Loading pretrained weight....")
            pretrained_model = tf.keras.models.load_model(cfg['train_config']['checkpoint'])
            for layer in pretrained_model.layers[1:]:
                # The layer must match up to prediction layers.
                try:
                    l_return = model.get_layer(layer.name)
                except ValueError:
                    # Some layers are not there
                    print(f"Skipping as {layer.name} is not found.")
                try:
                    l_return.set_weights(layer.get_weights())
                except ValueError:
                    print(f"Skipping {layer.name} due to shape mismatch.")

    # TODO(@yuw): Enable QAT
    if cfg['train_config']['qat']:
        model = quantize.quantize_model(model, do_quantize_residual_connections=True)
    # Compile model
    # pick focal loss implementation
    focal_loss = losses.StableFocalLoss(
        config.alpha,
        config.gamma,
        label_smoothing=config.label_smoothing,
        reduction=tf.keras.losses.Reduction.NONE)
    model.compile(
        optimizer=optimizer_builder.get_optimizer(cfg['train_config'], steps_per_epoch),
        loss={
            'box_loss':
                losses.BoxLoss(
                    0.1, # config['delta'],
                    reduction=tf.keras.losses.Reduction.NONE),
            'box_iou_loss':
                losses.BoxIouLoss(
                    config.iou_loss_type,
                    config.min_level,
                    config.max_level,
                    config.num_scales,
                    config.aspect_ratios,
                    config.anchor_scale,
                    config.image_size,
                    reduction=tf.keras.losses.Reduction.NONE),
            'class_loss': focal_loss,
            'seg_loss':
                tf.keras.losses.SparseCategoricalCrossentropy(
                    from_logits=True,
                    reduction=tf.keras.losses.Reduction.NONE)
        })

    # resume from checkpoint or load pretrained backbone
    train_from_epoch = 0
    if os.path.exists(resume_ckpt_path) and is_main_process():
        print("Resume training...")
        ckpt_path, _ = decode_eff(resume_ckpt_path, cfg.key)
        train_from_epoch = keras_utils.restore_ckpt(
            model,
            ckpt_path, 
            config.moving_average_decay,
            steps_per_epoch=steps_per_epoch,
            expect_partial=False)
        # TODO(@yuw): remove ckpt_path

    # set up callbacks
    num_samples = (cfg['eval_config']['eval_samples'] + get_world_size() - 1) // get_world_size()
    num_samples = (num_samples + cfg['eval_config']['eval_batch_size'] - 1) // cfg['eval_config']['eval_batch_size']
    cfg['eval_config']['eval_samples'] = num_samples

    callbacks = callback_builder.get_callbacks(
        cfg, 'traineval', eval_dataset.shard(get_world_size(), get_rank()).take(num_samples),
        DLLogger,
        profile=False, 
        time_history=False,
        log_steps=1,
        lr_tb=False,
        benchmark=False,
        eval_model=eval_model)

    trainer = EfficientDetTrainer(model, config, callbacks)
    trainer.fit(
        train_dataset,
        eval_dataset,
        config.num_epochs,
        steps_per_epoch,
        initial_epoch=train_from_epoch,
        validation_steps=num_samples // cfg['eval_config']['eval_batch_size'],
        verbose=1 if is_main_process() else 0)


spec_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
@hydra_runner(
    config_path=os.path.join(spec_root, "experiment_specs"),
    config_name="train", schema=ExperimentConfig
)
def main(cfg: ExperimentConfig) -> None:
    """Wrapper function for EfficientDet training.
    """
    run_experiment(cfg=cfg,
                   results_dir=cfg.results_dir,
                   key=cfg.key)


if __name__ == '__main__':
    main()
