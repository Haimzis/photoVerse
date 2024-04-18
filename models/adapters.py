import torch
from torch import nn


class PhotoVerseAdapter(nn.Module):
    def __init__(self,
                 clip_embedding_dim=1024,
                 cross_attention_dim=768,
                 num_tokens=5
                 ):
        super(PhotoVerseAdapter, self).__init__()

        for i in range(num_tokens):
            setattr(self, f'mapping_{i}', nn.Sequential(nn.Linear(clip_embedding_dim, 1024),
                                                        nn.LayerNorm(1024),
                                                        nn.LeakyReLU(),
                                                        nn.Linear(1024, 1024),
                                                        nn.LayerNorm(1024),
                                                        nn.LeakyReLU(),
                                                        nn.Linear(1024, cross_attention_dim)))

            setattr(self, f'mapping_patch_{i}', nn.Sequential(nn.Linear(clip_embedding_dim, 1024),
                                                              nn.LayerNorm(1024),
                                                              nn.LeakyReLU(),
                                                              nn.Linear(1024, 1024),
                                                              nn.LayerNorm(1024),
                                                              nn.LeakyReLU(),
                                                              nn.Linear(1024, cross_attention_dim)))

    def forward(self, embs):
        hidden_states = ()
        for i, emb in enumerate(embs):
            hidden_state = getattr(self, f'mapping_{i}')(emb[:, :1]) + getattr(self, f'mapping_patch_{i}')(
                emb[:, 1:]).mean(dim=1, keepdim=True)
            hidden_states += (hidden_state,)
        hidden_states = torch.cat(hidden_states, dim=1)
        return hidden_states
