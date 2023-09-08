#!/bin/bash
#SBATCH --output=sbatch_log/%j.out
#SBATCH --gpus=1
#SBATCH --mem-per-cpu=16000
#SBATCH --gres=gpumem:20g
#SBATCH --time 600
source $SCRATCH/miniconda3/etc/profile.d/conda.sh
source activate pytorch-transformer 
python -u scripts/full_sentence/training_ALSR.py "$@"