# Standard
from pathlib import Path
from typing import List, Optional
import inspect
import logging
import random
import subprocess
import sys
import time

# Third Party
from rich.logging import RichHandler
from torch import distributed as dist
from torch.distributed import get_rank, is_initialized
from torch.distributed.fsdp import FullStateDictConfig
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.fsdp import StateDictType
import numpy as np
import torch


def add_noisy_embeddings(model, noise_alpha=None):
    if not noise_alpha:
        return model

    def noised_embed(orig_embed, noise_alpha):
        def new_func(x):
            if model.training:
                embed_init = orig_embed(x)
                dims = torch.tensor(torch.numel(x))
                mag_norm = noise_alpha / torch.sqrt(dims)
                return embed_init + torch.zeros_like(embed_init).uniform_(
                    -mag_norm, mag_norm
                )
            else:
                return orig_embed(x)

        return new_func

    model_class_name = model.__class__.__name__
    if model_class_name in ["GPTMegatronForCausalLM", "GPTDolomiteForCausalLM"]:
        orig_forward = model.get_input_embeddings().forward
        model.get_input_embeddings().forward = noised_embed(orig_forward, noise_alpha)
    elif model_class_name in ["MistralForCausalLM", "LlamaForCausalLM"]:
        orig_forward = model.base_model.embed_tokens.forward
        model.base_model.embed_tokens.forward = noised_embed(orig_forward, noise_alpha)
    else:
        raise ValueError(f"Unsupported model class: {model_class_name}")
    return model


class StreamablePopen(subprocess.Popen):
    """
    Provides a way of reading stdout and stderr line by line.
    """

    def __init__(self, *args, **kwargs):
        # remove the stderr and stdout from kwargs
        kwargs.pop("stderr", None)
        kwargs.pop("stdout", None)

        super().__init__(*args, **kwargs)
        while True:
            if self.stdout:
                output = self.stdout.readline().strip()
                print(output)
            if self.stderr:
                error = self.stderr.readline().strip()
                print(error, file=sys.stderr)
            if self.poll() is not None:
                break


def convert_loss_to_reduce_sum(model, is_granite=False):
    """
    this is necessary because multipack changes the samples per gpu, which biases the gradients to be larger for batches with less samples but longer lengths.
    """
    if is_granite:

        def get_autoregressive_language_modeling_loss(
            lm_logits: torch.Tensor,
            labels: torch.Tensor,
            cu_seqlens: torch.Tensor,
        ) -> torch.Tensor:
            loss = None
            # Shift so that tokens < n predict n
            if labels is not None:
                if model._use_padding_free_transformer:
                    shift_logits = lm_logits[:-1, :]
                    shift_labels = labels[1:].to(shift_logits.device)

                    # this is needed so that the last token of current example doesn't predict first token of next example
                    drop_loss_positions = cu_seqlens[1:-1] - 1
                    shift_labels[drop_loss_positions] = -100
                else:
                    shift_logits = lm_logits[..., :-1, :].contiguous()
                    shift_labels = labels[..., 1:].contiguous().to(shift_logits.device)

                # Flatten the tokens
                loss_fct = torch.nn.CrossEntropyLoss(reduction="sum")
                loss = loss_fct(
                    shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1)
                )

            return loss

        model.get_autoregressive_language_modeling_loss = (
            get_autoregressive_language_modeling_loss
        )
        return model
    else:

        def reduce_sum_forward(
            input_ids: torch.LongTensor = None,
            attention_mask: Optional[torch.Tensor] = None,
            position_ids: Optional[torch.LongTensor] = None,
            past_key_values: Optional[List[torch.FloatTensor]] = None,
            inputs_embeds: Optional[torch.FloatTensor] = None,
            labels: Optional[torch.LongTensor] = None,
            use_cache: Optional[bool] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
            **deprecated_arguments,
        ):
            output = model.__original_forward__(
                input_ids,
                attention_mask,
                position_ids,
                past_key_values,
                inputs_embeds,
                labels,
                use_cache,
                output_attentions,
                output_hidden_states,
                return_dict,
            )

            return_dict = isinstance(output, dict)
            logits = output.logits if return_dict else output[0]
            loss = None
            if labels is not None:
                # Shift so that tokens < n predict n
                shift_logits = logits[..., :-1, :].contiguous()
                shift_labels = labels[..., 1:].contiguous()
                # Flatten the tokens
                shift_logits = shift_logits.view(-1, model.config.vocab_size)
                shift_labels = shift_labels.view(-1)
                # Ensure tensors are on the same device
                shift_labels = shift_labels.to(shift_logits.device)
                loss_fct = torch.nn.CrossEntropyLoss(reduction="sum")
                loss = loss_fct(shift_logits, shift_labels)

            if not return_dict:
                return ((loss,) + output) if loss is not None else output

            output.loss = loss
            return output

        model.__original_forward__ = model.forward
        model.forward = reduce_sum_forward
        return model


