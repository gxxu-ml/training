import argparse
from pathlib import Path
from datetime import timedelta
import math
import os
import re
import time
import yaml
import torch
from tqdm import tqdm
from transformers import AutoModelForCausalLM, get_scheduler, MistralForCausalLM
from torch.distributed import (
    ReduceOp,
    all_reduce,
)

import deepspeed
from deepspeed.ops.adam import FusedAdam
from instructlab.training.multipack_sampler import find_packing_max_batch_len_and_grad_accum
from instructlab.training.token_dataset import setup_dataloader, setup_dataset
from instructlab.training.tokenizer_utils import setup_tokenizer
from instructlab.training.utils import (
    save_hf_format_ds,
    save_model_ds_native,
    set_random_seed,
    setup_logger,
    convert_loss_to_reduce_sum,
)


class DataWrapper:

    def __init__(self, _args: argparse.ArgumentParser):

        self._args = _args
        self.dataset = setup_dataset(data_path=_args.data_path)
        self.tokenizer = setup_tokenizer(_args.model_name_or_path)
        self.packing_max_batch_len, self.grad_accum = (
            find_packing_max_batch_len_and_grad_accum(
                num_gpus=torch.distributed.get_world_size(),
                avg_sample_len=self.dataset.get_lengths().mean(),
                effective_batch_size=self._args.effective_batch_size,
                max_batch_len_per_gpu=self._args.max_batch_len,
            )
        )
        self.samples_per_gpu = (
            self._args.effective_batch_size
            // self.grad_accum
            // torch.distributed.get_world_size()
        )
        self.train_loader = setup_dataloader(
            self.dataset,
            self.tokenizer.pad_token_id,
            num_workers=8,
            is_granite=self._args.is_granite,
            max_batch_len=self._args.max_batch_len,
            packing_max_batch_len=self.packing_max_batch_len,
            seed=self._args.seed,
        )


