# coding: utf-8
__author__ = 'Roman Solovyev (ZFTurbo): https://github.com/ZFTurbo/'

import argparse
import time
import librosa
from tqdm import tqdm
import sys
import os
import copy
import glob
import torch
import numpy as np
import soundfile as sf
import torch.nn as nn
from utils import demix_track, demix_track_demucs, get_model_from_config

import warnings
warnings.filterwarnings("ignore")

def stereo_widering(wave, value):
    n = (100 - value) / 100
    k1 = .5 + n / 2
    k2 = .5 - n / 2
    wave0 = copy.copy(wave[0])
    wave[0] = wave[0] * (k1 / n) - wave[1] * (k2 / n)
    wave[1] = wave[1] * (k1 / n) - wave0 * (k2 / n)
    
    return wave
    
def stereo_narrowing(wave, value):
    n = 100 - value
    k1 = (50 + n / 2) / 100
    k2 = (50 - n / 2) / 100
    wave0 = copy.copy(wave[0])
    wave[0] = wave[0] * k1 + wave[1] * k2
    wave[1] = wave[1] * k1 + wave0 * k2
    
    return wave
    
def run_single_file(model, args, config, device, verbose=False):
    start_time = time.time()
    model.eval()

    if not os.path.isfile(args.input_file):
        time.sleep(1)
        if not os.path.isfile(args.input_file):
            print("Input file doesn't exist!")
            return

    if not os.path.isdir(args.store_dir):
        os.mkdir(args.store_dir)

    try:
        mix, sr = librosa.load(args.input_file, sr=44100, mono=False)
        is_stereo = mix.shape[0] == 2
        if args.stereo_narrowing != 0 and is_stereo:
            mix = stereo_narrowing(mix, args.stereo_narrowing)
        mix = mix.T
        original_length = mix.shape[0]
        
        # Adding 5 seconds of silence to fix the bug
        silence_duration = 5 
        silence = np.zeros((silence_duration * sr, mix.shape[1]))
        mix = np.vstack([mix, silence])
    except Exception as e:
        print('Can read track: {}'.format(args.input_file))
        print('Error message: {}'.format(str(e)))
        return

    # Convert mono to stereo if needed
    if len(mix.shape) == 1:
        mix = np.stack([mix, mix], axis=-1)
        
    mixture = torch.tensor(mix.T, dtype=torch.float32)
    if args.model_type == 'htdemucs':
        res = demix_track_demucs(config, model, mixture, device)
    else:
        res = demix_track(config, model, mixture, device)

    instruments = config.training.instruments
    if config.training.target_instrument is not None:
        instruments = [config.training.target_instrument]

    for instr in instruments:
        res[instr] = res[instr][:, :original_length] # Removing the last 5 seconds of silence
        if args.stereo_narrowing != 0 and is_stereo:
            sf.write("{}/{}_{}.wav".format(args.store_dir, os.path.basename(args.input_file)[:-4], instr), stereo_widering(res[instr], args.stereo_narrowing).T, sr, subtype='FLOAT')
        else:
            sf.write("{}/{}_{}.wav".format(args.store_dir, os.path.basename(args.input_file)[:-4], instr), res[instr].T, sr, subtype='FLOAT')


