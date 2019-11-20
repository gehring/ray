from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np

from rllib.models.tf import attention
from ray.rllib.utils import try_import_tf

tf = try_import_tf()


def bit_shift_generator(seq_length, shift, batch_size):
    while True:
        seq = np.random.choice([0., 1.], seq_length, batch_size, 1)
        targets = np.roll(seq, shift, axis=0).astype(np.int32)
        yield seq, targets


def make_model(seq_length, num_tokens, num_layers, attn_dim, num_heads, head_dim,
               ff_hidden_dim):

    pos_embedding = attention.relative_position_embedding(seq_length, attn_dim)

    layers = []
    for _ in range(num_layers):
        layers.append(
            attention.RelativeMultiHeadAttention(attn_dim, num_heads, head_dim,
                                                 pos_embedding)
        )
        layers.append(
            attention.PositionwiseFeedforward(attn_dim, ff_hidden_dim)
        )

    layers.append(tf.keras.layers.Dense(num_tokens))

    return tf.keras.Sequential(layers)


def train_loss(targets, outputs):
    loss = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=targets,
                                                          logits=outputs)
    return tf.reduce_mean(loss)


def train_bit_shift(seq_length, num_iterations, print_every_n):

    optimizer = tf.keras.optimizers.Adam(1e-2)

    model = make_model(
        seq_length,
        num_tokens=2,
        num_layers=1,
        attn_dim=10,
        num_heads=5,
        head_dim=5,
        ff_hidden_dim=10,
    )

    shift = 4
    train_batch = 10
    test_batch = 100
    data_gen = bit_shift_generator(seq_length,
                                   shift=shift,
                                   batch_size=train_batch)
    test_gen = bit_shift_generator(seq_length,
                                   shift=shift,
                                   batch_size=test_batch)

    loss_fn = lambda: train_loss(targets, model(inputs))
    var_fn = lambda: model.trainable_variables

    for i, (inputs, targets) in zip(range(num_iterations), data_gen):
        optimizer.minimize(loss_fn, var_fn)

        if i % print_every_n:
            test_inputs, test_targets = next(test_gen)
            print(i, train_loss(test_targets, model(test_inputs)))


if __name__ == "__main__":
    train_bit_shift(
        seq_length=10,
        num_iterations=1000,
        print_every_n=20,
    )
