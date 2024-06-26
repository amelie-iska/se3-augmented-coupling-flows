import chex
import jax

from eacf.utils.test import assert_is_invariant

from eacf.flow.base_dist import JointBaseDistribution
from eacf.flow.x_base_dist import CentreGravityGaussian, HarmonicPotential, \
    AldpTransformedInternals, assert_mean_zero


def test_base_distribution():
    """Test that the base distribution does not smoke. And that it's log prob is invariant to
    rotation and translation."""
    key = jax.random.PRNGKey(0)
    dim = 3
    n_nodes = 22
    n_aux = 3
    batch_size = 7
    shape = (batch_size, n_nodes,  n_aux + 1, dim)

    edges = list(zip(range(n_nodes - 1), range(1, n_nodes)))
    x_dists = [CentreGravityGaussian(dim=dim, n_nodes=n_nodes),
               HarmonicPotential(dim=dim, n_nodes=n_nodes, edges=edges),
               AldpTransformedInternals(data_path='eacf/targets/data/aldp_500K_train_mini.h5')]

    for x_dist in x_dists:
        dist = JointBaseDistribution(dim=dim, n_nodes=n_nodes, n_aux=n_aux,
                                     x_dist=x_dist)

        # Sample: Test that it does not smoke.
        sample = dist.sample(seed=key, sample_shape=batch_size)
        chex.assert_shape(sample, shape)
        assert_mean_zero(sample[:, :, 0], node_axis=1)

        # Log prob: Test that it is invariant to translation and rotation.
        log_prob = dist.log_prob(sample)
        chex.assert_shape(log_prob, (batch_size,))
        assert_is_invariant(invariant_fn=dist.log_prob, key=key, event_shape=shape[1:])


        # Single sample and log prob: Test that it does not smoke.
        sample = dist.sample(seed=key)
        log_prob = dist.log_prob(sample)
        chex.assert_shape(sample, (n_nodes, n_aux + 1, dim))
        chex.assert_shape(log_prob.shape, ())


if __name__ == '__main__':
    USE_64_BIT = True
    if USE_64_BIT:
        from jax.config import config
        config.update("jax_enable_x64", True)

    test_base_distribution()
