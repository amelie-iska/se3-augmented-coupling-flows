from typing import Tuple, Callable, Union

import chex
import distrax
import jax
import jax.numpy as jnp

from molboil.utils.graph_utils import get_senders_and_receivers_fully_connected

from utils.numerical import vector_rejection, safe_norm
from flow.distrax_with_extra import BijectorWithExtra, Array, BlockWithExtra, Extra

BijectorParams = chex.Array


def project(x, origin, change_of_basis_matrix):
    chex.assert_rank(x, 1)
    chex.assert_rank(change_of_basis_matrix, 2)
    chex.assert_equal_shape((x, origin, change_of_basis_matrix[0], change_of_basis_matrix[:, 0]))
    return change_of_basis_matrix.T @ (x - origin)

def unproject(x, origin, change_of_basis_matrix):
    chex.assert_rank(x, 1)
    chex.assert_rank(change_of_basis_matrix, 2)
    chex.assert_equal_shape((x, origin, change_of_basis_matrix[0], change_of_basis_matrix[:, 0]))
    return change_of_basis_matrix @ x + origin

def get_min_k_vectors_by_norm(norms, vectors, receivers, n_vectors, node_index):
    _, min_k_indices = jax.lax.top_k(-norms[receivers == node_index], n_vectors)
    min_k_vectors = vectors[receivers == node_index][min_k_indices]
    return min_k_vectors


def get_directions_for_closest_atoms(x: chex.Array, n_vectors: int) -> chex.Array:
    chex.assert_rank(x, 2)  # [n_nodes, dim]
    n_nodes, dim = x.shape
    senders, receivers = get_senders_and_receivers_fully_connected(dim)
    vectors = x[receivers] - x[senders]
    norms = safe_norm(vectors, axis=-1, keepdims=False)
    min_k_vectors = jax.vmap(get_min_k_vectors_by_norm, in_axes=(None, None, None, None, 0))(
        norms, vectors, receivers, n_vectors, jnp.arange(n_nodes))
    chex.assert_shape(min_k_vectors, (n_nodes, n_vectors, dim))
    return min_k_vectors




def get_new_space_basis(various_x_vectors: chex.Array, add_small_identity: bool = True):
    n_nodes, n_vectors, dim = various_x_vectors.shape
    # Calculate new basis for the affine transform
    basis_vectors = jnp.swapaxes(various_x_vectors, 0, 1)


    if add_small_identity:
        # Add independant vectors to try help improve numerical stability
        basis_vectors = basis_vectors + jnp.eye(dim)[:n_vectors][:, None, :]*1e-6


    z_basis_vector = basis_vectors[0]
    z_basis_vector = z_basis_vector / safe_norm(z_basis_vector, axis=-1, keepdims=True)
    if dim == 3:
        chex.assert_tree_shape_suffix(various_x_vectors, (3,))
        x_basis_vector = basis_vectors[1]
        # Compute reference axes.
        x_basis_vector = x_basis_vector / safe_norm(x_basis_vector, axis=-1, keepdims=True)
        x_basis_vector = vector_rejection(x_basis_vector, z_basis_vector)
        x_basis_vector = x_basis_vector / safe_norm(x_basis_vector, axis=-1, keepdims=True)
        y_basis_vector = jnp.cross(z_basis_vector, x_basis_vector)
        y_basis_vector = y_basis_vector / safe_norm(y_basis_vector, axis=-1, keepdims=True)
        change_of_basis_matrix = jnp.stack([z_basis_vector, x_basis_vector, y_basis_vector], axis=-1)
    else:
        chex.assert_tree_shape_suffix(various_x_vectors, (2,))
        y_basis_vector = vector_rejection(jnp.ones_like(z_basis_vector), z_basis_vector)
        y_basis_vector = y_basis_vector / safe_norm(y_basis_vector, axis=-1, keepdims=True)
        change_of_basis_matrix = jnp.stack([z_basis_vector, y_basis_vector], axis=-1)


    chex.assert_shape(change_of_basis_matrix, (n_nodes, dim, dim))
    return change_of_basis_matrix


