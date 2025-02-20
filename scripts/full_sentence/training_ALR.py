from pickle import UnpicklingError
import os
import argparse
import time

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.optim import Adam
from torch.nn.utils.rnn import pad_sequence
from torch.nn.functional import pad

# Local imports
from pathlib import Path
import sys
path_root = Path(__file__).parents[2]
sys.path.append(str(path_root))
import models.definitions.ALR_FF as FF_models
from utils.constants import SCRATCH, MAX_LEN, CHECKPOINTS_SCRATCH, ALR_CHECKPOINT_FORMAT
from utils.data_utils import LanguageDirection
DATA_PATH=os.path.join(SCRATCH,"pytorch-original-transformer", "mha_outputs")
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")  # checking whether you have a GPU, I hope so!
def MAPE(target, output):
    #Mean Absolute Percentage Error
    with torch.no_grad():
        relative_error = torch.abs(output - target) / torch.max(torch.abs(target), torch.ones(output.shape, device = device)*1e-32)
        return torch.mean(relative_error)
         
def prepare_data(data_path,language_direction, chosen_layer = 0, batch_size = 5, t = "train", att_replacement = 'encoder'):
    if t not in ["train", "test", "val"]:
        raise ValueError("ERROR: t must be train, test, or val.")
    if t == "val":
        print("#"*100)
        print("ATTENTION VALIDATION USED IN TRAINING, ONLY OK FOR DEBUGGING")
        print("#"*100)
    if (att_replacement == 'encoder'):
        in_path =   os.path.join(data_path,"encoder", f"128emb_20ep_IWSLT_{language_direction}_layer{chosen_layer}_v_inputs_{t}")
        out_path =  os.path.join(data_path,"encoder", f"128emb_20ep_IWSLT_{language_direction}_layer{chosen_layer}_outputs_{t}")
        mask_path = os.path.join(data_path,"encoder", f"128emb_20ep_IWSLT_{language_direction}_masks_{t}")
        dataset = AttentionEncoderDataset(in_path, out_path, mask_path, MAX_LEN)
        return DataLoader(dataset,  collate_fn=collate_batch, batch_size= batch_size)
    elif(att_replacement == 'decoder'):
        in_path =   os.path.join(data_path,"decoder_self", f"128emb_20ep_IWSLT_{language_direction}_layer{chosen_layer}_v_inputs_{t}")
        out_path =  os.path.join(data_path,"decoder_self", f"128emb_20ep_IWSLT_{language_direction}_layer{chosen_layer}_outputs_{t}")
        mask_path = os.path.join(data_path,"decoder_self", f"128emb_20ep_IWSLT_{language_direction}_masks_{t}")
        dataset = AttentionDecoderDataset(in_path, out_path, mask_path, MAX_LEN)
        return DataLoader(dataset, collate_fn=collate_batch_decoder, batch_size = batch_size )
    elif(att_replacement == 'decoder_ca'):
        in_enc_path =   os.path.join(data_path,"decoder_cross", f"128emb_20ep_IWSLT_{language_direction}_layer{chosen_layer}_v_inputs_{t}")
        in_dec_path =   os.path.join(data_path,"decoder_cross", f"128emb_20ep_IWSLT_{language_direction}_layer{chosen_layer}_q_inputs_{t}")
        out_path =  os.path.join(data_path,"decoder_cross", f"128emb_20ep_IWSLT_{language_direction}_layer{chosen_layer}_outputs_{t}")
        src_mask_path = os.path.join(data_path,"decoder_cross", f"128emb_20ep_IWSLT_{language_direction}_masks_{t}_src")
        trg_mask_path = os.path.join(data_path,"decoder_cross", f"128emb_20ep_IWSLT_{language_direction}_masks_{t}")
        dataset = AttentionDecoderCADataset(in_enc_path, in_dec_path, out_path, src_mask_path, trg_mask_path, MAX_LEN)
        return DataLoader(dataset, collate_fn=collate_batch_decoder_ca, batch_size = batch_size )
    else:
        raise ValueError("ERROR: att_replacement must be encoder, decoder or decoder_ca.")
    
