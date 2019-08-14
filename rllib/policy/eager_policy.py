from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import numpy as np

from ray.rllib.evaluation.episode import _flatten_action
from ray.rllib.models.catalog import ModelCatalog
from ray.rllib.policy.policy import Policy, LEARNER_STATS_KEY
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.policy.tf_policy import ACTION_PROB, ACTION_LOGP
from ray.rllib.utils import add_mixins
from ray.rllib.utils.annotations import override
from ray.rllib.utils.debug import log_once
from ray.rllib.utils import try_import_tf

tf = try_import_tf()
logger = logging.getLogger(__name__)


# TODO(ekl) decide what stuff goes in this class vs the builder below
class TFEagerPolicy(Policy):
    def __init__(self, model, observation_space, action_space, gradients_fn):
        self.model = model
        self.observation_space = observation_space
        self.action_space = action_space
        self.is_training = False
        self._gradients_fn = gradients_fn
        self._sess = None

    def _loss(self, outputs, samples):
        raise NotImplementedError

    def _stats(self, outputs, samples):
        raise NotImplementedError

    @override(Policy)
    def learn_on_batch(self, samples):
        grads_and_vars, stats = self._compute_gradients(samples)
        self.optimizer.apply_gradients(grads_and_vars)
        return stats

    @override(Policy)
    def compute_gradients(self, samples):
        grads_and_vars, stats = self._compute_gradients(samples)
        grads = [g for g, v in grads_and_vars]
        grads = [(g.numpy() if g is not None else None) for g in grads]
        return grads, stats

    def _compute_gradients(self, samples):
        """Computes and returns grads as eager tensors."""

        self.is_training = True

        samples = {
            k: tf.convert_to_tensor(v)
            for k, v in samples.items() if v.dtype != np.object
        }

        with tf.GradientTape() as tape:
            # TODO: set seq len and state in properly
            self.seq_lens = tf.ones(len(samples[SampleBatch.CUR_OBS]))
            self.state_in = []
            self.model_out, self.state_out = self.model(
                samples, self.state_in, self.seq_lens)
            if self.dist_class:
                self.action_dist = self.dist_class(self.model_out)
            loss = self._loss(self, samples)

        variables = self.model.trainable_variables()

        if self._gradients_fn:

            class OptimizerWrapper(object):
                def __init__(self, tape):
                    self.tape = tape

                def compute_gradients(self, loss, var_list):
                    return list(
                        zip(self.tape.gradient(loss, var_list), var_list))

            grads_and_vars = self._gradients_fn(self, OptimizerWrapper(tape),
                                                loss)
        else:
            grads_and_vars = list(
                zip(tape.gradient(loss, variables), variables))

        if log_once("grad_vars"):
            for _, v in grads_and_vars:
                logger.info("Optimizing variable {}".format(v.name))

        grads = [g for g, v in grads_and_vars]
        stats = self._stats(self, samples, grads)
        return grads_and_vars, stats

    @override(Policy)
    def apply_gradients(self, gradients):
        self.optimizer.apply_gradients(
            zip([(tf.convert_to_tensor(g) if g is not None else None)
                 for g in gradients], self.model.trainable_variables()))

    @override(Policy)
    def get_weights(self):
        return tf.nest.map_structure(lambda var: var.numpy(),
                                     self.model.variables())

    @override(Policy)
    def set_weights(self, weights):
        tf.nest.map_structure(lambda var, value: var.assign(value),
                              self.model.variables(), weights)

    @override(Policy)
    def export_model(self, export_dir):
        return NotImplementedError

    @override(Policy)
    def export_checkpoint(self, export_dir):
        return NotImplementedError

    def get_session(self):
        return None  # None implies eager

    def _get_is_training_placeholder(self):
        return tf.convert_to_tensor(self.is_training)