# Standard
from typing import Any
import importlib


# taken from https://github.com/foundation-model-stack/fms-acceleration/blob/main/plugins/accelerated-peft/src/fms_acceleration_peft/autogptq_utils.py
def patch_target_module(
    to_patch: str,
    replace_with: Any,
):
    to_patch = to_patch.split(".")
    assert len(to_patch) > 1, "must have an object to patch"

    to_patch, obj_name_to_patch = to_patch[:-1], to_patch[-1]
    to_patch = ".".join(to_patch)
    source = importlib.import_module(to_patch)
    setattr(source, obj_name_to_patch, replace_with)


def prepare_peft_model(
    model,
    peft_config,
    gradient_checkpointing=True,
    gradient_checkpointing_kwargs={"use_reentrant": True},
    mixed_precision="bf16",
):
    # will guard this
    # Third Party
    from peft import (
        PeftConfig,
        PeftModel,
        get_peft_model,
        prepare_model_for_kbit_training,
    )
    from trl.trainer.utils import peft_module_casting_to_bf16

    if not isinstance(peft_config, PeftConfig):
        raise ValueError(
            "If you want to use the PeftModel, you need to pass a PeftConfig object, "
            f"and you passed a {type(peft_config)}."
        )

    if not isinstance(model, PeftModel):
        if getattr(model, "is_loaded_in_8bit", False) or getattr(
            model, "is_loaded_in_4bit", False
        ):
            preprare_model_kwargs = {
                "use_gradient_checkpointing": gradient_checkpointing
            }

            # if _support_gc_kwargs:
            preprare_model_kwargs["gradient_checkpointing_kwargs"] = (
                gradient_checkpointing_kwargs
            )

            model = prepare_model_for_kbit_training(model, **preprare_model_kwargs)

        elif gradient_checkpointing:
            # For backward compatibility with older versions of transformers
            if hasattr(model, "enable_input_require_grads"):
                model.enable_input_require_grads()
            else:

                def make_inputs_require_grad(module, input, output):
                    output.requires_grad_(True)

                model.get_input_embeddings().register_forward_hook(
                    make_inputs_require_grad
                )

        model = get_peft_model(model, peft_config)
        if mixed_precision == "bf16" and getattr(model, "is_loaded_in_4bit", False):
            peft_module_casting_to_bf16(model)

    return model


def setup_logger(level="DEBUG"):
    logging.basicConfig(
        level=level, format="%(message)s", datefmt="[%X]", handlers=[RichHandler()]
    )


def get_caller(num_frames=1):
    frame = inspect.currentframe().f_back
    for _ in range(num_frames - 1):
        frame = frame.f_back
    file_name = frame.f_code.co_filename
    line_number = frame.f_lineno
    return f"In {file_name}, line {line_number}"


def log_rank_0(msg, include_caller=False, rank=None, to_print=False):
    if rank is None:
        rank = get_rank() if is_initialized() else 0
    if rank <= 0:
        if include_caller:
            msg = f"{get_caller(num_frames=2)}: {msg}"
        if to_print:
            print(msg)
        else:
            logging.info(msg)
        # print(msg)


