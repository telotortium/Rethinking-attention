#!/usr/bin/env bash
#SBATCH --output=../sbatch_log/%j.out
#SBATCH --mem-per-cpu=16000
#SBATCH --gres=gpumem:11g
#SBATCH --gpus=gtx_1080_ti:1
#SBATCH --time=300
eval "$(conda shell.bash hook)"
conda activate pytorch-transformer
python -u scripts/random_embedding/train_random_embedding.py $@