def training_replacement_FF(params):
    FF_net = getattr(FF_models, params["substitute_class"])
    print(f"Training model: {FF_net}")
    model=FF_net()
    if not params["multi_device"]:
        model.to(device)
    # print(model)
    #model.init_weights()
    model.train(True)
    print("FF model created")
    lr_optimizer = Adam(model.parameters(), lr=0.0001,betas=(0.9, 0.98), eps=1e-9)
    print("Preparing data")
    data_loader=prepare_data(params['dataset_path'], params['language_direction'], chosen_layer = params['num_of_curr_trained_layer'], batch_size = params["batch_size"], att_replacement = params["att_replacement"]) 
    mse_loss=nn.MSELoss()
    for epoch in range(params['num_of_epochs']):
        print("Epoch: ",epoch)
        epoch_loss=0
        num_embeddings=0
        mapes = []
        start = time.time()
        for (data,label, mask) in data_loader:
            lr_optimizer.zero_grad()
            pred=model(data,mask)
            with torch.no_grad():
                num_embeddings+=torch.sum(torch.flatten(mask)).item()
                loss_normalizer=torch.sum(torch.flatten(mask)).item()/(mask.shape[0]*mask.shape[1])
            loss=mse_loss(label,pred)/loss_normalizer
            loss.backward()
            loss /= loss_normalizer
            lr_optimizer.step()
            with torch.no_grad():
                epoch_loss+=loss.item()*torch.sum(torch.flatten(mask)).item()
                mapes.append(MAPE(label, pred))
        if epoch % 20 == 0:
            ckpt_model_name = ALR_CHECKPOINT_FORMAT.format(epoch+1, params['num_of_curr_trained_layer'])
            torch.save(model.state_dict(), os.path.join(params["checkpoints_folder"], ckpt_model_name))
        print(f"Loss per embedding element:{epoch_loss/num_embeddings}, MAPE: {MAPE(label, pred)}, time: {time.time() - start}")

class AttentionEncoderDataset(torch.utils.data.Dataset):
    def __init__(self, input_path, output_path, mask_path, n, t = "max"):
        print(f"Starting to load datasets from {input_path} and {output_path} and {mask_path}")
        start = time.time()

        self.n = n
        if t != "max" and t != "exact":
            raise ValueError("ERROR: t has to be either 'max' or 'exact'.")
        self.t = t
        self.input = []
        self.output = []
        if t == "max":
            self.mask = []
            mask_cache = f"{mask_path}_fixed_{n}_{t}.cache"

        in_cache = f"{input_path}_fixed_{n}_{t}.cache"
        out_cache = f"{output_path}_fixed_{n}_{t}.cache"

        if os.path.exists(in_cache) and os.path.exists(out_cache) and (t == "exact" or os.path.exists(mask_cache)):
            self.input = torch.load(in_cache)
            self.output = torch.load(out_cache)
            if t == "max":
                self.mask = torch.load(mask_cache)
                print(f"Finished loading mask dataset from cache {mask_cache}")
            print(f"Finished loading datasets from cache {in_cache} and {out_cache}")
            print(f"Loaded {len(self.output)} samples in {time.time() - start}s")
            return

        inf = open(input_path, "rb")
        outf = open(output_path, "rb")
        maskf = open(mask_path, "rb")
        try:
            while(True):
                # i represents one batch of sentences -> dim: batch size x padded sentence length x embedding size
                i = torch.from_numpy(np.load(inf))
                m = torch.from_numpy(np.load(maskf))
                m = torch.squeeze(m, dim=1)
                m = torch.squeeze(m, dim=1)
                o = torch.from_numpy(np.load(outf))
                l = torch.sum(m, dim = 1)
                for j in range(i.shape[0]):
                    if t == "max":
                        if l[j] <= n:
                            self.input.append(i[j, :l[j]])
                            self.output.append(o[j,:,:l[j]])
                            self.mask.append(m[j,  :l[j]])
                    else:
                        if l[j] == n:
                            self.input.append(i[j, :l[j]])
                            self.output.append(o[j, :l[j]])
        except (UnpicklingError, ValueError):
            print(f"Finished loading datasets from {input_path} and {output_path}")
            print(f"Loaded {len(self.output)} samples in {time.time() - start}s")
        finally:
            inf.close()
            outf.close()
            maskf.close()
        # self.input = torch.cat(self.input, dim=0)
        # self.output = torch.cat(self.output, dim=0)
        print(self.input[0].shape)
        torch.save(self.input, in_cache)
        torch.save(self.output, out_cache)
        if t == "max":
            # self.mask = torch.cat(self.mask, dim=0)
            torch.save(self.mask, mask_cache)

    def __len__(self):
        return len(self.input)

    def __getitem__(self, idx):
        # if we have exactly the same length, there is no need for padding/masking
        if self.t == "exact":
            return (self.input[idx], self.output[idx])
        return (self.input[idx], self.output[idx], self.mask[idx])

    def emb_size(self):
        return self.input.shape[1]

