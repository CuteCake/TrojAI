import json
import os
import torch
from os.path import join as join
import pandas as pd

def modify_args_for_training(args):
    metadata = pd.read_csv(join(args.training_data_path, 'METADATA.csv'))

    id_str = str(100000000 + args.model_num)[1:]
    model_id = 'id-'+id_str

    data = metadata[metadata.model_name==model_id]
    
    # get the tokenizer name
    embedding_level = data.embedding.item()
    tokenizer_name = data.embedding_flavor.item().replace("/", "-")
    full_tokenizer_name = embedding_level+'-'+tokenizer_name+'.pt'

    args.model_filepath = join(args.training_data_path, 'models', model_id, 'model.pt')
    args.tokenizer_filepath = join(args.training_data_path, 'tokenizers', full_tokenizer_name)
    args.examples_dirpath = join(args.training_data_path, 'models', model_id, 'clean_example_data')
    return args


def eval_batch_helper(classification_model, all_vars, source_class_token_locations):
    loss, _ = \
        classification_model(all_vars['input_ids'], all_vars['attention_mask'], 
                            all_vars['labels'], is_triggered=True,
                            class_token_indices=source_class_token_locations)
    return loss


def evaluate_batch(classification_model, all_vars, source_class_token_locations,
                                                                 use_grad=False):
    if use_grad:
        loss = eval_batch_helper(classification_model, all_vars, 
                                 source_class_token_locations)
    else:
        with torch.no_grad():
            loss = eval_batch_helper(classification_model, all_vars, 
                                    source_class_token_locations)
    return loss


def get_max_input_length(config, tokenizer):
    if config['embedding'] == 'MobileBERT':
        max_input_length = \
            tokenizer.max_model_input_sizes[tokenizer.name_or_path.split('/')[1]]
    else:
        max_input_length = tokenizer.max_model_input_sizes[tokenizer.name_or_path]
    return max_input_length


def get_words_and_labels(examples_dirpath):
    fns = [os.path.join(examples_dirpath, fn) \
           for fn in os.listdir(examples_dirpath) \
           if fn.endswith('.txt')]
    fns.sort()
    original_words = []
    original_labels = []

    for fn in fns:
        if fn.endswith('_tokenized.txt'):
            continue
        # load the example
        with open(fn, 'r') as fh:
            lines = fh.readlines()
            temp_words = []
            temp_labels = []

            for line in lines:
                split_line = line.split('\t')
                word = split_line[0].strip()
                label = split_line[2].strip()
                
                temp_words.append(word)
                temp_labels.append(int(label))
        original_words.append(temp_words)
        original_labels.append(temp_labels)

    return original_words, original_labels


def load_tokenizer(tokenizer_filepath):
    tokenizer = torch.load(tokenizer_filepath)
    if not hasattr(tokenizer, 'pad_token') or tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    return tokenizer


def load_config(model_filepath):
    model_dirpath, _ = os.path.split(model_filepath)
    with open(os.path.join(model_dirpath, 'config.json')) as json_file:
        config = json.load(json_file)
    print('Source dataset name = "{}"'.format(config['source_dataset']))
    if 'data_filepath' in config.keys():
        print('Source dataset filepath = "{}"'.format(config['data_filepath']))
    return config

def tokenize_and_align_labels(tokenizer, original_words, 
                              original_labels, max_input_length):

    tokenized_inputs = tokenizer(original_words, padding=True, truncation=True, 
                                 is_split_into_words=True, max_length=max_input_length)
    
    word_ids = [tokenized_inputs.word_ids(i) for i in range(len(original_labels))]
    labels, label_mask = [], []
    previous_word_idx = None

    for i, sentence in enumerate(word_ids):
        temp_labels, temp_mask = [], []
        for word_idx in sentence:
            if word_idx is not None:
                cur_label = original_labels[i][word_idx]
            if word_idx is None:
                temp_labels.append(-100)
                temp_mask.append(0)
            elif word_idx != previous_word_idx:
                temp_labels.append(cur_label)
                temp_mask.append(1)
            else:
                temp_labels.append(-100)
                temp_mask.append(0)
            previous_word_idx = word_idx
        labels.append(temp_labels)
        label_mask.append(temp_mask)
        
    return tokenized_inputs['input_ids'], tokenized_inputs['attention_mask'], \
           labels, label_mask