def run_folder(model, args, config, device, verbose=False):
    start_time = time.time()
    model.eval()
    all_mixtures_path = glob.glob(args.input_folder + '/*.*')
    print('Total files found: {}'.format(len(all_mixtures_path)))

    instruments = config.training.instruments
    if config.training.target_instrument is not None:
        instruments = [config.training.target_instrument]

    if not os.path.isdir(args.store_dir):
        os.mkdir(args.store_dir)

    if not verbose:
        all_mixtures_path = tqdm(all_mixtures_path)

    for path in all_mixtures_path:
        if not verbose:
            all_mixtures_path.set_postfix({'track': os.path.basename(path)})
        try:
            # mix, sr = sf.read(path)
            mix, sr = librosa.load(path, sr=44100, mono=False)
            mix = mix.T
        except Exception as e:
            print('Can read track: {}'.format(path))
            print('Error message: {}'.format(str(e)))
            continue

        # Convert mono to stereo if needed
        if len(mix.shape) == 1:
            mix = np.stack([mix, mix], axis=-1)

        mixture = torch.tensor(mix.T, dtype=torch.float32)
        if args.model_type == 'htdemucs':
            res = demix_track_demucs(config, model, mixture, device)
        else:
            res = demix_track(config, model, mixture, device)
        for instr in instruments:
            sf.write("{}/{}_{}.wav".format(args.store_dir, os.path.basename(path)[:-4], instr), res[instr].T, sr, subtype='FLOAT')

        if 'vocals' in instruments and args.extract_instrumental:
            instrum_file_name = "{}/{}_{}.wav".format(args.store_dir, os.path.basename(path)[:-4], 'instrumental')
            sf.write(instrum_file_name, mix - res['vocals'].T, sr, subtype='FLOAT')

    time.sleep(1)
    print("Elapsed time: {:.2f} sec".format(time.time() - start_time))


def proc_single_file(args):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_type", type=str, default='mdx23c', help="One of mdx23c, htdemucs, segm_models, mel_band_roformer, bs_roformer, swin_upernet, bandit")
    parser.add_argument("--config_path", type=str, help="path to config file")
    parser.add_argument("--start_check_point", type=str, default='', help="Initial checkpoint to valid weights")
    #parser.add_argument("--input_folder", type=str, help="folder with mixtures to process")
    parser.add_argument("--input_file", type=str, help="path to input audio file")
    parser.add_argument("--store_dir", default="", type=str, help="path to store results as wav file")
    parser.add_argument("--device_ids", nargs='+', type=int, default=0, help='list of gpu ids')
    parser.add_argument("--extract_instrumental", action='store_true', help="invert vocals to get instrumental if provided")
    if args is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(args)

    torch.backends.cudnn.benchmark = True

    model, config = get_model_from_config(args.model_type, args.config_path)
    if args.start_check_point != '':
        print('Start from checkpoint: {}'.format(args.start_check_point))
        state_dict = torch.load(args.start_check_point)
        if args.model_type == 'htdemucs':
            # Fix for htdemucs pround etrained models
            if 'state' in state_dict:
                state_dict = state_dict['state']
        model.load_state_dict(state_dict)
    print("Instruments: {}".format(config.training.instruments))

    if torch.cuda.is_available():
        device_ids = args.device_ids
        if type(device_ids)==int:
            device = torch.device(f'cuda:{device_ids}')
            model = model.to(device)
        else:
            device = torch.device(f'cuda:{device_ids[0]}')
            model = nn.DataParallel(model, device_ids=device_ids).to(device)
    else:
        device = 'cpu'
        print('CUDA is not avilable. Run inference on CPU. It will be very slow...')
        model = model.to(device)

    run_single_file(model, args, config, device, verbose=False)


if __name__ == "__main__":
    if not os.path.isdir(args.store_dir):
        os.mkdir(args.store_dir)

    try:
        mix, sr = librosa.load(args.input_file, sr=44100, mono=False)
        mix = mix.T
        original_length = mix.shape[0]
        
        # Adding 5 seconds of silence to fix the bug
        silence_duration = 5 
        silence = np.zeros((silence_duration * sr, mix.shape[1]))
        mix = np.vstack([mix, silence])
    except Exception as e:
        print('Can read track: {}'.format(args.input_file))
        print('Error message: {}'.format(str(e)))
        return

    # Convert mono to stereo if needed
    if len(mix.shape) == 1:
        mix = np.stack([mix, mix], axis=-1)

    mixture = torch.tensor(mix.T, dtype=torch.float32)
    if args.model_type == 'htdemucs':
        res = demix_track_demucs(config, model, mixture, device)
    else:
        res = demix_track(config, model, mixture, device)

    instruments = config.training.instruments
    if config.training.target_instrument is not None:
        instruments = [config.training.target_instrument]

    for instr in instruments:
        res[instr] = res[instr][:, :original_length] # Removing the last 5 seconds of silence
        sf.write("{}/{}_{}.wav".format(args.store_dir, os.path.basename(args.input_file)[:-4], instr), res[instr].T, sr, subtype='FLOAT')