class AttentionDecoderCADataset(torch.utils.data.Dataset):
    def __init__(self, in_enc_path, in_dec_path, out_path, src_mask_path, trg_mask_path,  n, t = "max"):
        print(f"Starting to load datasets from {in_enc_path}, {in_dec_path}, {out_path}, {src_mask_path}  and {trg_mask_path}")
        start = time.time()

        self.n = n
        if t != "max" and t != "exact":
            raise ValueError("ERROR: t has to be either 'max' or 'exact'.")
        self.t = t
        self.input_enc = []
        self.input_dec = []
        self.output = []
        if t == "max":
            self.src_mask = []
            self.trg_mask = []
            src_mask_cache = f"{src_mask_path}_fixed_{n}_{t}.cache"
            trg_mask_cache = f"{trg_mask_path}_fixed_{n}_{t}.cache"

        in_enc_cache = f"{in_enc_path}_fixed_{n}_{t}.cache"
        in_dec_cache = f"{in_dec_path}_fixed_{n}_{t}.cache"
        out_cache = f"{out_path}_fixed_{n}_{t}.cache"

        if os.path.exists(in_enc_cache) and os.path.exists(in_dec_cache) and os.path.exists(out_cache) and (t == "exact" or (os.path.exists(src_mask_cache) and os.path.exists(trg_mask_cache))):
            self.input_enc = torch.load(in_enc_cache)
            self.input_dec = torch.load(in_dec_cache)
            self.output = torch.load(out_cache)
            if t == "max":
                self.src_mask = torch.load(src_mask_cache)
                self.trg_mask = torch.load(trg_mask_cache)
                print(f"Finished loading mask dataset from cache {src_mask_cache} and {trg_mask_cache}")
            print(f"Finished loading datasets from cache {in_enc_cache}, {in_dec_cache} and {out_cache}")
            print(f"Loaded {len(self.output)} samples in {time.time() - start}s")
            return

        inf_enc = open(in_enc_path, "rb")
        inf_dec = open(in_dec_path, "rb")
        outf = open(out_path, "rb")
        maskf_enc = open(src_mask_path, "rb")
        maskf_dec = open(trg_mask_path, "rb")

        i_enc_list = []
        i_dec_list = []
        o_list = []
        m_enc_list = []
        m_dec_list = []
        l1_list = []
        l2_list = []

        try:
            while(True):
                # i represents one batch of sentences -> dim: batch size x padded sentence length x embedding size
                i_enc = torch.from_numpy(np.load(inf_enc))
                i_dec = torch.from_numpy(np.load(inf_dec))
                o = torch.from_numpy(np.load(outf))
                
                m = torch.from_numpy(np.load(maskf_enc))
                m = torch.squeeze(m, dim=1)
                m_enc = torch.squeeze(m, dim=1)

                m = torch.from_numpy(np.load(maskf_dec))
                m = m[:,:,-1]
                m_dec = torch.squeeze(m, dim=1)

                l1 = torch.sum(m_enc, dim = 1)
                l2 = torch.sum(m_dec, dim = 1)

                i_enc_list.extend(list(i_enc))
                i_dec_list.extend(list(i_dec))
                o_list.extend(list(o))
                m_enc_list.extend(list(m_enc))
                m_dec_list.extend(list(m_dec))
                l1_list.extend(list(l1))
                l2_list.extend(list(l2))


        except (UnpicklingError, ValueError):
            print(f"Finished loading datasets from {in_enc_path}, {in_dec_path} and {out_path}")
            print(f"Loaded {len(self.output)} samples in {time.time() - start}s")
        finally:
            inf_enc.close()
            inf_dec.close()
            outf.close()
            maskf_enc.close()
            maskf_dec.close()
        
        for j in range(len(i_enc_list)):
            if t == "max":
                if l1_list[j] <= n and l2_list[j] <= n:
                    self.input_enc.append(i_enc_list[j][:l1_list[j]])
                    self.src_mask.append(m_enc_list[j][:l1_list[j]])

                    self.input_dec.append(i_dec_list[j][:l2_list[j]])
                    self.output.append(o_list[j][:,:l2_list[j]])
                    self.trg_mask.append(m_dec_list[j][:l2_list[j]])

        print(f"Encoder input shape: {self.input_enc[0].shape}")
        print(f"Decoder input shape: {self.input_dec[0].shape}")
        torch.save(self.input_enc, in_enc_cache)
        torch.save(self.input_dec, in_dec_cache)
        torch.save(self.output, out_cache)
        if t == "max":
            torch.save(self.src_mask, src_mask_cache)
            torch.save(self.trg_mask, trg_mask_cache)

    def __len__(self):
        return len(self.input_enc)

    def __getitem__(self, idx):
        # if we have exactly the same length, there is no need for padding/masking
        if self.t == "exact":
            return (self.input_enc[idx],self.input_dec[idx], self.output[idx])
        return (self.input_enc[idx],self.input_dec[idx], self.output[idx], self.src_mask[idx], self.trg_mask[idx])

    def emb_size(self):
        return self.input.shape[1]

