# Standard
from pathlib import Path
from typing import List
import logging
from functools import partial
import os
import random


# Third Party
from datasets import load_dataset
from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast
import numpy as np

# First Party
from instructlab.training.config import DataProcessArgs
from instructlab.training.tokenizer_utils import get_sp_token, setup_tokenizer
from instructlab.training.utils import log_rank_0, retrieve_chat_template, setup_logger, set_random_seed


def check_valid_sample(
    tokenizer: PreTrainedTokenizer | PreTrainedTokenizerFast,
    whole_sentence_tk: list[int],
    system_tk: int,
    assistant_tk: int,
    user_tk: int,
    eos_tk: int,
    contrastive_tk: int,
    max_len: int = 1024,
):
    if len(whole_sentence_tk) >= max_len or len(whole_sentence_tk) < 20:
        return False
    # last token should be eos_token
    if whole_sentence_tk[-1] != eos_tk:
        print(f"\033[91mthe last token is not eos\033[0m")
        log_rank_0(tokenizer.decode(whole_sentence_tk), to_print=True)
        return False

    special_tokens = [system_tk, assistant_tk, user_tk, contrastive_tk]
    if not any(token in whole_sentence_tk for token in special_tokens):
        return True

    whole_sentence_tk = np.array(whole_sentence_tk)
    user_token_index = (whole_sentence_tk == user_tk).nonzero()[0]
    assistant_token_index = (whole_sentence_tk == assistant_tk).nonzero()[0]
    eos_token_index = (whole_sentence_tk == eos_tk).nonzero()[0]
    contrastive_token_index = (whole_sentence_tk == contrastive_tk).nonzero()[0]

    # check that there are at least one user_token, assistant_token and eos_token
    if (
        len(user_token_index) == 0
        or len(assistant_token_index) == 0
        or len(eos_token_index) == 0
    ):
        print(f"\033[91mthere are no user_token, assistant_token or eos_token\033[0m")
        log_rank_0(tokenizer.decode(whole_sentence_tk), to_print=True)
        return False

    # if there is contrastive token, then sub-sample assistant_token_index by the number of contrastive tokens
    # and then do alternating pattern check
    if len(contrastive_token_index) > 0:
        # select every second assistant_token_index
        assistant_token_index = assistant_token_index[::(len(contrastive_token_index)+1)]

    # check that user_index_token is less than all other indices
    if len(user_token_index) != len(assistant_token_index):
        print(
            "\033[91mthe number of user_token and assistant_token is not the same\033[0m"
        )
        log_rank_0(tokenizer.decode(whole_sentence_tk), to_print=True)
        return False
    if (
        user_token_index[0] > assistant_token_index[0]
        or user_token_index[0] > eos_token_index[0]
    ):
        print("\033[91mthe first sp token is not user_token\033[0m")
        log_rank_0(tokenizer.decode(whole_sentence_tk), to_print=True)
        return False

    # check alternating pattern of user_token and assistant_token
    for user_token_i, assistant_token_i in zip(user_token_index, assistant_token_index):
        if user_token_i > assistant_token_i:
            print("\033[91mthe user_token is after the assistant_token\033[0m")
            log_rank_0(tokenizer.decode(whole_sentence_tk), to_print=True)
            return False

    return True


