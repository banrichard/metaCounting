import torch.nn as nn
import torch
from torch.nn.init import trunc_normal_
from pretrain.matcher import Matcher
from utils.mask import make_mask
from pretrain.base import PreTrain
from model.mlp import Mlp
from model.attention import TransformerRegressor
import torch.nn.functional as F


class GIN(PreTrain):
    def __init__(self, num_layer, input_dim, hid_dim, output_dim, dropout):
        super().__init__("GIN", dropout=dropout)
        self.num_layer = num_layer
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.hid_dim = hid_dim
        self.initialize_gnn(self.input_dim, self.hid_dim)
        self.init_emb = nn.Parameter(torch.randn(self.gnn.input_dim))
        self.mask_token = nn.Parameter(torch.zeros(1, self.hid_dim))
        self.projection_head = nn.Sequential(
            nn.Linear(hid_dim, hid_dim),
            nn.ReLU(inplace=True),
            nn.Linear(hid_dim, output_dim),
        )
        self.pos_decoder = nn.Linear(self.hid_dim, self.hid_dim)
        self.matcher = nn.Linear(self.hid_dim,self.input_dim)
        self.build_regressor()

    @torch.no_grad()
    def momentum_update(self, base_momentum=0):
        """Momentum update of the teacher network."""
        for param_encoder, param_teacher in zip(
            self.student.parameters(), self.teacher.parameters()
        ):
            param_teacher.data = (
                param_teacher.data * base_momentum
                + param_encoder.data * (1.0 - base_momentum)
            )

    def build_regressor(self):
        self.mask_regressor = TransformerRegressor(
            embed_dim=self.hid_dim,
            drop_path_rate=0.1,
        )

    def build_masked_decoder(self):
        if self.mask_ratio > 0.0:
            self.mask_token = nn.Parameter(torch.zeros(1, 1, self.embed_dim))
            self.decoder_pos_embed = nn.Sequential(
                nn.Linear(3, 128), nn.GELU(), nn.Linear(128, self.embed_dim)
            )
            dpr = [
                x.item()
                for x in torch.linspace(0, self.drop_path_rate, self.decoder_depth)
            ]
            self.RAE_decoder = Mlp(
                in_features=self.output_dim, out_features=self.output_dim
            )
            trunc_normal_(self.mask_token, std=0.02)
        else:
            self.mask_token = None
            self.RAE_decoder = None

    def importance_loss(self, pred_importance, target_importance):
        return F.mse_loss(
            pred_importance.float(), target_importance.float(), reduction="mean"
        )

    def similarity_loss(self, pred_feat, orig_feat):
        return -(
            F.cosine_similarity(pred_feat, orig_feat, dim=1)
            .mean(dim=-1)
            .to(torch.float32)
        )

    def forward(self, data, use_mask=True):
        x, edge_index, edge_attr, importance = (
            data.x,
            data.edge_index,
            data.edge_attr,
            data.degree_centrality,
        )
        if use_mask:
            mask = make_mask(x)
        else:
            mask = None
        # generate embedding
        pred = self.gnn(x, edge_index, edge_attr)
        pred_importance = self.projection_head(pred)
        importance_loss = self.importance_loss(pred_importance, importance)
        num_nodes, channel = x.shape

        if mask is not None:
            pos_emd_vis = self.pos_decoder(pred[mask])
            pos_emd_mask = self.pos_decoder(pred[~mask])
            num_mask, _ = pos_emd_mask.shape
            mask_token = self.mask_token.expand(num_mask, -1)
            pred_attr = self.mask_regressor(
                mask_token, pred[mask], pos_emd_mask, pos_emd_vis, mask
            )
            pred_attr = self.matcher(pred_attr)
            # temporarily can not find a good solution to solve the attr loss, current is cossimilarity
            attr_loss = self.similarity_loss(pred_attr, data.x[~mask])
            return importance_loss, attr_loss
