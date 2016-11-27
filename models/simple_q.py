import tensorflow as tf
import numpy as np

def build_q_model(observation_shape, dim_action, reuse=False, trainable=True,
                  batch=None):
    obs_ph = tf.placeholder('float', [batch] + list(observation_shape), name='observation')
    keep_prob_ph = tf.placeholder('float', name='keep_prob')
    tf.add_to_collection('inputs', obs_ph)
    tf.add_to_collection('inputs', keep_prob_ph)

    fc1 = tf.contrib.layers.fully_connected(
        # inputs=tf.nn.dropout(obs_ph, keep_prob_ph),
        inputs=obs_ph,
        num_outputs=dim_action,
        biases_initializer=tf.zeros_initializer,
        weights_initializer=tf.contrib.layers.xavier_initializer(),
        activation_fn=None,
        trainable=trainable,
        scope='fc1'
    )
    tf.add_to_collection(tf.GraphKeys.ACTIVATIONS, fc1)

    action_values = fc1
    tf.add_to_collection('outputs', action_values)

    return obs_ph, keep_prob_ph, action_values
