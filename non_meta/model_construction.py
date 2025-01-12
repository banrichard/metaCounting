import torch
from dgl import graph
from torch import nn
from torch_geometric.data import Batch
from torch_geometric.nn import global_mean_pool

from model.graphconv import Backbone, Graphormer
from model.mlp import Mlp
from model.motifNN import MotifGNN
from model.regularization import CCANet


class Pipeline(torch.nn.Module):
    def __init__(self, input_dim, pre_train_path=None, layer_num=3, hid_dim=64, frozen_gnn='all',
                 frozen_project_head=False, frozen_prompt=False, pool_mode=0, gnn_type='GIN', mnn_type="ginconcat",
                 project_head_path=None,
                 m_layer_num=3,
                 without_snn=False,
                 without_prompt=False):

        super().__init__()
        self.pool_mode = pool_mode
        self.mnn_type = mnn_type
        self.frozen_prompt = frozen_prompt
        self.norm = nn.LayerNorm(hid_dim)
        self.without_prompt = without_prompt
        if without_snn:
            self.gnn = MotifGNN(num_layers=layer_num, num_g_hid=hid_dim, out_g_ch=hid_dim, model_type="GIN",
                                dropout=0.2)
        else:
            self.gnn = Backbone(type=gnn_type, num_layers=layer_num, input_dim=input_dim, hidden_dim=hid_dim,
                                output_dim=hid_dim)
        if self.mnn_type == "graphormer":
            self.motifnn = Graphormer(num_layers=m_layer_num)
        elif self.mnn_type == "vector":
            self.motifnn = nn.Parameter(torch.Tensor(1, hid_dim))
        else:
            self.motifnn = MotifGNN(num_layers=m_layer_num, num_g_hid=32, num_e_hid=32, out_g_ch=hid_dim,
                                    model_type="NNGINConcat",
                                    dropout=0.2)
        if frozen_prompt:
            for p in self.motifnn.parameters():
                p.requires_grad = False
        if without_prompt:
            self.projection_head = Mlp(hid_dim, hid_dim, 1, drop=0.5)
        else:
            self.projection_head = CCANet(hid_dim, hid_dim, 32)
        self.set_gnn_project_head(pre_train_path, frozen_gnn, frozen_project_head, project_head_path)

    def set_gnn_project_head(self, pre_train_path, frozen_gnn, frozen_project_head, project_head_path=None):
        if pre_train_path:
            self.gnn.load_state_dict(torch.load(pre_train_path), strict=False)
            print("successfully load pre-trained weights for gnn! @ {}".format(pre_train_path))

        if project_head_path:
            self.project_head.load_state_dict(torch.load(project_head_path))
            print("successfully load project_head! @ {}".format(project_head_path))

        if frozen_gnn == 'all':
            for p in self.gnn.parameters():
                p.requires_grad = False
        elif frozen_gnn == 'none':
            for p in self.gnn.parameters():
                p.requires_grad = True
        else:
            pass

        if frozen_project_head:
            for p in self.project_head.parameters():
                p.requires_grad = False

    def to_cuda(self):
        self.motifnn = self.motifnn.cuda()
        self.project_head = self.project_head.cuda()

    def forward(self, graph_batch: Batch, motif_batch):
        # num_graphs = graph_batch.num_graphs

        graph_emb = self.gnn(graph_batch.x, graph_batch.edge_index, graph_batch.edge_attr)
        if self.without_prompt:
            graph_emb = global_mean_pool(graph_emb, batch=graph_batch.batch)
            reg = 0
            pre = self.projection_head(graph_emb)
        else:
            if self.mnn_type == "graphormer":
                motif_emb = self.motifnn(motif_batch)
            elif self.mnn_type == "vector":
                motif_emb = self.motifnn
            else:
                motif_emb = self.motifnn(motif_batch.x, motif_batch.edge_index, motif_batch.edge_attr)
            graph_emb = global_mean_pool(graph_emb, batch=graph_batch.batch)
            graph_emb = self.norm(graph_emb)
            # final_emb = torch.cat([graph_emb, motif_emb], dim=1)
            graph_emb += motif_emb  # x = x + p
            pre, reg = self.projection_head(graph_emb, motif_emb)
        return pre, reg


class ImportancePipeline(torch.nn.Module):
    def __init__(self, input_dim, pre_train_path=None, layer_num=3, hid_dim=64, frozen_gnn='all',
                 frozen_prompt=False, frozen_project_head=False, gnn_type='GIN', project_head_path=None):
        super().__init__()
        self.norm = nn.LayerNorm(hid_dim)
        self.gnn = Backbone(type=gnn_type, num_layers=layer_num, input_dim=input_dim, hidden_dim=hid_dim,
                            output_dim=hid_dim)
        self.prompt = nn.Parameter(torch.Tensor(1, hid_dim))
        self.prompt = torch.nn.init.kaiming_uniform_(self.prompt)
        self.project_head = torch.nn.Sequential(
            torch.nn.Linear(hid_dim, 32),
            torch.nn.ReLU(),
            torch.nn.Linear(32, 1),
            torch.nn.ReLU())
        self.set_gnn_project_head(pre_train_path, frozen_gnn, frozen_project_head, frozen_prompt, project_head_path)

    def set_gnn_project_head(self, pre_train_path, frozen_gnn, frozen_project_head, frozen_prompt,
                             project_head_path=None):
        if pre_train_path:
            self.gnn.load_state_dict(torch.load(pre_train_path), strict=False)
            print("successfully load pre-trained weights for gnn! @ {}".format(pre_train_path))

        if project_head_path:
            self.project_head.load_state_dict(torch.load(project_head_path))
            print("successfully load project_head! @ {}".format(project_head_path))

        if frozen_gnn == 'all':
            for p in self.gnn.parameters():
                p.requires_grad = False
        elif frozen_gnn == 'none':
            for p in self.gnn.parameters():
                p.requires_grad = True
        else:
            pass

        if frozen_project_head:
            for p in self.project_head.parameters():
                p.requires_grad = False
        if frozen_prompt:
            self.prompt.requires_grad = False

    def forward(self, graph_batch: Batch):
        graph_emb = self.gnn(graph_batch.x, graph_batch.edge_index, graph_batch.edge_attr)
        # graph_emb = global_mean_pool(graph_emb, batch=graph_batch.batch)
        graph_emb = self.norm(graph_emb)
        graph_emb = self.prompt + graph_emb
        pre = self.project_head(graph_emb)
        return pre
