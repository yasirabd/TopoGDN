import numpy as np
import torch
import matplotlib.pyplot as plt
import torch.nn as nn
import time
from util.time import *
from util.env import *
from torch_geometric.nn import GCNConv, GATConv, EdgeConv
from torch_geometric.data import Data, Batch
import math
import torch.nn.functional as F

from .graph_layer import GraphLayer
from models.topoPooling import TopologyLayer

def get_batch_edge_index(org_edge_index, batch_num, node_num):
    # org_edge_index:(2, edge_num)
    edge_index = org_edge_index.clone().detach()
    edge_num = org_edge_index.shape[1]
    batch_edge_index = edge_index.repeat(1,batch_num).contiguous()

    for i in range(batch_num):
        batch_edge_index[:, i*edge_num:(i+1)*edge_num] += i*node_num

    return batch_edge_index.long()


class OutLayer(nn.Module):
    def __init__(self, in_num, node_num, layer_num, inter_num = 512):
        super(OutLayer, self).__init__()

        modules = []

        for i in range(layer_num):
            # last layer, output shape:1
            if i == layer_num-1:
                modules.append(nn.Linear( in_num if layer_num == 1 else inter_num, 1))
            else:
                layer_in_num = in_num if i == 0 else inter_num
                modules.append(nn.Linear( layer_in_num, inter_num ))
                modules.append(nn.BatchNorm1d(inter_num))
                modules.append(nn.ReLU())

        self.mlp = nn.ModuleList(modules)
        self.temp = nn.Linear( in_num if layer_num == 1 else inter_num, 1)

    def forward(self, x):
        out = x        
        for mod in self.mlp:
            if isinstance(mod, nn.BatchNorm1d):
                out = out.permute(0,2,1)
                out = mod(out)
                out = out.permute(0,2,1)
            else:
                out = mod(out)

        return out



class GNNLayer(nn.Module):
    def __init__(self, in_channel, out_channel, inter_dim=0, heads=1, node_num=100, use_topo=False):
        super(GNNLayer, self).__init__()


        self.gnn = GraphLayer(in_channel, out_channel, inter_dim=inter_dim, heads=heads, concat=False)

        self.bn = nn.BatchNorm1d(out_channel)
        self.relu = nn.ReLU()
        self.leaky_relu = nn.LeakyReLU()
        self.use_topo = use_topo
        if self.use_topo:
            self.topoPooling = TopologyLayer(out_channel, out_channel, num_filtrations=8, 
                                            num_coord_funs={"Triangle_transform": 3,
                                                            "Gaussian_transform": 3,
                                                            "Line_transform": 3,
                                                            "RationalHat_transform": 3
                                                            }, 
                                            filtration_hidden=32)

    def forward(self, x, edge_index, embedding=None, node_num=0):
        # # 先Pooling
        # data = Data(x=x, edge_index=edge_index)
        # batch_data = Batch.from_data_list([data])  
        # topoOut, _, _ = self.topoPooling(batch_data.x, batch_data)

        out, (new_edge_index, att_weight) = self.gnn(x, edge_index, embedding, return_attention_weights=True)
        self.att_weight_1 = att_weight
        self.edge_index_1 = new_edge_index

        if self.use_topo:
            # 能work
            data = Data(x=out, edge_index=new_edge_index)
            batch_data = Batch.from_data_list([data])  
            topoOut, _, _ = self.topoPooling(batch_data.x, batch_data)
            out += topoOut 

        # 不能work(没改)
        # out = out.reshape(16, 50, -1)
        # new_edge_index = new_edge_index.reshape(2, 16, -1)
        # dataList = []
        # for i in range(16):
        #     data = Data(x=out[i], edge_index=new_edge_index[:, i])
        #     batch_data = Batch.from_data_list([data])  # 假设这里只有一个图
        #     topoOut, _, _ = self.topoPooling(batch_data.x, batch_data)
        #     topoList.append(topoOut)
        # out = torch.cat(topoList, dim=0)

        # 不能work
        # out = out.reshape(16, 50, -1)
        # new_edge_index = new_edge_index.reshape(2, 16, -1)
        # dataList = []
        # for i in range(16):
        #     # 创建一个 Batch 对象
        #     data = Data(x=out[i], edge_index=new_edge_index[:, i])
        #     dataList.append(data)
        # batch_data = Batch.from_data_list(data_list=dataList)
        # out, _, _ = self.topoPooling(batch_data.x, batch_data)

        out = self.bn(out)
        
        return self.relu(out)


