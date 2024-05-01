from diffusers import AutoencoderKL, UNet2DConditionModel, DDPMScheduler
from peft import LoraConfig, inject_adapter_in_model
import torch
import os

from models.adapters import PhotoVerseAdapter
from models.clip import patch_clip_text_transformer
from models.unet import set_visual_cross_attention_adapter

from transformers import CLIPTextModel, CLIPTokenizer, CLIPVisionModel


def load_photoverse_model(path, image_adapter, text_adapter, unet):
    state_dict = torch.load(path)
    if "image_adapter" in state_dict:
        image_adapter.load_state_dict(state_dict["image_adapter"])
    if "text_adapter" in state_dict:
        text_adapter.load_state_dict(state_dict["text_adapter"])
    if "cross_attention_adapter" in state_dict:
        unet.load_state_dict(state_dict["cross_attention_adapter"], strict=False)

    return image_adapter, text_adapter, unet


def save_progress(image_adapter, text_adapter, unet, accelerator, output_path, step=None):
    state_dict_image_adapter = accelerator.unwrap_model(image_adapter).state_dict()
    state_dict_text_adapter = accelerator.unwrap_model(text_adapter).state_dict()
    state_dict_cross_attention = {}
    unet_state_dict = accelerator.unwrap_model(unet).state_dict()
    for key, value in unet_state_dict.items():
        if "attn2" in key:
            if "processor" in key or "to_q" in key or "to_k" in key or "to_v" in key:
                state_dict_cross_attention[key] = value
    final_state_dict = {
        "image_adapter": state_dict_image_adapter,
        "text_adapter": state_dict_text_adapter,
        "cross_attention_adapter": state_dict_cross_attention
    }
    if step is not None:
        torch.save(final_state_dict, os.path.join(output_path, f"photoverse_{str(step).zfill(6)}.pt"))
    else:
        torch.save(final_state_dict, os.path.join(output_path, "photoverse.pt"))


def load_models(pretrained_model_name_or_path, extra_num_tokens, photoverse_path=None):
    # Load models and tokenizer
    tokenizer = CLIPTokenizer.from_pretrained(pretrained_model_name_or_path, subfolder="tokenizer")
    text_encoder = CLIPTextModel.from_pretrained(pretrained_model_name_or_path, subfolder="text_encoder")
    vae = AutoencoderKL.from_pretrained(pretrained_model_name_or_path, subfolder="vae")
    unet = UNet2DConditionModel.from_pretrained(pretrained_model_name_or_path, subfolder="unet")
    image_encoder = CLIPVisionModel.from_pretrained("openai/clip-vit-large-patch14")
    scheduler = DDPMScheduler.from_pretrained(pretrained_model_name_or_path, subfolder="scheduler")

    # Freeze models
    unet.requires_grad_(False)
    vae.requires_grad_(False)
    text_encoder.requires_grad_(False)
    image_encoder.requires_grad_(False)

    # Create adapters
    image_adapter = PhotoVerseAdapter(
        cross_attention_dim=unet.config.cross_attention_dim,
        clip_embedding_dim=image_encoder.config.hidden_size,
        num_tokens=extra_num_tokens+1
    )
    text_adapter = PhotoVerseAdapter(
        cross_attention_dim=unet.config.cross_attention_dim,
        clip_embedding_dim=image_encoder.config.hidden_size,
        num_tokens=extra_num_tokens+1
    )

    # Patch the text encoder
    text_encoder = patch_clip_text_transformer(text_encoder)

    # set lora on textual cross attention layers add visual cross attention adapter
    lora_config = LoraConfig(
        lora_alpha=1,
        lora_dropout=0.1,
        r=64,
        bias="none",
        target_modules=["attn2.to_k", "attn2.to_v", "attn2.to_q"],
    )
    unet = inject_adapter_in_model(lora_config, unet)
    unet = set_visual_cross_attention_adapter(unet, num_tokens=(extra_num_tokens + 1,))

    if photoverse_path is not None:
        # Load pretrained weights into models
        image_adapter, text_adapter, unet = load_photoverse_model(photoverse_path, image_adapter, text_adapter, unet)

    return tokenizer, text_encoder, vae, unet, image_encoder, image_adapter, text_adapter, scheduler