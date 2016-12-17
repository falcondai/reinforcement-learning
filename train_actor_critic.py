#!/usr/bin/env python

import tensorflow as tf
import numpy as np
import os, sys, cPickle, time, itertools, json
from Queue import deque
import tqdm
import argparse
import importlib
import gym
from util import pad_zeros, duplicate_obs, vector_slice, \
    get_current_run_id, restore_vars, rollout

def partial_rollout(behavior_policy, env_spec, env_step, env_reset, last_obs_q,
            last_done=True, env_render=None, n_update_ticks=256, n_obs_ticks=1):
    '''rollout based on behavior policy from an environment'''

    observations, actions, rewards, nonterminals = [], [], [], []
    t = 0
    done = last_done
    # continue with the previous obs queue
    obs_q = last_obs_q
    obs = last_obs_q[-1]
    while t < n_update_ticks:
        if done or t >= env_spec['timestep_limit']:
            obs = env_reset()
            # pad the first observation with zeros
            obs_q = deque(pad_zeros([obs], n_obs_ticks), n_obs_ticks)
        policy_input = np.concatenate(obs_q, axis=-1)
        action_probs = behavior_policy(policy_input)
        action = np.random.choice(env_spec['action_size'], p=action_probs)
        obs_q.popleft()
        observations.append(obs)
        actions.append(action)
        obs, reward, done = env_step(action)
        nonterminals.append(not done)
        rewards.append([reward])
        obs_q.append(obs)
        if env_render != None:
            env_render()
        t += 1
    return observations, actions, rewards, nonterminals, done, obs_q

def process_partial_rollout(observations, rewards, nonterminals, last_obs_q,
                            n_obs_ticks, reward_gamma, state_value_func):
    observations = list(last_obs_q)[:-1] + observations
    if nonterminals[-1]:
        last_state_value = state_value_func(np.concatenate(
            observations[-n_obs_ticks:], axis=-1))
    else:
        last_state_value = 0.
    acc_rewards = []
    for reward in rewards[::-1]:
        last_state_value = reward[0] + reward_gamma * last_state_value
        acc_rewards.insert(0, last_state_value)

    obs = list(duplicate_obs(observations, n_obs_ticks))
    return obs, acc_rewards

def process_ticks(observations, actions, rewards, nonterminals, last_obs_q,
                  n_obs_ticks, reward_gamma, state_value_func):
    split_observations = []
    split_rewards = []
    split_nonterminals = []
    obs_q = list(last_obs_q)
    merged_obs = []
    merged_acc_rewards = []
    for observation, reward, nonterminal in zip(observations, rewards,
                                                nonterminals):
        split_observations.append(observation)
        split_rewards.append(reward)
        split_nonterminals.append(nonterminal)
        if not nonterminal:
            obs, acc_rewards = process_partial_rollout(split_observations, split_rewards,
                                                       split_nonterminals, obs_q, n_obs_ticks,
                                                       reward_gamma, state_value_func)
            merged_obs += obs
            merged_acc_rewards += acc_rewards
            obs_q = pad_zeros(split_observations[-1:], n_obs_ticks)
            split_observations = []
            split_rewards = []
            split_nonterminals = []

    if len(split_rewards) > 0:
        obs_q = (list(last_obs_q)[:-1] + observations)[:n_obs_ticks]
        obs, acc_rewards = process_partial_rollout(split_observations, split_rewards,
                                                   split_nonterminals, obs_q, n_obs_ticks,
                                                   reward_gamma, state_value_func)
        merged_obs += obs
        merged_acc_rewards += acc_rewards
    return merged_obs, actions, merged_acc_rewards


