from diffusers import DPMSolverMultistepScheduler
import torch

from tqdm import tqdm


def run_inference(example, tokenizer, image_encoder, text_encoder, unet, text_adapter, image_adapter, vae, scheduler,
                  device, image_encoder_layers_idx, latent_size=64, guidance_scale=1, timesteps=100, token_index=0,
                  disable_tqdm=False, seed=None, from_noised_image=False, training_mode=False):
    """
    Runs inference for image generation.

    Args:
        example (dict): Input example containing pixel values, text input ids, and more.
        tokenizer: Tokenizer for text processing.
        image_encoder: Model for encoding image features.
        text_encoder: Model for encoding text features.
        unet: U-Net model for image generation.
        text_adapter: Adapter for processing text embeddings.
        image_adapter: Adapter for processing image embeddings.
        vae: Variational Autoencoder for encoding and decoding latent representations.
        scheduler: Scheduler for controlling diffusion process.
        device: Device for computation (CPU or GPU).
        image_encoder_layers_idx (list): Indices of image encoder layers to be used.
        latent_size (int): Size of latent space. Default is 64.
        guidance_scale (float): Scale factor for guidance during image generation. Default is 1.
        timesteps (int): Number of diffusion timesteps. Default is 100.
        token_index (int): Index of the token. Default is 0.
        disable_tqdm (bool): Whether to disable tqdm progress bar. Default is False.
        seed (int): Random seed for reproducibility. Default is None.
        from_noised_image (bool): Whether input image is noised. Default is False.
        training_mode (bool): Whether in training mode. Default is False.

    Returns:
        torch.Tensor: Generated images.
    """

    # Load and set the scheduler
    scheduler = DPMSolverMultistepScheduler.from_config(scheduler.config)
    scheduler.set_timesteps(timesteps)

    # Create the unconditional input ids
    uncond_input_ids = example.get("negative_text_input_ids", None)
    uncond_input_ids = tokenizer(
        [''] * example["pixel_values"].shape[0],
        padding="max_length",
        max_length=tokenizer.model_max_length,
        return_tensors="pt",
    ).input_ids if uncond_input_ids is None else uncond_input_ids

    # Create the noise
    if seed is None:
        noise = torch.randn(
            (example["pixel_values"].shape[0], unet.config.in_channels, latent_size, latent_size)
        ).to(device)
    else:
        generator = torch.manual_seed(seed)
        noise = torch.randn(
            (example["pixel_values"].shape[0], unet.config.in_channels, latent_size, latent_size), generator=generator).to(device)

    # Setup the latent depending if we are using the noised image or not
    if from_noised_image:
        latents = vae.encode(example["pixel_values"].to(device)).latent_dist.sample().detach()
        latents = latents * vae.config.scaling_factor
        latents = scheduler.add_noise(latents, noise, scheduler.timesteps[:1].repeat(latents.shape[0]))

    else:
        latents = noise

    latents = latents * scheduler.init_noise_sigma

    placeholder_idx = example["concept_placeholder_idx"].to(device)
    pixel_values_clip = example["pixel_values_clip"].to(device)

    # get conditional image embeddings and text embeddings
    image_features = image_encoder(pixel_values_clip, output_hidden_states=True)
    uncond_image_features = image_encoder(torch.zeros_like(example["pixel_values_clip"]).to(device),
                                          output_hidden_states=True)

    image_embeddings = [image_features[0]] + [image_features[2][i] for i in image_encoder_layers_idx if
                                              i < len(image_features[2])]
    uncond_image_emmbedings = [uncond_image_features[0]] + [uncond_image_features[2][i] for i in
                                                            image_encoder_layers_idx if
                                                            i < len(uncond_image_features[2])]

    image_embeddings = [emb.detach() for emb in image_embeddings]
    uncond_image_emmbedings = [emb.detach() for emb in uncond_image_emmbedings]

    concept_text_embeddings = text_adapter(image_embeddings, token_index=token_index)
    encoder_hidden_states_image = image_adapter(image_embeddings, token_index=token_index)
    uncond_encoder_hidden_states_image = image_adapter(uncond_image_emmbedings, token_index=token_index)

    uncond_embeddings = text_encoder({'text_input_ids': uncond_input_ids.to(device)})[0]
    encoder_hidden_states = text_encoder({'text_input_ids': example["text_input_ids"].to(device),
                                          "concept_text_embeddings": concept_text_embeddings,
                                          "concept_placeholder_idx": placeholder_idx})[0]

    for i, t in enumerate(tqdm(scheduler.timesteps, desc="Denoising", disable=disable_tqdm)):
        with torch.set_grad_enabled(training_mode and (i == len(scheduler.timesteps) - 1)):
            latent_model_input = scheduler.scale_model_input(latents, t)

            # Noise prediction based on unconditional inputs
            noise_pred_uncond = unet(
                latent_model_input,
                t,
                encoder_hidden_states=(uncond_embeddings, uncond_encoder_hidden_states_image)
            ).sample

            # Noise prediction based on conditional inputs (text + image)
            noise_pred_text = unet(
                latent_model_input,
                t,
                encoder_hidden_states=(encoder_hidden_states, encoder_hidden_states_image)
            ).sample

            noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

            # compute the previous noisy sample x_t -> x_t-1
            latents = scheduler.step(noise_pred, t, latents).prev_sample

    _latents = 1 / vae.config.scaling_factor * latents.clone()
    images = vae.decode(_latents).sample.clamp(-1, 1)
    return images