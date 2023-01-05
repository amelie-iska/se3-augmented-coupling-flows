import haiku as hk
import distrax

from flow.test_utils import bijector_test
from flow.bijector_scale_and_shift_along_vector import make_se_equivariant_vector_scale_shift


def test_bijector_with_proj(dim: int = 2, n_layers: int = 2):

    def make_flow():
        bijectors = []
        for i in range(n_layers):
            swap = i % 2 == 0
            bijector = make_se_equivariant_vector_scale_shift(dim, swap=swap, identity_init=False)
            bijectors.append(bijector)
        flow = distrax.Chain(bijectors)
        return flow

    @hk.without_apply_rng
    @hk.transform
    def bijector_forward(x):
        return make_flow().forward_and_log_det(x)

    @hk.without_apply_rng
    @hk.transform
    def bijector_backward(x):
        return make_flow().inverse_and_log_det(x)

    bijector_test(bijector_forward, bijector_backward, dim=dim, n_nodes=4)




if __name__ == '__main__':
    # Passing with 1 layer but not more?


    USE_64_BIT = True
    if USE_64_BIT:
        from jax.config import config

        config.update("jax_enable_x64", True)

    test_bijector_with_proj()