def unmask_only_assistant_responses(
    chosen_token, user_token, assist_token, system_tk="<|system|>"
):
    """
    Generate a labels tensor for language model training, where the model should predict
    the assistant's responses within a conversation. The labels for the assistant's responses
    are unmasked, while the rest of the tokens, including the user's responses and the initial
    prompt, are masked with a value of -100.

    Parameters:
    - chosen_token (dict): A dictionary containing 'input_ids', a 1D tensor of tokenized text.
    - user_token (int): The token ID representing the user's turn in the conversation.
    - assist_token (int): The token ID representing the assistant's turn in the conversation.
    - prompt_length (int): The length of the initial prompt in tokens, which should be masked.

    Returns:
    - torch.Tensor: A tensor of the same shape as `chosen_token['input_ids']` where each token
      corresponding to the assistant's response is unmasked (retains its original token ID),
      and all other tokens are masked with -100.

    The function assumes that each assistant's response is followed by a user's response. It masks
    the initial prompt and any tokens not part of the assistant's responses. The resulting labels
    tensor can be used in a language model that is trained to generate text by predicting the next
    token in a sequence.
    """

    assert chosen_token["input_ids"].dim() == 1
    sentence_legth = chosen_token["attention_mask"].sum().item()
    labels = chosen_token["input_ids"].clone()
    whole_sentence = chosen_token["input_ids"][:sentence_legth].clone()

    # pre-training mode
    if not (
        system_tk in whole_sentence
        or user_token in whole_sentence
        or assist_token in whole_sentence
    ):
        return labels

    labels[:sentence_legth] = -100
    assist_ids = (whole_sentence == assist_token).nonzero(as_tuple=True)[0]
    user_ids = (whole_sentence == user_token).nonzero(as_tuple=True)[0]

    # Find the first user_id that is greater than each assist_id
    valid_assist_mask = (assist_ids[:, None] < user_ids).float()
    first_user_ids_after_assist_ids = user_ids[valid_assist_mask.argmax(dim=1)]

    # Filter out assist_ids that do not have a corresponding user_id
    valid_assist_ids = assist_ids[first_user_ids_after_assist_ids != 0]
    valid_user_ids = first_user_ids_after_assist_ids[
        first_user_ids_after_assist_ids != 0
    ]

    # Assign labels for each valid assist_id-user_id pair (without including the assist_id nor the user_id)
    for assist_id, user_id in zip(valid_assist_ids, valid_user_ids):
        labels[assist_id + 1 : user_id] = whole_sentence[assist_id + 1 : user_id]

    # Assert that the conversation ends with an assistant token
    assert (
        assist_ids[-1] > user_ids[-1]
    ), "Conversation does not end with an assistant token"

    # Unmask the final assistant response
    labels[assist_ids[-1] + 1 : sentence_legth] = whole_sentence[
        assist_ids[-1] + 1 : sentence_legth
    ]

    return labels


def unmask_only_assistant_responses_from_list(
    sentence_tk: List[int], user_token: int, assist_token: int, contrastive_token: int,
) -> List[int]:
    sentence_tk = np.array(sentence_tk)
    assert sentence_tk.ndim == 1

    if user_token not in sentence_tk or assist_token not in sentence_tk:
        return sentence_tk.tolist()

    labels = np.full_like(sentence_tk, -100)

    user_ids = (sentence_tk == user_token).nonzero()[0]
    assist_ids = (sentence_tk == assist_token).nonzero()[0]
    contrastive_ids = (sentence_tk == contrastive_token).nonzero()[0]
    num_contrastive = len(contrastive_ids) + 1


    end_assist_ids = user_ids
    # more than 1 sample per user query, then the end of the assistant response is the next contrastive token, not user tokens
    if num_contrastive > 1:
        end_assist_ids = contrastive_ids

    valid_assist_mask = assist_ids[:, None] < end_assist_ids
    first_user_ids_after_assist_ids = end_assist_ids[valid_assist_mask.argmax(axis=1)]

    # Filter out assist_ids that do not have a corresponding user_id
    valid_assist_ids = assist_ids[first_user_ids_after_assist_ids != 0]
    valid_user_ids = first_user_ids_after_assist_ids[
        first_user_ids_after_assist_ids != 0
    ]

    # Assign labels for each valid assist_id-user_id pair (without including the assist_id nor the user_id)
    for assist_id, user_id in zip(valid_assist_ids, valid_user_ids):
        # include the contrastive token as separator
        if num_contrastive > 1:
            labels[assist_id + 1: user_id+1] = sentence_tk[assist_id + 1: user_id+1]
        else:
            labels[assist_id + 1 : user_id] = sentence_tk[assist_id + 1 : user_id]

    # Assert that the conversation ends with an assistant token
    assert (
        assist_ids[-1] > user_ids[-1]
    ), "Conversation does not end with an assistant token"

    # Unmask the final assistant response
    labels[assist_ids[-1] + 1 :] = sentence_tk[assist_ids[-1] + 1 :]

    return labels.tolist()