class AttentionDecoderDataset(torch.utils.data.Dataset):
    def __init__(self, input_path, output_path, mask_path, n, t = "max"):
        print(f"Starting to load datasets from {input_path} and {output_path} and {mask_path}")
        start = time.time()

        self.n = n
        if t != "max" and t != "exact":
            raise ValueError("ERROR: t has to be either 'max' or 'exact'.")
        self.t = t
        self.input = []
        self.output = []
        if t == "max":
            self.mask = []
            mask_cache = f"{mask_path}_fixed_{n}_{t}.cache"

        in_cache = f"{input_path}_fixed_{n}_{t}.cache"
        out_cache = f"{output_path}_fixed_{n}_{t}.cache"

        if os.path.exists(in_cache) and os.path.exists(out_cache) and (t == "exact" or os.path.exists(mask_cache)):
            self.input = torch.load(in_cache)
            self.output = torch.load(out_cache)
            if t == "max":
                self.mask = torch.load(mask_cache)
                print(f"Finished loading mask dataset from cache {mask_cache}")
            print(f"Finished loading datasets from cache {in_cache} and {out_cache}")
            print(f"Loaded {len(self.output)} samples in {time.time() - start}s")
            return

        inf = open(input_path, "rb")
        outf = open(output_path, "rb")
        maskf = open(mask_path, "rb")
        try:
            while(True):
                # i represents one batch of sentences -> dim: batch size x padded sentence length x embedding size
                i = torch.from_numpy(np.load(inf))
                m = torch.from_numpy(np.load(maskf))
                m = torch.squeeze(m, dim = 1)
                o = torch.from_numpy(np.load(outf))
                l = torch.max(torch.sum(m, dim = -1), dim = -1).values
                for j in range(i.shape[0]):
                    if t == "max":
                        if l[j] <= n:
                            self.input.append(i[j, :l[j]])
                            self.output.append(o[j,:,:l[j]])
                            self.mask.append(m[j,  :l[j], :l[j]])
                    else:
                        if l[j] == n:
                            self.input.append(i[j, :n])
                            self.output.append(o[j, :n])
        except (UnpicklingError, ValueError):
            print(f"Finished loading datasets from {input_path} and {output_path}")
            print(f"Loaded {len(self.output)} samples in {time.time() - start}s")
        finally:
            inf.close()
            outf.close()
            maskf.close()
        # self.input = torch.cat(self.input, dim=0)
        # self.output = torch.cat(self.output, dim=0)
        torch.save(self.input, in_cache)
        torch.save(self.output, out_cache)
        if t == "max":
            # self.mask = torch.cat(self.mask, dim=0)
            torch.save(self.mask, mask_cache)

    def __len__(self):
        return len(self.input)

    def __getitem__(self, idx):
        # if we have exactly the same length, there is no need for padding/masking
        if self.t == "exact":
            return (self.input[idx], self.output[idx])
        return (self.input[idx], self.output[idx], self.mask[idx])

    def emb_size(self):
        return self.input.shape[1]

