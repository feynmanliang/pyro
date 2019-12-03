"""
This module implements the Causal Effect Variational Autoencoder [1], which
demonstrates a number of innovations including:

- a generative model for causal effect inference with hidden confounders;
- a model and guide with twin neural nets to allow imbalanced treatment; and
- a custom training loss that includes both ELBO terms and extra terms needed
  to train the guide to be able to answer counterfactual queries.

The main interface is the :class:`CEVAE` class, but users may customize by
using components :class:`Model`, :class:`Guide`,
:class:`TraceCausalEffect_ELBO` and utilities.

**References**

[1] C. Louizos, U. Shalit, J. Mooij, D. Sontag, R. Zemel, M. Welling (2017).
    | Causal Effect Inference with Deep Latent-Variable Models.
    | http://papers.nips.cc/paper/7223-causal-effect-inference-with-deep-latent-variable-models.pdf
    | https://github.com/AMLab-Amsterdam/CEVAE
"""
import logging

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

import pyro
import pyro.distributions as dist
from pyro import poutine
from pyro.infer import SVI, Trace_ELBO
from pyro.infer.util import torch_item
from pyro.nn import PyroModule
from pyro.optim import ClippedAdam

logger = logging.getLogger(__name__)


class FullyConnected(nn.Sequential):
    """
    Fully connected multi-layer network with ELU activations.
    """
    def __init__(self, sizes):
        layers = []
        for in_size, out_size in zip(sizes, sizes[1:]):
            layers.append(nn.Linear(in_size, out_size))
            layers.append(nn.ELU())
        layers.pop(-1)
        super().__init__(*layers)


class DiagNormalNet(nn.Module):
    """
    :class:`FullyConnected` network outputting a constrained ``loc,scale``
    pair.

    This is used to represent a conditional probability distribution of a
    ``sizes[-1]``-sized diagonal Normal random variable conditioned on a
    ``sizes[0]``-size real value, for example::

        net = DiagNormalNet([3, 4, 5])
        z = torch.randn(3)
        loc, scale = net(z)
        x = dist.Normal(loc, scale).sample()
    """
    def __init__(self, sizes):
        assert len(sizes) >= 2
        super().__init__()
        self.fc = FullyConnected(sizes[:-1] + [sizes[-1] * 2])

    def forward(self, x):
        loc_scale = self.fc(x)
        d = loc_scale.size(-1) // 2
        loc = loc_scale[..., :d]
        scale = nn.functional.softplus(loc_scale[..., d:]).clamp(min=1e-10)
        return loc, scale


class BernoulliNet(nn.Module):
    """
    :class:`FullyConnected` network outputting a single ``logits`` value.

    This is used to represent a conditional probability distribution of a
    single Bernoulli random variable conditioned on a ``sizes[0]``-sized real
    value, for example::

        net = BernoulliNet([3, 4])
        z = torch.randn(3)
        t = dist.Bernoulli(logits=net(z)).sample()
    """
    def __init__(self, sizes):
        assert len(sizes) >= 1
        super().__init__()
        self.fc = FullyConnected(sizes + [1])

    def forward(self, x):
        return self.fc(x).squeeze(-1)


class Model(PyroModule):
    """
    Generative model for a causal model with latent confounder ``z`` and binary
    treatment ``t``::

        z ~ p(z)      # latent confounder
        x ~ p(x|z)    # partial noisy observation of z
        t ~ p(t|z)    # treatment, whose application is biased by z
        y ~ p(y|t,z)  # outcome

    Each of these distributions is defined by a neural network.  The ``y``
    distribution is defined by a disjoint pair of neural networks defining
    ``p(y|t=0,z)`` and ``p(y|t=1,z)``; this allows highly imbalanced treatment.

    :param dict config: A dict specifying ``feature_dim``, ``latent_dim``,
        ``hidden_dim``, and ``num_layers``.
    """
    def __init__(self, config):
        self.latent_dim = config["latent_dim"]
        super().__init__()
        self.x_nn = DiagNormalNet([config["latent_dim"]] +
                                  [config["hidden_dim"]] * config["num_layers"] +
                                  [config["feature_dim"]])
        self.y0_nn = BernoulliNet([config["latent_dim"]] +
                                  [config["hidden_dim"]] * config["num_layers"])
        self.y1_nn = BernoulliNet([config["latent_dim"]] +
                                  [config["hidden_dim"]] * config["num_layers"])
        self.t_nn = BernoulliNet([config["latent_dim"]])

    def forward(self, x, t=None, y=None, size=None):
        if size is None:
            size = x.size(0)
        with pyro.plate("data", size, subsample=x):
            z = pyro.sample("z", self.z_dist())
            x = pyro.sample("x", self.x_dist(z), obs=x)
            t = pyro.sample("t", self.t_dist(z), obs=t)
            y = pyro.sample("y", self.y_dist(t, z), obs=y)
        return y

    def z_dist(self):
        return dist.Normal(0, 1).expand([self.latent_dim]).to_event(1)

    def x_dist(self, z):
        loc, scale = self.x_nn(z)
        return dist.Normal(loc, scale).to_event(1)

    def y_dist(self, t, z):
        # Parameters are not shared among t values.
        logits0 = self.y0_nn(z)
        logits1 = self.y1_nn(z)
        logits = torch.where(t.bool(), logits1, logits0)
        return dist.Bernoulli(logits=logits)

    def t_dist(self, z):
        logits = self.t_nn(z)
        return dist.Bernoulli(logits=logits)