class DSModelWrapper:

    def __init__(self, _args: argparse.ArgumentParser, dataw: DataWrapper):
        self._args = _args
        self.dataw = dataw
        self._ds_config = self._get_ds_config(
            world_size=self._args.world_size,
            samples_per_gpu=dataw.samples_per_gpu,
            grad_accum=dataw.grad_accum,
        )
        self.model = self._setup_model()
        self.model = self._maybe_resume_training()

    def _get_ds_config(self, world_size, samples_per_gpu, grad_accum):
        ds_config = {
            "train_batch_size": samples_per_gpu * world_size * grad_accum,
            "gradient_accumulation_steps": grad_accum,
            "train_micro_batch_size_per_gpu": samples_per_gpu,
            "steps_per_print": 1,
            "zero_optimization": {
                "stage": 2,
                "offload_param": {"device": "none"},
                "offload_optimizer": {"device": "none"},
            },
            "bf16": {"enabled": True},
            "gradient_clipping": 1.0,
            "prescale_gradients": False,
            "wall_clock_breakdown": False,
        }
        return ds_config

    def _setup_model(self):
        bnb_config = None
        if self._args.lora_r > 0 and self._args.lora_quant_bits == 4:
            from transformers import BitsAndBytesConfig

            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.float16,  # if not set will throw a warning about slow speeds when training
            )

        if self._args.is_granite:
            from dolomite_engine.hf_models.models import GPTDolomiteForCausalLM

            model = GPTDolomiteForCausalLM.from_pretrained(
                self._args.model_name_or_path,
                attn_implementation="flash_attention_2",
                torch_dtype=torch.bfloat16,
                use_padding_free_transformer=True,
                quantization_config=bnb_config,
            )
        else:
            model = AutoModelForCausalLM.from_pretrained(
                self._args.model_name_or_path,
                attn_implementation="flash_attention_2",
                torch_dtype=torch.bfloat16,
                quantization_config=bnb_config,
            )

        if len(self.dataw.tokenizer) > model.config.vocab_size:
            print(
                f"WARNING: tokenizer has {len(self.dataw.tokenizer)} tokens but model has {model.config.vocab_size} vocab size"
            )
            model.resize_token_embeddings(
                int(8 * math.ceil(len(self.dataw.tokenizer) / 8.0))
            )  # make the vocab size multiple of 8 for sharding the embedding layer.

        assert model.__class__.__name__ in [
            "MistralForCausalLM",
            "GPTDolomiteForCausalLM",
            "LlamaForCausalLM",
            "Starcoder2ForCausalLM",
            "GemmaForCausalLM",
        ], f"Model class name: {model.__class__.__name__} is not supported."

        model = convert_loss_to_reduce_sum(model, is_granite=self._args.is_granite)

        # handling of gradient checkpointing
        # it is handled differently for lora and full
        # - with the exception of granite, which handles it
        #   in the later stanza
        if self._args.lora_r > 0:
            # if lora
            from peft import LoraConfig
            from utils import prepare_peft_model, patch_target_module

            if self._args.lora_target_modules is None:
                self._args.__dict__["lora_target_modules"] = [
                    "q_proj",
                    "k_proj",
                    "v_proj",
                    "o_proj",
                ]

            peft_config = LoraConfig(
                lora_alpha=self._args.lora_alpha,
                lora_dropout=self._args.lora_dropout,
                r=self._args.lora_r,
                bias="none",
                task_type="CAUSAL_LM",
                target_modules=args.lora_target_modules,
            )
            model = prepare_peft_model(
                model, peft_config, gradient_checkpointing=not self._args.is_granite
            )

            # patch DS to work with quantized models
            from deepspeed import DeepSpeedEngine
            from functools import partial

            if self._args.lora_quant_bits is not None:
                patch_target_module(
                    "deepspeed.DeepSpeedEngine",
                    partial(DeepSpeedEngine, dont_change_device=True),
                )
        elif not self._args.is_granite:
            model.gradient_checkpointing_enable()

        # granite gradient checkpointing is handled uniformly
        # for both lora and full here
        if self._args.is_granite:
            from dolomite_engine.gradient_checkpointing import (
                apply_gradient_checkpointing,
            )
            from dolomite_engine.enums import GradientCheckpointingMethod

            block_name = model._no_split_modules[0]
            apply_gradient_checkpointing(
                model,
                GradientCheckpointingMethod.block,
                block_name=block_name,
                use_reentrant=True,  # this should be the HF default mode
            )

            if self._args.lora_r > 0:

                def make_inputs_require_grad(module, input, output):
                    output.requires_grad_(True)

                model.get_input_embeddings().register_forward_hook(
                    make_inputs_require_grad
                )

        optimizer = FusedAdam(
            model.parameters(), lr=self._args.learning_rate, betas=(0.9, 0.95)
        )
        lr_scheduler = get_scheduler(
            name="cosine",
            optimizer=optimizer,
            num_warmup_steps=self._args.num_warmup_steps,
            num_training_steps=self._args.num_epochs * len(self.dataw.train_loader),
        )

        model, _, _, lr_scheduler = deepspeed.initialize(
            model=model,
            optimizer=optimizer,
            config=self._ds_config,
            lr_scheduler=lr_scheduler,
            dist_init_required=True,
        )
        # model = torch.compile(model)
        return model

    def _maybe_resume_training(self):

        model = self.model

        local_rank = int(os.environ["LOCAL_RANK"])

        # DS's loading function will not raise if fails to reload a checkpoint
        # - if lora is used, then the checkpoints will only be for the adapters
        #   so we need to disable load_module_strict
        # - load checkpoint will find the latest checkpoint
        # - it will also load the optimizer and scheduler states by default
        load_module_strict = (
            self._args.lora_r == 0
        )  # can only be true if lora is not used
        output_dir = Path(self._args.output_dir) / "ds_native"
        model.load_checkpoint(output_dir, load_module_strict=load_module_strict)

        output_dir = Path(self._args.output_dir) / "ds_native"
        # need to figure out the resumed start step
        latest_file = output_dir / "latest"
        try:
            with open(latest_file, encoding="b") as f:
                # there is some assumption here that the ds_native
                # checkpoints are tagged as <something>_(samples_seen)
                samples_seen = f.read()
                (samples_seen,) = re.match("\w+_(\d+)", samples_seen).groups()
                samples_seen = int(samples_seen)

                last_step = samples_seen // args.effective_batch_size
                self._args.__dict__["last_step"] = last_step

                if local_rank == 0:
                    print(f"\033[93mStarting from: {last_step}\033[0m")
        except FileNotFoundError:
            pass

        # we will update the start step here
        return model


