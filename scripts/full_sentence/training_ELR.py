import os
import argparse
import time

from pickle import UnpicklingError
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, random_split
from torch.optim import Adam
from torch.nn.utils.rnn import pad_sequence


# Local imports
from pathlib import Path
import sys
path_root = Path(__file__).parents[2]
sys.path.append(str(path_root))

from utils.constants import ALR_CHECKPOINT_FORMAT, SCRATCH, MAX_LEN,CHECKPOINTS_SCRATCH
import models.definitions.ELR_FF as nets
from utils.data_utils import LanguageDirection

DATA_PATH=os.path.join(SCRATCH, "pytorch-original-transformer","layer_outputs")
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")  # checking whether you have a GPU, I hope so!

def MAPE(target, output):
    #Mean Absolute Percentage Error
    with torch.no_grad():
        relative_error = torch.abs(output - target) / torch.max(torch.abs(target), torch.ones(output.shape, device = device)*1e-32)
        return torch.mean(relative_error)
         
def prepare_data(data_path, language_direction, chosen_layer = 0, batch_size = 5, t = "train", dev = False):
    if t not in ["train", "test", "val"]:
        raise ValueError("ERROR: t must be train, test, or val.")
    in_path =   os.path.join(data_path,f"128emb_20ep_IWSLT_{language_direction}_ELR_layer{chosen_layer}_inputs_{t}")
    out_path =  os.path.join(data_path,f"128emb_20ep_IWSLT_{language_direction}_ELR_layer{chosen_layer}_outputs_{t}")
    mask_path = os.path.join(data_path,f"128emb_20ep_IWSLT_{language_direction}_masks_{t}")
    dataset = AttentionDataset(in_path, out_path, mask_path, MAX_LEN)
    if dev:
        dataset, _ = dataset = random_split(dataset, [0.2, 0.8])
    return DataLoader(dataset,  collate_fn=collate_batch, batch_size= batch_size)
    
def training_replacement_FF(params):
    FF_net = getattr(nets, params["substitute_class"])
    print(f"Training model: {FF_net}")
    model=FF_net().to(device)
    model.train(True)
    print("FF model created")
    lr_optimizer = Adam(model.parameters(), lr=0.001,betas=(0.9, 0.98), eps=1e-9)
    print("Preparing data")
    data_loader=prepare_data(params['dataset_path'], params['language_direction'], chosen_layer = params['num_of_curr_trained_layer'], batch_size = params["batch_size"]) 
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
            lr_optimizer.step()
            with torch.no_grad():
                epoch_loss+=loss.item()*torch.sum(torch.flatten(mask)).item()
                mapes.append(MAPE(label, pred))
        if(epoch%20==0):
            ckpt_model_name = ALR_CHECKPOINT_FORMAT.format(epoch+1, params['num_of_curr_trained_layer'])
            torch.save(model.state_dict(), os.path.join(training_config["checkpoints_folder"],ckpt_model_name))
        print(f"Loss per embedding element:{epoch_loss/num_embeddings}, MAPE: {MAPE(label, pred)}, time: {time.time() - start}")

class AttentionDataset(torch.utils.data.Dataset):
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
                            self.input.append( i[ j, : l[j] ] )
                            self.output.append(o[ j, : l[j] ] )
                            self.mask.append(  m[ j, : l[j] ] )
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
    
def pad_shape(batch, masks = False):
    shape = batch.shape
    if masks:
        return shape[0],MAX_LEN-shape[1] 
    return shape[0], MAX_LEN-shape[1], shape[2]

def collate_batch(batch):
    # pad batch to a fixed length
    inputs  = pad_sequence([x[0] for x in batch], batch_first=True, padding_value=0)
    outputs = pad_sequence([x[1] for x in batch], batch_first=True, padding_value=0)
    masks   = pad_sequence([x[2] for x in batch], batch_first=True, padding_value=0)
    # pad batch to MAX_LEN
    inputs = torch.cat([inputs, torch.zeros(pad_shape(inputs))], dim = 1).to(device)
    outputs = torch.cat([outputs, torch.zeros(pad_shape(outputs))], dim = 1).to(device)
    masks = torch.cat([masks, torch.zeros(pad_shape(masks, masks = True), dtype=torch.bool)], dim = 1).to(device)
    # reshape 
    masks = torch.repeat_interleave(masks, inputs.shape[-1] ,dim=1)
    inputs = torch.reshape(inputs, (inputs.shape[0],inputs.shape[1]*inputs.shape[2]))
    outputs = torch.reshape(outputs, (outputs.shape[0],outputs.shape[1]*outputs.shape[2]))
    
    return inputs, outputs, masks

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--num_of_epochs", type=int, help="number of training epochs", default=21)
    parser.add_argument("--dataset_path", type=str, help='download dataset to this path', default=DATA_PATH)
    parser.add_argument("--model_dimension", type=str, help='embedding size', default=128)
    parser.add_argument("--num_of_curr_trained_layer", type=str, help='num_of_curr_trained_layer', default=0)
    parser.add_argument("--batch_size", type=str, help='batch_size', default=2000)
    parser.add_argument("--substitute_class", type = str, help="name of the FF to train defined in models/definitions/ALR.py", required=True)
    parser.add_argument("--language_direction", choices=[el.name for el in LanguageDirection], help='which direction to translate', default=LanguageDirection.de_en.name)
    
    args = parser.parse_args()
    # Wrapping training configuration into a dictionary
    training_config = dict()
    for arg in vars(args):
        training_config[arg] = getattr(args, arg)

    training_config["checkpoints_folder"] = os.path.join(CHECKPOINTS_SCRATCH,"ELR" ,training_config["substitute_class"], f"layer{training_config['num_of_curr_trained_layer']}")
    os.makedirs(training_config["checkpoints_folder"], exist_ok = True)
    print("Training arguments parsed")
    print("Training layer {0}".format(training_config["num_of_curr_trained_layer"]))
    training_replacement_FF(training_config)