def train(train_env, eval_env, args, build_model):
    env_spec, env_step, env_reset, env_render = train_env
    eval_env_spec, eval_env_step, eval_env_reset, eval_env_render = eval_env

    summary_dir = 'tf-log/%s%d-%s' % (args['summary_prefix'], time.time(),
                                      os.path.basename(args['checkpoint_dir']))

    # set seeds
    np.random.seed(args['np_seed'])
    tf.set_random_seed(args['tf_seed'])

    # create checkpoint dirs
    if not os.path.exists(args['checkpoint_dir']):
        os.makedirs(args['checkpoint_dir'])

    print '* training hyperparameters:'
    for k in sorted(args.keys()):
        print k, args[k]
    n_run = get_current_run_id(args['checkpoint_dir'])
    with open('%s/hyperparameters.%i.json' % (args['checkpoint_dir'], n_run),
              'wb') as hpf:
        json.dump(args, hpf)

    with tf.Graph().as_default() as g:
        # model
        print '* building model %s' % args['model']
        policy_input_shape = list(env_spec['observation_shape'])
        policy_input_shape[-1] *= args['n_obs_ticks']
        with tf.variable_scope('model'):
            obs_ph, keep_prob_ph, action_probs, state_value = build_model(
                policy_input_shape,
                env_spec['action_size'])
        # with tf.variable_scope('model', reuse=True):
        #     next_obs_ph, _, _, next_state_value = build_model(
        #         policy_input_shape,
        #         env_spec['action_size'])
        actions_taken_ph = tf.placeholder('int32')
        reward_ph = tf.placeholder('float')
        nonterminal_ph = tf.placeholder('float')
        target_value_ph = tf.placeholder('float')

        avg_v_objective_ph = tf.placeholder('float')
        avg_len_episode_ph = tf.placeholder('float')
        avg_episode_reward_ph = tf.placeholder('float')
        max_episode_reward_ph = tf.placeholder('float')
        min_episode_reward_ph = tf.placeholder('float')
        avg_tick_reward_ph = tf.placeholder('float')
        avg_action_entropy_ph = tf.placeholder('float')

        # expected reward under policy
        # entropy regularizer to encourage action diversity
        log_action_probs = tf.log(action_probs)
        action_entropy = - tf.reduce_mean(tf.reduce_sum(action_probs \
                                                        * log_action_probs, 1))
        action_logits = vector_slice(log_action_probs, actions_taken_ph)

        # objective for value estimation
        value_objective = tf.reduce_sum(tf.square(target_value_ph \
                                                  - state_value))
        # target = reward_ph + nonterminal_ph * args['reward_gamma'] \
        #     * next_state_value
        # value_objective = tf.reduce_sum(tf.square(tf.stop_gradient(target) \
        #                                           - state_value))

        # objective for computing policy gradient
        state_advantage = target_value_ph - state_value
        # state_advantage = target - state_value
        policy_objective = tf.reduce_sum(action_logits * \
                                         tf.stop_gradient(state_advantage))

        # total objective
        # maximize policy objective and minimize value objective
        # and maximize action entropy
        objective = - policy_objective + args['value_objective_coeff'] \
            * value_objective - args['action_entropy_coeff'] * action_entropy

        # optimization
        global_step = tf.Variable(0, trainable=False, name='global_step')
        # global tick keeps track of ticks experienced by the agent
        global_tick = tf.Variable(0, trainable=False, name='global_tick')
        delta_tick = tf.placeholder('int32')
        increment_global_tick = global_tick.assign_add(delta_tick)

        learning_rate = tf.train.exponential_decay(
            args['initial_learning_rate'],
            global_step, args['n_decay_steps'],
            args['decay_rate'],
            staircase=not args['no_decay_staircase'])

        if args['optimizer'] == 'adam':
            optimizer = tf.train.AdamOptimizer(learning_rate,
                                               args['adam_beta1'],
                                               args['adam_beta2'],
                                               args['adam_epsilon'])
        elif args['optimizer'] == 'ag':
            optimizer = tf.train.MomentumOptimizer(learning_rate,
                                                   args['momentum'],
                                                   use_nesterov=True)
        elif args['optimizer'] == 'rmsprop':
            optimizer = tf.train.RMSPropOptimizer(learning_rate,
                                                  args['rmsprop_decay'],
                                                  args['momentum'],
                                                  args['rmsprop_epsilon'])
        else:
            optimizer = tf.train.MomentumOptimizer(learning_rate,
                                                   args['momentum'])

        # train ops
        grad_vars = optimizer.compute_gradients(objective)
        update_op = optimizer.apply_gradients(grad_vars,
                                              global_step=global_step)

        # summaries
        # per_episode_summary = tf.summary.merge([
        #     tf.summary.scalar('episodic/reward', ),
        #     tf.summary.scalar('episodic/length', ),
        # ])
        #
        # per_step_summary = tf.summary.merge([
        #
        # ])

        # per_tick_summary = tf.summary.merge([
        #     tf.summary.scalar('tick/ticks_per_second'),
        # ])

        eval_summary_op = tf.summary.merge([
            tf.summary.scalar('average_episode_reward', avg_episode_reward_ph),
            tf.summary.scalar('max_episode_reward', max_episode_reward_ph),
            tf.summary.scalar('min_episode_reward', min_episode_reward_ph),
            tf.summary.scalar('average_episode_length', avg_len_episode_ph),
            ])

        train_summaries = []
        print '* extra summary'
        for g, v in grad_vars:
            train_summaries.append(tf.summary.histogram('gradients/%s' % v.name, g))
            print 'gradients/%s' % v.name

        train_summary_op = tf.summary.merge(train_summaries + [
            tf.summary.scalar('learning_rate', learning_rate),
            tf.summary.scalar('average_action_entropy', avg_action_entropy_ph),
            tf.summary.scalar('average_tick_reward', avg_tick_reward_ph),
            tf.summary.scalar('average_v_objective', avg_v_objective_ph),
            ])

        saver = tf.train.Saver(max_to_keep=2, keep_checkpoint_every_n_hours=1)
        with tf.Session() as sess:
            writer = tf.summary.FileWriter(summary_dir, sess.graph,
                                           flush_secs=30)
            print '* writing summary to', summary_dir

            tf.train.export_meta_graph('%s/model.meta' % args['checkpoint_dir'])
            restore_vars(saver, sess, args['checkpoint_dir'], args['restart'])

            # print '* regularized parameters:'
            # for v in tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES):
            #     print v.name

            # stochastic policy
            policy_func = lambda obs: action_probs.eval(feed_dict={
                obs_ph: [obs],
                keep_prob_ph: 1. - args['dropout_rate'],
            })[0]

            state_value_func = lambda obs: state_value.eval(feed_dict={
                obs_ph: [obs],
                keep_prob_ph: 1. - args['dropout_rate'],
            })[0,0]

            last_done = True
            last_obs_q = deque(pad_zeros([np.zeros(env_spec['observation_shape'])],
                                   args['n_obs_ticks']), args['n_obs_ticks'])
            for i in tqdm.tqdm(xrange(args['n_train_steps'])):
                # on-policy rollout for some ticks
                observations, actions, rewards, nonterminals, \
                last_done, _last_obs_q = partial_rollout(
                    policy_func,
                    env_spec,
                    env_step,
                    env_reset,
                    deque(last_obs_q),
                    last_done,
                    env_render,
                    n_update_ticks=args['n_update_ticks'] + 1,
                    n_obs_ticks=args['n_obs_ticks'],
                )
                avg_tick_reward = np.mean(rewards)

                obs, actions, acc_rewards = process_ticks(observations, actions,
                                                          rewards, nonterminals,
                                                          last_obs_q,
                                                          args['n_obs_ticks'],
                                                          args['reward_gamma'],
                                                          state_value_func)

                # # transform and preprocess the rollouts
                # obs = list(duplicate_obs(list(last_obs_q)[:-1] + observations,
                #                          args['n_obs_ticks']))
                # next_obs = obs[1:]
                # # ignore the last tick
                # # TODO add the last tick to the next
                # obs = obs[:-1]

                # estimate policy gradient by batches
                # accumulate gradients over batches
                acc_grads = dict([(grad, np.zeros(grad.get_shape()))
                                    for grad, var in grad_vars])
                acc_reg = 0.
                acc_v_obj_val = 0.
                n_batch = int(np.ceil(args['n_update_ticks'] * 1. / args['n_batch_ticks']))
                for j in xrange(n_batch):
                    start = j * args['n_batch_ticks']
                    end = min(start + args['n_batch_ticks'], args['n_update_ticks'])
                    grad_feed = {
                        obs_ph: obs[start:end],
                        keep_prob_ph: 1. - args['dropout_rate'],
                        actions_taken_ph: actions[start:end],
                        target_value_ph: acc_rewards[start:end],
                        # reward_ph: rewards[start:end],
                        # nonterminal_ph: nonterminals[start:end],
                        # next_obs_ph: next_obs[start:end],
                    }

                    # compute the expectation of gradients
                    v_obj_val, grad_vars_val, action_entropy_val = sess.run([
                        value_objective,
                        grad_vars,
                        action_entropy,
                        ], feed_dict=grad_feed)

                    for (g, _), (g_val, _) in zip(grad_vars, grad_vars_val):
                        acc_grads[g] += g_val / args['n_update_ticks']

                    acc_reg += action_entropy_val * (end - start)
                    acc_v_obj_val += v_obj_val
                last_obs_q = _last_obs_q

                # evaluation
                if i % args['n_eval_interval'] == 0:
                    episode_rewards = []
                    episode_lens = []
                    for j in xrange(args['n_eval_episodes']):
                        _, _, er = rollout(
                            policy_func,
                            eval_env_spec,
                            eval_env_step,
                            eval_env_reset,
                            None,
                            args['n_obs_ticks'],
                        )
                        episode_rewards.append(np.sum(er))
                        episode_lens.append(len(er))
                    eval_summary_val = eval_summary_op.eval({
                        avg_episode_reward_ph: np.mean(episode_rewards),
                        max_episode_reward_ph: np.max(episode_rewards),
                        min_episode_reward_ph: np.min(episode_rewards),
                        avg_len_episode_ph: np.mean(episode_lens),
                    })
                    writer.add_summary(eval_summary_val, global_step.eval())

                # update policy with the sample expectation of gradients
                update_dict = {
                    avg_tick_reward_ph: avg_tick_reward,
                    avg_v_objective_ph: acc_v_obj_val / args['n_update_ticks'],
                    avg_action_entropy_ph: acc_reg / args['n_update_ticks'],
                }
                update_dict.update(acc_grads)

                train_summary_val, _ = sess.run([train_summary_op,
                                                 update_op],
                                                feed_dict=update_dict)

                writer.add_summary(train_summary_val, global_step.eval())

                if i % args['n_save_interval'] == 0:
                    saver.save(sess, args['checkpoint_dir'] + '/model',
                               global_step=global_step.eval(), write_meta_graph=False)

            # save again at the end
            saver.save(sess, args['checkpoint_dir'] + '/model',
                       global_step=global_step.eval())

