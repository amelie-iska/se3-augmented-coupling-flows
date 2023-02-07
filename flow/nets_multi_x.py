from typing import NamedTuple, Sequence, Optional, Callable

import chex
import jax
import jax.numpy as jnp
import haiku as hk
import numpy as np

from flow.nets_emile import EGNN


# TODO: need to be careful of mean if number of nodes is varying? Could normalisation a parameter function of
#  the number of nodes?


def get_norms_sqrd(x):
    diff_combos = x - x[:, None]  # [n_nodes, n_nodes, dim]
    diff_combos = diff_combos.at[jnp.arange(x.shape[0]), jnp.arange(x.shape[0])].set(0.0)  # prevents nan grads
    sq_norms = jnp.sum(diff_combos ** 2, axis=-1)
    return sq_norms


class EGCL_Multi(hk.Module):
    def __init__(self, name: str, n_heads, mlp_units: Sequence[int], identity_init_x: bool, residual: bool = True,
                 normalize_by_x_norm: bool = True, activation_fn: Callable = jax.nn.silu,
                 normalization_constant: float = 1.0, tanh: bool = False, phi_x_max: float = 10.0,
                 agg='mean', stop_gradient_for_norm: bool = False,
                 variance_scaling_init: float =0.001):


        super().__init__(name=name + "equivariant")
        self.phi_e = hk.nets.MLP(mlp_units, activation=activation_fn, activate_final=True)
        self.phi_inf = lambda x: jax.nn.sigmoid(hk.Linear(1)(x))

        self.phi_x = hk.Sequential([
            hk.nets.MLP(mlp_units, activate_final=True, activation=activation_fn),
             hk.Linear(n_heads, w_init=jnp.zeros, b_init=jnp.zeros) if identity_init_x else
             hk.Linear(n_heads, w_init=hk.initializers.VarianceScaling(variance_scaling_init, "fan_avg", "uniform")),
             lambda x: jax.nn.tanh(x)*phi_x_max if tanh else x])

        self.phi_h_mlp = hk.nets.MLP(mlp_units, activate_final=False, activation=activation_fn)
        self.residual_h = residual
        self.normalize_by_x_norm = normalize_by_x_norm
        self.normalization_constant = normalization_constant
        self.agg = agg
        self.stop_gradient_for_norm = stop_gradient_for_norm
        self.n_heads = n_heads

    def __call__(self, x, h):
        assert x.shape[-2] == self.n_heads
        if len(x.shape) == 3:
            return self.forward_single(x, h)
        else:
            chex.assert_rank(x, 4)
            return hk.vmap(self.forward_single, split_rng=False)(x, h)

    def forward_single(self, x, h):
        """Forward pass for a single graph (no batch dimension)."""
        chex.assert_rank(x, 3)
        chex.assert_rank(h, 2)
        n_nodes, n_heads = x.shape[0:2]

        # Equation 4(a)
        # Append norms between all heads to h.
        diff_combos_heads = jax.vmap(lambda x: x - x[:, None], in_axes=0)(x)  # [n_nodes, n_heads, n_heads, dim]

        diff_combos_heads = diff_combos_heads.at[:, jnp.arange(n_heads), jnp.arange(n_heads)].set(0.0)  # prevents nan grads
        sq_norms_heads = jnp.sum(diff_combos_heads**2, axis=-1)
        sq_norms_heads = sq_norms_heads.reshape(sq_norms_heads.shape[0], np.prod(sq_norms_heads.shape[1:]))  # Flatten.
        h_concat = jnp.concatenate([h, sq_norms_heads], axis=-1)

        # create h_combos.
        h_combos = jnp.concatenate([jnp.repeat(h_concat[None, ...], h.shape[0], axis=0),
                                    jnp.repeat(h_concat[:, None, ...], h.shape[0], axis=1)], axis=-1)

        diff_combos_nodes = jax.vmap(lambda x: x - x[:, None], in_axes=-2, out_axes=-2)(x)  # [n_nodes, n_nodes, n_heads, dim]
        diff_combos_nodes = diff_combos_nodes.at[jnp.arange(x.shape[0]), jnp.arange(x.shape[0])].set(0.0)  # prevents nan grads
        sq_norms_nodes = jnp.sum(diff_combos_nodes**2, axis=-1)


        m_ij = self.phi_e(jnp.concatenate([sq_norms_nodes, h_combos], axis=-1))
        m_ij = m_ij.at[jnp.arange(x.shape[0]), jnp.arange(x.shape[0])].set(0.0)  # explicitly set diagonal to 0

        # Equation 4(b)
        e = self.phi_inf(m_ij)
        e = e.at[jnp.arange(x.shape[0]), jnp.arange(x.shape[0])].set(0.0)  # explicitly set diagonal to 0
        m_i = jnp.einsum('ijd,ij->id', m_ij, jnp.squeeze(e, axis=-1))

        # Equation 5(a)
        phi_x_out = self.phi_x(m_ij)
        phi_x_out = phi_x_out.at[jnp.arange(x.shape[0]), jnp.arange(x.shape[0])].set(0.0)  # explicitly set diagonal to 0

        # TODO: add "cross attention" shifting term also here.
        if self.normalize_by_x_norm:
            norm = jnp.sqrt(sq_norms_nodes + 1e-8) + self.normalization_constant
            if self.stop_gradient_for_norm:
                norm = jax.lax.stop_gradient(norm)
            norm_diff_combo = diff_combos_nodes / norm[..., None]
            norm_diff_combo = norm_diff_combo.at[jnp.arange(x.shape[0]), jnp.arange(x.shape[0])].set(0.0)
            equivariant_shift = jnp.einsum('ijhd,ijh->ihd', norm_diff_combo, phi_x_out)
        else:
            equivariant_shift = jnp.einsum('ijhd,ijh->id', diff_combos_nodes, phi_x_out)


        if self.agg == 'mean':
            equivariant_shift = equivariant_shift / (n_nodes - 1)
        else:
            assert self.agg == 'sum'

        x_new = x + equivariant_shift

        # Equation 5(b)
        phi_h = hk.Sequential([self.phi_h_mlp, hk.Linear(h.shape[-1])])
        phi_h_in = jnp.concatenate([m_i, h], axis=-1)
        h_new = phi_h(phi_h_in)
        if self.residual_h:
            h_new = h + h_new

        return x_new, h_new


