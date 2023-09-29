"""
Trainer script for the ACTA modules.

   Copyright 2023 The ANTIDOTE Project Contributors <https://univ-cotedazur.eu/antidote>

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""

import argparse
import csv
import logging
import lightning.pytorch as pl
import os
import re
import sys

from datetime import datetime
from lightning.pytorch.callbacks import EarlyStopping, ModelCheckpoint
from lightning.pytorch.loggers import TensorBoardLogger
from pathlib import Path, PosixPath
from sklearn.metrics import classification_report
from typing import Dict, Optional, Tuple, Union

from acta.data import RelationClassificationDataModule, SequenceTaggingDataModule
from acta.models import RelationClassificationTransformerModule, SequenceTaggingTransformerModule
from acta.utils import compute_metrics, TTYAwareProgressBar


MODELS = {
    'bert': 'bert-base-uncased',
    'biobert': 'monologg/biobert_v1.1_pubmed',
    'deberta-v3': 'microsoft/deberta-v3-base',
    'roberta': 'roberta-base',
    'scibert-monolog': 'monologg/scibert_scivocab_uncased',
    'scibert-allenai': 'allenai/scibert_scivocab_uncased',
    'xlm-roberta': 'xlm-roberta-base',
    'tiny-bert': 'prajjwal1/bert-tiny'  # Only for debug purposes
}
TASKS = {
    'rel-class': (RelationClassificationDataModule, RelationClassificationTransformerModule, 'tsv'),
    'seq-tag': (SequenceTaggingDataModule, SequenceTaggingTransformerModule, 'conll')
}
DATA_SPLITS = ['train', 'test', 'validation', 'dev']

logger = logging.getLogger(__name__)


def get_data_splits(input_dir: PosixPath, task_type: str) -> Dict[str, PosixPath]:
    """
    Function that search for possible split files given an input directory.

    It will return a map between each of the three possible splits (train, test,
    validation) and the path to the corresponding file. Since there could be a
    `dev.*` file that is usually used for validation purposes, the function
    maps that file to the `validation` split in order to keep the convention used
    in Lightning and HuggingFace.

    It will search the files based on the task name.

    Parameters
    ----------
    input_dir: PosixPath
        Path to the input dir with the split files.
    task_type: str
        Used to determine the file extension.

    Returns
    -------
    Dict[str, PosixPath]
        A mapping between the split name and the associated file path to that
        split.
    """
    assert task_type in TASKS, f"Invalid task, use on of: {', '.join(TASKS.keys())}."
    file_type = TASKS[task_type][2]
    splits_files = {}
    for split in DATA_SPLITS:
        split_file = list(input_dir.glob(f"{split}*.{file_type}"))
        if split_file:
            if split == 'dev' and 'validation' in splits_files:
                logger.warning(
                    f"Ignoring the file {split_file[0]} since there is {splits_files['validation']}"
                )
            elif split == 'validation' and 'validation' in splits_files:
                # It detected the existence of a `dev.*` file and it uses that
                logger.warning(
                    f"Ignoring the file {split_file[0]} since there is {splits_files['validation']}"
                )
            elif len(split_file) > 1:
                logger.warning(
                    f"There's more than 1 file for {split} split. Using {split_file[0]}."
                )
            # We use the `dev.*` files as validation files and rename the split to match the
            # convention both for Lightning and HuggingFace
            split = 'validation' if split == 'dev' else split
            splits_files[split] = split_file[0]
    return splits_files


def train_model(data_module: pl.LightningDataModule, model: pl.LightningModule,
                config: argparse.Namespace) -> Tuple[pl.Trainer, ModelCheckpoint]:
    """
    Trains a model and returns the checkpoints for that model.
    """
    model_name = config.model if config.model in MODELS else os.path.basename(config.model)
    model_name = f"{model_name}_{config.task_type}"
    callbacks = []

    model_logger = TensorBoardLogger(
        save_dir=config.output_dir / config.logging_dir,
        name=model_name,
        version=config.timestamp
    )

    model_checkpoints = ModelCheckpoint(
        dirpath=config.output_dir / config.checkpoint_path,
        filename=model_name + "_{epoch:02d}_{step:05d}",
        save_top_k=-1,  # Save all models
        every_n_train_steps=config.save_every_n_steps
    )
    callbacks.append(model_checkpoints)

    progress_bar = TTYAwareProgressBar(refresh_rate=config.log_every_n_steps)
    callbacks.append(progress_bar)

    if config.early_stopping:
        early_stopping = EarlyStopping(
            monitor='val_loss',
            min_delta=1e-6,
            patience=config.early_stopping
        )
        callbacks.append(early_stopping)

    trainer = pl.Trainer(
        accelerator=config.accelerator,
        devices=config.num_devices,
        precision='16-mixed' if config.fp16 else '32-true',
        logger=model_logger,
        callbacks=callbacks,
        max_epochs=config.epochs if config.max_steps < 0 else None,
        max_steps=config.max_steps,
        val_check_interval=config.log_every_n_steps,
        log_every_n_steps=config.log_every_n_steps,
        accumulate_grad_batches=config.gradient_accumulation_steps,
        gradient_clip_val=config.max_grad_norm if config.max_grad_norm else None,
        limit_train_batches=0.1 if config.debug else 1.0,  # Use only 10% of training for debug
        limit_test_batches=0.1 if config.debug else 1.0,
        limit_predict_batches=0.1 if config.debug else 1.0,
        limit_val_batches=0 if not config.validation else 0.1 if config.debug else 1.0,
        num_sanity_val_steps=0 if not config.validation else 1 if config.debug else 2
    )

    logger.info("Starting model training routine")
    trainer.fit(model, datamodule=data_module, ckpt_path=config.load_from_checkpoint)
    logger.info("Finished model training routine")

    logger.info("Saving last model checkpoint")
    last_model_checkpoint = Path(model_checkpoints.format_checkpoint_name(
        {"epoch": trainer.current_epoch, "step": trainer.global_step}
    ))
    if not last_model_checkpoint.is_file():
        # Save a checkpoint for the last epoch and last step
        trainer.save_checkpoint(last_model_checkpoint)
    # Create a link to the last model checkpoint (overwrite if necessary)
    last_model_checkpoint_symlink = Path(f"{model_checkpoints.dirpath}/{model_name}_final.ckpt")
    if last_model_checkpoint_symlink.exists():
        logger.warning(f"Overriding link to last checkpoint to {last_model_checkpoint}")
        os.unlink(last_model_checkpoint_symlink)
    os.symlink(last_model_checkpoint, last_model_checkpoint_symlink)

    return trainer, model_checkpoints


def evaluate_model(data_module: pl.LightningDataModule,
                   model_or_checkpoint: Union[pl.LightningModule, PosixPath],
                   config: argparse.Namespace, trainer: pl.Trainer, model_name: str):
    """
    Evaluates a single model and write its outputs.
    """
    logger.info(f"Evaluating {model_name}")
    results_dir = config.output_dir / 'results' / config.timestamp
    if isinstance(model_or_checkpoint, pl.LightningModule):
        decoded_predictions = [
            decoded_prediction
            for batch_prediction in trainer.predict(model=model_or_checkpoint,
                                                    datamodule=data_module)
            for decoded_prediction in data_module.decode_predictions(**batch_prediction)
        ]
    else:
        decoded_predictions = [
            decoded_prediction
            for batch_prediction in trainer.predict(ckpt_path=model_or_checkpoint,
                                                    datamodule=data_module)
            for decoded_prediction in data_module.decode_predictions(**batch_prediction)
        ]

    if config.task_type == 'rel-class':
        # Predictions have the form (true_label, predicted_label, sentence1, sentence2)
        true_labels = []
        pred_labels = []
        for prediction in decoded_predictions:
            true_labels.append(prediction[0])
            pred_labels.append(prediction[1])
        with open(results_dir / f'{model_name}_predictions.tsv', 'w') as fh:
            print('true\tpredicted\tsentence1\tsentence2', file=fh)
            print('\n'.join(['\t'.join(pred) for pred in decoded_predictions]), file=fh)
        metrics = compute_metrics(
            true_labels, pred_labels,
            limited_labels=[lbl for lbl in data_module.label2id.keys() if lbl != 'noRel'],
            prefix="eval"
        )
    elif config.task_type == 'seq-tag':
        # Predictions are a list of lists of tuples, where each tuple has the form
        # (token, predicted_label, true_label)
        true_labels = []
        pred_labels = []
        for sentence in decoded_predictions:
            true_labels.extend([token[2] for token in sentence])
            pred_labels.extend([token[1] for token in sentence])
        with open(results_dir / f'{model_name}_predictions.conll', 'w') as fh:
            print("\n\n".join(["\n".join(["\t".join(token) for token in sentence])
                               for sentence in decoded_predictions]), file=fh)
        metrics = compute_metrics(
            true_labels, pred_labels,
            limited_labels=[lbl for lbl in data_module.label2id.keys()
                            if lbl not in {'O', 'X', 'PAD'}],
            prefix="eval"
        )

    for metric, value in metrics.items():
        with open(results_dir / f'{metric}.csv', 'at') as fh:
            csv_writer = csv.writer(fh)
            csv_writer.writerow([model_name, value])

    with open(results_dir / f'{model_name}_report.txt', 'wt') as fh:
        print(
            classification_report(
                true_labels, pred_labels, labels=list(data_module.label2id.keys()), zero_division=0
            ),
            file=fh
        )

    return metrics


def evaluate_models(data_module: pl.LightningDataModule, model: pl.LightningModule,
                    config: argparse.Namespace, trainer: Optional[pl.Trainer] = None,
                    model_checkpoints: Optional[ModelCheckpoint] = None):
    """
    Evaluates the model on the evaluation dataset. Depending on the options, it
    will evaluate only in the final model or in all the models in the
    checkpoints.
    """
    # Create the results directory (should be unique)
    os.makedirs(config.output_dir / 'results' / config.timestamp)
    model_name = config.model if config.model in MODELS else os.path.basename(config.model)
    model_name = f"{model_name}_{config.task_type}"

    if not config.train:
        # Build a trainer for prediction purposes
        model_logger = TensorBoardLogger(
            save_dir=config.output_dir / config.logging_dir,
            name=model_name,
            version=config.timestamp
        )
        model_checkpoints = ModelCheckpoint(
            dirpath=config.output_dir / config.checkpoint_path,
            filename=model_name + "_{epoch:02d}_{step:05d}"
        )
        trainer = pl.Trainer(
            accelerator=config.accelerator,
            devices=config.num_devices,
            precision='16-mixed' if config.fp16 else '32-true',
            logger=model_logger,
            max_epochs=1,
            max_steps=-1,
            limit_train_batches=0.1 if config.debug else 1.0,  # Use only 10% of training for debug
            limit_test_batches=0.1 if config.debug else 1.0,
            limit_predict_batches=0.1 if config.debug else 1.0,
            limit_val_batches=0 if not config.validation else 0.1 if config.debug else 1.0,
            num_sanity_val_steps=0 if not config.validation else 1 if config.debug else 2
        )

    if config.eval_all_checkpoints:
        if config.train:
            # Training mode, try to fetch the last checkpoint from the symlink and check it
            # corresponds to the actual last training checkpoint from trainer
            last_model_checkpoint = Path(model_checkpoints.format_checkpoint_name(
                {"epoch": trainer.current_epoch, "step": trainer.global_step}
            ))
            last_model_checkpoint_symlink = Path(
                f"{model_checkpoints.dirpath}/{model_name}_final.ckpt"
            )
            if not last_model_checkpoint_symlink.exists() or\
                    Path(os.readlink(last_model_checkpoint_symlink)) != last_model_checkpoint:
                logger.warning(f"The last model checkpoint `{last_model_checkpoint}` doesn't "
                               "correspond to the final checkpoint link "
                               f"`{last_model_checkpoint_symlink}`. The evaluation will be done "
                               f"with `{last_model_checkpoint}` as final checkpoint.")
        elif config.load_from_checkpoint is not None:
            # If there was no training, assumes the last checkpoint was loaded by
            # the `--load-from-checkpoint` option
            last_model_checkpoint = Path(config.load_from_checkpoint)
        else:
            # There isn't any information on what the last checkpoint is, it will run
            # all found checkpoints files with a warning
            logger.warning("There is no information on what the last checkpoint was. "
                           "This will evaluate on all the found checkpoints files, "
                           "with unexpected results (checkpoint may come from different runs).")
            last_model_checkpoint = None

        if last_model_checkpoint is not None:
            checkpoint_step = re.search(r"(?<=step=)\d+", last_model_checkpoint.name)
            if checkpoint_step:
                # Get the global step of the checkpoint
                last_checkpoint_step = int(checkpoint_step.group(0))
            else:
                last_checkpoint_step = 0
                logger.warning("It wasn't possible to determine last checkpoint step. "
                               "This will evaluate on all the found checkpoints files, "
                               "with unexpected results (checkpoint may come from different runs).")
        else:
            last_checkpoint_step = 0

        multiversions_warning = True
        for checkpoint_file in sorted(Path(model_checkpoints.dirpath).glob(f'{model_name}_*.ckpt')):
            if checkpoint_file.name.endswith('_final.ckpt') or\
                    checkpoint_file.name == last_model_checkpoint.name:
                # Ignore the last checkpoint, it will be run at the end
                continue
            if re.search(r"-v\d+.ckpt$", checkpoint_file.name) and multiversions_warning:
                logger.warning("Multiple versions of checkpoint files were found "
                               "this could give unexpected results (checkpoints come "
                               "from different runs).")
                multiversions_warning = False
            checkpoint_step = re.search(r"(?<=step=)\d+", checkpoint_file.name)
            checkpoint_step = int(checkpoint_step.group(0)) if checkpoint_step else None
            if last_checkpoint_step > 0 and checkpoint_step is None:
                # Do not run unkown checkpoints when last_checkpoint_step is known
                logger.warning(f"Ignoring {checkpoint_file} since it doesn't have a declared step.")
                continue
            elif (checkpoint_step is not None and checkpoint_step < last_checkpoint_step) \
                    or last_checkpoint_step == 0:
                # Run checkpoint steps previous to last_checkpoint_step
                # Or run every checkpoint file following the previous warning
                # (i.e. last_checkpoint_step == 0)
                if not config.train:
                    # Need to load the checkpoint_file
                    checkpoint_file = TASKS[config.task_type][1].load_from_checkpoint(
                        checkpoint_file
                    )
                metrics = evaluate_model(data_module, checkpoint_file, config, trainer,
                                         checkpoint_file.name.split('.ckpt')[0])
                trainer.logger.log_metrics(metrics, step=checkpoint_step)
        if last_checkpoint_step is not None:
            # Evaluates the final checkpoint (if exists)
            metrics = evaluate_model(data_module, last_model_checkpoint, config, trainer,
                                     f"{model_name}_final")
            trainer.logger.log_metrics(metrics, step=trainer.global_step)
    else:
        metrics = evaluate_model(data_module, model, config, trainer, f"{model_name}_final")
        trainer.logger.log_metrics(metrics, step=trainer.global_step)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument("--input-dir",
                        required=True,
                        type=Path,
                        help="The input directory. It has the train, test and validation (dev) "
                             "files. Depending on the task they might be tsv or conll.")
    parser.add_argument("--output-dir",
                        required=True,
                        type=Path,
                        help="The output directory where the model predictions and checkpoints "
                             "will be stored.")
    parser.add_argument("--task-type",
                        choices=TASKS.keys(),
                        required=True,
                        help=f"Type of task. Use one of: {', '.join(TASKS.keys())}")
    parser.add_argument("--model",
                        required=True,
                        help="Either the name of one of the available models: "
                             f"{', '.join(MODELS.keys())}; or a path to a pre-trained model file. "
                             "If using a file, it must be a Hugging Face model path. To load a "
                             "checkpoint reached using this same trainer script please use the "
                             "`--load-from-checkpoint` option.")

    # Other parameters
    parser.add_argument("--config",
                        help="Pretrained config name or path (if not the same as `model`).")
    parser.add_argument("--tokenizer",
                        help="Pretrained tokenizer name or path (if not the same as `model`).")
    parser.add_argument("--cache-dir",
                        help="Directory for Hugging Face downloaded models.")
    parser.add_argument("--checkpoint-path",
                        default="checkpoints",
                        help="Name of directory (inside output-dir) to store the checkpoint files.")
    parser.add_argument("--load-from-checkpoint",
                        help="Path to a checkpoint file to continue training.")
    parser.add_argument("--logging-dir",
                        default="logs",
                        help="Name of directory (inside output-dir) to store the TensorBoard logs.")
    parser.add_argument("--train",
                        action="store_true",
                        help="Train the model.")
    parser.add_argument("--evaluation-split",
                        choices=["train", "test", "validation"],
                        help="The split to use for evaluation at the end of training "
                             "(train, validation, test)."
                             "If not given there won't be any evaluation done.")
    parser.add_argument("--validation",
                        action="store_true",
                        help="If active, runs validation after `--log-every-n-steps` steps. "
                             "Validation is useful for early stopping of the training process.")
    parser.add_argument("--accelerator",
                        default="auto",
                        help="What device to use as accelerator (cpu, gpu, tpu, etc).")
    parser.add_argument("--num-devices",
                        default=-1,
                        type=int,
                        help="Number of devices to use. If not given selects automatically.")
    parser.add_argument("--num-workers",
                        default=-1,
                        type=int,
                        help="Number of workers to use for DataLoaders. Set to -1 to use all cpus.")
    parser.add_argument("--epochs",
                        default=3,
                        type=int,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--early-stopping",
                        default=0,
                        type=int,
                        help="If > 0 then stops if there are `early-stopping` logs without "
                             "improvement on the validation loss.")
    parser.add_argument("--max-steps",
                        default=-1,
                        type=int,
                        help="If > 0: set total number of training steps to perform. "
                             "Overrides epochs.")
    parser.add_argument("--train-batch-size",
                        default=8,
                        type=int,
                        help="Batch size (per GPU/CPU) for training.")
    parser.add_argument("--eval-batch-size",
                        default=8,
                        type=int,
                        help="Batch size (per GPU/CPU) for evaluation.")
    parser.add_argument("--gradient-accumulation-steps",
                        default=1,
                        type=int,
                        help="Number of updates steps to accumulate before "
                             "performing a backward/update pass.")
    parser.add_argument("--max-grad-norm",
                        default=1.0,
                        type=float,
                        help="Max gradient norm (for gradient clipping). Set to 0 to deactivate.")
    parser.add_argument("--learning-rate",
                        default=5e-5,
                        type=float,
                        help="The initial learning rate for AdamW.")
    parser.add_argument("--max-seq-length",
                        type=int,
                        help="The maximum total input sequence length after tokenization."
                             "Sequences longer than this will be truncated, "
                             "sequences shorter will be padded. "
                             "If left empty it will truncate to the model's max size and pad to "
                             "the maximum size of each training step.")
    parser.add_argument("--lower-case",
                        action="store_true",
                        help="Should be active for lowercase transformers.")
    parser.add_argument("--weight-decay",
                        default=0.0,
                        type=float,
                        help="Weight decay for parameters that accept it.")
    parser.add_argument("--adam-epsilon",
                        default=1e-8,
                        type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--warmup-steps",
                        default=0,
                        type=int,
                        help="Number of steps for linear warmup.")
    parser.add_argument("--log-every-n-steps",
                        default=50,
                        type=int,
                        help="Log every N update steps.")
    parser.add_argument("--save-every-n-steps",
                        default=50,
                        type=int,
                        help="Save checkpoint every N update steps.")
    parser.add_argument("--eval-all-checkpoints",
                        action="store_true",
                        help="Evaluate all checkpoints for the model.")
    parser.add_argument("--overwrite-output",
                        action="store_true",
                        help="Overwrite the content of the output directory.")
    parser.add_argument("--random-seed",
                        default=42,
                        type=int,
                        help="Initial random seed.")
    parser.add_argument("--fp16",
                        action="store_true",
                        help="Whether to use 16-bit (mixed) precision")
    parser.add_argument("--server_ip",
                        default="",
                        help="For distant debugging.")
    parser.add_argument("--server_port",
                        default="",
                        help="For distant debugging.")
    parser.add_argument("--debug",
                        action="store_true",
                        help="Set for debug mode.")
    config = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.DEBUG if config.debug else logging.INFO,
    )

    # Checking pre-conditions
    if not config.train and not config.evaluation_split:
        logger.error("The script must be run for training or at least have 1 evaluation split.")
        sys.exit(1)

    if not config.train and config.load_from_checkpoint is None:
        logger.warning("Evaluation to be run on model without finetuning.")

    if config.output_dir.exists() and list(config.output_dir.glob('*')) and config.train\
            and not config.overwrite_output:
        logger.error(f"Output directory ({config.output_dir}) already exists and is not empty. "
                     "Use --overwrite-output to ovewrite the directory (information will be lost).")
        sys.exit()

    if config.model not in MODELS and not Path(config.model).is_file():
        logger.error(f"The model {config.model} is not available in the list of models: "
                     f"{', '.join(MODELS.keys())}; and is not an existing file.")
        sys.exit()

    data_splits = get_data_splits(config.input_dir, config.task_type)
    if not data_splits:
        logger.error("There are no files to train nor evaluate. Exiting the trainer.")
        sys.exit(1)

    if config.train and 'train' not in data_splits:
        logger.error("There's no file for training.")
        sys.exit(1)

    if config.evaluation_split and config.evaluation_split not in data_splits:
        logger.error(f"The evaluation split {config.evaluation_split} file is missing.")
        sys.exit(1)

    if config.validation and 'validation' not in data_splits:
        logger.error("There's no file for validation.")
        sys.exit(1)

    if config.early_stopping and 'validation' not in data_splits:
        logger.error("There's no validation file for early stopping")
        sys.exit(1)

    if config.model == 'tiny-bert' and not config.debug:
        logger.error("The model `tiny-bert` is only available for debug mode")
        sys.exit(1)

    config.num_devices = config.num_devices if config.num_devices > 0 else "auto"

    # Setup distant debugging if needed
    if config.server_ip and config.server_port:
        # Distant debugging
        # see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
        import ptvsd

        logger.debug("Waiting for debugger attach")
        ptvsd.enable_attach(address=(config.server_ip, config.server_port), redirect_output=True)
        ptvsd.wait_for_attach()

    logger.info(
        f"Accelerator: {config.accelerator}.\n"
        f"No. of devices: {config.num_devices}.\n"
        f"16-bit precision training: {config.fp16}."
    )

    # Create output directory
    os.makedirs(config.output_dir, exist_ok=True)

    # Timestamp to keep track of results
    config.timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")

    # Set random seed
    pl.seed_everything(config.random_seed)

    if config.tokenizer:
        hf_tokenizer_name_or_path = config.tokenizer
    elif config.model in MODELS:
        hf_tokenizer_name_or_path = MODELS[config.model]
    else:
        hf_tokenizer_name_or_path = config.model

    data_module = TASKS[config.task_type][0](
        data_splits=data_splits,
        tokenizer_name_or_path=hf_tokenizer_name_or_path,
        tokenizer_config=dict(
            cache_dir=config.cache_dir,
            do_lower_case=config.lower_case,
            use_fast=True
        ),
        datasets_config=dict(
            max_seq_length=config.max_seq_length
        ),
        train_batch_size=config.train_batch_size,
        eval_batch_size=config.eval_batch_size,
        evaluation_split=config.evaluation_split,
        num_workers=config.num_workers
    )
    data_module.prepare_data()
    data_module.setup('fit')

    # Setting up the Hugging Face model or path
    if config.model in MODELS:
        hf_model_name_or_path = MODELS[config.model]
    else:
        hf_model_name_or_path = config.model

    if config.load_from_checkpoint is not None:
        if not Path(config.load_from_checkpoint).is_file():
            logger.error(f"The checkpoint file doesn't exists: {config.load_from_checkpoint}")
            sys.exit(1)
        model = TASKS[config.task_type][1].load_from_checkpoint(config.load_from_checkpoint)
    else:
        model = TASKS[config.task_type][1](
            model_name_or_path=hf_model_name_or_path,
            id2label=data_module.id2label,
            label2id=data_module.label2id,
            config_name_or_path=config.config,
            cache_dir=config.cache_dir,
            masked_label=data_module.label2id.get('PAD', -100),
            learning_rate=config.learning_rate,
            weight_decay=config.weight_decay,
            adam_epsilon=config.adam_epsilon,
            warmup_steps=config.warmup_steps
        )

    if config.train:
        trainer, model_checkpoints = train_model(data_module, model, config)

    if config.evaluation_split:
        evaluate_models(data_module, model, config, trainer if config.train else None,
                        model_checkpoints if config.train else None)