class ProjSplitCoupling(BijectorWithExtra):
    def __init__(self,
                 split_index: int,
                 event_ndims: int,
                 graph_features: chex.Array,
                 get_basis_vectors_and_invariant_vals: Callable,
                 bijector: Callable[[BijectorParams], Union[BijectorWithExtra, distrax.Bijector]],
                 origin_on_coupled_pair: bool = True,
                 swap: bool = False,
                 split_axis: int = -1):
        super().__init__(event_ndims_in=event_ndims, is_constant_jacobian=False)
        if split_index < 0:
          raise ValueError(
              f'The split index must be non-negative; got {split_index}.')
        if split_axis >= 0:
          raise ValueError(f'The split axis must be negative; got {split_axis}.')
        if event_ndims < 0:
          raise ValueError(
              f'`event_ndims` must be non-negative; got {event_ndims}.')
        if split_axis < -event_ndims:
          raise ValueError(
              f'The split axis points to an axis outside the event. With '
              f'`event_ndims == {event_ndims}`, the split axis must be between -1 '
              f'and {-event_ndims}. Got `split_axis == {split_axis}`.')
        self._origin_on_aug = origin_on_coupled_pair
        self._split_index = split_index
        self._bijector = bijector
        self._swap = swap
        self._split_axis = split_axis
        self._get_basis_vectors_and_invariant_vals = get_basis_vectors_and_invariant_vals
        self._graph_features = graph_features
        super().__init__(event_ndims_in=event_ndims)

    def _split(self, x: Array) -> Tuple[Array, Array]:
        x1, x2 = jnp.split(x, [self._split_index], self._split_axis)
        if self._swap:
          x1, x2 = x2, x1
        return x1, x2

    def _recombine(self, x1: Array, x2: Array) -> Array:
        if self._swap:
          x1, x2 = x2, x1
        return jnp.concatenate([x1, x2], self._split_axis)

    def _inner_bijector(self, params: BijectorParams) -> Union[BijectorWithExtra, distrax.Bijector]:
      """Returns an inner bijector for the passed params."""
      bijector = self._bijector(params)
      if bijector.event_ndims_in != bijector.event_ndims_out:
          raise ValueError(
              f'The inner bijector must have `event_ndims_in==event_ndims_out`. '
              f'Instead, it has `event_ndims_in=={bijector.event_ndims_in}` and '
              f'`event_ndims_out=={bijector.event_ndims_out}`.')
      extra_ndims = self.event_ndims_in - bijector.event_ndims_in
      if extra_ndims < 0:
          raise ValueError(
              f'The inner bijector can\'t have more event dimensions than the '
              f'coupling bijector. Got {bijector.event_ndims_in} for the inner '
              f'bijector and {self.event_ndims_in} for the coupling bijector.')
      elif extra_ndims > 0:
          if isinstance(bijector, BijectorWithExtra):
              bijector = BlockWithExtra(bijector, extra_ndims)
          else:
              bijector = distrax.Block(bijector, extra_ndims)
      return bijector

    def get_basis_and_h(self, x: chex.Array, graph_features: chex.Array) ->\
            Tuple[chex.Array, chex.Array, chex.Array, Extra]:
        chex.assert_rank(x, 3)
        n_nodes, multiplicity, dim = x.shape

        # Calculate new basis for the affine transform
        vectors_out, h = self._get_basis_vectors_and_invariant_vals(x, graph_features)
        if self._origin_on_aug:
            origin = x
            vectors = vectors_out
        else:
            origin = x + vectors_out[:, :, 0]
            vectors = vectors_out[:, :, 1:]
        # jax.vmap(get_directions_for_closest_atoms, in_axes=(1, None), out_axes=1)(x, vectors.shape[2])
        # Vmap over multiplicity.
        change_of_basis_matrix = jax.vmap(get_new_space_basis, in_axes=1, out_axes=1)(vectors)

        # Stack h, and x projected into the space.
        x_proj = jax.vmap(jax.vmap(project))(x, origin, change_of_basis_matrix)
        bijector_feat_in = jnp.concatenate([x_proj, h], axis=-1)
        extra = self.get_extra(vectors)
        return origin, change_of_basis_matrix, bijector_feat_in, extra

    def get_vector_info_single(self, basis_vectors: chex.Array) -> Tuple[chex.Array, chex.Array, chex.Array]:
        basis_vectors = basis_vectors + jnp.eye(basis_vectors.shape[-1])[:basis_vectors.shape[1]][None, :, :] * 1e-30
        vec1 = basis_vectors[:, 0]
        vec2 = basis_vectors[:, 1]
        arccos_in = jax.vmap(jnp.dot)(vec1, vec2) / safe_norm(vec1, axis=-1) / safe_norm(vec2, axis=-1)
        theta = jax.vmap(jnp.arccos)(arccos_in)
        log_barrier_in = 1 - jnp.abs(arccos_in) + 1e-6
        aux_loss = - jnp.log(log_barrier_in)
        return theta, aux_loss, log_barrier_in

    def get_extra(self, various_x_points: chex.Array) -> Extra:
        theta, aux_loss, log_barrier_in = jax.vmap(self.get_vector_info_single)(various_x_points)
        info = {}
        info_aggregator = {}
        info_aggregator.update(
            mean_abs_theta=jnp.mean, min_abs_theta=jnp.min,
            min_log_barrier_in=jnp.min
        )
        info.update(
            mean_abs_theta=jnp.mean(jnp.abs(theta)), min_abs_theta=jnp.min(jnp.abs(theta)),
            min_log_barrier_in=jnp.min(log_barrier_in)
        )
        aux_loss = jnp.mean(aux_loss)
        extra = Extra(aux_loss=aux_loss, aux_info=info, info_aggregator=info_aggregator)
        return extra

    def forward_and_log_det_single(self, x: Array, graph_features: chex.Array) -> Tuple[Array, Array]:
        """Computes y = f(x) and log|det J(f)(x)|."""
        self._check_forward_input_shape(x)
        x1, x2 = self._split(x)
        origin, change_of_basis_matrix, bijector_feat_in, _ = self.get_basis_and_h(x1, graph_features)
        x2_proj = jax.vmap(jax.vmap(project))(x2, origin, change_of_basis_matrix)
        y2, logdet = self._inner_bijector(bijector_feat_in).forward_and_log_det(x2_proj)
        y2 = jax.vmap(jax.vmap(unproject))(y2, origin, change_of_basis_matrix)
        return self._recombine(x1, y2), logdet

    def inverse_and_log_det_single(self, y: Array, graph_features: chex.Array) -> Tuple[Array, Array]:
        """Computes x = f^{-1}(y) and log|det J(f^{-1})(y)|."""
        self._check_inverse_input_shape(y)
        y1, y2 = self._split(y)
        origin, change_of_basis_matrix, bijector_feat_in, _ = self.get_basis_and_h(y1, graph_features)
        y2_proj = jax.vmap(jax.vmap(project))(y2, origin, change_of_basis_matrix)
        x2, logdet = self._inner_bijector(bijector_feat_in).inverse_and_log_det(y2_proj)
        x2 = jax.vmap(jax.vmap(unproject))(x2, origin, change_of_basis_matrix)
        return self._recombine(y1, x2), logdet

    def forward_and_log_det(self, x: Array) -> Tuple[Array, Array]:
        """Computes y = f(x) and log|det J(f)(x)|."""
        if len(x.shape) == 3:
            return self.forward_and_log_det_single(x, self._graph_features)
        elif len(x.shape) == 4:
            if self._graph_features.shape[0] != x.shape[0]:
                print("graph features has no batch size")
                return jax.vmap(self.forward_and_log_det_single, in_axes=(0, None))(x, self._graph_features)
            else:
                return jax.vmap(self.forward_and_log_det_single)(x, self._graph_features)
        else:
            raise NotImplementedError

    def inverse_and_log_det(self, y: Array) -> Tuple[Array, Array]:
        """Computes x = f^{-1}(y) and log|det J(f^{-1})(y)|."""
        if len(y.shape) == 3:
            return self.inverse_and_log_det_single(y, self._graph_features)
        elif len(y.shape) == 4:
            if self._graph_features.shape[0] != y.shape[0]:
                print("graph features has no batch size")
                return jax.vmap(self.inverse_and_log_det_single, in_axes=(0, None))(y, self._graph_features)
            else:
                return jax.vmap(self.inverse_and_log_det_single)(y, self._graph_features)
        else:
            raise NotImplementedError

    def forward_and_log_det_with_extra_single(self, x: Array, graph_features: chex.Array) -> Tuple[Array, Array, Extra]:
        """Computes y = f(x) and log|det J(f)(x)|."""
        self._check_forward_input_shape(x)
        x1, x2 = self._split(x)
        origin, change_of_basis_matrix, bijector_feat_in, extra = self.get_basis_and_h(x1, graph_features)
        x2_proj = jax.vmap(jax.vmap(project))(x2, origin, change_of_basis_matrix)
        y2, logdet = self._inner_bijector(bijector_feat_in).forward_and_log_det(x2_proj)
        y2 = jax.vmap(jax.vmap(unproject))(y2, origin, change_of_basis_matrix)
        return self._recombine(x1, y2), logdet, extra

    def inverse_and_log_det_with_extra_single(self, y: Array, graph_features: chex.Array) -> Tuple[Array, Array, Extra]:
        """Computes x = f^{-1}(y) and log|det J(f^{-1})(y)|."""
        self._check_inverse_input_shape(y)
        y1, y2 = self._split(y)
        origin, change_of_basis_matrix, bijector_feat_in, extra = self.get_basis_and_h(y1, graph_features)
        y2_proj = jax.vmap(jax.vmap(project))(y2, origin, change_of_basis_matrix)
        x2, logdet = self._inner_bijector(bijector_feat_in).inverse_and_log_det(y2_proj)
        x2 = jax.vmap(jax.vmap(unproject))(x2, origin, change_of_basis_matrix)
        return self._recombine(y1, x2), logdet, extra

    def forward_and_log_det_with_extra(self, x: Array) -> Tuple[Array, Array, Extra]:
        """Computes y = f(x) and log|det J(f)(x)|."""
        if len(x.shape) == 3:
            h, logdet, extra = self.forward_and_log_det_with_extra_single(x, self._graph_features)
        elif len(x.shape) == 4:
            if self._graph_features.shape[0] != x.shape[0]:
                print("graph features has no batch size")
                h, logdet, extra = jax.vmap(self.forward_and_log_det_with_extra_single, in_axes=(0, None))(
                    x, self._graph_features)
            else:
                h, logdet, extra = jax.vmap(self.forward_and_log_det_with_extra_single)(
                    x, self._graph_features)
            extra = extra._replace(aux_info=extra.aggregate_info(), aux_loss=jnp.mean(extra.aux_loss))
        else:
            raise NotImplementedError
        return h, logdet, extra

    def inverse_and_log_det_with_extra(self, y: Array) -> Tuple[Array, Array, Extra]:
        """Computes x = f^{-1}(y) and log|det J(f^{-1})(y)|."""
        if len(y.shape) == 3:
            x, logdet, extra = self.inverse_and_log_det_with_extra_single(y, self._graph_features)
        elif len(y.shape) == 4:
            if self._graph_features.shape[0] != y.shape[0]:
                print("graph features has no batch size")
                x, logdet, extra = jax.vmap(self.inverse_and_log_det_with_extra_single, in_axes=(0, None))(
                    y, self._graph_features)
            else:
                x, logdet, extra = jax.vmap(self.inverse_and_log_det_with_extra_single)(y, self._graph_features)
            extra = extra._replace(aux_info=extra.aggregate_info(), aux_loss=jnp.mean(extra.aux_loss))
        else:
            raise NotImplementedError
        return x, logdet, extra
