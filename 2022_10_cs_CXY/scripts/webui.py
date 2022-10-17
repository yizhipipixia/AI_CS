import argparse, os, sys, glob, re

from frontend.frontend import draw_gradio_ui
from frontend.ui_functions import resize_image

print("okokok666")

parser = argparse.ArgumentParser()
parser.add_argument("--outdir", type=str, nargs="?", help="dir to write results to", default=None)
parser.add_argument("--outdir_txt2img", type=str, nargs="?", help="dir to write txt2img results to (overrides --outdir)", default=None)
parser.add_argument("--outdir_img2img", type=str, nargs="?", help="dir to write img2img results to (overrides --outdir)", default=None)
parser.add_argument("--save-metadata", action='store_true', help="Whether to embed the generation parameters in the sample images", default=False)
parser.add_argument("--skip-grid", action='store_true', help="do not save a grid, only individual samples. Helpful when evaluating lots of samples", default=False)
parser.add_argument("--skip-save", action='store_true', help="do not save indiviual samples. For speed measurements.", default=False)
parser.add_argument("--grid-format", type=str, help="png for lossless png files; jpg:quality for lossy jpeg; webp:quality for lossy webp, or webp:-compression for lossless webp", default="jpg:95")
parser.add_argument("--n_rows", type=int, default=-1, help="rows in the grid; use -1 for autodetect and 0 for n_rows to be same as batch_size (default: -1)",)
parser.add_argument("--config", type=str, default="configs/stable-diffusion/v1-inference.yaml", help="path to config which constructs model",)
parser.add_argument("--ckpt", type=str, default="models/ldm/stable-diffusion-v1/model.ckpt", help="path to checkpoint of model",)
parser.add_argument("--precision", type=str, help="evaluate at this precision", choices=["full", "autocast"], default="autocast")
parser.add_argument("--optimized", action='store_true', help="load the model onto the device piecemeal instead of all at once to reduce VRAM usage at the cost of performance")
parser.add_argument("--optimized-turbo", action='store_true', help="alternative optimization mode that does not save as much VRAM but runs siginificantly faster")
parser.add_argument("--gfpgan-dir", type=str, help="GFPGAN directory", default=('./src/gfpgan' if os.path.exists('./src/gfpgan') else './GFPGAN')) # i disagree with where you're putting it but since all guidefags are doing it this way, there you go
parser.add_argument("--realesrgan-dir", type=str, help="RealESRGAN directory", default=('./src/realesrgan' if os.path.exists('./src/realesrgan') else './RealESRGAN'))
parser.add_argument("--realesrgan-model", type=str, help="Upscaling model for RealESRGAN", default=('RealESRGAN_x4plus'))
parser.add_argument("--no-verify-input", action='store_true', help="do not verify input to check if it's too long", default=False)
parser.add_argument("--no-half", action='store_true', help="do not switch the model to 16-bit floats", default=False)
parser.add_argument("--no-progressbar-hiding", action='store_true', help="do not hide progressbar in gradio UI (we hide it because it slows down ML if you have hardware accleration in browser)", default=False)
parser.add_argument("--share", action='store_true', help="Should share your server on gradio.app, this allows you to use the UI from your mobile app", default=False)
parser.add_argument("--share-password", type=str, help="Sharing is open by default, use this to set a password. Username: webui", default=None)
parser.add_argument("--defaults", type=str, help="path to configuration file providing UI defaults, uses same format as cli parameter", default='configs/webui/webui.yaml')
parser.add_argument("--gpu", type=int, help="choose which GPU to use if you have multiple", default=0)
parser.add_argument("--extra-models-cpu", action='store_true', help="run extra models (GFGPAN/ESRGAN) on cpu", default=False)
parser.add_argument("--extra-models-gpu", action='store_true', help="run extra models (GFGPAN/ESRGAN) on cpu", default=False)
parser.add_argument("--esrgan-cpu", action='store_true', help="run ESRGAN on cpu", default=False)
parser.add_argument("--gfpgan-cpu", action='store_true', help="run GFPGAN on cpu", default=False)
parser.add_argument("--esrgan-gpu", type=int, help="run ESRGAN on specific gpu (overrides --gpu)", default=0)
parser.add_argument("--gfpgan-gpu", type=int, help="run GFPGAN on specific gpu (overrides --gpu) ", default=0)
parser.add_argument("--cli", type=str, help="don't launch web server, take Python function kwargs from this file.", default=None)
opt = parser.parse_args()

#Should not be needed anymore
#os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"   # see issue #152
# all selected gpus, can probably be done nicer
#if opt.extra_models_gpu:
#    gpus = set([opt.gpu, opt.esrgan_gpu, opt.gfpgan_gpu])
#    os.environ["CUDA_VISIBLE_DEVICES"] = ','.join(str(g) for g in set(gpus))
#else: 
#    os.environ["CUDA_VISIBLE_DEVICES"] = str(opt.gpu)

import gradio as gr
import k_diffusion as K
import math
import mimetypes
import numpy as np
import pynvml
import random
import threading, asyncio
import time
import torch
import torch.nn as nn
import yaml
import glob
from typing import List, Union
from pathlib import Path

from contextlib import contextmanager, nullcontext
from einops import rearrange, repeat
from itertools import islice
from omegaconf import OmegaConf
from PIL import Image, ImageFont, ImageDraw, ImageFilter, ImageOps
from PIL.PngImagePlugin import PngInfo
from io import BytesIO
import base64
import re
from torch import autocast
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler
from ldm.util import instantiate_from_config

try:
    # this silences the annoying "Some weights of the model checkpoint were not used when initializing..." message at start.

    from transformers import logging
    logging.set_verbosity_error()
except:
    pass

# this is a fix for Windows users. Without it, javascript files will be served with text/html content-type and the bowser will not show any UI
mimetypes.init()
mimetypes.add_type('application/javascript', '.js')

# some of those options should not be changed at all because they would break the model, so I removed them from options.
opt_C = 4
opt_f = 8

LANCZOS = (Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS)
invalid_filename_chars = '<>:"/\|?*\n'

GFPGAN_dir = opt.gfpgan_dir
RealESRGAN_dir = opt.realesrgan_dir

if opt.optimized_turbo:
    opt.optimized = True

# should probably be moved to a settings menu in the UI at some point
grid_format = [s.lower() for s in opt.grid_format.split(':')]
grid_lossless = False
grid_quality = 100
if grid_format[0] == 'png':
    grid_ext = 'png'
    grid_format = 'png'
elif grid_format[0] in ['jpg', 'jpeg']:
    grid_quality = int(grid_format[1]) if len(grid_format) > 1 else 100
    grid_ext = 'jpg'
    grid_format = 'jpeg'
elif grid_format[0] == 'webp':
    grid_quality = int(grid_format[1]) if len(grid_format) > 1 else 100
    grid_ext = 'webp'
    grid_format = 'webp'
    if grid_quality < 0: # e.g. webp:-100 for lossless mode
        grid_lossless = True
        grid_quality = abs(grid_quality)


def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


def load_model_from_config(config, ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    print("webui.py//132")
    print(ckpt)
    ckpt = "F:\CS_stable-diffusion-main\models\ldm\stable-diffusion-v1/model.ckpt"
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)

    model.cuda()
    model.eval()
    return model