class GDN(nn.Module):
    def __init__(self, edge_index_sets, node_num, dim=64, out_layer_inter_dim=256, input_dim=10, out_layer_num=1, topk=20, MSConv=None, use_topo=False):

        super(GDN, self).__init__()

        self.MSConv = MSConv

        self.edge_index_sets = edge_index_sets

        device = get_device()

        edge_index = edge_index_sets[0]


        embed_dim = dim
        self.embedding = nn.Embedding(node_num, embed_dim)
        self.bn_outlayer_in = nn.BatchNorm1d(embed_dim)


        edge_set_num = len(edge_index_sets)

        self.gnn_layers = nn.ModuleList([
            GNNLayer(input_dim, dim, inter_dim=dim+embed_dim, heads=1, use_topo=use_topo) for i in range(edge_set_num)
        ])


        self.node_embedding = None
        self.topk = topk
        self.learned_graph = None

        self.out_layer = OutLayer(dim*edge_set_num, node_num, out_layer_num, inter_num = out_layer_inter_dim)

        self.cache_edge_index_sets = [None] * edge_set_num
        self.cache_embed_index = None

        self.dp = nn.Dropout(0.2)        

        self.init_params()
    
    def init_params(self):
        nn.init.kaiming_uniform_(self.embedding.weight, a=math.sqrt(5))


    def forward(self, data):
        
        # PE = self.positionEmb[]

        x = data.clone()
        if self.MSConv is not None:
            x = self.MSConv(x)
            
        edge_index_sets = self.edge_index_sets

        device = data.device

        batch_num, node_num, all_feature = x.shape

        x = x.view(-1, all_feature).contiguous()


        gcn_outs = []
        for i, edge_index in enumerate(edge_index_sets):
            edge_num = edge_index.shape[1]
            cache_edge_index = self.cache_edge_index_sets[i]

            if cache_edge_index is None or cache_edge_index.shape[1] != edge_num*batch_num:
                self.cache_edge_index_sets[i] = get_batch_edge_index(edge_index, batch_num, node_num).to(device)
            
            batch_edge_index = self.cache_edge_index_sets[i]
            
            all_embeddings = self.embedding(torch.arange(node_num).to(device))

            weights_arr = all_embeddings.detach().clone()
            all_embeddings = all_embeddings.repeat(batch_num, 1)

            weights = weights_arr.view(node_num, -1)

            cos_ji_mat = torch.matmul(weights, weights.T)
            normed_mat = torch.matmul(weights.norm(dim=-1).view(-1,1), weights.norm(dim=-1).view(1,-1))
            cos_ji_mat = cos_ji_mat / normed_mat

            dim = weights.shape[-1]
            topk_num = self.topk

            topk_indices_ji = torch.topk(cos_ji_mat, topk_num, dim=-1)[1]

            # self.learned_graph = topk_indices_ji

            gated_i = torch.arange(0, node_num).T.unsqueeze(1).repeat(1, topk_num).flatten().to(device).unsqueeze(0)
            gated_j = topk_indices_ji.flatten().unsqueeze(0)
            gated_edge_index = torch.cat((gated_j, gated_i), dim=0)

            batch_gated_edge_index = get_batch_edge_index(gated_edge_index, batch_num, node_num).to(device)
            gcn_out = self.gnn_layers[i](x, batch_gated_edge_index, node_num=node_num*batch_num, embedding=all_embeddings)
            self.learned_graph = gated_edge_index

            gcn_outs.append(gcn_out)

        x = torch.cat(gcn_outs, dim=1)
        x = x.view(batch_num, node_num, -1)

        indexes = torch.arange(0,node_num).to(device)
        
        # 对embedding做一个归一化
        norm_embedding = F.softmax(self.embedding(indexes), dim=1)

        # 如果不做mul，会导致embedding的维度不一致
        out = torch.mul(x, norm_embedding)
        
        out = out.permute(0,2,1)
        out = F.relu(self.bn_outlayer_in(out))
        out = out.permute(0,2,1)

        out = self.dp(out)
        out = self.out_layer(out)
        out = out.view(-1, node_num)
   
        return out, self.learned_graph
        