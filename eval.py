#!/usr/bin/env python

import tensorflow as tf
import numpy as np
import os, sys, cPickle, time, glob, itertools, csv
from functools import partial
import tqdm
import argparse
import gym
from gym import envs
from train import rollout, vector_slice

def restore_vars(sess, checkpoint_path, meta_path):
    """ Restore saved net, global score and step, and epsilons OR
    create checkpoint directory for later storage. """
    saver = tf.train.import_meta_graph(meta_path)
    saver.restore(sess, checkpoint_path)

    print '* restoring from %s' % checkpoint_path
    print '* using metagraph from %s' % meta_path
    saver.restore(sess, checkpoint_path)
    return True

def to_argmax(policy_prob_func, obs):
    ps = policy_prob_func(obs)
    z = np.zeros(ps.shape)
    z[np.argmax(ps)] = 1.
    return z

def evaluate(checkpoint_path, meta_path, env_id, render_env, n_samples, use_argmax):
    with tf.Graph().as_default() as g:
        with tf.Session() as sess:
            restore_vars(sess, checkpoint_path, meta_path)

            # model
            obs_ph, keep_prob_ph = tf.get_collection('inputs')
            logits, probs = tf.get_collection('outputs')

            actions_taken_ph = tf.placeholder('int32')
            action_logits = vector_slice(tf.log(probs), actions_taken_ph)
            observed_reward_ph = tf.placeholder('float')
            objective = (observed_reward_ph) * tf.reduce_mean(action_logits)

            # env
            env = gym.make(env_id)
            spec = envs.registry.env_specs[env_id]
            print '* environment', env_id
            print 'observation space', env.observation_space
            print 'action space', env.action_space
            print 'timestep limit', spec.timestep_limit
            print 'reward threshold', spec.reward_threshold

            # evaluation
            episode_rewards = []
            episode_lengths = []

            # policy function
            if use_argmax:
                policy = partial(to_argmax, lambda obs: probs.eval(feed_dict={
                    obs_ph: [obs],
                    keep_prob_ph: 1.,
                })[0])
            else:
                policy = lambda obs: probs.eval(feed_dict={
                    obs_ph: [obs],
                    keep_prob_ph: 1.,
                })[0]

            obj_val = []
            for i in tqdm.tqdm(xrange(n_samples)):
                # rollout with policy
                observations, actions, rewards, done = rollout(
                    policy,
                    env,
                    spec.timestep_limit,
                    render_env,
                )
                episode_rewards.append(np.sum(rewards))
                episode_lengths.append(len(observations))

                val_feed = {
                    obs_ph: observations,
                    keep_prob_ph: 1.,
                    actions_taken_ph: actions,
                    observed_reward_ph: np.sum(rewards),
                }
                objective_val = sess.run([
                    objective,
                    ], feed_dict=val_feed)
                obj_val.append(objective.eval(feed_dict=val_feed))
            print 'obj', np.mean(obj_val)

            # summary
            print '* summary'
            print 'episode lengths:',
            print 'mean', np.mean(episode_lengths),
            print 'median', np.median(episode_lengths),
            print 'std', np.std(episode_lengths)
            print 'episode rewards:',
            print 'mean', np.mean(episode_rewards),
            print 'median', np.median(episode_rewards),
            print 'std', np.std(episode_rewards)

if __name__ == '__main__':
    # arguments
    parse = argparse.ArgumentParser()
    parse.add_argument('--checkpoint_path', required=True)
    parse.add_argument('--meta_path')
    parse.add_argument('--latest', action='store_true')
    parse.add_argument('--no_render', action='store_true')
    parse.add_argument('--n_samples', type=int, default=16)
    parse.add_argument('--env', default='CartPole-v0')
    parse.add_argument('--argmax', action='store_true')

    args = parse.parse_args()

    if args.latest:
        # use the latest checkpoint in the folder
        checkpoint_path = tf.train.latest_checkpoint(args.checkpoint_path)
    else:
        checkpoint_path = args.checkpoint_path

    if args.meta_path == None:
        meta_path = checkpoint_path + '.meta'
    else:
        meta_path = args.meta_path

    evaluate(checkpoint_path, meta_path, args.env, not args.no_render,
             args.n_samples, args.argmax)