def build_argparser():
    parse = argparse.ArgumentParser()

    parse.add_argument('--model', required=True)

    # gym options
    parse.add_argument('--env', default='CartPole-v0')
    parse.add_argument('--monitor', action='store_true')
    parse.add_argument('--monitor_dir',
                       default='/tmp/gym-monitor-%i' % time.time())
    parse.add_argument('--n_obs_ticks', type=int, default=1)
    parse.add_argument('--timestep_limit', type=int, default=10**9)
    parse.add_argument('--use_render_state', action='store_true')
    parse.add_argument('--scale', type=float, default=1.)
    parse.add_argument('--interpolation', choices=['nearest', 'bilinear',
                                                   'bicubic', 'cubic'],
                       default='bilinear')

    parse.add_argument('--action_entropy_coeff', type=float, default=0.01)
    parse.add_argument('--value_objective_coeff', type=float, default=0.1)
    parse.add_argument('--reward_gamma', type=float, default=1.)
    parse.add_argument('--dropout_rate', type=float, default=0.2)

    parse.add_argument('--restart', action='store_true')
    parse.add_argument('--checkpoint_dir', required=True)
    parse.add_argument('--summary_prefix', default='')
    parse.add_argument('--render', action='store_true')

    # how many episodes to rollout before update parameters
    parse.add_argument('--n_update_ticks', type=int, default=256)
    parse.add_argument('--n_batch_ticks', type=int, default=128)
    parse.add_argument('--n_save_interval', type=int, default=8)
    parse.add_argument('--n_train_steps', type=int, default=10**5)
    parse.add_argument('--n_eval_episodes', type=int, default=4)
    parse.add_argument('--n_eval_interval', type=int, default=8)

    # optimizer options
    parse.add_argument('--momentum', type=float, default=0.2)
    parse.add_argument('--adam_beta1', type=float, default=0.9)
    parse.add_argument('--adam_beta2', type=float, default=0.999)
    parse.add_argument('--adam_epsilon', type=float, default=1e-8)
    parse.add_argument('--rmsprop_decay', type=float, default=0.9)
    parse.add_argument('--rmsprop_epsilon', type=float, default=1e-10)

    # training options
    parse.add_argument('--optimizer', choices=['adam', 'momentum', 'ag',
                                               'rmsprop'], default='rmsprop')
    parse.add_argument('--initial_learning_rate', type=float, default=0.01)
    parse.add_argument('--n_decay_steps', type=int, default=512)
    parse.add_argument('--no_decay_staircase', action='store_true')
    parse.add_argument('--decay_rate', type=float, default=0.8)

    parse.add_argument('--np_seed', type=int, default=123)
    parse.add_argument('--tf_seed', type=int, default=1234)

    return parse


