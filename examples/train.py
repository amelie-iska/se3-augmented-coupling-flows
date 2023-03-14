from typing import Callable, Tuple, Optional, List, NamedTuple

import chex
import haiku as hk
import jax
import jax.numpy as jnp
import optax
import numpy as np
import wandb
from tqdm.autonotebook import tqdm
import matplotlib.pyplot as plt
import pickle
import os
import pathlib
from datetime import datetime
from omegaconf import DictConfig
import matplotlib as mpl
from functools import partial

from flow.build_flow import build_flow, FlowDistConfig, ConditionalAuxDistConfig
from flow.aug_flow_dist import FullGraphSample, AugmentedFlow
from nets.base import NetsConfig, MLPHeadConfig, EnTransformerTorsoConfig, E3GNNTorsoConfig, EgnnTorsoConfig, \
    MACETorsoConfig
from nets.transformer import TransformerConfig
from utils.plotting import plot_history
from utils.aug_flow_train_and_eval import eval_fn, ml_step
from utils.numerical import get_pairwise_distances
from utils.loggers import Logger, WandbLogger, ListLogger
from flow.distrax_with_extra import Extra


mpl.rcParams['figure.dpi'] = 150
TestData = chex.Array
TrainData = chex.Array
FlowSampleFn = Callable[[hk.Params, chex.PRNGKey, chex.Shape], chex.Array]
Plotter = Callable[[hk.Params, FlowSampleFn, TestData, TrainData], List[plt.Figure]]


def plot_orig_aug_centre_mass_diff_hist(samples,
                                        ax, max_distance=10,
                                        *args, **kwargs):
    dim = samples.shape[-1] // 2
    centre_mass_original = jnp.mean(samples[..., :dim], axis=-2)
    centre_mass_augmented = jnp.mean(samples[..., dim:], axis=-2)
    d = jnp.linalg.norm(centre_mass_original - centre_mass_augmented, axis=-1)
    d = d[jnp.isfinite(d)]
    d = d.clip(max=max_distance)  # Clip keep plot reasonable.
    ax.hist(d, bins=50, density=True, alpha=0.4, *args, **kwargs)


def plot_sample_hist(samples,
                     ax,
                     original_coords,  # or augmented
                     n_vertices: Optional[int] = None,
                     max_distance = 10, *args, **kwargs):
    """n_vertices argument allows us to look at pairwise distances for subset of vertices,
    to prevent plotting taking too long"""
    dim = samples.shape[-1] // 2
    dims = jnp.arange(dim) + (0 if original_coords else dim)
    n_vertices = samples.shape[1] if n_vertices is None else n_vertices
    n_vertices = min(samples.shape[1], n_vertices)
    differences = jax.jit(jax.vmap(get_pairwise_distances))(samples[:, :n_vertices, dims])
    mask = jnp.ones_like(differences, dtype=bool).at[:, jnp.arange(n_vertices), jnp.arange(n_vertices)].set(False)
    d = differences.flatten()
    d = d[mask.flatten()]
    d = d[jnp.isfinite(d)]
    d = d.clip(max=max_distance)  # Clip keep plot reasonable.
    ax.hist(d, bins=50, density=True, alpha=0.4, *args, **kwargs)

def plot_original_aug_norms_sample_hist(samples, ax, max_distance=10, *args, **kwargs):
    dim = samples.shape[-1] // 2
    norms = jnp.linalg.norm(samples[..., :dim] - samples[..., dim:], axis=-1).flatten()
    norms = norms.clip(max=max_distance)  # Clip keep plot reasonable.
    ax.hist(norms, bins=50, density=True, alpha=0.4, *args, **kwargs)



