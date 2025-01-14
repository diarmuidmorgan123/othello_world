import os
# set up logging
import logging
logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
)

# make deterministic
from mingpt.utils import set_seed
import numpy as np
seed=np.random.randint(0, 12389823)
set_seed(seed)

import time
from tqdm import tqdm
import numpy as np
from matplotlib import pyplot as plt
import argparse
import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.utils.data import Dataset
from torch.utils.data.dataloader import DataLoader
from data import get_othello
from data.othello import permit, start_hands, OthelloBoardState
from mingpt.dataset import CharDataset
from mingpt.model import GPT, GPTConfig, GPTforProbing
from mingpt.probe_trainer import Trainer, TrainerConfig
from mingpt.probe_model import BatteryProbeClassification, BatteryProbeClassificationTwoLayer

parser = argparse.ArgumentParser(description='Train classification network')
parser.add_argument('--layer',
                    required=True,
                    default=-1,
                    type=int)

parser.add_argument('--epo',
                    default=16,
                    type=int)

parser.add_argument('--mid_dim',
                    default=128,
                    type=int)
#Add arg for running with consecutive split
parser.add_argument('--splitconsec',
                    dest='splitconsec',
                    action='store_true')

parser.add_argument('--twolayer',
                    dest='twolayer', 
                    action='store_true')

parser.add_argument('--random',
                    dest='random', 
                    action='store_true')

parser.add_argument('--championship',
                    dest='championship', 
                    action='store_true')

parser.add_argument('--exp',
                    default="state", 
                    type=str)

args, _ = parser.parse_known_args()

folder_name = f"battery_othello/{args.exp}"

if args.twolayer:
    folder_name = folder_name + f"_tl{args.mid_dim}"  # tl for probes without batchnorm
if args.random:
    folder_name = folder_name + "_random"
if args.championship:
    folder_name = folder_name + "_championship"

print(f"Running experiment for {folder_name}")
othello = get_othello(data_root="data/othello_championship")

train_dataset = CharDataset(othello)

mconf = GPTConfig(train_dataset.vocab_size, train_dataset.block_size, n_layer=8, n_head=8, n_embd=512)
model = GPTforProbing(mconf, probe_layer=args.layer)
if args.random:
    model.apply(model._init_weights)
elif args.championship:
    load_res = model.load_state_dict(torch.load("gpt_championship.ckpt"))
else:  # trained on synthetic dataset
    load_res = model.load_state_dict(torch.load("./ckpts/gpt_synthetic.ckpt"))
if torch.cuda.is_available():
    device = torch.cuda.current_device()
    model = model.to(device)

loader = DataLoader(train_dataset, shuffle=False, pin_memory=True, batch_size=1, num_workers=1)
act_container = []
property_container = []
for x, y in tqdm(loader, total=len(loader)):
    tbf = [train_dataset.itos[_] for _ in x.tolist()[0]]
    valid_until = tbf.index(-100) if -100 in tbf else 999
    a = OthelloBoardState()
    properties = a.get_gt(tbf[:valid_until], "get_" + args.exp)  # [block_size, ]
    
    #Skip computing the activations as we don't need them.
    
    # act = model(x.to(device))[0, ...].detach().cpu()  # [block_size, f]
    # act_container.extend([_[0] for _ in act.split(1, dim=0)[:valid_until]])
    property_container.extend(properties)
    
age_container = []
for x, y in tqdm(loader, total=len(loader)):
    tbf = [train_dataset.itos[_] for _ in x.tolist()[0]]
    valid_until = tbf.index(-100) if -100 in tbf else 999
    a = OthelloBoardState()
    ages = a.get_gt(tbf[:valid_until], "get_age")  # [block_size, ]
    age_container.extend(ages)
    
#Get the raw tokens from the loader
raw_seq=[]
for x, y in tqdm(loader, total=len(loader)):
    tbf = [train_dataset.itos[_] for _ in x.tolist()[0]]
    valid_until = tbf.index(-100) if -100 in tbf else 999
    seq = x.detach().cpu().numpy()[0]
    seq=seq[:valid_until]  
    for i in range(1, len(seq)+1):
        raw_seq.append(seq[:i])
        
#Create one hot vectors for the raw tokens
mat = np.full([len(raw_seq),59], -1)
for idx, seq in enumerate(raw_seq):
    
    mat[idx, :len(seq)]=seq
    
from sklearn.preprocessing import OneHotEncoder
mat = OneHotEncoder().fit_transform(mat)
mat=mat.todense()

mat_container = [torch.tensor(m.astype(np.float32)) for m in mat]

if args.exp == "state":
    probe_class=3

#Change input dim of probe to match the one hot vectors
if args.twolayer:
    probe = BatteryProbeClassificationTwoLayer(device, probe_class=probe_class, num_task=64, mid_dim=args.mid_dim, input_dim=mat.shape[1])
else:
    probe = BatteryProbeClassification(device, probe_class=probe_class, num_task=64, input_dim=mat.shape[1])
    
class ProbingDataset(Dataset):
    def __init__(self, act, y, age):
        assert len(act) == len(y)
        assert len(act) == len(age)
        print(f"{len(act)} pairs loaded...")
        self.act = act
        self.y = y
        self.age = age
        print(np.sum(np.array(y)==0), np.sum(np.array(y)==1), np.sum(np.array(y)==2))
        
        long_age = []
        for a in age:
            long_age.extend(a)
        long_age = np.array(long_age)
        counts = [np.count_nonzero(long_age == i) for i in range(60)]
        del long_age
        print(counts)
    def __len__(self, ):
        return len(self.y)
    def __getitem__(self, idx):
        return self.act[idx], torch.tensor(self.y[idx]).to(torch.long), torch.tensor(self.age[idx]).to(torch.long)

splitconsec=args.splitconsec

probing_dataset = ProbingDataset(mat_container, property_container, age_container)
train_size = int(0.8 * len(probing_dataset))
test_size = len(probing_dataset) - train_size

#Split the data consecutively so that no games end up in both train and test (well I guess one game will).
if splitconsec:
    train_dataset= ProbingDataset(mat_container[:train_size], property_container[:train_size], age_container[:train_size])
    test_dataset = ProbingDataset(mat_container[train_size:], property_container[train_size:], age_container[train_size:])

else:
    train_dataset, test_dataset = torch.utils.data.random_split(probing_dataset, [train_size, test_size])

sampler = None
train_loader = DataLoader(train_dataset, shuffle=False, sampler=sampler, pin_memory=True, batch_size=128, num_workers=1)
test_loader = DataLoader(test_dataset, shuffle=True, pin_memory=True, batch_size=128, num_workers=1)

max_epochs = args.epo
t_start = time.strftime("_%Y%m%d_%H%M%S")
tconf = TrainerConfig(
    max_epochs=max_epochs, batch_size=1024, learning_rate=1e-3,
    betas=(.9, .999), 
    lr_decay=True, warmup_tokens=len(train_dataset)*5, 
    final_tokens=len(train_dataset)*max_epochs,
    num_workers=4, weight_decay=0., 
    ckpt_path=os.path.join("./ckpts/", folder_name, f"layer{args.layer}")
)
trainer = Trainer(probe, train_dataset, test_dataset, tconf)
trainer.train(prt=True)
trainer.save_traces()
trainer.save_checkpoint()
acc = np.max(trainer.test_acc_cont)
if args.twolayer:
    n_layers = 2
else:
    n_layers=1
with open('results.txt', 'a') as handle:
    
    handle.write(f'raw_seq_{n_layers}-layer,{splitconsec},{acc},{seed}\n')
    

