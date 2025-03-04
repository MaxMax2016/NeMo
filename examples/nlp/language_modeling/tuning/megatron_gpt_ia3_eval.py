# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import torch
import torch.multiprocessing as mp
from megatron.core import parallel_state
from omegaconf import OmegaConf
from omegaconf.omegaconf import open_dict
from pytorch_lightning.trainer.trainer import Trainer

from nemo.collections.nlp.models.language_modeling.megatron_gpt_adapter_model import MegatronGPTInfusedAdapterModel
from nemo.collections.nlp.models.language_modeling.megatron_gpt_model import MegatronGPTModel
from nemo.collections.nlp.parts.nlp_overrides import NLPDDPStrategy, NLPSaveRestoreConnector
from nemo.core.config import hydra_runner

mp.set_start_method("spawn", force=True)

"""
This is the script to run an Adapter Tuned GPT Model for text generation.

Usage:
    Assume the model has TP=1, PP=1 in the following use cases.
    a. run greedy inference using a base gpt nemo file, and an adapter nemo file:
        python megatron_gpt_ia3_eval.py \
            gpt_model_file=PATH TO GPT MODEL NEMO FILE \
            adapter_model_file=PATH TO ADAPTER MODEL NEMO FILE (generated by training script: ./megatron_gpt_ia3_tuning.py) \
            data_paths=[PATH TO A JSONL FILE CONTAINING PROMPTS], \
            pred_file_path=PATH TO OUTPUT FILE TO DUMP PREDICTIONS
"""

if not torch.cuda.is_available():
    raise EnvironmentError("GPU is needed for the inference")


@hydra_runner(config_path="conf", config_name="megatron_gpt_adapter_inference")
def main(cfg) -> None:

    # trainer required for restoring model parallel models
    trainer = Trainer(strategy=NLPDDPStrategy(), **cfg.trainer)

    if (
        cfg.tensor_model_parallel_size < 0
        or cfg.pipeline_model_parallel_size < 0
        or cfg.get('pipeline_model_parallel_split_rank', -1) < 0
    ):
        save_restore_connector = NLPSaveRestoreConnector()
        if os.path.isdir(cfg.gpt_model_file):
            save_restore_connector.model_extracted_dir = cfg.gpt_model_file
        model_config = MegatronGPTModel.restore_from(
            restore_path=cfg.gpt_model_file,
            trainer=trainer,
            return_config=True,
            save_restore_connector=save_restore_connector,
        )

        with open_dict(cfg):
            cfg.tensor_model_parallel_size = model_config.get('tensor_model_parallel_size', 1)
            cfg.pipeline_model_parallel_size = model_config.get('pipeline_model_parallel_size', 1)
            cfg.pipeline_model_parallel_split_rank = model_config.get('pipeline_model_parallel_split_rank', 0)

    # Load an adapter model,  must be provided in config
    if cfg.get("adapter_model_file", None) is not None:
        # Update frozen GPT model path in case it has changed
        ia3_tuning_cfg = MegatronGPTInfusedAdapterModel.restore_from(
            cfg.adapter_model_file, trainer=trainer, return_config=True
        )
        with open_dict(ia3_tuning_cfg):
            ia3_tuning_cfg.language_model_path = cfg.gpt_model_file

        # Now load prompt learning model with frozen gpt model base
        model = MegatronGPTInfusedAdapterModel.restore_from(
            restore_path=cfg.adapter_model_file, trainer=trainer, override_config_path=ia3_tuning_cfg
        )

    # Or load regular GPT model
    else:
        raise NotImplementedError(
            "This script is meant for inference from an Adapter Tuned GPT Model, for inference from a Megatron GPT model, refer to ../megatron_gpt_eval.py"
        )

    model.freeze()

    # Have to turn off activations_checkpoint_method for inference
    try:
        model.model.language_model.encoder.activations_checkpoint_method = None
    except AttributeError:
        pass

    try:
        model.frozen_model.model.language_model.encoder.activations_checkpoint_method = None
    except AttributeError:
        pass

    max_input_length = model.frozen_model.cfg.encoder_seq_length - cfg.inference.tokens_to_generate
    # check whether the DDP is initialized
    if parallel_state.is_unitialized():

        def dummy():
            return

        if trainer.strategy.launcher is not None:
            trainer.strategy.launcher.launch(dummy, trainer=trainer)
        trainer.strategy.setup_environment()

    _, dataloader = model.build_virtual_prompt_dataset(
        data=cfg.data_paths,
        batch_size=cfg.get("batch_size", 1),
        max_seq_length=max_input_length,
        min_seq_length=model.cfg.data.get('min_seq_length', 1),
        add_bos=cfg.inference.add_BOS,
        add_eos=False,
        for_train=False,
        tokens_to_generate=cfg.inference.tokens_to_generate,
        drop_last=False,
        shuffle=False,
        num_workers=cfg.get("num_workers", 1),
    )

    config = OmegaConf.to_container(cfg.inference)
    model.set_inference_config(config)
    response = trainer.predict(model, dataloader)
    print("***************************")
    if cfg.pred_file_path is not None:
        with open(cfg.pred_file_path, "w", encoding="utf-8") as f:
            for batch in response:
                for sentence in batch['sentences']:
                    s = ' '.join(sentence.split('\n'))
                    f.write(s + "\n")
        print("predictions saved to {}".format(cfg.pred_file_path))
    else:
        print(response)
    print("***************************")


if __name__ == '__main__':
    main()  # noqa pylint: disable=no-value-for-parameter
