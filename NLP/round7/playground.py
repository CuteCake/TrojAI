# Code inspired by: Eric Wallace Universal Triggers Repo
# https://github.com/Eric-Wallace/universal-triggers/blob/ed657674862c965b31e0728d71765d0b6fe18f22/gpt2/create_adv_token.py#L28

# TODO:
# Test code

import argparse
import torch
import torch.nn.functional as F
from copy import deepcopy
import torch.optim as optim
from operator import itemgetter
import heapq
import tools


# CONSTANTS
DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
EXTRACTED_GRADS = []


def to_tensor_and_device(var):
    var = torch.as_tensor(var)
    var = var.to(DEVICE)
    return var


def extract_grad_hook(module, grad_in, grad_out):
    EXTRACTED_GRADS.append(grad_out[0])    


def get_embedding_weight(classification_model):
    word_embedding = find_word_embedding_module(classification_model)
    return deepcopy(word_embedding.weight)


def find_word_embedding_module(classification_model):
    word_embedding_tuple = [(name, module) 
        for name, module in classification_model.named_modules() 
        if 'embeddings.word_embeddings' in name]
    assert len(word_embedding_tuple) == 1
    return word_embedding_tuple[0][1]


def add_hooks(classification_model):
    module = find_word_embedding_module(classification_model)
    module.weight.requires_grad = True
    module.register_backward_hook(extract_grad_hook)


def get_source_class_token_locations(source_class, labels):   
    source_class_token_locations = torch.eq(labels, source_class)
    source_class_token_locations = torch.nonzero(source_class_token_locations)
    return source_class_token_locations


def insert_trigger(all_vars, trigger_mask, trigger_token_ids):
    repeated_trigger = \
        trigger_token_ids.repeat(1, all_vars['input_ids'].shape[0]).long().view(-1)
    all_vars['input_ids'][trigger_mask] = repeated_trigger.to(DEVICE)
    return all_vars


def expand_and_insert_tokens(trigger_token_ids, masked_vars, 
                            source_class_token_locations):
    num_tokens = len(trigger_token_ids)
    # get prior and after matrix
    masked_priors_matrix = torch.zeros_like(masked_vars['input_ids']).bool()
    for i, source_class_token_row_col in enumerate(source_class_token_locations):
        masked_priors_matrix[i, :source_class_token_row_col[1]] = 1
    masked_after_matrix = ~masked_priors_matrix
    
    # expand variables
    for key, old_var in masked_vars.items():
        before_tk = old_var * masked_priors_matrix
        tk_and_after = old_var * masked_after_matrix

        before_tk = F.pad(before_tk, (0, num_tokens))
        tk_and_after = F.pad(tk_and_after, (num_tokens, 0))

        new_var = \
            torch.zeros((len(old_var), old_var.shape[1]+num_tokens), device=DEVICE).long()
        new_var += (before_tk + tk_and_after)
        masked_vars[key] = new_var

    # get the trigger mask
    expanded_priors_matrix = F.pad(masked_priors_matrix, (0, num_tokens))
    expanded_masked_after_matrix = F.pad(masked_after_matrix, (num_tokens, 0))
    trigger_mask = ~(expanded_priors_matrix + expanded_masked_after_matrix)

    # use the trigger mask to updata token_ids, attention_mask and labels
    masked_vars = insert_trigger(masked_vars, trigger_mask, trigger_token_ids)
    masked_vars['attention_mask'][trigger_mask] = 1           # set attention to 1
    masked_vars['labels'][trigger_mask] = -100                # set label to -100
    masked_sorce_class_token_locations = \
        shift_source_class_token_locations(source_class_token_locations, num_tokens)

    return masked_vars, trigger_mask, masked_sorce_class_token_locations


def filter_vars_to_sentences_with_source_class(all_vars, source_class_token_locations):
    source_class_sentence_ids = source_class_token_locations[:, 0]
    masked_vars = deepcopy(all_vars)
    masked_vars = {k:v[source_class_sentence_ids] for k, v in masked_vars.items()}
    return masked_vars


def make_initial_trigger_tokens(tokenizer, num_tokens=10, initial_trigger_word='the'):
    tokenized_initial_trigger_word = \
        tokenizer.encode(initial_trigger_word, add_special_tokens=False)
    trigger_token_ids = \
        torch.tensor(tokenized_initial_trigger_word * num_tokens).cpu()
    return trigger_token_ids


