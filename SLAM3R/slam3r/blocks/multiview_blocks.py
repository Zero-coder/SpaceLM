import torch
import torch.nn as nn

from .basic_blocks import Mlp, Attention, CrossAttention, DropPath
try:
    import xformers.ops as xops
    XFORMERS_AVALIABLE = True
except ImportError:
    print("xformers not avaliable, use self-implemented attention instead")
    XFORMERS_AVALIABLE = False


class XFormer_Attention(nn.Module):
    """Warpper for self-attention module with xformers.
    Calculate attention scores with xformers memory_efficient_attention.
    When inference is performed on the CPU or when xformer is not installed, it will degrade to the normal attention.
    """
    def __init__(self, old_module:Attention):
        super().__init__()
        self.num_heads = old_module.num_heads
        self.scale = old_module.scale
        self.qkv = old_module.qkv
        self.attn_drop_prob = old_module.attn_drop.p
        self.proj = old_module.proj
        self.proj_drop = old_module.proj_drop
        self.rope = old_module.rope
        self.attn_drop = old_module.attn_drop

    def forward(self, x, xpos):
        B, N, C = x.shape

        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).transpose(1,3)
        q, k, v = [qkv[:,:,i] for i in range(3)]  #shape: (B, num_heads, N, C//num_heads)
               
        if self.rope is not None:
            q = self.rope(q, xpos) # (B, H, N, K)
            k = self.rope(k, xpos)
            
        if x.is_cuda and XFORMERS_AVALIABLE:
            q = q.permute(0, 2, 1, 3)  # (B, N, H, K)
            k = k.permute(0, 2, 1, 3)
            v = v.permute(0, 2, 1, 3)
            drop_prob = self.attn_drop_prob if self.training else 0
            x = xops.memory_efficient_attention(q, k, v, scale=self.scale, p=drop_prob) # (B, N, H, K)
        else:   
            attn = (q @ k.transpose(-2, -1)) * self.scale
            attn = attn.softmax(dim=-1)
            attn = self.attn_drop(attn)
            x = (attn @ v).transpose(1, 2)
            
        x=x.reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class MultiviewDecoderBlock_max(nn.Module):
    """Multiview decoder block, 
    which takes as input arbitrary number of source views and target view features.
    Use max-pooling to merge features queried from different src views.
    """

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, norm_mem=True, rope=None):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(dim, rope=rope, num_heads=num_heads, qkv_bias=qkv_bias, attn_drop=attn_drop, proj_drop=drop)
        self.cross_attn = CrossAttention(dim, rope=rope, num_heads=num_heads, qkv_bias=qkv_bias, attn_drop=attn_drop, proj_drop=drop)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        self.norm3 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        self.norm_y = norm_layer(dim) if norm_mem else nn.Identity()
        if XFORMERS_AVALIABLE:
            self.attn = XFormer_Attention(self.attn)        

    def batched_cross_attn(self, xs, ys, xposes, yposes, rel_ids_list_d, M):
        """
        Calculate cross-attention between Vx target views and Vy source views in a single batch.
        """
        xs_normed = self.norm2(xs)
        ys_normed = self.norm_y(ys)
        cross_attn = self.cross_attn
        Vx, B, Nx, C = xs.shape
        Vy, B, Ny, C = ys.shape
        num_heads = cross_attn.num_heads
        
        #precompute q,k,v for each view to save computation
        qs = cross_attn.projq(xs_normed).reshape(Vx*B,Nx,num_heads, C//num_heads).permute(0, 2, 1, 3) # (Vx*B,num_heads,Nx,C//num_heads)
        ks = cross_attn.projk(ys_normed).reshape(Vy*B,Ny,num_heads, C//num_heads).permute(0, 2, 1, 3) # (Vy*B,num_heads,Ny,C//num_heads)
        vs = cross_attn.projv(ys_normed).reshape(Vy,B,Ny,num_heads, C//num_heads) # (Vy*B,num_heads,Ny,C//num_heads)
        
        #add rope 
        if cross_attn.rope is not None:
            qs = cross_attn.rope(qs, xposes)
            ks = cross_attn.rope(ks, yposes)
        qs = qs.permute(0, 2, 1, 3).reshape(Vx,B,Nx,num_heads,C// num_heads)  # (Vx, B, Nx, H, K)
        ks = ks.permute(0, 2, 1, 3).reshape(Vy,B,Ny,num_heads,C// num_heads)  # (Vy, B, Ny, H, K)

        # construct query, key, value for each target view
        ks_respect = torch.index_select(ks, 0, rel_ids_list_d)  # (Vx*M, B, Ny, H, K)
        vs_respect = torch.index_select(vs, 0, rel_ids_list_d)  # (Vx*M, B, Ny, H, K)
        qs_corresp = torch.unsqueeze(qs, 1).expand(-1, M, -1, -1, -1, -1)  # (Vx, M, B, Nx, H, K)
        
        ks_compact = ks_respect.reshape(Vx*M*B, Ny, num_heads, C//num_heads)
        vs_compact = vs_respect.reshape(Vx*M*B, Ny, num_heads, C//num_heads)
        qs_compact = qs_corresp.reshape(Vx*M*B, Nx, num_heads, C//num_heads)
        
        # calculate attention results for all target views in one go 
        if xs.is_cuda and XFORMERS_AVALIABLE:
            drop_prob = cross_attn.attn_drop.p if self.training else 0
            attn_outputs = xops.memory_efficient_attention(qs_compact, ks_compact, vs_compact, 
                                                           scale=self.cross_attn.scale, p=drop_prob) # (V*M*B, N, H, K)
        else:
            ks_compact = ks_compact.permute(0, 2, 1, 3)  # (Vx*M*B, H, Ny, K)
            qs_compact = qs_compact.permute(0, 2, 1, 3)  # (Vx*M*B, H, Nx, K)
            vs_compact = vs_compact.permute(0, 2, 1, 3)  # (Vx*M*B, H, Ny, K)
            attn = (qs_compact @ ks_compact.transpose(-2, -1)) * self.cross_attn.scale # (V*M*B, H, Nx, Ny)
            attn = attn.softmax(dim=-1)  # (V*M*B, H, Nx, Ny)
            attn = self.cross_attn.attn_drop(attn)   
            attn_outputs = (attn @ vs_compact).transpose(1, 2).reshape(Vx*M*B, Nx, num_heads, C//num_heads) # (V*M*B, Nx, H, K)
            
        attn_outputs = attn_outputs.reshape(Vx, M, B, Nx, C)  #(Vx, M, B, Nx, C)
        attn_outputs = cross_attn.proj_drop(cross_attn.proj(attn_outputs))  #(Vx, M, B, Nx, C)
  
        return attn_outputs      

    def forward(self, xs:torch.Tensor, ys:torch.Tensor, 
                xposes:torch.Tensor, yposes:torch.Tensor, 
                rel_ids_list_d:torch.Tensor, M:int):
        """refine Vx target view feature parallelly, with the information of Vy source view 
        
        Args:
            xs: (Vx,B,S,D):  features of target views to refine.(S: number of tokens, D: feature dimension)
            ys: (Vy,B,S,D):  features of source views to query from.
            M: number of source views to query from for each target view
            rel_ids_list_d: (Vx*M,) indices of source views to query from for each target view
        
        For example: 
            Suppose we have 3 target views and 4 source views, 
            then xs shuold has shape (3,B,S,D), ys should has shape (4,B,S,D).
            
            If we require xs[0] to query features from ys[0], ys[1], 
            xs[1] to query features from ys[2], ys[2],(duplicate number supported) 
            xs[2] to query features from ys[2], ys[3],
            then we should set M=2, rel_ids_list_d=[0,1,  2,2,  2,3]
        """
        Vx, B, Nx, C = xs.shape
        
        # self-attention on each target view feature
        xs = xs.reshape(-1, *xs.shape[2:]) # (Vx*B,S,C)
        xposes = xposes.reshape(-1, *xposes.shape[2:]) # (Vx*B,S,2)
        yposes = yposes.reshape(-1, *yposes.shape[2:])
        xs = xs + self.drop_path(self.attn(self.norm1(xs), xposes))  #(Vx*B,S,C)
        
        # each target view conducts cross-attention with all source views to query features 
        attn_outputs = self.batched_cross_attn(xs.reshape(Vx,B,Nx,C), ys, xposes, yposes, rel_ids_list_d, M)
        
        # max-pooling to aggregate features queried from different source views
        merged_ys, indices = torch.max(attn_outputs, dim=1)  #(Vx, B, Nx, C)
        merged_ys = merged_ys.reshape(Vx*B,Nx,C)  #(Vx*B,Nx,C)
        
        xs = xs + self.drop_path(merged_ys)
        xs = xs + self.drop_path(self.mlp(self.norm3(xs))) #(VB,N,C)
        xs = xs.reshape(Vx,B,Nx,C)
        return xs

