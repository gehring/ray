from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import ray
from ray.rllib.agents.pg import pg_policy
from ray.rllib.models import catalog
from ray.rllib.policy import eager_policy
from ray.rllib.utils import try_import_tf

tf = try_import_tf()


def make_model(policy, observation_space, action_space, config):
    del policy
    _, logit_dim = catalog.ModelCatalog.get_action_dist(
        action_space, config["model"])

    return catalog.ModelCatalog.get_model_v2(
        observation_space,
        action_space,
        logit_dim,
        config["model"],
        framework="tf",
    )


def make_optimizer(policy, observation_space, action_space, config):
    del policy, observation_space, action_space

    return tf.train.AdamOptimizer(config["lr"])


PGTFPolicy = eager_policy.build_tf_policy(
    name="PGTFPolicy",
    get_default_config=lambda: ray.rllib.agents.pg.pg.DEFAULT_CONFIG,
    postprocess_fn=pg_policy.postprocess_advantages,
    loss_fn=pg_policy.policy_gradient_loss,
    make_model=make_model,
    make_optimizer=make_optimizer,
)