def shift_source_class_token_locations(source_class_token_locations, num_tokens):
    class_token_indices = deepcopy(source_class_token_locations)
    class_token_indices[:, 1] += num_tokens
    class_token_indices[:, 0] = \
        torch.arange(class_token_indices.shape[0], device=DEVICE).long()
    return class_token_indices


def clear_model_grads(classification_model):
    EXTRACTED_GRADS = []
    optimizer = optim.Adam(classification_model.parameters())
    optimizer.zero_grad()


def get_loss_per_candidate(classification_model, all_vars, source_class_token_locations, 
                           trigger_mask, trigger_token_ids, best_k_ids, trigger_token_pos):
    '''
    all_vars: dictionary with input_ids, attention_mask, labels, labels_mask 
              already includes old triggers from previous iteration
    returns the loss per candidate trigger (all tokens)
    '''
    # get the candidate trigger token location
    cand_trigger_token_location = deepcopy(source_class_token_locations)
    num_trigger_tokens = best_k_ids.shape[0]
    offset = trigger_token_pos - num_trigger_tokens
    cand_trigger_token_location[:, 1] += offset
    cand_trigger_token_mask = cand_trigger_token_location.split(1, dim=1)

    # save current loss with old triggers
    loss_per_candidate = []
    curr_loss = tools.evaluate_batch(classification_model, all_vars, 
                               source_class_token_locations, use_grad=False)
    curr_loss = curr_loss.cpu().numpy()
    loss_per_candidate.append((deepcopy(trigger_token_ids), curr_loss))
    
    # evaluate loss with each of the candidate triggers
    for cand_token_id in best_k_ids[trigger_token_pos]:
        trigger_token_ids_one_replaced = deepcopy(trigger_token_ids) # copy trigger
        trigger_token_ids_one_replaced[trigger_token_pos] = cand_token_id # replace one token
        temp_all_vars = deepcopy(all_vars)
        temp_all_vars = insert_trigger(temp_all_vars, trigger_mask, trigger_token_ids_one_replaced)
        loss = tools.evaluate_batch(classification_model, temp_all_vars, 
                              source_class_token_locations, use_grad=False).cpu().numpy()
        loss_per_candidate.append((deepcopy(trigger_token_ids_one_replaced), loss))
    return loss_per_candidate

def best_k_candidates_for_each_trigger_token(loss, trigger_mask, num_tokens, 
                                             embedding_matrix, num_candidates):
    loss.backward()
    trigger_grad_shape = [trigger_mask.shape[0], num_tokens, -1]
    trigger_grads = EXTRACTED_GRADS[0][trigger_mask].reshape(trigger_grad_shape)
    mean_grads = torch.mean(trigger_grads,dim=0).unsqueeze(0) 
    gradient_dot_embedding_matrix = torch.einsum("bij,kj->bik",
                                                 (mean_grads, embedding_matrix))[0]
    _, best_k_ids = torch.topk(gradient_dot_embedding_matrix, num_candidates, dim=1)

    return best_k_ids

def get_best_candidate(classification_model, all_vars, source_class_token_locations,
                       trigger_mask, trigger_token_ids, best_k_ids, beam_size=1):
    loss_per_candidate = \
        get_loss_per_candidate(classification_model, all_vars, source_class_token_locations, 
                               trigger_mask, trigger_token_ids, best_k_ids, 0) 

    top_candidates = heapq.nlargest(beam_size, loss_per_candidate, key=itemgetter(1))                                         
    for idx in range(1, len(trigger_token_ids)):
        loss_per_candidate = []
        for cand, _ in top_candidates:
            loss_per_candidate = \
                get_loss_per_candidate(classification_model, all_vars, 
                                       source_class_token_locations,
                                       trigger_mask, cand, best_k_ids, idx)
        top_candidates.extend(loss_per_candidate)                                 
    return max(top_candidates, key=itemgetter(1))[0]

def decode_tensor_of_token_ids(tokenizer, word_id_tensor):
    word_list = []
    for word_id in word_id_tensor:
        word_list.append(tokenizer.decode(word_id))
    return ' '.join(word_list)