def remove_pretrain_system_messages(example: dict):
    messages = example["messages"]
    has_pretraining = any(m["role"] == "pretraining" for m in messages)
    if has_pretraining:
        messages = [m for m in messages if m["role"] != "system"]
        assert len(messages) == 1
    return {"messages": messages}


def process_messages_format(example: dict, model_path: str, num_negatives: int):
    messages = []
    if 'granite' in model_path:
        SYS_PROMPT = 'I am, Red Hat® Instruct Model based on Granite 7B, an AI language model developed by Red Hat and IBM Research, based on the Granite-7b-base language model. My primary function is to be a chat assistant.'
    elif 'mistral' in model_path:
        # SYS_PROMPT = 'You are an AI language model developed by IBM Research. You are a cautious assistant. You carefully follow instructions. You are helpful and harmless and you follow ethical guidelines and promote positive behavior.'
        SYS_PROMPT = None
    
    # system prompt
    if SYS_PROMPT is not None:
        messages.append({'content': SYS_PROMPT, 'role': 'system'})
    # user prompt (context doc) and input
    messages.append({'content': example['question'], 'role': 'user'})
    # pick num_negatives number of negative samples randomly from the list of rejected answers
    rejected = random.sample(example['current_response'], num_negatives)
    messages.append({'content': example['answer'].strip(), 'rejected': rejected, 'role': 'assistant_w_rejected'})
    
    return {"messages": messages}


def main(args: DataProcessArgs):
    CHAT_TEMPLATE, SPECIAL_TOKENS = retrieve_chat_template(args.chat_tmpl_path)
    tokenizer = setup_tokenizer(args.model_path, SPECIAL_TOKENS, CHAT_TEMPLATE)

    eos_tk = get_sp_token(tokenizer, SPECIAL_TOKENS.eos)
    pad_tk = get_sp_token(tokenizer, SPECIAL_TOKENS.pad)
    if SPECIAL_TOKENS.system:
        system_tk = get_sp_token(tokenizer, SPECIAL_TOKENS.system)
    else:
        system_tk = None
    user_tk = get_sp_token(tokenizer, SPECIAL_TOKENS.user)
    assistant_tk = get_sp_token(tokenizer, SPECIAL_TOKENS.assistant)
    contrastive_tk = get_sp_token(tokenizer, SPECIAL_TOKENS.contrastive_sep)
    log_rank_0(
        f"eos: {eos_tk}, pad: {pad_tk}, system: {system_tk}, user: {user_tk}, assistant: {assistant_tk}, contrastive: {contrastive_tk}"
    )

    if 'json' in args.data_path:
        data = load_dataset("json", data_files=args.data_path, split="train")
    else:
        data = load_dataset(args.data_path, split='train')

    # if data is not preprocessed to be in the messages format, process it
    if "messages" not in data.column_names:
        logging.info('data is not in "messages" format, packing it into "messages" format...')
        # data = data.filter(lambda x: x["rejected"] != "", num_proc=72)
        data = data.map(partial(process_messages_format, model_path=args.model_path, num_negatives=args.num_negatives), num_proc=72)

    print("\033[92mremoving pretraining samples system msg\033[0m")
    data = data.map(remove_pretrain_system_messages, num_proc=72)

    logging.info(f"tokenizing the dataset with {args.model_path} tokenizer...")
    data_with_input_ids = data.map(
        lambda x: {
            "input_ids": tokenizer.apply_chat_template(x["messages"], tokenize=True)
        },
        num_proc=72,
    )

    print("\033[38;2;255;165;0mten largest length percentiles:")
    lens = np.array(
        data_with_input_ids.map(lambda x: {"len": len(x["input_ids"])}, num_proc=72)[
            "len"
        ]
    )
    biggest_10_percent = np.quantile(lens, (90 + np.arange(11)) / 100.0)
    for i, q in enumerate(biggest_10_percent):
        print(f"quantile {90+i*1}th: {q}")
    print("\033[0m")

    num_dropped_samples = np.sum(lens > args.max_seq_len)
    print(
        f"\033[36mat {args.max_seq_len} max sequence length, the number of samples to be dropped is {num_dropped_samples}\033[0m"
    )
    print(f"\033[36m({((num_dropped_samples / len(lens)) * 100):.2f}% of total)\033[0m")

    lowest_10_percent = np.quantile(lens, (0 + np.arange(11)) / 100.0)
    for i, q in enumerate(lowest_10_percent):
        print(f"quantile {i}th: {q}")
    num_dropped_samples = np.sum(lens < 20)
    print(
        f"\033[36mat 20 min sequence length, the number of samples to be dropped is {num_dropped_samples}\033[0m"
    )

    logging.info("checking the validity of the samples...")
    data_with_input_ids = data_with_input_ids.filter(
        lambda x: check_valid_sample(
            tokenizer,
            x["input_ids"],
            system_tk,
            assistant_tk,
            user_tk,
            eos_tk,
            contrastive_tk,
            args.max_seq_len,
        ),
        num_proc=72,
    )
    log_rank_0(
        f"\033[33mnumber of dropped samples: {len(data) - len(data_with_input_ids)} -- out of {len(data)}\033[0m"
    )

    logging.info("unmasking the assistant responses...")
    data_with_labels = data_with_input_ids.map(
        lambda x: {
            "labels": unmask_only_assistant_responses_from_list(
                x["input_ids"], user_tk, assistant_tk, contrastive_tk
            )
        },
        num_proc=1, # TODO: revert to 72
    )
    # extract only labels and messages formatted into a new dataset
    data_with_labels = data_with_labels.select_columns(["labels", "input_ids"])
    # use path to get the stem of the file
    data_with_labels.to_json(Path(args.data_output_path) / f"data.jsonl")