def load_sd_from_config(ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    return sd

def crash(e, s):
    global model
    global device

    print(s, '\n', e)

    del model
    del device

    print('exiting...calling os._exit(0)')
    t = threading.Timer(0.25, os._exit, args=[0])
    t.start()

class MemUsageMonitor(threading.Thread):
    stop_flag = False
    max_usage = 0
    total = -1

    def __init__(self, name):
        threading.Thread.__init__(self)
        self.name = name

    def run(self):
        try:
            pynvml.nvmlInit()
        except:
            print(f"[{self.name}] Unable to initialize NVIDIA management. No memory stats. \n")
            return
        print(f"[{self.name}] Recording max memory usage...\n")
        handle = pynvml.nvmlDeviceGetHandleByIndex(opt.gpu)
        self.total = pynvml.nvmlDeviceGetMemoryInfo(handle).total
        while not self.stop_flag:
            m = pynvml.nvmlDeviceGetMemoryInfo(handle)
            self.max_usage = max(self.max_usage, m.used)
            # print(self.max_usage)
            time.sleep(0.1)
        print(f"[{self.name}] Stopped recording.\n")
        pynvml.nvmlShutdown()

    def read(self):
        return self.max_usage, self.total

    def stop(self):
        self.stop_flag = True

    def read_and_stop(self):
        self.stop_flag = True
        return self.max_usage, self.total

class CFGDenoiser(nn.Module):
    def __init__(self, model):
        super().__init__()
        self.inner_model = model

    def forward(self, x, sigma, uncond, cond, cond_scale):
        x_in = torch.cat([x] * 2)
        sigma_in = torch.cat([sigma] * 2)
        cond_in = torch.cat([uncond, cond])
        uncond, cond = self.inner_model(x_in, sigma_in, cond=cond_in).chunk(2)
        return uncond + (cond - uncond) * cond_scale


class KDiffusionSampler:
    def __init__(self, m, sampler):
        self.model = m
        self.model_wrap = K.external.CompVisDenoiser(m)
        self.schedule = sampler
    def get_sampler_name(self):
        return self.schedule
    def sample(self, S, conditioning, batch_size, shape, verbose, unconditional_guidance_scale, unconditional_conditioning, eta, x_T):
        sigmas = self.model_wrap.get_sigmas(S)
        x = x_T * sigmas[0]
        model_wrap_cfg = CFGDenoiser(self.model_wrap)

        samples_ddim = K.sampling.__dict__[f'sample_{self.schedule}'](model_wrap_cfg, x, sigmas, extra_args={'cond': conditioning, 'uncond': unconditional_conditioning, 'cond_scale': unconditional_guidance_scale}, disable=False)

        return samples_ddim, None


def create_random_tensors(shape, seeds):
    xs = []
    for seed in seeds:
        torch.manual_seed(seed)

        # randn results depend on device; gpu and cpu get different results for same seed;
        # the way I see it, it's better to do this on CPU, so that everyone gets same result;
        # but the original script had it like this so i do not dare change it for now because
        # it will break everyone's seeds.
        xs.append(torch.randn(shape, device=device))
    x = torch.stack(xs)
    return x

def torch_gc():
    torch.cuda.empty_cache()
    torch.cuda.ipc_collect()

def load_GFPGAN():
    model_name = 'GFPGANv1.3'
    model_path = os.path.join(GFPGAN_dir, 'experiments/pretrained_models', model_name + '.pth')
    if not os.path.isfile(model_path):
        raise Exception("GFPGAN model not found at path "+model_path)

    sys.path.append(os.path.abspath(GFPGAN_dir))
    from gfpgan import GFPGANer
    
    if opt.gfpgan_cpu or opt.extra_models_cpu:
        instance = GFPGANer(model_path=model_path, upscale=1, arch='clean', channel_multiplier=2, bg_upsampler=None, device=torch.device('cpu'))
    elif opt.extra_models_gpu:
        instance = GFPGANer(model_path=model_path, upscale=1, arch='clean', channel_multiplier=2, bg_upsampler=None, device=torch.device(f'cuda:{opt.gfpgan_gpu}'))
    else:
        instance = GFPGANer(model_path=model_path, upscale=1, arch='clean', channel_multiplier=2, bg_upsampler=None, device=torch.device(f'cuda:{opt.gpu}'))
    return instance

def load_RealESRGAN(model_name: str):
    from basicsr.archs.rrdbnet_arch import RRDBNet
    RealESRGAN_models = {
        'RealESRGAN_x4plus': RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=4),
        'RealESRGAN_x4plus_anime_6B': RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=6, num_grow_ch=32, scale=4)
    }

    model_path = os.path.join(RealESRGAN_dir, 'experiments/pretrained_models', model_name + '.pth')
    if not os.path.isfile(model_path):
        raise Exception(model_name+".pth not found at path "+model_path)

    sys.path.append(os.path.abspath(RealESRGAN_dir))
    from realesrgan import RealESRGANer

    if opt.esrgan_cpu or opt.extra_models_cpu:
        instance = RealESRGANer(scale=2, model_path=model_path, model=RealESRGAN_models[model_name], pre_pad=0, half=False) # cpu does not support half
        instance.device = torch.device('cpu')
        instance.model.to('cpu')
    elif opt.extra_models_gpu:
        instance = RealESRGANer(scale=2, model_path=model_path, model=RealESRGAN_models[model_name], pre_pad=0, half=not opt.no_half, device=torch.device(f'cuda:{opt.esrgan_gpu}'))
    else:
        instance = RealESRGANer(scale=2, model_path=model_path, model=RealESRGAN_models[model_name], pre_pad=0, half=not opt.no_half, device=torch.device(f'cuda:{opt.gpu}'))
    instance.model.name = model_name
    return instance

GFPGAN = None
if os.path.exists(GFPGAN_dir):
    try:
        GFPGAN = load_GFPGAN()
        print("Loaded GFPGAN")
    except Exception:
        import traceback
        print("Error loading GFPGAN:", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)