def build_tf_policy(name,
                    loss_fn,
                    get_default_config=None,
                    postprocess_fn=None,
                    stats_fn=None,
                    optimizer_fn=None,
                    gradients_fn=None,
                    grad_stats_fn=None,
                    extra_learn_fetches_fn=None,
                    extra_action_feed_fn=None,
                    extra_action_fetches_fn=None,
                    before_init=None,
                    before_loss_init=None,
                    after_init=None,
                    make_model=None,
                    action_sampler_fn=None,
                    mixins=None,
                    obs_include_prev_action_reward=True,
                    get_batch_divisibility_req=None):

    base = add_mixins(TFEagerPolicy, mixins)

    class policy_cls(base):
        def __init__(self, observation_space, action_space, config):
            assert tf.executing_eagerly()

            if get_default_config:
                config = dict(get_default_config(), **config)

            if before_init:
                before_init(self, observation_space, action_space, config)

            self.config = config
            self.extra_action_fetches_fn = extra_action_fetches_fn

            if action_sampler_fn:
                if not make_model:
                    raise ValueError(
                        "make_model is required if action_sampler_fn is given")
                self.dist_class = None
            else:
                self.dist_class, logit_dim = ModelCatalog.get_action_dist(
                    action_space, self.config["model"])
                self.logit_dim = logit_dim

            if make_model:
                model = make_model(self, observation_space, action_space,
                                   config)
            else:
                model = ModelCatalog.get_model_v2(
                    observation_space,
                    action_space,
                    logit_dim,
                    config["model"],
                    framework="tf",
                )

            model({
                SampleBatch.CUR_OBS: tf.convert_to_tensor(
                    np.array([observation_space.sample()])),
                SampleBatch.PREV_ACTIONS: tf.convert_to_tensor(
                    [_flatten_action(action_space.sample())]),
                SampleBatch.PREV_REWARDS: tf.convert_to_tensor([0.]),
            }, [tf.convert_to_tensor([s]) for s in model.get_initial_state()],
                  tf.convert_to_tensor([1]))

            TFEagerPolicy.__init__(self, model, observation_space,
                                   action_space, gradients_fn)
            if before_loss_init:
                before_loss_init(self, observation_space, action_space, config)

            self._do_loss_init()

            if optimizer_fn:
                self.optimizer = optimizer_fn(self, config)
            else:
                self.optimizer = tf.train.AdamOptimizer(config["lr"])

            if after_init:
                after_init(self, observation_space, action_space, config)

        def _do_loss_init(self):
            # Dummy forward pass to initialize any policy attributes, etc.
            action_dtype, action_shape = ModelCatalog.get_action_shape(
                self.action_space)
            dummy_batch = {
                SampleBatch.CUR_OBS: tf.convert_to_tensor(
                    np.array([self.observation_space.sample()])),
                SampleBatch.NEXT_OBS: tf.convert_to_tensor(
                    np.array([self.observation_space.sample()])),
                SampleBatch.DONES: tf.convert_to_tensor(
                    np.array([False], dtype=np.bool)),
                SampleBatch.ACTIONS: tf.convert_to_tensor(
                    np.zeros_like(
                        action_shape, dtype=action_dtype.as_numpy_dtype())),
                SampleBatch.REWARDS: tf.convert_to_tensor(
                    np.array([0], dtype=np.float32)),
            }
            if obs_include_prev_action_reward:
                dummy_batch.update({
                    SampleBatch.PREV_ACTIONS: dummy_batch[SampleBatch.ACTIONS],
                    SampleBatch.PREV_REWARDS: dummy_batch[SampleBatch.REWARDS],
                })
            state_init = self.get_initial_state()
            state_batches = []
            for i, h in enumerate(state_init):
                dummy_batch["state_in_{}".format(i)] = tf.convert_to_tensor(
                    np.expand_dims(h, 0))
                dummy_batch["state_out_{}".format(i)] = tf.convert_to_tensor(
                    np.expand_dims(h, 0))
                state_batches.append(
                    tf.convert_to_tensor(np.expand_dims(h, 0)))
            if state_init:
                dummy_batch["seq_lens"] = tf.convert_to_tensor(
                    np.array([1], dtype=np.int32))

            # Execute a forward pass to get self.action_dist etc initialized,
            # and also obtain the extra action fetches
            _, _, fetches = self.compute_actions(
                dummy_batch[SampleBatch.CUR_OBS], state_batches,
                dummy_batch.get(SampleBatch.PREV_ACTIONS),
                dummy_batch.get(SampleBatch.PREV_REWARDS))
            dummy_batch.update(fetches)

            postprocessed_batch = self.postprocess_trajectory(
                SampleBatch(dummy_batch))
            postprocessed_batch = {
                k: tf.convert_to_tensor(v)
                for k, v in postprocessed_batch.items()
            }

            loss_fn(self, postprocessed_batch)
            if stats_fn:
                stats_fn(self, postprocessed_batch)

        def postprocess_trajectory(self,
                                   samples,
                                   other_agent_batches=None,
                                   episode=None):
            assert tf.executing_eagerly()
            if postprocess_fn:
                return postprocess_fn(self, samples)
            else:
                return samples

        def compute_actions(self,
                            obs_batch,
                            state_batches,
                            prev_action_batch=None,
                            prev_reward_batch=None,
                            info_batch=None,
                            episodes=None,
                            **kwargs):

            assert tf.executing_eagerly()
            self.is_training = False

            seq_len = tf.ones(len(obs_batch))
            input_dict = {
                SampleBatch.CUR_OBS: tf.convert_to_tensor(obs_batch),
                "is_training": tf.convert_to_tensor(False),
            }
            if obs_include_prev_action_reward:
                input_dict.update({
                    SampleBatch.PREV_ACTIONS: tf.convert_to_tensor(
                        prev_action_batch),
                    SampleBatch.PREV_REWARDS: tf.convert_to_tensor(
                        prev_reward_batch),
                })
            self.state_in = state_batches
            self.model_out, self.state_out = self.model(
                input_dict, state_batches, seq_len)

            if self.dist_class:
                self.action_dist = self.dist_class(self.model_out)
                action = self.action_dist.sample().numpy()
                logp = self.action_dist.sampled_action_logp()
            else:
                action, logp = action_sampler_fn(
                    self, self.model, input_dict, self.observation_space,
                    self.action_space, self.config)
                action = action.numpy()

            fetches = {}
            if logp is not None:
                fetches.update({
                    ACTION_PROB: tf.exp(logp).numpy(),
                    ACTION_LOGP: logp.numpy(),
                })
            if extra_action_fetches_fn:
                fetches.update(extra_action_fetches_fn(self))
            return action, self.state_out, fetches

        def _loss(self, outputs, samples):
            assert tf.executing_eagerly()
            return loss_fn(outputs, samples)

        def _stats(self, outputs, samples, grads):
            assert tf.executing_eagerly()
            fetches = {}
            if stats_fn:
                fetches[LEARNER_STATS_KEY] = {
                    k: v.numpy()
                    for k, v in stats_fn(outputs, samples).items()
                }
            else:
                fetches[LEARNER_STATS_KEY] = {}
            if extra_learn_fetches_fn:
                fetches.update({
                    k: v.numpy()
                    for k, v in extra_learn_fetches_fn(self).items()
                })
            if grad_stats_fn:
                fetches.update({
                    k: v.numpy()
                    for k, v in grad_stats_fn(self, samples, grads).items()
                })
            return fetches

    policy_cls.__name__ = name
    return policy_cls