class DeepSpeedTrainer:

    def __init__(
        self, _args: argparse.ArgumentParser, modelw: DSModelWrapper, dataw: DataWrapper
    ):
        self._args = _args
        self.dataw = dataw
        self.modelw = modelw
        self.model = self.modelw.model
        self.local_rank = int(os.environ["LOCAL_RANK"])
        self.world_size = int(os.environ["WORLD_SIZE"])
        self.global_step = 1
        self.batch_size = self._args.effective_batch_size // self.dataw.grad_accum
        self.save_samples = (
            self._args.save_samples // self.batch_size
        ) * self.batch_size
        if self._args.save_samples_ds is not None:
            self.save_samples_ds = (
                self._args.save_samples_ds // self.batch_size
            ) * self.batch_size
            if self._args.local_rank == 0:
                print(
                    f"\033[93mNumber of samples per DS save: {self.save_samples_ds}\033[0m"
                )
        if self.local_rank == 0:
            print(f"\033[93mNumber of samples per save: {self.save_samples}\033[0m")

    def _run_epoch(self, epoch: int):

        self.dataw.train_loader.batch_sampler.set_epoch(epoch)

        if self.local_rank == 0:
            inner_pb = tqdm(range(len(self.dataw.train_loader)), desc=f"Epoch {epoch}")

        aggregated_values = torch.zeros(3, dtype=torch.float32).to(self.local_rank)
        for batch in self.dataw.train_loader:
            if self.global_step <= self._args.last_step:
                # in the case of resuming, last_step > 0
                self.global_step += 1
                if self.local_rank == 0:
                    inner_pb.update(1)
                continue

            start = time.time()
            aggregated_values[0] = batch.pop("num_loss_counted_tokens")
            aggregated_values[1] = len(batch["input_ids"])
            if not self._args.is_granite:
                for k in batch:
                    batch[k] = batch[k].to(self.local_rank)

            output = self.model(
                **batch,
                use_cache=False,
            )
            loss = output.loss

            aggregated_values[2] = loss.item()

            all_reduce(aggregated_values, op=ReduceOp.SUM)

            num_loss_counted_tokens = aggregated_values[0]
            loss = (
                loss / num_loss_counted_tokens * self.world_size
            )  # dividing by the total number of non-padding tokens and multiplying by the number of GPUs so when deepspeed averages by world_size, it will be the correct loss.

            print(
                f"\033[93mPer-token loss scaled by world size: {(loss/num_loss_counted_tokens) * self.world_size}\033[0m"
            )
            print(
                f"Epoch: {epoch}, Step: {self.global_step}, Rank: {torch.distributed.get_rank()}, loss = {loss}"
            )

            self.model.backward(loss)
            self.model.step()
            self._try_save_checkpoint(
                start, loss, num_loss_counted_tokens, aggregated_values
            )

            self.global_step += 1
            if self.local_rank == 0:
                inner_pb.update(1)
            torch.cuda.empty_cache()

    def _try_save_checkpoint(
        self, start, loss, num_loss_counted_tokens, aggregated_values
    ):
        if self.local_rank == 0:
            elapsed_time = time.time() - start
            overall_throughput = (
                self._args.samples_per_gpu * self.world_size / elapsed_time
            )
            current_lr = self.model.lr_scheduler.get_last_lr()[0]
            cuda_mem_allocated = torch.cuda.memory_allocated() / (1024**3)
            cuda_malloc_retries = torch.cuda.memory_stats()["num_alloc_retries"]

            print(
                f"throughput: {overall_throughput} "
                f"samples/s, lr: {current_lr}, "
                f"loss: {loss.item()} "
                f"cuda_mem_allocated: {cuda_mem_allocated} GB "
                f"cuda_malloc_retries: {cuda_malloc_retries} "
                f"num_loss_counted_tokens: {num_loss_counted_tokens} "
                f"batch_size: {aggregated_values[1]} "
                f"total loss: {aggregated_values[2]/num_loss_counted_tokens}"
            )

        if self.global_step * self.batch_size % args.save_samples == 0:
            save_hf_format_ds(
                args,
                self.model,
                self.dataw.tokenizer,
                self.global_step * self._args.samples_per_gpu * self.world_size,
            )

        if (
            self.save_samples_ds is not None
            and self.global_step * self.batch_size % self.save_samples_ds == 0
        ):
            save_model_ds_native(
                self._args,
                self.model,
                self.dataw.tokenizer,
                self.global_step * self._args.samples_per_gpu * self.world_size,
            )

    def train(self):
        self.model.train()

        for epoch in range(self._args.num_epochs):
            torch.distributed.barrier()
            self._run_epoch(epoch)


