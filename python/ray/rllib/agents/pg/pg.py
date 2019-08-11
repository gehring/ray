from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from ray.rllib.agents.trainer import with_common_config
from ray.rllib.agents.trainer_template import build_trainer
from ray.rllib.agents.pg.pg_policy import PGTFPolicy
from ray.rllib.agents.pg.eager_pg_policy import PGTFPolicy as EagerPGTFPolicy
from ray.rllib.utils import try_import_tf

# yapf: disable
# __sphinx_doc_begin__
DEFAULT_CONFIG = with_common_config({
    # No remote workers by default
    "num_workers": 0,
    # Learning rate
    "lr": 0.0004,
    # Use PyTorch as backend
    "use_pytorch": False,
    # Use TF eager:
    "use_eager": False,
})
# __sphinx_doc_end__
# yapf: enable


def get_policy_class(config):
    if config["use_pytorch"] and config["use_eager"]:
        raise ValueError(
            "Can't run in TF eager mode and PyTorch mode simultaneously")

    if config["use_pytorch"]:
        from ray.rllib.agents.pg.torch_pg_policy import PGTorchPolicy
        return PGTorchPolicy
    elif config["use_eager"]:
        tf = try_import_tf()
        tf.enable_eager_execution()
        return EagerPGTFPolicy
    else:
        return PGTFPolicy


PGTrainer = build_trainer(
    name="PG",
    default_config=DEFAULT_CONFIG,
    default_policy=PGTFPolicy,
    get_policy_class=get_policy_class)