def default_plotter(params, flow_sample_fn, key, n_samples, train_data, test_data,
                    plotting_n_nodes: Optional[int] = None):

    # Plot interatomic distance histograms.
    fig1, axs = plt.subplots(2, 3, figsize=(15, 10))
    samples = flow_sample_fn(params, key, (n_samples,))

    for i, og_coords in enumerate([True, False]):
        plot_sample_hist(samples, axs[0, i], original_coords=og_coords, label="flow samples",
                         n_vertices=plotting_n_nodes)
        plot_sample_hist(samples, axs[1, i], original_coords=og_coords, label="flow samples",
                         n_vertices=plotting_n_nodes)
        plot_sample_hist(train_data[:n_samples], axs[0, i], original_coords=og_coords, label="train samples",
                         n_vertices=plotting_n_nodes)
        plot_sample_hist(test_data[:n_samples], axs[1, i], original_coords=og_coords, label="test samples",
                         n_vertices=plotting_n_nodes)

    plot_original_aug_norms_sample_hist(samples, axs[0, 2], label='flow samples')
    plot_original_aug_norms_sample_hist(train_data, axs[0, 2], label='train samples')
    plot_original_aug_norms_sample_hist(samples, axs[1, 2], label='flow samples')
    plot_original_aug_norms_sample_hist(test_data, axs[1, 2], label='test samples')

    axs[0, 0].set_title(f"norms between original coordinates")
    axs[0, 1].set_title(f"norms between augmented coordinates")
    axs[0, 2].set_title(f"norms between original-aug pairs")
    axs[0, 0].legend()
    axs[1, 0].legend()
    fig1.tight_layout()

    # Plot histogram for centre of mean
    fig2, axs2 = plt.subplots(1, 2, figsize=(10, 5))
    plot_orig_aug_centre_mass_diff_hist(samples, ax=axs2[0], label='flow samples')
    plot_orig_aug_centre_mass_diff_hist(train_data, ax=axs2[0], label='train samples')
    plot_orig_aug_centre_mass_diff_hist(samples, ax=axs2[1], label='flow samples')
    plot_orig_aug_centre_mass_diff_hist(test_data, ax=axs2[1], label='test samples')
    axs2[0].legend()
    axs2[1].legend()
    axs2[0].set_title("norms between original - aug centre of mass histogram")
    axs2[1].set_title("norms between original - aug centre of mass histogram")
    fig2.tight_layout()

    return [fig1, fig2]


def plot_and_maybe_save(plotter, params, flow: AugmentedFlow, key, plot_batch_size, train_data, test_data, epoch_n,
                        save: bool,
                        plots_dir):
    figures = plotter(params, flow, key, plot_batch_size, train_data, test_data)
    for j, figure in enumerate(figures):
        if save:
            figure.savefig(os.path.join(plots_dir, f"{j}_iter_{epoch_n}.png"))
        else:
            plt.show()
        plt.close(figure)


class OptimizerConfig(NamedTuple):
    init_lr: float
    use_schedule: bool
    optimizer_name: str = "adam"
    max_global_norm: Optional[float] = None
    peak_lr: Optional[float] = None
    end_lr: Optional[float] = None
    warmup_n_epoch: Optional[int] = None


def get_optimizer_and_step_fn(optimizer_config: OptimizerConfig, n_iter_per_epoch, total_n_epoch):
    if optimizer_config.use_schedule:
        lr = optax.warmup_cosine_decay_schedule(
            init_value=optimizer_config.init_lr,
            peak_value=optimizer_config.peak_lr,
            end_value=optimizer_config.end_lr,
            warmup_steps=optimizer_config.warmup_n_epoch * n_iter_per_epoch,
            decay_steps=n_iter_per_epoch*total_n_epoch
                                                     )
    else:
        lr = optimizer_config.init_lr

    grad_transforms = [optax.zero_nans()]

    if optimizer_config.max_global_norm:
        clipping_fn = optax.clip_by_global_norm(optimizer_config.max_global_norm)
        grad_transforms.append(clipping_fn)
    else:
        pass

    grad_transforms.append(getattr(optax, optimizer_config.optimizer_name)(lr))
    optimizer = optax.chain(*grad_transforms)
    return optimizer, lr, ml_step


class TrainConfig(NamedTuple):
    n_epoch: int
    dim: int
    n_nodes: int
    flow_dist_config: FlowDistConfig
    aug_target_global_centering: bool
    aug_target_scale: float
    load_datasets: Callable[[int, int, int], Tuple[FullGraphSample, FullGraphSample]]
    optimizer_config: OptimizerConfig
    batch_size: int
    K_marginal_log_lik: int
    logger: Logger
    seed: int
    n_plots: int
    n_eval: int
    n_checkpoints: int
    plot_batch_size: int
    use_flow_aux_loss: bool = False
    aux_loss_weight: float = 1.0
    plotter: Plotter = default_plotter
    train_set_size: Optional[int] = None
    test_set_size: Optional[int] = None
    save: bool = True
    save_dir: str = "/tmp"
    wandb_upload_each_time: bool = True
    scan_run: bool = True  # Set to False is useful for debugging.
    use_64_bit: bool = False
    with_train_info: bool = True  # Grab info from the flow during each forward pass.
    # Only log the info from the last iteration within each epoch. Reduces runtime a lot if an epoch has many iter.
    last_iter_info_only: bool = True


def setup_logger(cfg: DictConfig) -> Logger:
    if hasattr(cfg.logger, "wandb"):
        logger = WandbLogger(**cfg.logger.wandb, config=dict(cfg))
    elif hasattr(cfg.logger, "list_logger"):
        logger = ListLogger()
    else:
        raise Exception("No logger specified, try adding the wandb or "
                        "pandas logger to the config file.")
    return logger