class HConfig(NamedTuple):
    """Config for h (node features), see https://arxiv.org/pdf/2105.09016.pdf."""
    h_embedding_dim: int = 3   # Dimension of h embedding in the EGCL.
    h_out: bool = False  # Whether to output h (it may be used as an invariant scale parameter for example).
    h_out_dim: int = 1  # Number of dimensions of h output by the EGNN.
    share_h: bool = True   # Whether to use the h from the EGCL for the computation of h-out.
    linear_softmax: bool = True    # Linear layer followed by softmax for improving stability.
    residual: bool = True


class MultiEgnnConfig(NamedTuple):
    """Config of the EGNN."""
    name: str = ""
    n_heads: int = 3
    mlp_units: Sequence[int] = (3,)
    identity_init_x: bool = False
    zero_init_h: int = False
    n_layers: int = 3  # Number of EGCL layers.
    h_config: HConfig = HConfig()
    hk_layer_stack: bool = True  # To lower compile time.
    compile_n_unroll: int = 1
    normalize_by_norms: bool = True
    emile_net: bool = False
    activation_fn: Callable = jax.nn.silu
    tanh: bool = False
    phi_x_max: float = 2.0
    agg: str = 'mean'
    stop_gradient_for_norm: bool = False
    variance_scaling_init: float = 0.001
    normalization_constant: float = 1.0


class se_equivariant_net(hk.Module):
    def __init__(self, config: MultiEgnnConfig):
        super().__init__(name=config.name + "_egnn")
        if config.hk_layer_stack:
            self.egnn_layer_fn = lambda x, h: EGCL_Multi(config.name,
                                                   n_heads=config.n_heads,
                                                   mlp_units=config.mlp_units,
                                                   identity_init_x=config.identity_init_x,
                                                   normalize_by_x_norm=config.normalize_by_norms,
                                                   residual=config.h_config.residual,
                                                   activation_fn=config.activation_fn,
                                                   tanh=config.tanh,
                                                   phi_x_max=config.phi_x_max,
                                                   agg=config.agg,
                                                   stop_gradient_for_norm=config.stop_gradient_for_norm,
                                                   variance_scaling_init=config.variance_scaling_init,
                                                   normalization_constant=config.normalization_constant
                                                   )(x, h)
        else:
            self.egnn_layers = [EGCL_Multi(config.name,
                               n_heads=config.n_heads,
                               mlp_units=config.mlp_units,
                               identity_init_x=config.identity_init_x,
                               normalize_by_x_norm=config.normalize_by_norms,
                               residual=config.h_config.residual,
                               activation_fn=config.activation_fn,
                               tanh=config.tanh,
                               phi_x_max=config.phi_x_max,
                               agg=config.agg,
                               stop_gradient_for_norm=config.stop_gradient_for_norm,
                               variance_scaling_init=config.variance_scaling_init,
                               normalization_constant=config.normalization_constant
                               ) for _ in range(config.n_layers)]

        if config.h_config.h_out:
            self.h_final_layer = hk.Linear(config.h_config.h_out_dim, w_init=jnp.zeros, b_init=jnp.zeros) \
                if config.zero_init_h else hk.Linear(config.h_config.h_out_dim)
        self.config = config

    def __call__(self, x):
        if len(x.shape) == 2:
            return self.forward_single(x)
        else:
            return hk.vmap(self.forward_single, split_rng=False)(x)
    
    def forward_single(self, x):
        """Compute forward pass of EGNN for a single x (no batch dimension)."""
        x_original = x
        # Perform forward pass of EGNN.

        # No node feature, so initialise them invariant fn of x.
        diff_combos = x - x[:, None]  # [n_nodes, n_nodes, dim]
        diff_combos = diff_combos.at[jnp.arange(x.shape[0]), jnp.arange(x.shape[0])].set(0.0)  # prevents nan grads
        sq_norms = jnp.sum(diff_combos**2, axis=-1)
        h = hk.Linear(self.config.h_config.h_embedding_dim)(sq_norms[..., None])

        if self.config.h_config.linear_softmax:
            h = h.at[jnp.arange(x.shape[0]), jnp.arange(x.shape[0])].set(-1e30)
            h = jax.nn.softmax(h, axis=-2)
        h = jnp.mean(h, axis=-2)


        # Project to number of heads
        x = jnp.repeat(x[:, None, ...], repeats=self.config.n_heads, axis=1)

        if self.config.hk_layer_stack:
            # Use layer_stack to speed up compilation time.
            stack = hk.experimental.layer_stack(self.config.n_layers, with_per_layer_inputs=False, name="EGCL_layer_stack",
                                                unroll=self.config.compile_n_unroll)
            x_out, h_egnn = stack(self.egnn_layer_fn)(x, h)
        else:
            x_out, h_egnn = x, h
            for layer in self.egnn_layers:
                x_out, h_egnn = layer(x_out, h_egnn)

        if not self.config.h_config.h_out:
            return x_out
        else:  # Extra processing to get h_out.

            # Pass square norms of x as a feature.
            sq_norms = get_norms_sqrd(x_original)[..., None]

            if self.config.h_config.linear_softmax:
                sq_norms = hk.Linear(self.config.h_config.h_embedding_dim)(sq_norms)
                sq_norms = sq_norms.at[jnp.arange(x.shape[0]), jnp.arange(x.shape[0])].set(-1e30)
                sq_norms = jax.nn.softmax(sq_norms, axis=-2)

            mlp_out = hk.nets.MLP((*self.config.mlp_units, self.config.h_config.h_embedding_dim),
                                  activate_final=True, activation=self.config.activation_fn)(sq_norms)
            h_out = jnp.mean(mlp_out, axis=-2)

            if self.config.h_config.share_h:
                # Use h_egnn output from the EGNN as a feature for h-out.
                if self.config.h_config.linear_softmax:
                    h_egnn = jax.nn.softmax(h_egnn, axis=-1)
                h_out = jnp.concatenate([h_out, h_egnn], axis=-1)

            h_out = self.h_final_layer(h_out)
            return x_out, h_out