def trojan_detector(model_filepath, tokenizer_filepath, 
                    result_filepath, scratch_dirpath, examples_dirpath):
    # 1. LOAD EVERYTHING
    config = tools.load_config(model_filepath)
    classification_model = torch.load(model_filepath, map_location=DEVICE)
    classification_model.eval()
    add_hooks(classification_model)
    embedding_matrix = get_embedding_weight(classification_model)
    
    tokenizer = tools.load_tokenizer(tokenizer_filepath) 
    max_input_length = tools.get_max_input_length(config, tokenizer)

    original_words, original_labels = tools.get_words_and_labels(examples_dirpath)
    var_names = ['input_ids', 'attention_mask', 'labels', 'labels_mask']
    vars = list(tools.tokenize_and_align_labels(tokenizer, original_words, 
                                          original_labels, max_input_length))
    all_vars = {k:to_tensor_and_device(v) for k, v in zip(var_names, vars)}

    # 2. INITIALIZE ATTACK FOR A SOURCE CLASS AND NUM_TOKENS
    # Get a mask for the sentences that have examples of source_class
    source_class, num_tokens = 1, 2
    source_class_token_locations = \
        get_source_class_token_locations(source_class, all_vars['labels'])  

    # Apply the mask to get the sentences that correspond to the source_class
    masked_vars = filter_vars_to_sentences_with_source_class(all_vars, 
                                            source_class_token_locations)

    # Make initial trigger tokens that repeat "the" num_token times
    trigger_token_ids = make_initial_trigger_tokens(tokenizer, num_tokens, 'the')
    
    # expand masked_vars to include the trigger and return a mask for the trigger
    masked_vars, trigger_mask, masked_source_class_token_locations = \
        expand_and_insert_tokens(trigger_token_ids, masked_vars, 
                                 source_class_token_locations)
    
    # 3. ITERATIVELY ATTACK THE MODEL CONSIDERING NUM CANDIDATES PER TOKEN
    clear_model_grads(classification_model)

    # forward prop with the current masked vars
    initial_loss = tools.evaluate_batch(classification_model, masked_vars, 
                          masked_source_class_token_locations, use_grad=True)
    
    num_candidates = 10
    best_k_ids = \
        best_k_candidates_for_each_trigger_token(initial_loss, trigger_mask, 
                                                 num_tokens, embedding_matrix, num_candidates)
    
    top_candidate = get_best_candidate(classification_model, masked_vars, 
                                       masked_source_class_token_locations,
                                       trigger_mask, trigger_token_ids, best_k_ids)                                                       
    
    decoded_top_candidate = decode_tensor_of_token_ids(tokenizer, top_candidate)

    # test for a single sentence
    temp_vars = deepcopy(masked_vars)
    temp_vars = insert_trigger(temp_vars, trigger_mask, top_candidate)
    tools.evaluate_batch(classification_model, all_vars, source_class_token_locations,
                                                                 use_grad=False)

    print('end')
    
    


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='Trojan Detector for Round 7.')

    #TODO: REMEMBER TO CHANGE DEFAULT OF IS_TRAINING BACK TO 0
    parser.add_argument('--is_training', type=int, choices=[0, 1], 
                        help='Helps determine if we are training or testing.'\
                             ' If training just specify model number', 
                        default=1)
    parser.add_argument('--model_num', type=int, 
                        help='Model id number', 
                        default=190)
    parser.add_argument('--training_data_path', type=str, 
                        help='Folder that contains the training data', 
                        default='/scratch/data/TrojAI/round7-train-dataset/')
    parser.add_argument('--model_filepath', type=str, 
                        help='File path to the pytorch model file to be evaluated.', 
                        default='/scratch/data/TrojAI/round7-train-dataset/models/id-00000000')
    parser.add_argument('--tokenizer_filepath', type=str, 
                        help='File path to the pytorch model (.pt) file containing the '\
                             'correct tokenizer to be used with the model_filepath.', 
                        default='./model/tokenizer.pt')
    parser.add_argument('--result_filepath', type=str, 
                        help='File path to the file where output result should be written. '\
                             'After execution this file should contain a single line with a'\
                             ' single floating point trojan probability.', 
                        default='./output.txt')
    parser.add_argument('--scratch_dirpath', type=str, 
                        help='File path to the folder where scratch disk space exists. '\
                             'This folder will be empty at execution start and will be '\
                             'deleted at completion of execution.', 
                        default='./scratch')
    parser.add_argument('--examples_dirpath', type=str, 
                        help='File path to the folder of examples which might be useful '\
                             'for determining whether a model is poisoned.', 
                        default='./test-model/clean_example_data')

    args = parser.parse_args()

    if args.is_training:
        args = tools.modify_args_for_training(args)

    trojan_detector(args.model_filepath, 
                    args.tokenizer_filepath, 
                    args.result_filepath, 
                    args.scratch_dirpath,
                    args.examples_dirpath)