def run_folder(model, args, config, device, verbose=False):
    start_time = time.time()
    model.eval()
    all_mixtures_path = glob.glob(args.input_folder + '/*.*')
    print('Total files found: {}'.format(len(all_mixtures_path)))

    instruments = config.training.instruments
    if config.training.target_instrument is not None:
        instruments = [config.training.target_instrument]

    if not os.path.isdir(args.store_dir):
        os.mkdir(args.store_dir)

    if not verbose:
        all_mixtures_path = tqdm(all_mixtures_path)

    for path in all_mixtures_path:
        if not verbose:
            all_mixtures_path.set_postfix({'track': os.path.basename(path)})
        try:
            # mix, sr = sf.read(path)
            mix, sr = librosa.load(path, sr=44100, mono=False)
            mix = mix.T
        except Exception as e:
            print('Can read track: {}'.format(path))
            print('Error message: {}'.format(str(e)))
            continue

        # Convert mono to stereo if needed
        if len(mix.shape) == 1:
            mix = np.stack([mix, mix], axis=-1)

        mixture = torch.tensor(mix.T, dtype=torch.float32)
        if args.model_type == 'htdemucs':
            res = demix_track_demucs(config, model, mixture, device)
        else:
            res = demix_track(config, model, mixture, device)
        for instr in instruments:
            sf.write("{}/{}_{}.wav".format(args.store_dir, os.path.basename(path)[:-4], instr), res[instr].T, sr, subtype='FLOAT')

        if 'vocals' in instruments and args.extract_instrumental:
            instrum_file_name = "{}/{}_{}.wav".format(args.store_dir, os.path.basename(path)[:-4], 'instrumental')
            sf.write(instrum_file_name, mix - res['vocals'].T, sr, subtype='FLOAT')

    time.sleep(1)
    print("Elapsed time: {:.2f} sec".format(time.time() - start_time))


def proc_single_file(args):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_type", type=str, default='mdx23c', help="One of mdx23c, htdemucs, segm_models, mel_band_roformer, bs_roformer, swin_upernet, bandit")
    parser.add_argument("--config_path", type=str, help="path to config file")
    parser.add_argument("--start_check_point", type=str, default='', help="Initial checkpoint to valid weights")
    #parser.add_argument("--input_folder", type=str, help="folder with mixtures to process")
    parser.add_argument("--input_file", type=str, help="path to input audio file")
    parser.add_argument("--store_dir", default="", type=str, help="path to store results as wav file")
    parser.add_argument("--device_ids", nargs='+', type=int, default=0, help='list of gpu ids')
    parser.add_argument("--extract_instrumental", action='store_true', help="invert vocals to get instrumental if provided")
	# pre-processing
    parser.add_argument("--stereo_narrowing", type=int, default=0, help='Pre-narrowing of stereo image')
    if args is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(args)

    torch.backends.cudnn.benchmark = True

    model, config = get_model_from_config(args.model_type, args.config_path)
    if args.start_check_point != '':
        print('Start from checkpoint: {}'.format(args.start_check_point))
        state_dict = torch.load(args.start_check_point)
        if args.model_type == 'htdemucs':
            # Fix for htdemucs pround etrained models
            if 'state' in state_dict:
                state_dict = state_dict['state']
        model.load_state_dict(state_dict)
    print("Instruments: {}".format(config.training.instruments))

    if torch.cuda.is_available():
        device_ids = args.device_ids
        if type(device_ids)==int:
            device = torch.device(f'cuda:{device_ids}')
            model = model.to(device)
        else:
            device = torch.device(f'cuda:{device_ids[0]}')
            model = nn.DataParallel(model, device_ids=device_ids).to(device)
    else:
        device = 'cpu'
        print('CUDA is not avilable. Run inference on CPU. It will be very slow...')
        model = model.to(device)

    run_single_file(model, args, config, device, verbose=False)


if __name__ == "__main__":
    proc_folder(None)