class TransformerConfig(NamedTuple):
    output_dim: Optional[int] = None
    num_heads: int = 3
    key_size: int = 4
    w_init_scale: float = 0.1
    mlp_units: Sequence[int] = (32, 32)
    n_layers: int = 3
    layer_stack: bool = True
    compile_n_unroll: int = 1
    zero_init: bool = False
    layer_norm: bool = True


class TransformerBlock(hk.Module):
    # Largely follows: https://theaisummer.com/jax-transformer/
    def __init__(self, name: str, config: TransformerConfig = TransformerConfig()):
        super().__init__(name=name)
        self.config = config

    def __call__(self, x):
        # Simplifying assumption for now to make residual connections and layer stacking easy.
        chex.assert_tree_shape_suffix(x, (self.config.key_size*self.config.num_heads,))

        x_in = hk.LayerNorm(axis=-1, create_scale=True, create_offset=True)(x) if self.config.layer_norm else x
        x_attn = hk.MultiHeadAttention(num_heads=self.config.num_heads, key_size=self.config.key_size,
                                       w_init=hk.initializers.VarianceScaling(self.config.w_init_scale))(
            x_in, x_in, x_in)
        x = x + x_attn
        x_in = hk.LayerNorm(axis=-1, create_scale=True, create_offset=True)(x) if self.config.layer_norm else x
        x_dense = hk.nets.MLP([*self.config.mlp_units, self.config.num_heads*self.config.key_size])(x_in)
        x = x + x_dense
        return x


class Transformer(hk.Module):
    def __init__(self, name: str, config: TransformerConfig = TransformerConfig()):
        super().__init__(name=name)
        self.config = config
        self.transformer_block_fn = lambda x: TransformerBlock(name=name, config=config)(x)

    def __call__(self, x):
        x_out = jax.nn.relu(hk.Linear(self.config.num_heads * self.config.key_size)(x))
        if self.config.layer_stack:
            stack = hk.experimental.layer_stack(self.config.n_layers, with_per_layer_inputs=False,
                                                name="EGCL_layer_stack",
                                                unroll=self.config.compile_n_unroll)
            x_out = stack(self.transformer_block_fn)(x_out)
        else:
            for i in range(self.config.n_layers):
                x_out = self.transformer_block_fn(x_out)
        if self.config.output_dim is not None:
            final_layer = hk.Linear(self.config.output_dim, w_init=jnp.zeros, b_init=jnp.zeros) \
                if self.config.zero_init else hk.Linear(self.config.output_dim,
                                                        w_init=hk.initializers.VarianceScaling(0.01),
                                                        b_init=hk.initializers.VarianceScaling(0.01))
            x_out = final_layer(x_out)
        return x_out