def distributed_init(_args: argparse.ArgumentParser):
    #### distributed init #####
    torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))
    _args.local_rank = int(os.environ["LOCAL_RANK"])
    deepspeed.init_distributed(timeout=timedelta(minutes=360))
    _args.global_rank = torch.distributed.get_rank()
    _args.world_size= int(os.environ["WORLD_SIZE"])
    tensor = torch.ByteTensor([False]).cuda()
    torch.distributed.all_reduce(tensor)
    torch.distributed.barrier()


def main(_args: argparse.ArgumentParser):

    if os.environ["LOCAL_RANK"] == "0":
        print(f"\033[38;5;120m{yaml.dump(vars(_args), sort_keys=False)}\033[0m")

    setup_logger(_args.log_level)

    distributed_init(_args)

    dataw = DataWrapper(_args)
    if _args.local_rank == 0:
        print(
            f"\033[96mnum_gpus: {torch.distributed.get_world_size()}\n"
            f"avg_sample_len: {dataw.dataset.get_lengths().mean()}\n"
            f"effective_batch_size: {_args.effective_batch_size}\n"
            f"max_batch_len_per_gpu: {_args.max_batch_len}\n"
            f"packing_max_batch_len: {dataw.packing_max_batch_len}\n"
            f"grad_accum: {dataw.grad_accum}\n"
            f"num batches: {len(dataw.train_loader)}\n"
            f"avg_samples_per_batch: {len(dataw.dataset)/len(dataw.train_loader)}\n"
            # f"samples_per_gpu: {_args.samples_per_gpu}\033[0m"
        )

    modelw = DSModelWrapper(_args=_args, dataw=dataw)

    DeepSpeedTrainer(_args=_args, modelw=modelw, dataw=dataw).train()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_name_or_path", type=str)
    parser.add_argument("--data_path", type=str)
    parser.add_argument("--output_dir", type=str)
    parser.add_argument("--num_epochs", type=int, default=1)
    parser.add_argument(
        "--last_step",
        type=int,
        default=0,
        help="understand this as the last completed step. "
        "The default is 0, since global_step starts from 1 by default.",
    )
    # parser.add_argument("--samples_per_gpu", type=int, default=8)
    parser.add_argument("--effective_batch_size", type=int, default=3840)
    parser.add_argument("--learning_rate", type=float, default=1e-4)
    parser.add_argument("--num_warmup_steps", type=int, default=1000)
    # parser.add_argument("--gradient_accumulation_steps", type=int, default=1)
    parser.add_argument("--save_samples", type=int)
    parser.add_argument(
        "--save_samples_ds",
        type=int,
        help="for saving in ds native format",
        default=None,
    )
    parser.add_argument("--log_level", type=str, default="INFO")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--mock_data", action="store_true")
    parser.add_argument("--mock_len", type=int, default=2600)
    parser.add_argument(
        "--sharding_strategy",
        type=str,
        # choices=[e.name for e in ShardingStrategy],
        default="FULL_SHARD",
        help="Sharding strategy to be used for distributed training.",
    )
    parser.add_argument("--is_granite", action="store_true")
    parser.add_argument("--lora_r", type=int, default=0)  # set to > 0 to activate lora
    parser.add_argument("--lora_alpha", type=int, default=32)
    parser.add_argument("--lora_dropout", type=float, default=0.1)
    parser.add_argument("--lora_quant_bits", type=int, default=None)
    parser.add_argument("--lora_target_modules", nargs="+", default=None)
    parser.add_argument("--max_batch_len", type=int, default=60000)
    args = parser.parse_args()
    set_random_seed(args.seed)
    main(args)