if __name__ == '__main__':
    from functools import partial
    from util import passthrough, use_render_state, scale_image

    # arguments
    parse = build_argparser()
    args = parse.parse_args()

    gym_env = gym.make(args.env)
    eval_env = gym.make(args.env)

    if args.use_render_state:
        env_spec, env_step, env_reset, env_render = use_render_state(
            gym_env, args.scale, args.interpolation)
        eval_env_spec, eval_env_step, eval_env_reset, eval_env_render = \
        use_render_state(eval_env, args.scale, args.interpolation)
    else:
        env_spec, env_step, env_reset, env_render = passthrough(gym_env)
        eval_env_spec, eval_env_step, eval_env_reset, eval_env_render = \
        passthrough(eval_env)

    env_spec['timestep_limit'] = min(gym_env.spec.timestep_limit,
                                     args.timestep_limit)
    eval_env_spec['timestep_limit'] = min(eval_env.spec.timestep_limit,
                                     args.timestep_limit)
    env_render = env_render if args.render else None
    eval_env_render = eval_env_render if args.render else None

    print '* environment', args.env
    print 'observation shape', env_spec['observation_shape']
    print 'action space', gym_env.action_space
    print 'timestep limit', env_spec['timestep_limit']
    print 'reward threshold', gym_env.spec.reward_threshold

    # model
    model = importlib.import_module('models.%s' % args.model)

    # train
    # gym monitor
    if args.monitor:
        env.monitor.start(args['monitor_dir'])

    train((env_spec, env_step, env_reset, env_render),
          (eval_env_spec, eval_env_step, eval_env_reset, eval_env_render),
          vars(args),
          model.build_model)

    if args.monitor:
        env.monitor.close()