class Guide(PyroModule):
    """
    Inference model for causal effect estimation with latent confounder ``z``
    and binary treatment ``t``::

        t ~ p(t|x)      # treatment
        y ~ p(y|t,x)    # outcome
        z ~ p(t|y,t,x)  # latent confounder, an embedding

    Each of these distributions is defined by a neural network.  The ``y`` and
    ``z`` distributions are defined by disjoint pairs of neural networks
    defining ``p(-|t=0,...)`` and ``p(-|t=1,...)``; this allows highly
    imbalanced treatment.

    :param dict config: A dict specifying ``feature_dim``, ``latent_dim``,
        ``hidden_dim``, and ``num_layers``.
    """
    def __init__(self, config):
        self.latent_dim = config["latent_dim"]
        super().__init__()
        self.t_nn = BernoulliNet([config["feature_dim"]])
        self.y_nn = FullyConnected([config["feature_dim"]] +
                                   [config["hidden_dim"]] * config["num_layers"])
        self.y0_nn = BernoulliNet([config["hidden_dim"]])
        self.y1_nn = BernoulliNet([config["hidden_dim"]])
        self.z0_nn = DiagNormalNet([1 + config["feature_dim"]] +
                                   [config["hidden_dim"]] * config["num_layers"] +
                                   [config["latent_dim"]])
        self.z1_nn = DiagNormalNet([1 + config["feature_dim"]] +
                                   [config["hidden_dim"]] * config["num_layers"] +
                                   [config["latent_dim"]])

    def forward(self, x, t=None, y=None, size=None):
        if size is None:
            size = x.size(0)
        with pyro.plate("data", size, subsample=x):
            # The t and y sites are needed for prediction, and participate in
            # the auxiliary CEVAE loss. We mark them auxiliary to indicate they
            # do not correspond to latent variables during training.
            t = pyro.sample("t", self.t_dist(x), obs=t, infer={"is_auxiliary": True})
            y = pyro.sample("y", self.y_dist(t, x), obs=y, infer={"is_auxiliary": True})
            # The z site participates only in the usual ELBO loss.
            pyro.sample("z", self.z_dist(y, t, x))

    def t_dist(self, x):
        logits = self.t_nn(x)
        return dist.Bernoulli(logits=logits)

    def y_dist(self, t, x):
        # The first n-1 layers are identical for all t values.
        hidden = self.y_nn(x)
        # In the final layer params are not shared among t values.
        logits0 = self.y0_nn(hidden)
        logits1 = self.y1_nn(hidden)
        logits = torch.where(t.bool(), logits1, logits0)
        return dist.Bernoulli(logits=logits)

    def z_dist(self, y, t, x):
        # Parameters are not shared among t values.
        y_x = torch.cat([y.unsqueeze(-1), x], dim=-1)
        loc0, scale0 = self.z0_nn(y_x)
        loc1, scale1 = self.z1_nn(y_x)
        loc = torch.where(t.bool().unsqueeze(-1), loc1, loc0)
        scale = torch.where(t.bool().unsqueeze(-1), scale1, scale0)
        return dist.Normal(loc, scale).to_event(1)


class TraceCausalEffect_ELBO(Trace_ELBO):
    """
    Loss function for training a :class:`CEVAE`.
    From [1], the CEVAE objective (to maximize) is::

        -loss = ELBO + log q(t|x) + log q(y|t,x)
    """
    def _differentiable_loss_particle(self, model_trace, guide_trace):
        # Construct -ELBO part.
        blocked_names = [name for name, site in guide_trace.nodes.items()
                         if site["type"] == "sample" and site["is_observed"]]
        blocked_guide_trace = guide_trace.copy()
        for name in blocked_names:
            del blocked_guide_trace.nodes[name]
        loss, surrogate_loss = super()._differentiable_loss_particle(
            model_trace, blocked_guide_trace)

        # Add log q terms.
        for name in blocked_names:
            log_q = guide_trace.nodes[name]["log_prob_sum"]
            loss = loss - torch_item(log_q)
            surrogate_loss = surrogate_loss - log_q

        return loss, surrogate_loss

    @torch.no_grad()
    def loss(self, model, guide, *args, **kwargs):
        return torch_item(self.differentiable_loss(model, guide, *args, **kwargs))