def create_nets_config(nets_config: DictConfig):
    """Configure nets (MACE, EGNN, Transformer, MLP)."""
    nets_config = dict(nets_config)
    egnn_cfg = EgnnTorsoConfig(**dict(nets_config.pop("egnn"))) if "egnn" in nets_config.keys() else None
    e3gnn_config = E3GNNTorsoConfig(**dict(nets_config.pop("e3gnn"))) if "e3gnn" in nets_config.keys() else None
    mace_config = MACETorsoConfig(**dict(nets_config.pop("mace"))) if "mace" in nets_config.keys() else None
    e3transformer_cfg = EnTransformerTorsoConfig(**dict(nets_config.pop("e3transformer"))) if "e3transformer" in nets_config.keys() else None
    transformer_cfg = dict(nets_config.pop("transformer")) if "transformer" in nets_config.keys() else None
    transformer_config = TransformerConfig(**dict(transformer_cfg)) if transformer_cfg else None
    mlp_head_config = MLPHeadConfig(**nets_config.pop('mlp_head_config')) if "mlp_head_config" in \
                                                                             nets_config.keys() else None
    type = nets_config['type']
    nets_config = NetsConfig(type=type,
                             egnn_torso_config=egnn_cfg,
                             e3gnn_torso_config=e3gnn_config,
                             mace_torso_config=mace_config,
                             e3transformer_lay_config=e3transformer_cfg,
                             transformer_config=transformer_config,
                             mlp_head_config=mlp_head_config)
    return nets_config

def create_flow_config(cfg: DictConfig):
    """Create config for building the flow."""
    flow_cfg = cfg.flow
    print(f"creating flow of type {flow_cfg.type}")
    flow_cfg = dict(flow_cfg)
    nets_config = create_nets_config(flow_cfg.pop("nets"))
    base_config = dict(flow_cfg.pop("base"))
    base_config = ConditionalAuxDistConfig(**base_config)
    target_aux_config = ConditionalAuxDistConfig(**dict(cfg.target.aux_target))
    flow_dist_config = FlowDistConfig(
        **flow_cfg,
        nets_config=nets_config,
        base_aux_config=base_config,
        target_aux_config=target_aux_config
    )
    return flow_dist_config

def create_train_config(cfg: DictConfig, load_dataset, dim, n_nodes) -> TrainConfig:
    logger = setup_logger(cfg)

    training_config = dict(cfg.training)
    save_path = os.path.join(training_config.pop("save_dir"), str(datetime.now().isoformat()))
    if cfg.training.save:
        if hasattr(cfg.logger, "wandb"):
            # if using wandb then save to wandb path
            save_path = os.path.join(wandb.run.dir, save_path)
        pathlib.Path(save_path).mkdir(parents=True, exist_ok=True)
    else:
        save_path = ''


    flow_config = create_flow_config(cfg)

    optimizer_config = OptimizerConfig(**dict(training_config.pop("optimizer")))

    experiment_config = TrainConfig(
        dim=dim,
        n_nodes=n_nodes,
        flow_dist_config=flow_config,
        load_datasets=load_dataset,
        optimizer_config=optimizer_config,
        **training_config,
        logger=logger,
        save_dir=save_path,
        aug_target_global_centering=cfg.target.aug_global_centering,
        aug_target_scale=cfg.target.aug_scale
    )
    return experiment_config


