"""Training with FAB. Note assumes fixed conditioning info."""

from typing import Callable, NamedTuple, Tuple

import chex
import jax.numpy as jnp
import jax.random
from jax.flatten_util import ravel_pytree
import optax
import numpy as np

from fabjax.sampling.smc import SequentialMonteCarloSampler, SMCState
from fabjax.buffer import PrioritisedBuffer, PrioritisedBufferState

from flow.aug_flow_dist import AugmentedFlow, AugmentedFlowParams, GraphFeatures, FullGraphSample
from utils.optimize import IgnoreNanOptState
from train.fab_train_no_buffer import flat_log_prob_components, build_smc_forward_pass

Params = chex.ArrayTree
LogProbFn = Callable[[chex.Array], chex.Array]
ParameterizedLogProbFn = Callable[[chex.ArrayTree, chex.Array], chex.Array]
Info = dict

def fab_loss_buffer_samples(
        params: AugmentedFlowParams,
        x: chex.Array,
        log_q_old: chex.Array,
        alpha: chex.Array,
        log_q_fn_apply: ParameterizedLogProbFn,
        w_adjust_clip: float,
) -> Tuple[chex.Array, Tuple[chex.Array, chex.Array]]:
    """Estimate FAB loss with a batch of samples from the prioritized replay buffer."""
    chex.assert_rank(x, 4)  # [batch_size, n_nodes, n_aug+1, dim]
    chex.assert_rank(log_q_old, 1)

    log_q = log_q_fn_apply(params, x)
    log_w_adjust = (1 - alpha) * (jax.lax.stop_gradient(log_q) - log_q_old)
    chex.assert_equal_shape((log_q, log_w_adjust))
    w_adjust = jnp.clip(jnp.exp(log_w_adjust), a_max=w_adjust_clip)
    return - jnp.mean(w_adjust * log_q), (log_w_adjust, log_q)



class TrainStateWithBuffer(NamedTuple):
    params: AugmentedFlowParams
    key: chex.PRNGKey
    opt_state: optax.OptState
    smc_state: SMCState
    buffer_state: PrioritisedBufferState