# input_ids: [PROMT_TOKENS... [ASSISTANT] POSITIVE_SAMPLE [EOS] [CONTRASTIVE TOKEN] [ASSISTANT] NEGATIVE SAMPLE [EOS] ]
# 



# input_ids: [PROMPT_TOKENS... [ASSISTANT] POSITIVE_SAMPLE [EOS]]
# input_ids: [PROMPT_TOKENS... [ASSISTANT] NEGATIVE_SAMPLE [EOS]]
# additional pad tokens b/c i'm not doing it at collator level

if __name__ == "__main__":
    # Standard
    import argparse

    parser = argparse.ArgumentParser(
        description="Preprocess a dataset for training a language model"
    )
    parser.add_argument(
        "--logging_level", type=str, default="INFO", help="Logging level"
    )
    parser.add_argument(
        "--data_path", type=str, required=True, help="Path to the dataset file"
    )
    parser.add_argument(
        "--data_output_path",
        type=str,
        required=True,
        help="Path to the output dataset file",
    )
    parser.add_argument(
        "--max_seq_len", type=int, required=True, help="Maximum sequence length"
    )
    parser.add_argument(
        "--model_name_or_path", type=str, required=True, help="Model name or path"
    )
    parser.add_argument(
        "--chat_tmpl_path",
        type=str,
        default=os.path.join(
            os.path.dirname(__file__), "chat_templates/ibm_generic_tmpl.py"
        ),
        help="Path to desired chat template and special tokens, defaults to IBM generic.",
    )
    parser.add_argument(
        "--num_negatives",
        type=int,
        default=1,
        help="Number of negative samples to use per positive sample",
    )
    
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
    )

    args = parser.parse_args()

    set_random_seed(args.seed)
    setup_logger(args.logging_level)
    data_process_args = DataProcessArgs(
        data_output_path=args.data_output_path,
        data_path=args.data_path,
        max_seq_len=args.max_seq_len,
        model_path=args.model_name_or_path,
        chat_tmpl_path=args.chat_tmpl_path,
        num_negatives=args.num_negatives,
    )
    main(data_process_args)

"""
python data_process.py --logging_level INFO --data_path "/new_data/refactored/chat-multiturn/oasst2_arena.jsonl" --data_output_path "./" --max_seq_len 4600 --model_name_or_path "mistralai/Mistral-7B-v0.1"
"""