def collate_batch_decoder(batch):
    NH = batch[0][1].shape[0]
    HD = batch[0][1].shape[2]
    batch_size = len(batch)
    inputs  = pad_sequence([x[0] for x in batch], batch_first=True, padding_value=0)
    outputs = pad_sequence([x[1].transpose(0,1).reshape(-1, NH * HD) for x in batch], batch_first=True, padding_value=0) # this reshaping must be transfered to the adapter as well
    trg_padding_mask = pad_sequence([x[2][-1] for x in batch], batch_first=True, padding_value=0)
    inputs = torch.cat([inputs, torch.zeros(pad_shape(inputs))], dim = 1).to(device)
    outputs = torch.cat([outputs, torch.zeros(pad_shape(outputs))], dim = 1).to(device)    
    trg_padding_mask = torch.cat([trg_padding_mask, torch.zeros(pad_shape(trg_padding_mask, masks = True), dtype=torch.bool)], dim = 1).view(batch_size, 1, -1).to(device)
    
    # Pad to fixed length    
    trg_no_look_forward_mask = torch.triu(torch.ones((1, MAX_LEN, MAX_LEN), device=device) == 1).transpose(1, 2)

    # logic AND operation (both padding mask and no-look-forward must be true to attend to a certain target token)
    trg_mask = trg_padding_mask & trg_no_look_forward_mask  # final shape = (B, T, T)
    return inputs, outputs, trg_mask    

def pad_shape(batch, masks = False):
    shape = batch.shape
    if masks:
        return shape[0],MAX_LEN-shape[1] 
    return shape[0], MAX_LEN-shape[1], shape[2]

def collate_batch(batch):   
    # print("COLLATE")
    # print(batch[0][0].shape)
    # print(batch[0][1].shape)
    # print(batch[0][2].shape)
    
    # Pad all elements to the same length
    NH = batch[0][1].shape[0]
    HD = batch[0][1].shape[2]
    inputs  = pad_sequence([x[0] for x in batch], batch_first=True, padding_value=0)
    outputs = pad_sequence([x[1].transpose(0,1).reshape(-1, NH * HD) for x in batch], batch_first=True, padding_value=0) # this reshaping must be transfered to the adapter as well
    masks   = pad_sequence([x[2] for x in batch], batch_first=True, padding_value=0) 
    # print(inputs.shape)
    # print(outputs.shape)
    # print(masks.shape)
    
    # Pad to fixed length
    inputs = torch.cat([inputs, torch.zeros(pad_shape(inputs))], dim = 1).to(device)
    outputs = torch.cat([outputs, torch.zeros(pad_shape(outputs))], dim = 1).to(device)
    masks = torch.cat([masks, torch.zeros(pad_shape(masks, masks = True), dtype=torch.bool)], dim = 1).to(device)
    
    # Reshape concatenating the embeddings for each sentence
    masks = torch.repeat_interleave(masks, inputs.shape[-1] ,dim=1)
    inputs = torch.reshape(inputs, (inputs.shape[0],inputs.shape[1]*inputs.shape[2]))
    outputs = torch.reshape(outputs, (outputs.shape[0],outputs.shape[1]*outputs.shape[2]))
    return inputs, outputs, masks