def build_fab_with_buffer_init_step_fns(
        flow: AugmentedFlow,
        log_p_x: LogProbFn,
        features: GraphFeatures,
        smc: SequentialMonteCarloSampler,
        buffer: PrioritisedBuffer,
        optimizer: optax.GradientTransformation,
        batch_size: int,
        n_updates_per_smc_forward_pass: int,
        alpha: float = 2.,
        w_adjust_clip: float = 10.,
):
    """Create the `init` and `step` functions that define the FAB algorithm."""
    assert smc.alpha == alpha

    n_nodes = features.shape[0]
    # Setup smc forward pass.
    smc_forward = build_smc_forward_pass(flow, log_p_x, features, smc, batch_size)
    features_with_multiplicity = features[:, None]
    event_shape = (n_nodes, 1 + flow.n_augmented, flow.dim_x)
    flat_event_shape = np.prod(event_shape)


    def init(key: chex.PRNGKey) -> TrainStateWithBuffer:
        """Initialise the flow, optimizer and smc states."""
        key1, key2, key3, key4 = jax.random.split(key, 4)
        dummy_sample = FullGraphSample(positions=jnp.zeros((n_nodes, flow.dim_x)), features=features)
        flow_params = flow.init(key1, dummy_sample)
        opt_state = optimizer.init(flow_params)
        smc_state = smc.init(key2)

        # Now run multiple forward passes of SMC to fill the buffer. This also
        # tunes the SMC state in the process.

        def body_fn(carry, xs):
            """fer."""
            smc_state = carry
            key = xs
            sample_flow, x_smc, log_w, log_q, smc_state, smc_info = smc_forward(flow_params, smc_state, key,
                                                                                unflatten_output=False)
            chex.assert_shape(x_smc, (batch_size, flat_event_shape))
            chex.assert_shape(log_w, (batch_size,))
            chex.assert_shape(log_q, (batch_size,))
            return smc_state, (x_smc, log_w, log_q)

        n_forward_pass = (buffer.min_lengtht_to_sample // batch_size) + 1
        smc_state, (x, log_w, log_q) = jax.lax.scan(body_fn, init=smc_state,
                                                    xs=jax.random.split(key3, n_forward_pass))

        buffer_state = buffer.init(x=jnp.reshape(x, (n_forward_pass*batch_size, flat_event_shape)),
                                               log_w=log_w.flatten(),
                                               log_q_old=log_q.flatten())

        return TrainStateWithBuffer(params=flow_params, key=key4, opt_state=opt_state,
                                    smc_state=smc_state, buffer_state=buffer_state)

    def one_gradient_update(carry: Tuple[AugmentedFlowParams, optax.OptState], xs: Tuple[chex.Array, chex.Array]):
        """Perform on update to the flow parameters with a batch of data from the buffer."""
        flow_params, opt_state = carry
        x, log_q_old = xs
        info = {}

        flatten, unflatten, log_p_flat_fn, log_q_flat_fn, flow_log_prob_apply = flat_log_prob_components(
            log_p_x=log_p_x, flow=flow, params=flow_params, features_with_multiplicity=features_with_multiplicity,
            event_shape=event_shape
        )

        x = unflatten(x)
        # Estimate loss and update flow params.
        (loss, (log_w_adjust, log_q)), grad = jax.value_and_grad(fab_loss_buffer_samples, has_aux=True)(
            flow_params, x, log_q_old, alpha, flow_log_prob_apply, w_adjust_clip)
        updates, new_opt_state = optimizer.update(grad, opt_state, params=flow_params)
        new_params = optax.apply_updates(flow_params, updates)
        grad_norm = optax.global_norm(grad)
        info.update(loss=loss)
        info.update(log10_grad_norm=jnp.log10(grad_norm))  # Makes scale nice for plotting
        info.update(log10_max_param_grad=jnp.log(jnp.max(ravel_pytree(grad)[0])))
        if isinstance(opt_state, IgnoreNanOptState):
            info.update(ignored_grad_count=opt_state.ignored_grads_count)
        return (new_params, new_opt_state), (info, log_w_adjust, log_q)

    @jax.jit
    @chex.assert_max_traces(4)
    def step(state: TrainStateWithBuffer) -> Tuple[TrainStateWithBuffer, Info]:
        """Perform a single iteration of the FAB algorithm."""
        info = {}

        # Sample from buffer.
        key, subkey = jax.random.split(state.key)
        x_buffer, log_q_old_buffer, indices = buffer.sample_n_batches(subkey, state.buffer_state, batch_size,
                                                                      n_updates_per_smc_forward_pass)
        # Perform sgd steps on flow.
        (new_flow_params, new_opt_state), (infos, log_w_adjust, log_q_old) = jax.lax.scan(
            one_gradient_update, init=(state.params, state.opt_state), xs=(x_buffer, log_q_old_buffer),
            length=n_updates_per_smc_forward_pass
        )
        # Adjust samples in the buffer.
        buffer_state = buffer.adjust(log_q=log_q_old.flatten(), log_w_adjustment=log_w_adjust.flatten(),
                                     indices=indices.flatten(),
                                     buffer_state=state.buffer_state)
        # Update info.
        for i in range(n_updates_per_smc_forward_pass):
            info.update(jax.tree_map(lambda x: x[i], infos))

        # Run smc and add samples to the buffer. Note this is done with the flow params before they were updated so that
        # this can occur in parallel (jax will do this after compilation).
        key, subkey = jax.random.split(key)
        sample_flow, x_smc, log_w, log_q, smc_state, smc_info = smc_forward(params=state.params,
                                                                            smc_state=state.smc_state, key=subkey,
                                                                            unflatten_output=False)
        info.update(smc_info)

        buffer_state = buffer.add(x=x_smc, log_w=log_w, log_q=log_q, buffer_state=buffer_state)

        new_state = TrainStateWithBuffer(params=new_flow_params, key=key, opt_state=new_opt_state,
                                         smc_state=smc_state, buffer_state=buffer_state)
        return new_state, info

    return init, step