def save_hf_format(args, model, tokenizer, samples_seen):
    torch.cuda.empty_cache()
    log_rank_0(
        f"\033[93mSaving model in huggingface format at samples_seen: {samples_seen}\033[0m",
        to_print=True,
    )
    start = time.time()
    # used to save huggingface format, so we can use it for hf.from_pretrained
    CONFIG_NAME = "config.json"
    WEIGHTS_NAME = "pytorch_model.bin"

    with FSDP.state_dict_type(
        model,
        StateDictType.FULL_STATE_DICT,
        FullStateDictConfig(offload_to_cpu=True, rank0_only=True),
    ):
        model_state = model.state_dict()
    output_dir = Path(args.output_dir) / "hf_format" / f"samples_{samples_seen}"
    if torch.distributed.get_rank() == 0:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_model_file = output_dir / WEIGHTS_NAME
        output_config_file = output_dir / CONFIG_NAME
        torch.save(model_state, str(output_model_file))
        model.module.config.to_json_file(str(output_config_file))
        tokenizer.save_pretrained(str(output_dir))
    dist.barrier()
    log_rank_0(f"\033[93mModel saved in {output_dir}\033[0m", to_print=True)
    log_rank_0(f"saving took {time.time() - start} seconds")


def save_hf_format_ds(args, model, tokenizer, samples_seen, convert_granite=True):
    model_to_save = model.module
    log_rank_0(
        f"\033[93mSaving model in huggingface format at samples_seen: {samples_seen}\033[0m",
        to_print=True,
    )
    start = time.time()
    # used to save huggingface format, so we can use it for hf.from_pretrained
    CONFIG_NAME = "config.json"
    if args.is_granite:
        # save if in a temp directory first then convert it
        WEIGHTS_NAME = "model.safetensors"
        MODEL_TYPE = "llama"
    else:
        WEIGHTS_NAME = "pytorch_model.bin"
    output_dir = Path(args.output_dir) / "hf_format" / f"samples_{samples_seen}"
    if torch.distributed.get_rank() == 0:
        model_state = model_to_save.state_dict()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_model_file = output_dir / WEIGHTS_NAME
        output_config_file = output_dir / CONFIG_NAME

        if args.is_granite and convert_granite:
            # guarded import
            # Standard
            from tempfile import TemporaryDirectory
            import shutil

            # Third Party
            from dolomite_engine.hf_models import export_to_huggingface
            from safetensors.torch import save_file

            with TemporaryDirectory("w") as tmpdir:
                save_file(model_state, Path(tmpdir) / WEIGHTS_NAME)
                model_to_save.config.to_json_file(Path(tmpdir) / CONFIG_NAME)
                tokenizer.save_pretrained(tmpdir)
                # export doesnt like the directory to exist
                shutil.rmtree(output_dir)

                export_to_huggingface(
                    pretrained_model_name_or_path=tmpdir,
                    save_path=output_dir,
                    model_type=MODEL_TYPE,
                )
        else:
            torch.save(model_state, str(output_model_file))
            model_to_save.config.to_json_file(str(output_config_file))
            tokenizer.save_pretrained(str(output_dir))

    dist.barrier()
    log_rank_0(f"\033[93mModel saved in {output_dir}\033[0m", to_print=True)
    log_rank_0(f"saving took {time.time() - start} seconds")


# this is native deepspeed saving with optimizer, schediuler
def save_model_ds_native(
    args,
    model,
    tokenizer,
    samples_seen,
):
    # to get a statedict from a zero checkpoint, all you need to do is
    # - from deepspeed.utils.zero_to_fp32 import get_fp32_state_dict_from_zero_checkpoint
    # - sd = get_fp32_state_dict_from_zero_checkpoint('ckpt')
    # - sum([math.prod(x.shape) for x in sd.values()]) # check the size (should be correct)

    log_rank_0(
        f"\033[93mSaving model+optimizer+scheduler in format at samples_seen: {samples_seen}\033[0m",
        to_print=True,
    )
    start = time.time()
    # used to save huggingface format, so we can use it for hf.from_pretrained
    output_dir = Path(args.output_dir) / "ds_native"
    tag = f"samples_{samples_seen}"
    use_lora = args.lora_r > 0

    # NOTE: this is a distributed save
    # if its lora, we only save the adapters
    # - so we exclude frozen if use_lora==True
    model.save_checkpoint(
        output_dir,
        exclude_frozen_parameters=use_lora,
        tag=tag,  # this will create the subdirectory with the correct name
    )

    # for now we are not saving tokenizer, config, eg..
    # so it is not totally "HF compatible"

    log_rank_0(f"\033[93mModel saved in {output_dir}\033[0m", to_print=True)
    log_rank_0(f"saving took {time.time() - start} seconds")


def set_random_seed(seed):
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