RealESRGAN = None
def try_loading_RealESRGAN(model_name: str):
    global RealESRGAN
    if os.path.exists(RealESRGAN_dir):
        try:
            RealESRGAN = load_RealESRGAN(model_name) # TODO: Should try to load both models before giving up
            print("Loaded RealESRGAN with model "+RealESRGAN.model.name)
        except Exception:
            import traceback
            print("Error loading RealESRGAN:", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
try_loading_RealESRGAN('RealESRGAN_x4plus')

if opt.optimized:
    sd = load_sd_from_config(opt.ckpt)
    li, lo = [], []
    for key, v_ in sd.items():
        sp = key.split('.')
        if(sp[0]) == 'model':
            if('input_blocks' in sp):
                li.append(key)
            elif('middle_block' in sp):
                li.append(key)
            elif('time_embed' in sp):
                li.append(key)
            else:
                lo.append(key)
    for key in li:
        sd['model1.' + key[6:]] = sd.pop(key)
    for key in lo:
        sd['model2.' + key[6:]] = sd.pop(key)

    config = OmegaConf.load("optimizedSD/v1-inference.yaml")
    device = torch.device(f"cuda:{opt.gpu}") if torch.cuda.is_available() else torch.device("cpu")

    model = instantiate_from_config(config.modelUNet)
    _, _ = model.load_state_dict(sd, strict=False)
    model.cuda()
    model.eval()
    model.turbo = opt.optimized_turbo

    modelCS = instantiate_from_config(config.modelCondStage)
    _, _ = modelCS.load_state_dict(sd, strict=False)
    modelCS.cond_stage_model.device = device
    modelCS.eval()
        
    modelFS = instantiate_from_config(config.modelFirstStage)
    _, _ = modelFS.load_state_dict(sd, strict=False)
    modelFS.eval()

    del sd

    if not opt.no_half:
        model = model.half()
        modelCS = modelCS.half()
        modelFS = modelFS.half()
else:
    config = OmegaConf.load(opt.config)
    model = load_model_from_config(config, opt.ckpt)

    device = torch.device(f"cuda:{opt.gpu}") if torch.cuda.is_available() else torch.device("cpu")
    model = (model if opt.no_half else model.half()).to(device)

def load_embeddings(fp):
    if fp is not None and hasattr(model, "embedding_manager"):
        model.embedding_manager.load(fp.name)


def get_font(fontsize):
    fonts = ["arial.ttf", "DejaVuSans.ttf"]
    for font_name in fonts:
        try:
            return ImageFont.truetype(font_name, fontsize)
        except OSError:
           pass

    # ImageFont.load_default() is practically unusable as it only supports
    # latin1, so raise an exception instead if no usable font was found
    raise Exception(f"No usable font found (tried {', '.join(fonts)})")

def image_grid(imgs, batch_size, force_n_rows=None, captions=None):
    if force_n_rows is not None:
        rows = force_n_rows
    elif opt.n_rows > 0:
        rows = opt.n_rows
    elif opt.n_rows == 0:
        rows = batch_size
    else:
        rows = math.sqrt(len(imgs))
        rows = round(rows)

    cols = math.ceil(len(imgs) / rows)

    w, h = imgs[0].size
    grid = Image.new('RGB', size=(cols * w, rows * h), color='black')

    fnt = get_font(30)

    for i, img in enumerate(imgs):
        grid.paste(img, box=(i % cols * w, i // cols * h))
        if captions:
            d = ImageDraw.Draw( grid )
            size = d.textbbox( (0,0), captions[i], font=fnt, stroke_width=2, align="center" )
            d.multiline_text((i % cols * w + w/2, i // cols * h + h - size[3]), captions[i], font=fnt, fill=(255,255,255), stroke_width=2, stroke_fill=(0,0,0), anchor="mm", align="center")

    return grid

def seed_to_int(s):
    if type(s) is int:
        return s
    if s is None or s == '':
        return random.randint(0, 2**32 - 1)
    n = abs(int(s) if s.isdigit() else random.Random(s).randint(0, 2**32 - 1))
    while n >= 2**32:
        n = n >> 32
    return n

def draw_prompt_matrix(im, width, height, all_prompts):
    def wrap(text, d, font, line_length):
        lines = ['']
        for word in text.split():
            line = f'{lines[-1]} {word}'.strip()
            if d.textlength(line, font=font) <= line_length:
                lines[-1] = line
            else:
                lines.append(word)
        return '\n'.join(lines)

    def draw_texts(pos, x, y, texts, sizes):
        for i, (text, size) in enumerate(zip(texts, sizes)):
            active = pos & (1 << i) != 0

            if not active:
                text = '\u0336'.join(text) + '\u0336'

            d.multiline_text((x, y + size[1] / 2), text, font=fnt, fill=color_active if active else color_inactive, anchor="mm", align="center")

            y += size[1] + line_spacing

    fontsize = (width + height) // 25
    line_spacing = fontsize // 2
    fnt = get_font(fontsize)
    color_active = (0, 0, 0)
    color_inactive = (153, 153, 153)

    pad_top = height // 4
    pad_left = width * 3 // 4 if len(all_prompts) > 2 else 0

    cols = im.width // width
    rows = im.height // height

    prompts = all_prompts[1:]

    result = Image.new("RGB", (im.width + pad_left, im.height + pad_top), "white")
    result.paste(im, (pad_left, pad_top))

    d = ImageDraw.Draw(result)

    boundary = math.ceil(len(prompts) / 2)
    prompts_horiz = [wrap(x, d, fnt, width) for x in prompts[:boundary]]
    prompts_vert = [wrap(x, d, fnt, pad_left) for x in prompts[boundary:]]

    sizes_hor = [(x[2] - x[0], x[3] - x[1]) for x in [d.multiline_textbbox((0, 0), x, font=fnt) for x in prompts_horiz]]
    sizes_ver = [(x[2] - x[0], x[3] - x[1]) for x in [d.multiline_textbbox((0, 0), x, font=fnt) for x in prompts_vert]]
    hor_text_height = sum([x[1] + line_spacing for x in sizes_hor]) - line_spacing
    ver_text_height = sum([x[1] + line_spacing for x in sizes_ver]) - line_spacing

    for col in range(cols):
        x = pad_left + width * col + width / 2
        y = pad_top / 2 - hor_text_height / 2

        draw_texts(col, x, y, prompts_horiz, sizes_hor)

    for row in range(rows):
        x = pad_left / 2
        y = pad_top + height * row + height / 2 - ver_text_height / 2

        draw_texts(row, x, y, prompts_vert, sizes_ver)

    return result




def check_prompt_length(prompt, comments):
    """this function tests if prompt is too long, and if so, adds a message to comments"""

    tokenizer = (model if not opt.optimized else modelCS).cond_stage_model.tokenizer
    max_length = (model if not opt.optimized else modelCS).cond_stage_model.max_length

    info = (model if not opt.optimized else modelCS).cond_stage_model.tokenizer([prompt], truncation=True, max_length=max_length, return_overflowing_tokens=True, padding="max_length", return_tensors="pt")
    ovf = info['overflowing_tokens'][0]
    overflowing_count = ovf.shape[0]
    if overflowing_count == 0:
        return

    vocab = {v: k for k, v in tokenizer.get_vocab().items()}
    overflowing_words = [vocab.get(int(x), "") for x in ovf]
    overflowing_text = tokenizer.convert_tokens_to_string(''.join(overflowing_words))

    comments.append(f"Warning: too many input tokens; some ({len(overflowing_words)}) have been truncated:\n{overflowing_text}\n")

def save_sample(image, sample_path_i, filename, jpg_sample, prompts, seeds, width, height, steps, cfg_scale, 
normalize_prompt_weights, use_GFPGAN, write_info_files, prompt_matrix, init_img, uses_loopback, uses_random_seed_loopback, skip_save,
skip_grid, sort_samples, sampler_name, ddim_eta, n_iter, batch_size, i, denoising_strength, resize_mode):
    filename_i = os.path.join(sample_path_i, filename)
    if not jpg_sample:
        if opt.save_metadata:
            metadata = PngInfo()
            metadata.add_text("SD:prompt", prompts[i])
            metadata.add_text("SD:seed", str(seeds[i]))
            metadata.add_text("SD:width", str(width))
            metadata.add_text("SD:height", str(height))
            metadata.add_text("SD:steps", str(steps))
            metadata.add_text("SD:cfg_scale", str(cfg_scale))
            metadata.add_text("SD:normalize_prompt_weights", str(normalize_prompt_weights))
            if init_img is not None:
                metadata.add_text("SD:denoising_strength", str(denoising_strength))
            metadata.add_text("SD:GFPGAN", str(use_GFPGAN and GFPGAN is not None))
            image.save(f"{filename_i}.png", pnginfo=metadata)
        else:
            image.save(f"{filename_i}.png")
    else:
        image.save(f"{filename_i}.jpg", 'jpeg', quality=100, optimize=True)
    if write_info_files:
        # toggles differ for txt2img vs. img2img:
        offset = 0 if init_img is None else 2
        toggles = []
        if prompt_matrix:
            toggles.append(0)
        if normalize_prompt_weights:
            toggles.append(1)
        if init_img is not None:
            if uses_loopback:
                toggles.append(2)
            if uses_random_seed_loopback:
                toggles.append(3)
        if not skip_save:
            toggles.append(2 + offset)
        if not skip_grid:
            toggles.append(3 + offset)
        if sort_samples:
            toggles.append(4 + offset)
        if write_info_files:
            toggles.append(5 + offset)
        if use_GFPGAN:
            toggles.append(6 + offset)
        info_dict = dict(
            target="txt2img" if init_img is None else "img2img",
            prompt=prompts[i], ddim_steps=steps, toggles=toggles, sampler_name=sampler_name,
            ddim_eta=ddim_eta, n_iter=n_iter, batch_size=batch_size, cfg_scale=cfg_scale,
            seed=seeds[i], width=width, height=height
        )
        if init_img is not None:
            # Not yet any use for these, but they bloat up the files:
            #info_dict["init_img"] = init_img
            #info_dict["init_mask"] = init_mask
            info_dict["denoising_strength"] = denoising_strength
            info_dict["resize_mode"] = resize_mode
        with open(f"{filename_i}.yaml", "w", encoding="utf8") as f:
            yaml.dump(info_dict, f, allow_unicode=True)


def get_next_sequence_number(path, prefix=''):
    """
    Determines and returns the next sequence number to use when saving an
    image in the specified directory.

    If a prefix is given, only consider files whose names start with that
    prefix, and strip the prefix from filenames before extracting their
    sequence number.

    The sequence starts at 0.
    """
    result = -1
    for p in Path(path).iterdir():
        if p.name.endswith(('.png', '.jpg')) and p.name.startswith(prefix):
            tmp = p.name[len(prefix):]
            try:
                result = max(int(tmp.split('-')[0]), result)
            except ValueError:
                pass
    return result + 1


def oxlamon_matrix(prompt, seed, n_iter, batch_size):
    pattern = re.compile(r'(,\s){2,}')

    class PromptItem:
        def __init__(self, text, parts, item):
            self.text = text
            self.parts = parts
            if item:
                self.parts.append( item )

    def clean(txt):
        return re.sub(pattern, ', ', txt)

    def getrowcount( txt ):
        for data in re.finditer( ".*?\\((.*?)\\).*", txt ):
            if data:
                return len(data.group(1).split("|"))
            break
        return None

    def repliter( txt ):
        for data in re.finditer( ".*?\\((.*?)\\).*", txt ):
            if data:
                r = data.span(1)
                for item in data.group(1).split("|"):
                    yield (clean(txt[:r[0]-1] + item.strip() + txt[r[1]+1:]), item.strip())
            break

    def iterlist( items ):
        outitems = []
        for item in items:
            for newitem, newpart in repliter(item.text):
                outitems.append( PromptItem(newitem, item.parts.copy(), newpart) )

        return outitems

    def getmatrix( prompt ):
        dataitems = [ PromptItem( prompt[1:].strip(), [], None ) ]
        while True:
            newdataitems = iterlist( dataitems )
            if len( newdataitems ) == 0:
                return dataitems
            dataitems = newdataitems

    def classToArrays( items, seed, n_iter ):
        texts = []
        parts = []
        seeds = []

        for item in items:
            itemseed = seed
            for i in range(n_iter):
                texts.append( item.text )
                parts.append( f"Seed: {itemseed}\n" + "\n".join(item.parts) )
                seeds.append( itemseed )
                itemseed += 1                

        return seeds, texts, parts

    all_seeds, all_prompts, prompt_matrix_parts = classToArrays(getmatrix( prompt ), seed, n_iter)
    n_iter = math.ceil(len(all_prompts) / batch_size)

    needrows = getrowcount(prompt)
    if needrows:
        xrows = math.sqrt(len(all_prompts))
        xrows = round(xrows)
        # if columns is to much
        cols = math.ceil(len(all_prompts) / xrows)
        if cols > needrows*4:
            needrows *= 2

    return all_seeds, n_iter, prompt_matrix_parts, all_prompts, needrows



def process_images(
        outpath, func_init, func_sample, prompt, seed, sampler_name, skip_grid, skip_save, batch_size,
        n_iter, steps, cfg_scale, width, height, prompt_matrix, use_GFPGAN, use_RealESRGAN, realesrgan_model_name,
        fp, ddim_eta=0.0, do_not_save_grid=False, normalize_prompt_weights=True, init_img=None, init_mask=None,
        keep_mask=False, mask_blur_strength=3, denoising_strength=0.75, resize_mode=None, uses_loopback=False,
        uses_random_seed_loopback=False, sort_samples=True, write_info_files=True, jpg_sample=False):
    """this is the main loop that both txt2img and img2img use; it calls func_init once inside all the scopes and func_sample once per batch"""
    assert prompt is not None
    torch_gc()
    # start time after garbage collection (or before?)
    start_time = time.time()

    mem_mon = MemUsageMonitor('MemMon')
    mem_mon.start()

    if hasattr(model, "embedding_manager"):
        load_embeddings(fp)

    os.makedirs(outpath, exist_ok=True)

    sample_path = os.path.join(outpath, "samples")
    os.makedirs(sample_path, exist_ok=True)

    if not ("|" in prompt) and prompt.startswith("@"):
        prompt = prompt[1:]

    comments = []

    prompt_matrix_parts = []
    if prompt_matrix:
        if prompt.startswith("@"):
            all_seeds, n_iter, prompt_matrix_parts, all_prompts, frows = oxlamon_matrix(prompt, seed, n_iter, batch_size)
        else:
            all_prompts = []
            prompt_matrix_parts = prompt.split("|")
            combination_count = 2 ** (len(prompt_matrix_parts) - 1)
            for combination_num in range(combination_count):
                current = prompt_matrix_parts[0]

                for n, text in enumerate(prompt_matrix_parts[1:]):
                    if combination_num & (2 ** n) > 0:
                        current += ("" if text.strip().startswith(",") else ", ") + text

                all_prompts.append(current)

            n_iter = math.ceil(len(all_prompts) / batch_size)
            all_seeds = len(all_prompts) * [seed]

        print(f"Prompt matrix will create {len(all_prompts)} images using a total of {n_iter} batches.")
    else:

        if not opt.no_verify_input:
            try:
                check_prompt_length(prompt, comments)
            except:
                import traceback
                print("Error verifying input:", file=sys.stderr)
                print(traceback.format_exc(), file=sys.stderr)

        all_prompts = batch_size * n_iter * [prompt]
        all_seeds = [seed + x for x in range(len(all_prompts))]

    precision_scope = autocast if opt.precision == "autocast" else nullcontext
    output_images = []
    stats = []
    with torch.no_grad(), precision_scope("cuda"), (model.ema_scope() if not opt.optimized else nullcontext()):
        init_data = func_init()
        tic = time.time()

        for n in range(n_iter):
            print(f"Iteration: {n+1}/{n_iter}")
            prompts = all_prompts[n * batch_size:(n + 1) * batch_size]
            seeds = all_seeds[n * batch_size:(n + 1) * batch_size]

            if opt.optimized:
                modelCS.to(device)
            uc = (model if not opt.optimized else modelCS).get_learned_conditioning(len(prompts) * [""])
            if isinstance(prompts, tuple):
                prompts = list(prompts)

            # split the prompt if it has : for weighting
            # TODO for speed it might help to have this occur when all_prompts filled??
            subprompts,weights = split_weighted_subprompts(prompts[0])
            # get total weight for normalizing, this gets weird if large negative values used
            totalPromptWeight = sum(weights)

            # sub-prompt weighting used if more than 1
            if len(subprompts) > 1:
                c = torch.zeros_like(uc) # i dont know if this is correct.. but it works
                for i in range(0,len(subprompts)): # normalize each prompt and add it
                    weight = weights[i]
                    if normalize_prompt_weights:
                        weight = weight / totalPromptWeight
                    #print(f"{subprompts[i]} {weight*100.0}%")
                    # note if alpha negative, it functions same as torch.sub
                    c = torch.add(c, (model if not opt.optimized else modelCS).get_learned_conditioning(subprompts[i]), alpha=weight)
            else: # just behave like usual
                c = (model if not opt.optimized else modelCS).get_learned_conditioning(prompts)

            shape = [opt_C, height // opt_f, width // opt_f]

            if opt.optimized:
                mem = torch.cuda.memory_allocated()/1e6
                modelCS.to("cpu")
                while(torch.cuda.memory_allocated()/1e6 >= mem):
                    time.sleep(1)

            # we manually generate all input noises because each one should have a specific seed
            x = create_random_tensors([opt_C, height // opt_f, width // opt_f], seeds=seeds)
            samples_ddim = func_sample(init_data=init_data, x=x, conditioning=c, unconditional_conditioning=uc, sampler_name=sampler_name)

            if opt.optimized:
                modelFS.to(device)

            

            x_samples_ddim = (model if not opt.optimized else modelFS).decode_first_stage(samples_ddim)
            x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
            for i, x_sample in enumerate(x_samples_ddim):
                sanitized_prompt = prompts[i].replace(' ', '_').translate({ord(x): '' for x in invalid_filename_chars})
                if sort_samples:
                    sanitized_prompt = sanitized_prompt[:128] #200 is too long
                    sample_path_i = os.path.join(sample_path, sanitized_prompt)
                    os.makedirs(sample_path_i, exist_ok=True)
                    base_count = get_next_sequence_number(sample_path_i)
                    filename = f"{base_count:05}-{steps}_{sampler_name}_{seeds[i]}"
                else:
                    sample_path_i = sample_path
                    base_count = get_next_sequence_number(sample_path_i)
                    sanitized_prompt = sanitized_prompt
                    filename = f"{base_count:05}-{steps}_{sampler_name}_{seeds[i]}_{sanitized_prompt}"[:128] #same as before

                x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                x_sample = x_sample.astype(np.uint8)
                original_sample = x_sample
                original_filename = filename
                if use_GFPGAN and GFPGAN is not None and not use_RealESRGAN:
                    torch_gc()
                    cropped_faces, restored_faces, restored_img = GFPGAN.enhance(x_sample[:,:,::-1], has_aligned=False, only_center_face=False, paste_back=True)
                    gfpgan_sample = restored_img[:,:,::-1]
                    image = Image.fromarray(gfpgan_sample)
                    gfpgan_filename = original_filename + '-gfpgan'
                    save_sample(image, sample_path_i, gfpgan_filename, jpg_sample, prompts, seeds, width, height, steps, cfg_scale, 
normalize_prompt_weights, use_GFPGAN, write_info_files, prompt_matrix, init_img, uses_loopback, uses_random_seed_loopback, skip_save,
skip_grid, sort_samples, sampler_name, ddim_eta, n_iter, batch_size, i, denoising_strength, resize_mode)
                    x_sample = gfpgan_sample

                if use_RealESRGAN and RealESRGAN is not None and not use_GFPGAN:
                    torch_gc()
                    if RealESRGAN.model.name != realesrgan_model_name:
                        try_loading_RealESRGAN(realesrgan_model_name)
                    output, img_mode = RealESRGAN.enhance(x_sample[:,:,::-1])
                    esrgan_filename = original_filename + '-esrgan4x'
                    esrgan_sample = output[:,:,::-1]
                    image = Image.fromarray(esrgan_sample)
                    save_sample(image, sample_path_i, esrgan_filename, jpg_sample, prompts, seeds, width, height, steps, cfg_scale, 
normalize_prompt_weights, use_GFPGAN, write_info_files, prompt_matrix, init_img, uses_loopback, uses_random_seed_loopback, skip_save,
skip_grid, sort_samples, sampler_name, ddim_eta, n_iter, batch_size, i, denoising_strength, resize_mode)
                    x_sample = esrgan_sample

                if use_RealESRGAN and RealESRGAN is not None and use_GFPGAN and GFPGAN is not None:
                    torch_gc()
                    cropped_faces, restored_faces, restored_img = GFPGAN.enhance(x_sample[:,:,::-1], has_aligned=False, only_center_face=False, paste_back=True)
                    gfpgan_sample = restored_img[:,:,::-1]
                    if RealESRGAN.model.name != realesrgan_model_name:
                        try_loading_RealESRGAN(realesrgan_model_name)
                    output, img_mode = RealESRGAN.enhance(gfpgan_sample[:,:,::-1])
                    gfpgan_esrgan_filename = original_filename + '-gfpgan-esrgan4x'
                    gfpgan_esrgan_sample = output[:,:,::-1]
                    image = Image.fromarray(gfpgan_esrgan_sample)
                    save_sample(image, sample_path_i, gfpgan_esrgan_filename, jpg_sample, prompts, seeds, width, height, steps, cfg_scale, 
normalize_prompt_weights, use_GFPGAN, write_info_files, prompt_matrix, init_img, uses_loopback, uses_random_seed_loopback, skip_save,
skip_grid, sort_samples, sampler_name, ddim_eta, n_iter, batch_size, i, denoising_strength, resize_mode)
                    x_sample = gfpgan_esrgan_sample

                image = Image.fromarray(x_sample)
                if init_mask:
                    #init_mask = init_mask if keep_mask else ImageOps.invert(init_mask)
                    init_mask = init_mask.filter(ImageFilter.GaussianBlur(mask_blur_strength))
                    init_mask = init_mask.convert('L')
                    init_img = init_img.convert('RGB')
                    image = image.convert('RGB')

                    if use_RealESRGAN and RealESRGAN is not None:
                        if RealESRGAN.model.name != realesrgan_model_name:
                            try_loading_RealESRGAN(realesrgan_model_name)
                        output, img_mode = RealESRGAN.enhance(np.array(init_img, dtype=np.uint8))
                        init_img = Image.fromarray(output)
                        init_img = init_img.convert('RGB')

                        output, img_mode = RealESRGAN.enhance(np.array(init_mask, dtype=np.uint8))
                        init_mask = Image.fromarray(output)
                        init_mask = init_mask.convert('L')

                    image = Image.composite(init_img, image, init_mask)
                if not skip_save:
                    save_sample(image, sample_path_i, filename, jpg_sample, prompts, seeds, width, height, steps, cfg_scale, 
normalize_prompt_weights, use_GFPGAN, write_info_files, prompt_matrix, init_img, uses_loopback, uses_random_seed_loopback, skip_save,
skip_grid, sort_samples, sampler_name, ddim_eta, n_iter, batch_size, i, denoising_strength, resize_mode)

                output_images.append(image)
                if opt.optimized:
                    mem = torch.cuda.memory_allocated()/1e6
                    modelFS.to("cpu")
                    while(torch.cuda.memory_allocated()/1e6 >= mem):
                        time.sleep(1)

        if (prompt_matrix or not skip_grid) and not do_not_save_grid:
            if prompt_matrix:
                if prompt.startswith("@"):
                    grid = image_grid(output_images, batch_size, force_n_rows=frows, captions=prompt_matrix_parts)
                else:
                    grid = image_grid(output_images, batch_size, force_n_rows=1 << ((len(prompt_matrix_parts)-1)//2))
                    try:
                        grid = draw_prompt_matrix(grid, width, height, prompt_matrix_parts)
                    except:
                        import traceback
                        print("Error creating prompt_matrix text:", file=sys.stderr)
                        print(traceback.format_exc(), file=sys.stderr)
            else:
                grid = image_grid(output_images, batch_size)
 
            if grid and (batch_size > 1  or n_iter > 1):
                output_images.insert(0, grid)

            grid_count = get_next_sequence_number(outpath, 'grid-')
            grid_file = f"grid-{grid_count:05}-{seed}_{prompts[i].replace(' ', '_').translate({ord(x): '' for x in invalid_filename_chars})[:128]}.{grid_ext}"
            grid.save(os.path.join(outpath, grid_file), grid_format, quality=grid_quality, lossless=grid_lossless, optimize=True)

        toc = time.time()

    mem_max_used, mem_total = mem_mon.read_and_stop()
    time_diff = time.time()-start_time

    info = f"""
{prompt}
Steps: {steps}, Sampler: {sampler_name}, CFG scale: {cfg_scale}, Seed: {seed}{', Denoising strength: '+str(denoising_strength) if init_img is not None else ''}{', GFPGAN' if use_GFPGAN and GFPGAN is not None else ''}{', '+realesrgan_model_name if use_RealESRGAN and RealESRGAN is not None else ''}{', Prompt Matrix Mode.' if prompt_matrix else ''}""".strip()
    stats = f'''
Took { round(time_diff, 2) }s total ({ round(time_diff/(len(all_prompts)),2) }s per image)
Peak memory usage: { -(mem_max_used // -1_048_576) } MiB / { -(mem_total // -1_048_576) } MiB / { round(mem_max_used/mem_total*100, 3) }%'''

    for comment in comments:
        info += "\n\n" + comment

    #mem_mon.stop()
    #del mem_mon
    torch_gc()

    return output_images, seed, info, stats


def txt2img(prompt: str, ddim_steps: int, sampler_name: str, toggles: List[int], realesrgan_model_name: str,
            ddim_eta: float, n_iter: int, batch_size: int, cfg_scale: float, seed: Union[int, str, None],
            height: int, width: int, fp):
    outpath = opt.outdir_txt2img or opt.outdir or "outputs/txt2img-samples"
    err = False
    seed = seed_to_int(seed)

    prompt_matrix = 0 in toggles
    normalize_prompt_weights = 1 in toggles
    skip_save = 2 not in toggles
    skip_grid = 3 not in toggles
    sort_samples = 4 in toggles
    write_info_files = 5 in toggles
    jpg_sample = 6 in toggles
    use_GFPGAN = 7 in toggles
    use_RealESRGAN = 8 in toggles

    if sampler_name == 'PLMS':
        sampler = PLMSSampler(model)
    elif sampler_name == 'DDIM':
        sampler = DDIMSampler(model)
    elif sampler_name == 'k_dpm_2_a':
        sampler = KDiffusionSampler(model,'dpm_2_ancestral')
    elif sampler_name == 'k_dpm_2':
        sampler = KDiffusionSampler(model,'dpm_2')
    elif sampler_name == 'k_euler_a':
        sampler = KDiffusionSampler(model,'euler_ancestral')
    elif sampler_name == 'k_euler':
        sampler = KDiffusionSampler(model,'euler')
    elif sampler_name == 'k_heun':
        sampler = KDiffusionSampler(model,'heun')
    elif sampler_name == 'k_lms':
        sampler = KDiffusionSampler(model,'lms')
    else:
        raise Exception("Unknown sampler: " + sampler_name)

    def init():
        pass

    def sample(init_data, x, conditioning, unconditional_conditioning, sampler_name):
        samples_ddim, _ = sampler.sample(S=ddim_steps, conditioning=conditioning, batch_size=int(x.shape[0]), shape=x[0].shape, verbose=False, unconditional_guidance_scale=cfg_scale, unconditional_conditioning=unconditional_conditioning, eta=ddim_eta, x_T=x)
        return samples_ddim

    try:
        output_images, seed, info, stats = process_images(
            outpath=outpath,
            func_init=init,
            func_sample=sample,
            prompt=prompt,
            seed=seed,
            sampler_name=sampler_name,
            skip_save=skip_save,
            skip_grid=skip_grid,
            batch_size=batch_size,
            n_iter=n_iter,
            steps=ddim_steps,
            cfg_scale=cfg_scale,
            width=width,
            height=height,
            prompt_matrix=prompt_matrix,
            use_GFPGAN=use_GFPGAN,
            use_RealESRGAN=use_RealESRGAN,
            realesrgan_model_name=realesrgan_model_name,
            fp=fp,
            ddim_eta=ddim_eta,
            normalize_prompt_weights=normalize_prompt_weights,
            sort_samples=sort_samples,
            write_info_files=write_info_files,
            jpg_sample=jpg_sample,
        )

        del sampler

        return output_images, seed, info, stats
    except RuntimeError as e:
        err = e
        err_msg = f'CRASHED:<br><textarea rows="5" style="color:white;background: black;width: -webkit-fill-available;font-family: monospace;font-size: small;font-weight: bold;">{str(e)}</textarea><br><br>Please wait while the program restarts.'
        stats = err_msg
        return [], seed, 'err', stats
    finally:
        if err:
            crash(err, '!!Runtime error (txt2img)!!')


class Flagging(gr.FlaggingCallback):

    def setup(self, components, flagging_dir: str):
        pass

    def flag(self, flag_data, flag_option=None, flag_index=None, username=None):
        import csv

        os.makedirs("log/images", exist_ok=True)

        # those must match the "txt2img" function !! + images, seed, comment, stats !! NOTE: changes to UI output must be reflected here too
        prompt, ddim_steps, sampler_name, toggles, ddim_eta, n_iter, batch_size, cfg_scale, seed, height, width, fp, images, seed, comment, stats = flag_data

        filenames = []

        with open("log/log.csv", "a", encoding="utf8", newline='') as file:
            import time
            import base64

            at_start = file.tell() == 0
            writer = csv.writer(file)
            if at_start:
                writer.writerow(["sep=,"])
                writer.writerow(["prompt", "seed", "width", "height", "sampler", "toggles", "n_iter", "n_samples", "cfg_scale", "steps", "filename"])

            filename_base = str(int(time.time() * 1000))
            for i, filedata in enumerate(images):
                filename = "log/images/"+filename_base + ("" if len(images) == 1 else "-"+str(i+1)) + ".png"

                if filedata.startswith("data:image/png;base64,"):
                    filedata = filedata[len("data:image/png;base64,"):]

                with open(filename, "wb") as imgfile:
                    imgfile.write(base64.decodebytes(filedata.encode('utf-8')))

                filenames.append(filename)

            writer.writerow([prompt, seed, width, height, sampler_name, toggles, n_iter, batch_size, cfg_scale, ddim_steps, filenames[0]])

        print("Logged:", filenames[0])


def img2img(prompt: str, image_editor_mode: str, init_info, mask_mode: str, mask_blur_strength: int, ddim_steps: int, sampler_name: str,
            toggles: List[int], realesrgan_model_name: str, n_iter: int, batch_size: int, cfg_scale: float, denoising_strength: float,
            seed: int, height: int, width: int, resize_mode: int, fp):
    outpath = opt.outdir_img2img or opt.outdir or "outputs/img2img-samples"
    err = False
    seed = seed_to_int(seed)

    prompt_matrix = 0 in toggles
    normalize_prompt_weights = 1 in toggles
    loopback = 2 in toggles
    random_seed_loopback = 3 in toggles
    skip_save = 4 not in toggles
    skip_grid = 5 not in toggles
    sort_samples = 6 in toggles
    write_info_files = 7 in toggles
    jpg_sample = 8 in toggles
    use_GFPGAN = 9 in toggles
    use_RealESRGAN = 10 in toggles

    if sampler_name == 'DDIM':
        sampler = DDIMSampler(model)
    elif sampler_name == 'k_dpm_2_a':
        sampler = KDiffusionSampler(model,'dpm_2_ancestral')
    elif sampler_name == 'k_dpm_2':
        sampler = KDiffusionSampler(model,'dpm_2')
    elif sampler_name == 'k_euler_a':
        sampler = KDiffusionSampler(model,'euler_ancestral')
    elif sampler_name == 'k_euler':
        sampler = KDiffusionSampler(model,'euler')
    elif sampler_name == 'k_heun':
        sampler = KDiffusionSampler(model,'heun')
    elif sampler_name == 'k_lms':
        sampler = KDiffusionSampler(model,'lms')
    else:
        raise Exception("Unknown sampler: " + sampler_name)

    if image_editor_mode == 'Mask':
        init_img = init_info["image"]
        init_img = init_img.convert("RGB")
        init_img = resize_image(resize_mode, init_img, width, height)
        init_mask = init_info["mask"]
        init_mask = init_mask.convert("RGB")
        init_mask = resize_image(resize_mode, init_mask, width, height)
        keep_mask = mask_mode == 0
        init_mask = init_mask if keep_mask else ImageOps.invert(init_mask)
    else:
        init_img = init_info
        init_mask = None
        keep_mask = False

    assert 0. <= denoising_strength <= 1., 'can only work with strength in [0.0, 1.0]'
    t_enc = int(denoising_strength * ddim_steps)

    def init():
        image = init_img.convert("RGB")
        image = resize_image(resize_mode, image, width, height)
        image = np.array(image).astype(np.float32) / 255.0
        image = image[None].transpose(0, 3, 1, 2)
        image = torch.from_numpy(image)

        if opt.optimized:
            modelFS.to(device)

        init_image = 2. * image - 1.
        init_image = init_image.to(device)
        init_image = repeat(init_image, '1 ... -> b ...', b=batch_size)
        init_latent = (model if not opt.optimized else modelFS).get_first_stage_encoding((model if not opt.optimized else modelFS).encode_first_stage(init_image))  # move to latent space
        
        if opt.optimized:
            mem = torch.cuda.memory_allocated()/1e6
            modelFS.to("cpu")
            while(torch.cuda.memory_allocated()/1e6 >= mem):
                time.sleep(1)

        return init_latent,

    def sample(init_data, x, conditioning, unconditional_conditioning, sampler_name):
        if sampler_name != 'DDIM':
            x0, = init_data

            sigmas = sampler.model_wrap.get_sigmas(ddim_steps)
            noise = x * sigmas[ddim_steps - t_enc - 1]

            xi = x0 + noise
            sigma_sched = sigmas[ddim_steps - t_enc - 1:]
            model_wrap_cfg = CFGDenoiser(sampler.model_wrap)
            samples_ddim = K.sampling.__dict__[f'sample_{sampler.get_sampler_name()}'](model_wrap_cfg, xi, sigma_sched, extra_args={'cond': conditioning, 'uncond': unconditional_conditioning, 'cond_scale': cfg_scale}, disable=False)
        else:
            x0, = init_data
            sampler.make_schedule(ddim_num_steps=ddim_steps, ddim_eta=0.0, verbose=False)
            z_enc = sampler.stochastic_encode(x0, torch.tensor([t_enc]*batch_size).to(device))
                                # decode it
            samples_ddim = sampler.decode(z_enc, conditioning, t_enc,
                                            unconditional_guidance_scale=cfg_scale,
                                            unconditional_conditioning=unconditional_conditioning,)
        return samples_ddim


    try:
        if loopback:
            output_images, info = None, None
            history = []
            initial_seed = None

            for i in range(n_iter):
                output_images, seed, info, stats = process_images(
                    outpath=outpath,
                    func_init=init,
                    func_sample=sample,
                    prompt=prompt,
                    seed=seed,
                    sampler_name=sampler_name,
                    skip_save=skip_save,
                    skip_grid=skip_grid,
                    batch_size=1,
                    n_iter=1,
                    steps=ddim_steps,
                    cfg_scale=cfg_scale,
                    width=width,
                    height=height,
                    prompt_matrix=prompt_matrix,
                    use_GFPGAN=use_GFPGAN,
                    use_RealESRGAN=False, # Forcefully disable upscaling when using loopback
                    realesrgan_model_name=realesrgan_model_name,
                    fp=fp,
                    do_not_save_grid=True,
                    normalize_prompt_weights=normalize_prompt_weights,
                    init_img=init_img,
                    init_mask=init_mask,
                    keep_mask=keep_mask,
                    mask_blur_strength=mask_blur_strength,
                    denoising_strength=denoising_strength,
                    resize_mode=resize_mode,
                    uses_loopback=loopback,
                    uses_random_seed_loopback=random_seed_loopback,
                    sort_samples=sort_samples,
                    write_info_files=write_info_files,
                    jpg_sample=jpg_sample,
                )

                if initial_seed is None:
                    initial_seed = seed

                init_img = output_images[0]
                if not random_seed_loopback:
                    seed = seed + 1
                else:
                    seed = seed_to_int(None)
                denoising_strength = max(denoising_strength * 0.95, 0.1)
                history.append(init_img)

            if not skip_grid:
                grid_count = get_next_sequence_number(outpath, 'grid-')
                grid = image_grid(history, batch_size, force_n_rows=1)
                grid_file = f"grid-{grid_count:05}-{seed}_{prompt.replace(' ', '_').translate({ord(x): '' for x in invalid_filename_chars})[:128]}.{grid_ext}"
                grid.save(os.path.join(outpath, grid_file), grid_format, quality=grid_quality, lossless=grid_lossless, optimize=True)


            output_images = history
            seed = initial_seed

        else:
            output_images, seed, info, stats = process_images(
                outpath=outpath,
                func_init=init,
                func_sample=sample,
                prompt=prompt,
                seed=seed,
                sampler_name=sampler_name,
                skip_save=skip_save,
                skip_grid=skip_grid,
                batch_size=batch_size,
                n_iter=n_iter,
                steps=ddim_steps,
                cfg_scale=cfg_scale,
                width=width,
                height=height,
                prompt_matrix=prompt_matrix,
                use_GFPGAN=use_GFPGAN,
                use_RealESRGAN=use_RealESRGAN,
                realesrgan_model_name=realesrgan_model_name,
                fp=fp,
                normalize_prompt_weights=normalize_prompt_weights,
                init_img=init_img,
                init_mask=init_mask,
                keep_mask=keep_mask,
                mask_blur_strength=mask_blur_strength,
                denoising_strength=denoising_strength,
                resize_mode=resize_mode,
                uses_loopback=loopback,
                sort_samples=sort_samples,
                write_info_files=write_info_files,
                jpg_sample=jpg_sample,
            )

        del sampler

        return output_images, seed, info, stats
    except RuntimeError as e:
        err = e
        err_msg = f'CRASHED:<br><textarea rows="5" style="color:white;background: black;width: -webkit-fill-available;font-family: monospace;font-size: small;font-weight: bold;">{str(e)}</textarea><br><br>Please wait while the program restarts.'
        stats = err_msg
        return [], seed, 'err', stats
    finally:
        if err:
            crash(err, '!!Runtime error (img2img)!!')

# grabs all text up to the first occurrence of ':' as sub-prompt
# takes the value following ':' as weight
# if ':' has no value defined, defaults to 1.0
# repeats until no text remaining
# TODO this could probably be done with less code
def split_weighted_subprompts(text):
    print(text)
    remaining = len(text)
    prompts = []
    weights = []
    while remaining > 0:
        if ":" in text:
            idx = text.index(":") # first occurrence from start
            # grab up to index as sub-prompt
            prompt = text[:idx]
            remaining -= idx
            # remove from main text
            text = text[idx+1:]
            # find value for weight, assume it is followed by a space or comma
            idx = len(text) # default is read to end of text
            if " " in text:
                idx = min(idx,text.index(" ")) # want the closer idx
            if "," in text:
                idx = min(idx,text.index(",")) # want the closer idx
            if idx != 0:
                try:
                    weight = float(text[:idx])
                except: # couldn't treat as float
                    print(f"Warning: '{text[:idx]}' is not a value, are you missing a space or comma after a value?")
                    weight = 1.0
            else: # no value found
                weight = 1.0
            # remove from main text
            remaining -= idx
            text = text[idx+1:]
            # append the sub-prompt and its weight
            prompts.append(prompt)
            weights.append(weight)
        else: # no : found
            if len(text) > 0: # there is still text though
                # take remainder as weight 1
                prompts.append(text)
                weights.append(1.0)
            remaining = 0
    return prompts, weights

def run_GFPGAN(image, strength):
    image = image.convert("RGB")

    cropped_faces, restored_faces, restored_img = GFPGAN.enhance(np.array(image, dtype=np.uint8), has_aligned=False, only_center_face=False, paste_back=True)
    res = Image.fromarray(restored_img)

    if strength < 1.0:
        res = Image.blend(image, res, strength)

    return res

def run_RealESRGAN(image, model_name: str):
    if RealESRGAN.model.name != model_name:
            try_loading_RealESRGAN(model_name)

    image = image.convert("RGB")

    output, img_mode = RealESRGAN.enhance(np.array(image, dtype=np.uint8))
    res = Image.fromarray(output)

    return res


if opt.defaults is not None and os.path.isfile(opt.defaults):
    try:
        with open(opt.defaults, "r", encoding="utf8") as f:
            user_defaults = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        print(f"Error loading defaults file {opt.defaults}:", e, file=sys.stderr)
        print("Falling back to program defaults.", file=sys.stderr)
        user_defaults = {}
else:
    user_defaults = {}

# make sure these indicies line up at the top of txt2img()
txt2img_toggles = [
    'Create prompt matrix (separate multiple prompts using |, and get all combinations of them)',
    'Normalize Prompt Weights (ensure sum of weights add up to 1.0)',
    'Save individual images',
    'Save grid',
    'Sort samples by prompt',
    'Write sample info files',
    'jpg samples',
]
if GFPGAN is not None:
    txt2img_toggles.append('Fix faces using GFPGAN')
if RealESRGAN is not None:
    txt2img_toggles.append('Upscale images using RealESRGAN')

txt2img_defaults = {
    'prompt': '',
    'ddim_steps': 50,
    'toggles': [1, 2, 3],
    'sampler_name': 'k_lms',
    'ddim_eta': 0.0,
    'n_iter': 1,
    'batch_size': 1,
    'cfg_scale': 7.5,
    'seed': '',
    'height': 512,
    'width': 512,
    'fp': None,
    'submit_on_enter': 'Yes'
}

if 'txt2img' in user_defaults:
    txt2img_defaults.update(user_defaults['txt2img'])

txt2img_toggle_defaults = [txt2img_toggles[i] for i in txt2img_defaults['toggles']]

sample_img2img = "assets/stable-samples/img2img/sketch-mountains-input.jpg"
sample_img2img = sample_img2img if os.path.exists(sample_img2img) else None

# make sure these indicies line up at the top of img2img()
img2img_toggles = [
    'Create prompt matrix (separate multiple prompts using |, and get all combinations of them)',
    'Normalize Prompt Weights (ensure sum of weights add up to 1.0)',
    'Loopback (use images from previous batch when creating next batch)',
    'Random loopback seed',
    'Save individual images',
    'Save grid',
    'Sort samples by prompt',
    'Write sample info files',
    'jpg samples',
]
if GFPGAN is not None:
    img2img_toggles.append('Fix faces using GFPGAN')
if RealESRGAN is not None:
    img2img_toggles.append('Upscale images using RealESRGAN')

img2img_mask_modes = [
    "Keep masked area",
    "Regenerate only masked area",
]

img2img_resize_modes = [
    "Just resize",
    "Crop and resize",
    "Resize and fill",
]

img2img_defaults = {
    'prompt': '',
    'ddim_steps': 50,
    'toggles': [1, 4, 5],
    'sampler_name': 'k_lms',
    'ddim_eta': 0.0,
    'n_iter': 1,
    'batch_size': 1,
    'cfg_scale': 5.0,
    'denoising_strength': 0.75,
    'mask_mode': 0,
    'resize_mode': 0,
    'seed': '',
    'height': 512,
    'width': 512,
    'fp': None,
}

if 'img2img' in user_defaults:
    img2img_defaults.update(user_defaults['img2img'])

img2img_toggle_defaults = [img2img_toggles[i] for i in img2img_defaults['toggles']]
img2img_image_mode = 'sketch'

def change_image_editor_mode(choice, cropped_image, resize_mode, width, height):
    if choice == "Mask":
        return [gr.update(visible=False), gr.update(visible=True), gr.update(visible=False), gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), gr.update(visible=True), gr.update(visible=True)]
    return [gr.update(visible=True), gr.update(visible=False), gr.update(visible=True), gr.update(visible=False), gr.update(visible=True), gr.update(visible=True), gr.update(visible=False), gr.update(visible=False)]

def update_image_mask(cropped_image, resize_mode, width, height):
    resized_cropped_image = resize_image(resize_mode, cropped_image, width, height) if cropped_image else None
    return gr.update(value=resized_cropped_image)

def copy_img_to_input(img):
    try:
        image_data = re.sub('^data:image/.+;base64,', '', img)
        processed_image = Image.open(BytesIO(base64.b64decode(image_data)))
        tab_update = gr.update(selected='img2img_tab')
        img_update = gr.update(value=processed_image)
        return {img2img_image_mask: processed_image, img2img_image_editor: img_update, tabs: tab_update}
    except IndexError:
        return [None, None]


def copy_img_to_upscale_esrgan(img):
    update = gr.update(selected='realesrgan_tab')
    image_data = re.sub('^data:image/.+;base64,', '', img)
    processed_image = Image.open(BytesIO(base64.b64decode(image_data)))
    return {realesrgan_source: processed_image, tabs: update}


help_text = """
    ## Mask/Crop
    * The masking/cropping is very temperamental.
    * It may take some time for the image to show when switching from Crop to Mask.
    * If the image doesn't appear after switching to Mask, switch back to Crop and then back again to Mask
    * If the mask appears distorted (the brush is weirdly shaped instead of round), switch back to Crop and then back again to Mask.

    ## Advanced Editor
    * For now the button needs to be clicked twice the first time.
    * Once you have edited your image, you _need_ to click the save button for the next step to work.
    * Clear the image from the crop editor (click the x)
    * Click "Get Image from Advanced Editor" to get the image you saved. If it doesn't work, try opening the editor and saving again.

    If it keeps not working, try switching modes again, switch tabs, clear the image or reload.
"""

def show_help():
    return [gr.update(visible=False), gr.update(visible=True), gr.update(value=help_text)]

def hide_help():
    return [gr.update(visible=True), gr.update(visible=False), gr.update(value="")]


demo = draw_gradio_ui(opt,
                      user_defaults=user_defaults,
                      txt2img=txt2img,
                      img2img=img2img,
                      txt2img_defaults=txt2img_defaults,
                      txt2img_toggles=txt2img_toggles,
                      txt2img_toggle_defaults=txt2img_toggle_defaults,
                      show_embeddings=hasattr(model, "embedding_manager"),
                      img2img_defaults=img2img_defaults,
                      img2img_toggles=img2img_toggles,
                      img2img_toggle_defaults=img2img_toggle_defaults,
                      img2img_mask_modes=img2img_mask_modes,
                      img2img_resize_modes=img2img_resize_modes,
                      sample_img2img=sample_img2img,
                      RealESRGAN=RealESRGAN,
                      GFPGAN=GFPGAN,
                      run_GFPGAN=run_GFPGAN,
                      run_RealESRGAN=run_RealESRGAN
                        )

class ServerLauncher(threading.Thread):
    def __init__(self, demo):
        threading.Thread.__init__(self)
        self.name = 'Gradio Server Thread'
        self.demo = demo

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        gradio_params = {
            'show_error': True, 
            'server_name': '0.0.0.0', 
            'share': opt.share
        }
        if not opt.share:
            demo.queue(concurrency_count=1)
        if opt.share and opt.share_password:
            gradio_params['auth'] = ('webui', opt.share_password)    
        self.demo.launch(**gradio_params)

    def stop(self):
        self.demo.close() # this tends to hang

def launch_server():
    server_thread = ServerLauncher(demo)
    server_thread.start()

    try:
        while server_thread.is_alive():
            time.sleep(60)
    except (KeyboardInterrupt, OSError) as e:
        crash(e, 'Shutting down...')

def run_headless():
    with open(opt.cli, 'r', encoding='utf8') as f:
        kwargs = yaml.safe_load(f)
    target = kwargs.pop('target')
    if target == 'txt2img':
        target_func = txt2img
    elif target == 'img2img':
        target_func = img2img
        raise NotImplementedError()
    else:
        raise ValueError(f'Unknown target: {target}')
    prompts = kwargs.pop("prompt")
    prompts = prompts if type(prompts) is list else [prompts]
    for i, prompt_i in enumerate(prompts):
        print(f"===== Prompt {i+1}/{len(prompts)}: {prompt_i} =====")
        output_images, seed, info, stats = target_func(prompt=prompt_i, **kwargs)
        print(f'Seed: {seed}')
        print(info)
        print(stats)
        print()

if __name__ == '__main__':
    if opt.cli is None:
        launch_server()
    else:
        run_headless()