def train(config: TrainConfig):
    """Generic Training script."""
    if config.use_64_bit:
        jax.config.update("jax_enable_x64", True)

    assert config.flow_dist_config.dim == config.dim
    assert config.flow_dist_config.nodes == config.n_nodes

    if config.save:
        plots_dir = os.path.join(config.save_dir, f"plots")
        pathlib.Path(plots_dir).mkdir(exist_ok=False)
        checkpoints_dir = os.path.join(config.save_dir, f"model_checkpoints")
        pathlib.Path(checkpoints_dir).mkdir(exist_ok=False)
    else:
        plots_dir = None
        checkpoints_dir = None

    checkpoint_iter = list(np.linspace(0, config.n_epoch - 1, config.n_checkpoints, dtype="int"))
    eval_iter = list(np.linspace(0, config.n_epoch - 1, config.n_eval, dtype="int"))
    plot_iter = list(np.linspace(0, config.n_epoch - 1, config.n_plots, dtype="int"))

    train_data, test_data = config.load_datasets(config.batch_size, config.train_set_size,
                                                                   config.test_set_size)

    # Define flow, and initialise params.
    flow_dist = build_flow(config.flow_dist_config)

    key = jax.random.PRNGKey(config.seed)
    key, subkey = jax.random.split(key)
    params = flow_dist.init(subkey, train_data[0:2])
    params_test = flow_dist.init(subkey, train_data[0])
    chex.assert_trees_all_equal_shapes(params, params_test)



    print(f"training data position shape of {train_data.positions.shape}, "
          f"feature shape of {train_data.features.shape}")
    chex.assert_tree_shape_suffix(train_data.positions, (config.n_nodes, config.dim))

    plot_and_maybe_save(config.plotter, params, flow_dist, key, config.plot_batch_size, train_data, test_data, 0,
                        config.save, plots_dir)

    optimizer, lr, step_fn = get_optimizer_and_step_fn(config.optimizer_config,
                                              n_iter_per_epoch=train_data.positions.shape[0] // config.batch_size,
                                              total_n_epoch=config.n_epoch)
    opt_state = optimizer.init(params)


    if config.scan_run:
        def scan_fn(carry, xs):
            params, opt_state, key = carry
            key, subkey = jax.random.split(key)
            x = xs
            params, opt_state, info = step_fn(params, x, opt_state, flow_dist, optimizer,
                                              subkey,
                                              config.use_flow_aux_loss, config.aux_loss_weight)
            if config.last_iter_info_only:
                info = None
            return (params, opt_state, key), info

        @jax.jit
        # @chex.assert_max_traces(2)
        def scan_epoch(params, opt_state, key, batched_data):
            if config.last_iter_info_only:
                final_batch = batched_data[-1]
                batched_data = batched_data[:-1]
            (params, opt_state, key), info = jax.lax.scan(scan_fn, (params, opt_state, key),
                                                                 batched_data,
                                                                 unroll=1)
            if config.last_iter_info_only:
                key, subkey = jax.random.split(key)
                params, opt_state, info = step_fn(params, final_batch, opt_state,
                                                  flow_dist,
                                                  optimizer,
                                                  subkey,
                                                  config.use_flow_aux_loss,
                                                  config.aux_loss_weight)
            return params, opt_state, key, info


    pbar = tqdm(range(config.n_epoch))

    @jax.jit
    # @chex.assert_max_traces(n=2)
    def shuffle_and_batchify_data(key, train_data=train_data, config=config):
        indices = jax.random.permutation(key, train_data.positions.shape[0])
        train_data = train_data[indices]
        batched_data = jax.tree_map(lambda x: jnp.reshape(x, (-1, config.batch_size, *x.shape[1:])), train_data)
        return batched_data

    for i in pbar:
        key, subkey = jax.random.split(key)
        batched_data = shuffle_and_batchify_data(subkey)

        if config.scan_run:
            key, subkey = jax.random.split(key)
            params, opt_state, key, info_out = scan_epoch(params, opt_state, subkey, batched_data)
            if config.last_iter_info_only:
                info = info_out
                info.update(epoch=i)
                info.update(n_optimizer_steps=opt_state[-1][0].count)
                if hasattr(lr, "__call__"):
                    info.update(lr=lr(info["n_optimizer_steps"]))
                config.logger.write(info)
                if jnp.isnan(info["grad_norm"]):
                    print("nan grad")
            else:
                for batch_index in range(batched_data.shape[0]):
                    info = jax.tree_map(lambda x: x[batch_index], info_out)
                    info.update(epoch=i)
                    info.update(n_optimizer_steps=opt_state[-1][0].count)
                    if hasattr(lr, "__call__"):
                        info.update(lr=lr(info["n_optimizer_steps"]))
                    config.logger.write(info)
                    if jnp.isnan(info["grad_norm"]):
                        print("nan grad")
        else:
            for i in range(batched_data.shape[0]):
                x = batched_data[i]
                key, subkey = jax.random.split(key)
                params, opt_state, info = step_fn(params, x, opt_state, flow_dist, optimizer,
                                                  subkey,
                                                  config.use_flow_aux_loss, config.aux_loss_weight
                                                  )
                config.logger.write(info)
                info.update(epoch=i)
                if jnp.isnan(info["grad_norm"]):
                    print("nan grad")

        if i in plot_iter:
            plot_and_maybe_save(config.plotter, params, flow_dist, key, config.plot_batch_size, train_data, test_data,
                                i + 1, config.save,
                                plots_dir)

        if i in eval_iter:
            key, subkey = jax.random.split(key)
            eval_info = eval_fn(params=params, x=test_data,
                                flow=flow_dist,
                                global_centering=config.aug_target_global_centering,
                                aug_scale=config.aug_target_scale,
                                key=subkey,
                                batch_size=config.batch_size,
                                K=config.K_marginal_log_lik)
            pbar.write(str(eval_info))
            eval_info.update(epoch=i)
            config.logger.write(eval_info)

        if i in checkpoint_iter and config.save:
            checkpoint_path = os.path.join(checkpoints_dir, f"iter_{i}/")
            pathlib.Path(checkpoint_path).mkdir(exist_ok=False)
            with open(os.path.join(checkpoint_path, "state.pkl"), "wb") as f:
                pickle.dump(params, f)

    if isinstance(config.logger, ListLogger):
        plot_history(config.logger.history)
        plt.show()

    return config.logger, params, flow_dist