def collate_batch_decoder_ca(batch):   
    # Pad all elements to the same length
    NH = batch[0][2].shape[0]
    HD = batch[0][2].shape[2]
    inputs_enc  = pad_sequence([x[0] for x in batch], batch_first=True, padding_value=0)
    inputs_dec  = pad_sequence([x[1] for x in batch], batch_first=True, padding_value=0)
    outputs = pad_sequence([x[2].transpose(0,1).reshape(-1, NH * HD) for x in batch], batch_first=True, padding_value=0) # this reshaping must be transfered to the adapter as well
    src_masks   = pad_sequence([x[3] for x in batch], batch_first=True, padding_value=0) 
    trg_masks   = pad_sequence([x[4] for x in batch], batch_first=True, padding_value=0) 
    # print(inputs.shape)
    # print(outputs.shape)
    # print(masks.shape)
    
    # Pad to fixed length
    inputs_enc = torch.cat([inputs_enc, torch.zeros(pad_shape(inputs_enc))], dim = 1).to(device)
    inputs_dec = torch.cat([inputs_dec, torch.zeros(pad_shape(inputs_dec))], dim = 1).to(device)
    outputs = torch.cat([outputs, torch.zeros(pad_shape(outputs))], dim = 1).to(device)
    src_masks = torch.cat([src_masks, torch.zeros(pad_shape(src_masks, masks = True), dtype=torch.bool)], dim = 1).to(device)
    trg_masks = torch.cat([trg_masks, torch.zeros(pad_shape(trg_masks, masks = True), dtype=torch.bool)], dim = 1).to(device)
    # Reshape concatenating the embeddings for each sentence
    src_masks = torch.repeat_interleave(src_masks, inputs_enc.shape[-1] ,dim=1)
    trg_masks = torch.repeat_interleave(trg_masks, inputs_dec.shape[-1] ,dim=1)
    inputs_enc = torch.reshape(inputs_enc, (inputs_enc.shape[0],inputs_enc.shape[1]*inputs_enc.shape[2]))
    inputs_dec = torch.reshape(inputs_dec, (inputs_dec.shape[0],inputs_dec.shape[1]*inputs_dec.shape[2]))
    inputs = torch.cat([inputs_enc, inputs_dec], dim = 1)
    outputs = torch.reshape(outputs, (outputs.shape[0],outputs.shape[1]*outputs.shape[2]))
    return inputs, outputs, trg_masks


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--num_of_epochs", type=int, help="number of training epochs", default=21)
    parser.add_argument("--dataset_path", type=str, help='download dataset to this path', default=DATA_PATH)
    parser.add_argument("--model_dimension", type=str, help='embedding size', default=128)
    parser.add_argument("--batch_size", type=str, help='batch_size', default=2000)
    parser.add_argument("--multi_device", action = "store_true")
    
    # Params to set
    parser.add_argument("--num_of_curr_trained_layer", type=str, help='num_of_curr_trained_layer', default=0)
    parser.add_argument("--substitute_class", type = str, help="name of the FF to train defined in models/definitions/ALR.py", required=True)
    parser.add_argument("--att_replacement", help = "Which attention to replace", choices = ["encoder", "decoder", "decoder_ca"], default = "encoder")
    parser.add_argument("--language_direction", choices=[el.name for el in LanguageDirection], help='which direction to translate', default=LanguageDirection.de_en.name)
    args = parser.parse_args()
    # Wrapping training configuration into a dictionary
    training_config = dict()
    for arg in vars(args):
        training_config[arg] = getattr(args, arg)
    print("Training arguments parsed")
    training_config["checkpoints_folder"] = os.path.join(CHECKPOINTS_SCRATCH,"ALR", training_config["substitute_class"], f"layer{training_config['num_of_curr_trained_layer']}")
    os.makedirs(training_config["checkpoints_folder"], exist_ok = True)
    print(training_config["checkpoints_folder"])
    print(training_config)
    training_replacement_FF(training_config)