class CEVAE(nn.Module):
    """
    Main class implementing a Causal Effect VAE [1]. This assumes a graphical model

    .. graphviz:: :graphviz_dot: neato
        
        digraph {
            Z [pos="1,2!",style=filled];
            X [pos="2,1!"];
            y [pos="1,0!"];
            t [pos="0,1!"];
            Z -> X;
            Z -> t;
            Z -> y;
            t -> y;
        }

    where `t` is a binary treatment variable, `y` is an outcome, `Z` is
    an unobserved confounder, and `X` is a noisy function of the hidden
    confounder `Z`.

    Example::

        cevae = CEVAE(feature_dim=5)
        cevae.fit(x_train, t_train, y_train)
        ite = cevae.ite(x_test)  # individual treatment effect
        ate = ite.mean()         # average treatment effect

    :ivar Model ~CEVAE.model: Generative model.
    :ivar Guide ~CEVAE.guide: Inference model.
    :param int feature_dim: Dimension of the feature space `x`.
    :param int latent_dim: Dimension of the latent variable `z`.
        Defaults to 20.
    :param int hidden_dim: Dimension of hidden layers of fully connected
        networks. Defaults to 200.
    :param int num_layers: Number of hidden layers in fully connected networks.
    """
    def __init__(self, feature_dim, latent_dim=20, hidden_dim=200, num_layers=3):
        config = dict(feature_dim=feature_dim, latent_dim=latent_dim,
                     hidden_dim=hidden_dim, num_layers=num_layers)
        for name, size in config.items():
            if not (isinstance(size, int) and size > 0):
                raise ValueError("Expected {} > 0 but got {}".format(name, size))
        self.feature_dim = config["feature_dim"]

        super().__init__()
        self.model = Model(config)
        self.guide = Guide(config)

    def fit(self, x, t, y, num_epochs,
            batch_size=100,
            learning_rate=1e-3,
            learning_rate_decay=0.1,
            weight_decay=1e-4):
        """
        Train using :class:`~pyro.infer.svi.SVI` with the
        :class:`TraceCausalEffect_ELBO` loss.

        :param ~torch.Tensor x:
        :param ~torch.Tensor t:
        :param ~torch.Tensor y:
        :return: list of epoch losses
        """
        assert x.dim() == 2 and x.size(-1) == self.feature_dim
        assert t.shape == x.shape[:1]
        assert y.shape == y.shape[:1]

        dataset = TensorDataset(x, t, y)
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
        num_steps = num_epochs * len(dataloader)
        optim = ClippedAdam({"lr": learning_rate,
                             "weight_decay": weight_decay,
                             "lrd": learning_rate_decay ** (1 / num_steps)})
        svi = SVI(self.model, self.guide, optim, TraceCausalEffect_ELBO())
        losses = []
        for epoch in range(num_epochs):
            loss = 0
            for x, t, y in dataloader:
                loss += svi.step(x, t, y, size=len(dataset)) / len(dataset)
            logger.info("epoch {: >3d} loss = {:0.6g}".format(epoch, loss / len(dataloader)))
            losses.append(loss)
        return losses

    def ite(self, x, num_samples=100):
        r"""
        Computes Individual Treatment Effect for a batch of data ``x``.

        .. math::

            ITE(x) = \mathbb E\left[ \mathbf y \mid \mathbf X=x, do(\mathbf t=1) \right]
                   - \mathbb E\left[ \mathbf y \mid \mathbf X=x, do(\mathbf t=0) \right]

        This has complexity ``O(len(x) * num_samples ** 2``.

        :param ~torch.Tensor x: A batch of data.
        :param int num_samples: The number of monte carlo samples.
        :return: A ``len(x)``-sized tensor of estimated effects.
        :rtype: ~torch.Tensor
        """
        assert x.dim() == 2 and x.size(-1) == self.feature_dim

        with pyro.plate("num_particles", num_samples, dim=-2):
            with poutine.trace() as tr, poutine.block(hide=["y", "t"]):
                self.guide(x)
            with poutine.do(data=dict(t=torch.tensor(0.))):
                y0 = poutine.replay(self.model, tr.trace)(x)
            with poutine.do(data=dict(t=torch.tensor(1.))):
                y1 = poutine.replay(self.model, tr.trace)(x)
        return (y1 - y0).mean(0)
