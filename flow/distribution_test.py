import chex
import matplotlib.pyplot as plt
import jax
import haiku as hk

from flow.test_utils import test_fn_is_invariant, test_fn_is_equivariant, bijector_test
from flow.distribution import make_equivariant_augmented_flow_dist


_N_FLOW_LAYERS = 4
_N_NODES = 16
_FLOW_TYPE = "vector_scale"  # "nice", "proj", 'vector_scale_shift' 'vector_scale'


def test_distribution():
    """Visualise samples from the distribution, and check that it's log prob is invariant to
    translation and rotation."""

    dim = 2
    n_nodes = _N_NODES
    n_layers = _N_FLOW_LAYERS
    batch_size = 5
    key = jax.random.PRNGKey(0)
    flow_type = _FLOW_TYPE
    identity_init = False


    @hk.transform
    def sample_and_log_prob_fn(sample_shape=()):
        distribution = make_equivariant_augmented_flow_dist(
            dim=dim, nodes=n_nodes, n_layers=n_layers,
            flow_identity_init=identity_init, type=flow_type)
        return distribution.sample_and_log_prob(seed=hk.next_rng_key(), sample_shape=sample_shape)


    @hk.without_apply_rng
    @hk.transform
    def log_prob_fn(x):
        distribution = make_equivariant_augmented_flow_dist(
            dim=dim, nodes=n_nodes, n_layers=n_layers,
            flow_identity_init=identity_init, type=flow_type)
        return distribution.log_prob(x)


    # Init params.
    key, subkey = jax.random.split(key)
    params = sample_and_log_prob_fn.init(subkey)

    key, subkey = jax.random.split(key)
    sample, log_prob = sample_and_log_prob_fn.apply(params, subkey, (batch_size,))

    log_prob_check = log_prob_fn.apply(params, sample)

    chex.assert_trees_all_close(log_prob, log_prob_check)

    plt.plot(sample[0, :, 0], sample[0, :, 1], 'o')
    plt.show()

    # Test log prob function is invariant.
    key, subkey = jax.random.split(key)
    test_fn_is_invariant(lambda x: log_prob_fn.apply(params, x), subkey, n_nodes=n_nodes)


def test_flow():
    dim = 2
    n_nodes = _N_NODES
    n_layers = _N_FLOW_LAYERS
    key = jax.random.PRNGKey(0)
    flow_type = _FLOW_TYPE
    identity_init = False

    @hk.without_apply_rng
    @hk.transform
    def forward_and_log_det(x):
        distribution = make_equivariant_augmented_flow_dist(
            dim=dim, nodes=n_nodes, n_layers=n_layers,
            flow_identity_init=identity_init, type=flow_type)
        return distribution.bijector.forward_and_log_det(x)

    @hk.without_apply_rng
    @hk.transform
    def inverse_and_log_det(x):
        distribution = make_equivariant_augmented_flow_dist(
            dim=dim, nodes=n_nodes, n_layers=n_layers,
            flow_identity_init=identity_init, type=flow_type)
        return distribution.bijector.inverse_and_log_det(x)


    bijector_test(forward_and_log_det, inverse_and_log_det, dim=dim, n_nodes=n_nodes)



if __name__ == '__main__':
    USE_64_BIT = True
    if USE_64_BIT:
        from jax.config import config
        config.update("jax_enable_x64", True)

    test_flow()
    test_distribution()

