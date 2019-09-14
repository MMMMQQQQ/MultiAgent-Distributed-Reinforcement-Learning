import numpy as np
import tensorflow as tf
from agents.utils import *


class Policy:
    def __init__(self, n_a, n_s, n_step, policy_name, agent_name):
        self.name = policy_name
        if agent_name is not None:
            # for multi-agent system
            self.name += '_' + str(agent_name)
        self.n_a = n_a
        self.n_s = n_s
        self.n_step = n_step

    def forward(self, ob, *_args, **_kwargs):
        raise NotImplementedError()

    def prepare_loss(self, v_coef, e_coef, max_grad_norm, alpha, epsilon):
        self.A = tf.placeholder(tf.int32, [self.n_step])
        self.ADV = tf.placeholder(tf.float32, [self.n_step])
        self.R = tf.placeholder(tf.float32, [self.n_step])
        A_sparse = tf.one_hot(self.A, self.n_a)
        log_pi = tf.log(tf.clip_by_value(self.pi, 1e-10, 1.0))
        entropy = -tf.reduce_sum(self.pi * log_pi, axis=1)
        entropy_loss = -tf.reduce_mean(entropy) * e_coef
        policy_loss = -tf.reduce_mean(tf.reduce_sum(log_pi * A_sparse, axis=1) * self.ADV)
        value_loss = tf.reduce_mean(tf.square(self.R - self.v)) * 0.5 * v_coef
        self.loss = policy_loss + value_loss + entropy_loss

        wts = tf.trainable_variables(scope=self.name)
        grads = tf.gradients(self.loss, wts)
        if max_grad_norm > 0:
            grads, self.grad_norm = tf.clip_by_global_norm(grads, max_grad_norm)
        self.lr = tf.placeholder(tf.float32, [])
        self.optimizer = tf.train.RMSPropOptimizer(learning_rate=self.lr, decay=alpha,
                                                   epsilon=epsilon)
        self._train = self.optimizer.apply_gradients(list(zip(grads, wts)))
        # monitor training
        summaries = []
        summaries.append(tf.summary.scalar('loss/%s_entropy_loss' % self.name, entropy_loss))
        summaries.append(tf.summary.scalar('loss/%s_policy_loss' % self.name, policy_loss))
        summaries.append(tf.summary.scalar('loss/%s_value_loss' % self.name, value_loss))
        summaries.append(tf.summary.scalar('loss/%s_total_loss' % self.name, self.loss))
        summaries.append(tf.summary.scalar('train/%s_lr' % self.name, self.lr))
        summaries.append(tf.summary.scalar('train/%s_gradnorm' % self.name, self.grad_norm))
        self.summary = tf.summary.merge(summaries)

    def _build_actor_head(self, h, agent_name=None):
        name = 'pi'
        if agent_name is not None:
            name += '_' + str(agent_name)
        pi = fc(h, name, self.n_a, act=tf.nn.softmax)
        return pi

    def _build_critic_head(self, h, na, n_n=None, agent_name=None):
        name = 'v'
        if agent_name is not None:
            name += '_' + str(agent_name)
        if n_n is None:
            n_n = na.shape[-1]
        na_sparse = tf.one_hot(na, self.n_a, axis=-1)
        na_sparse = tf.reshape(na_sparse, [-1, self.n_a*n_n])
        h = tf.concat([h, na_sparse], 1)
        v = fc(h, name, 1, act=lambda x: x)
        return v


class LstmPolicy(Policy):
    def __init__(self, n_s, n_a, n_n, n_step, n_fc=64, n_lstm=64, name=None):
        super().__init__(n_a, n_s, n_step, 'lstm', name)
        self.n_lstm = n_lstm
        self.n_fc = n_fc
        self.n_n = n_n
        self.ob_fw = tf.placeholder(tf.float32, [1, n_s]) # forward 1-step
        self.naction_fw = tf.placeholder(tf.int32, [1, n_n])
        self.done_fw = tf.placeholder(tf.float32, [1])
        self.ob_bw = tf.placeholder(tf.float32, [n_step, n_s]) # backward n-step
        self.naction_bw = tf.placeholder(tf.int32, [n_step, n_n])
        self.done_bw = tf.placeholder(tf.float32, [n_step])
        self.states = tf.placeholder(tf.float32, [n_lstm * 2])
        with tf.variable_scope(self.name):
            self.pi_fw, self.v_fw, self.new_states = self._build_net('forward')
        with tf.variable_scope(self.name, reuse=True):
            self.pi, self.v, _ = self._build_net('backward')
        self._reset()

    def backward(self, sess, obs, nactions, acts, dones, Rs, Advs, cur_lr,
                 summary_writer=None, global_step=None):
        summary, _ = sess.run([self.summary, self._train],
                              {self.ob_bw: obs,
                               self.naction_bw: nactions,
                               self.done_bw: dones,
                               self.states: self.states_bw,
                               self.A: acts,
                               self.ADV: Advs,
                               self.R: Rs,
                               self.lr: cur_lr})
        self.states_bw = np.copy(self.states_fw)
        if summary_writer is not None:
            summary_writer.add_summary(summary, global_step=global_step)

    def forward(self, sess, ob, done, naction=None, out_type='p'):
        # update state only when p is called
        ins = {self.ob_fw: np.array([ob]),
               self.done_fw: np.array([done]),
               self.states: self.states_fw}
        if out_type.startswith('p'):
            outs = [self.pi_fw, self.new_states]
        else:
            outs = [self.v_fw]
            ins[self.naction_fw] = np.array([naction])
        out_values = sess.run(outs, ins)
        out_value = out_values[0]
        if out_type.startswith('p'):
            self.states_fw = out_values[-1]
        return out_value

    def _build_net(self, in_type):
        if in_type == 'forward':
            ob = self.ob_fw
            done = self.done_fw
            naction = self.naction_fw
        else:
            ob = self.ob_bw
            done = self.done_bw
            naction = self.naction_bw
        h = fc(ob, 'fc', self.n_fc)
        h, new_states = lstm(h, done, self.states, 'lstm')
        pi = self._build_actor_head(h)
        v = self._build_critic_head(h, naction)
        return tf.squeeze(pi), tf.squeeze(v), new_states

    def _reset(self):
        # forget the cumulative states every cum_step
        self.states_fw = np.zeros(self.n_lstm * 2, dtype=np.float32)
        self.states_bw = np.zeros(self.n_lstm * 2, dtype=np.float32)


class FPPolicy(LstmPolicy):
    def __init__(self, n_s, n_a, n_n, n_step, n_fc=64, n_lstm=64, name=None):
        super().__init__(n_s, n_a, n_n, n_step, n_fc, n_lstm, name)

    def _build_net(self, in_type):
        if in_type == 'forward':
            ob = self.ob_fw
            done = self.done_fw
            naction = self.naction_fw
        else:
            ob = self.ob_bw
            done = self.done_bw
            naction = self.naction_bw
        n_x = int(self.n_s - self.n_n * self.n_a)
        hx = fc(ob[:,:n_x], 'fcs', self.n_fc)
        hp = fc(ob[:,n_x:], 'fcp', self.n_fc)
        h = tf.concat([hx, hp], axis=1)
        h, new_states = lstm(h, done, self.states, 'lstm')
        pi = self._build_actor_head(h)
        v = self._build_critic_head(h, naction)
        return tf.squeeze(pi), tf.squeeze(v), new_states


class NCMultiAgentPolicy(Policy):
    """ Inplemented as a centralized agent. To simplify the implementation, all input
    and output dimensions are identical among all agents, and invalid values are casted as
    zeros during runtime."""
    def __init__(self, n_s, n_a, n_agent, n_step, neighbor_mask, n_fc=64, n_h=64):
        super().__init__(n_a, n_s, n_step, 'nc', None)
        self._init_policy(n_agent, neighbor_mask, n_h)

    def backward(self, sess, obs, policies, acts, dones, Rs, Advs, cur_lr,
                 summary_writer=None, global_step=None):
        summary, _ = sess.run([self.summary, self._train],
                              {self.ob_bw: obs,
                               self.policy_bw: policies,
                               self.action_bw: acts,
                               self.done_bw: dones,
                               self.states: self.states_bw,
                               self.ADV: Advs,
                               self.R: Rs,
                               self.lr: cur_lr})
        self.states_bw = np.copy(self.states_fw)
        if summary_writer is not None:
            summary_writer.add_summary(summary, global_step=global_step)

    def forward(self, sess, ob, done, policy, action=None, out_type='p'):
        # update state only when p is called
        ins = {self.ob_fw: np.expand_dims(ob, axis=1),
               self.done_fw: np.expand_dims(done, axis=1),
               self.policy_fw: np.expand_dims(policy, axis=1),
               self.states: self.states_fw}
        if out_type.startswith('p'):
            outs = [self.pi_fw, self.new_states]
        else:
            outs = [self.v_fw]
            ins[self.action_fw] = np.expand_dims(action, axis=1)
        out_values = sess.run(outs, ins)
        out_value = out_values[0]
        if out_type.startswith('p'):
            self.states_fw = out_values[-1]
        return out_value

    def prepare_loss(self, v_coef, e_coef, max_grad_norm, alpha, epsilon):
        self.ADV = tf.placeholder(tf.float32, [self.n_agent, self.n_step])
        self.R = tf.placeholder(tf.float32, [self.n_agent, self.n_step])
        A_sparse = tf.one_hot(self.action_bw, self.n_a)
        # all losses are averaged over steps but summed over agents
        log_pi = tf.log(tf.clip_by_value(self.pi, 1e-10, 1.0))
        entropy = -tf.reduce_sum(self.pi * log_pi, axis=-1)
        entropy_loss = -tf.reduce_sum(tf.reduce_mean(entropy, axis=-1)) * e_coef
        policy_loss = -tf.reduce_sum(tf.reduce_mean(tf.reduce_sum(log_pi * A_sparse, axis=-1) * self.ADV, axis=-1))
        value_loss = tf.reduce_sum(tf.reduce_mean(tf.square(self.R - self.v), axis=-1)) * 0.5 * v_coef
        self.loss = policy_loss + value_loss + entropy_loss

        wts = tf.trainable_variables(scope=self.name)
        grads = tf.gradients(self.loss, wts)
        if max_grad_norm > 0:
            grads, self.grad_norm = tf.clip_by_global_norm(grads, max_grad_norm)
        self.lr = tf.placeholder(tf.float32, [])
        self.optimizer = tf.train.RMSPropOptimizer(learning_rate=self.lr, decay=alpha,
                                                   epsilon=epsilon)
        self._train = self.optimizer.apply_gradients(list(zip(grads, wts)))
        # monitor training
        summaries = []
        summaries.append(tf.summary.scalar('loss/%s_entropy_loss' % self.name, entropy_loss))
        summaries.append(tf.summary.scalar('loss/%s_policy_loss' % self.name, policy_loss))
        summaries.append(tf.summary.scalar('loss/%s_value_loss' % self.name, value_loss))
        summaries.append(tf.summary.scalar('loss/%s_total_loss' % self.name, self.loss))
        summaries.append(tf.summary.scalar('train/%s_lr' % self.name, self.lr))
        summaries.append(tf.summary.scalar('train/%s_gradnorm' % self.name, self.grad_norm))
        self.summary = tf.summary.merge(summaries)

    def _build_net(self, in_type):
        if in_type == 'forward':
            ob = self.ob_fw
            policy = self.policy_fw
            action = self.action_fw
            done = self.done_fw
        else:
            ob = self.ob_bw
            policy = self.policy_bw
            action = self.action_bw
            done = self.done_bw
        h, new_states = lstm_comm_new(ob, policy, done, self.neighbor_mask, self.states, 'lstm_comm')
        pi_ls = []
        v_ls = []
        for i in range(self.n_agent):
            h_i = h[i] # Txn_h
            naction_i = tf.transpose(tf.boolean_mask(action, self.neighbor_mask[i])) # Txn_n
            pi = self._build_actor_head(h_i, agent_name='%d' % i)
            v = self._build_critic_head(h_i, naction_i, n_n=int(np.sum(self.neighbor_mask[i])),
                                        agent_name='%d' % i)
            pi_ls.append(tf.expand_dims(pi, axis=0))
            v_ls.append(tf.expand_dims(v, axis=0))
        return tf.squeeze(tf.concat(pi_ls, axis=0)), tf.squeeze(tf.concat(v_ls, axis=0)), new_states

    def _init_policy(self, n_agent, neighbor_mask, n_h):
        self.n_agent = n_agent
        self.neighbor_mask = neighbor_mask #n_agent x n_agent
        self.n_h = n_h
        self.ob_fw = tf.placeholder(tf.float32, [n_agent, 1, self.n_s]) # forward 1-step
        self.policy_fw = tf.placeholder(tf.float32, [n_agent, 1, self.n_a])
        self.action_fw = tf.placeholder(tf.int32, [n_agent, 1])
        self.done_fw = tf.placeholder(tf.float32, [1])
        self.ob_bw = tf.placeholder(tf.float32, [n_agent, self.n_step, self.n_s]) # backward n-step
        self.policy_bw = tf.placeholder(tf.float32, [n_agent, self.n_step, self.n_a])
        self.action_bw = tf.placeholder(tf.int32, [n_agent, self.n_step])
        self.done_bw = tf.placeholder(tf.float32, [self.n_step])
        self.states = tf.placeholder(tf.float32, [n_agent, n_h * 2])

        with tf.variable_scope(self.name):
            self.pi_fw, self.v_fw, self.new_states = self._build_net('forward')
        with tf.variable_scope(self.name, reuse=True):
            self.pi, self.v, _ = self._build_net('backward')
        self._reset()

    def _reset(self):
        self.states_fw = np.zeros((self.n_agent, self.n_h * 2), dtype=np.float32)
        self.states_bw = np.zeros((self.n_agent, self.n_h * 2), dtype=np.float32)


class ConsensusPolicy(NCMultiAgentPolicy):
    def __init__(self, n_s, n_a, n_agent, n_step, neighbor_mask, n_fc=64, n_h=64):
        Policy.__init__(self, n_a, n_s, n_step, 'cu', None)
        self.n_agent = n_agent
        self.n_h = n_h
        self.neighbor_mask = neighbor_mask
        self._init_policy(n_agent, neighbor_mask, n_h)

    def backward(self, sess, obs, policies, acts, dones, Rs, Advs, cur_lr,
                 summary_writer=None, global_step=None):
        super().backward(sess, obs, policies, acts, dones, Rs, Advs, cur_lr,
                         summary_writer, global_step)
        sess.run(self._consensus_update)

    def prepare_loss(self, v_coef, e_coef, max_grad_norm, alpha, epsilon):
        super().prepare_loss(v_coef, e_coef, max_grad_norm, alpha, epsilon)
        consensus_update = []
        for i in range(self.n_agent):
            wt_from, wt_to = self._get_critic_wts(i)
            for w1, w2 in zip(wt_from, wt_to):
                consensus_update.append(w2.assign(w1))
        self._consensus_update = tf.group(*consensus_update)

    def _build_net(self, in_type):
        if in_type == 'forward':
            ob = self.ob_fw
            done = self.done_fw
            action = self.action_fw
        else:
            ob = self.ob_bw
            done = self.done_bw
            action = self.action_bw
        pi_ls = []
        v_ls = []
        new_states_ls = []
        for i in range(self.n_agent):
            h = fc(ob[i], 'fc_%d' % i, self.n_h)
            h, new_states = lstm(h, done, self.states[i], 'lstm_%d' % i)
            pi = self._build_actor_head(h, agent_name='%d' % i)
            naction = tf.transpose(tf.boolean_mask(action, self.neighbor_mask[i]))
            v = self._build_critic_head(h, naction, n_n=int(np.sum(self.neighbor_mask[i])), agent_name='%d' % i)
            pi_ls.append(tf.expand_dims(pi, axis=0))
            v_ls.append(tf.expand_dims(v, axis=0))
            new_states_ls.append(tf.expand_dims(new_states, axis=0))
        pi_ls = tf.squeeze(tf.concat(pi_ls, axis=0))
        v_ls = tf.squeeze(tf.concat(v_ls, axis=0))
        new_states_ls = tf.squeeze(tf.concat(new_states_ls, axis=0))
        return pi_ls, v_ls, new_states_ls

    def _get_critic_wts(self, agent_i):
        neighbor_mask = self.neighbor_mask[agent_i]
        agents = [agent_i] + list(np.where(neighbor_mask == 1)[0])
        wt_i = []
        wt_n = []
        for i in agents:
            critic_scope = [self.name + ('/lstm_%d' % i)]
            wt = []
            for scope in critic_scope:
                wt += tf.trainable_variables(scope=scope)
            if i == agent_i:
                wt_i = wt
            wt_n.append(wt)
        mean_wt_n = []
        n_n = len(wt_n)
        n_w = len(wt_n[0])
        for i in range(n_w):
            cur_wts = []
            for j in range(n_n):
                cur_wts.append(tf.expand_dims(wt_n[j][i], axis=-1))
            cur_wts = tf.concat(cur_wts, axis=-1)
            cur_wts = tf.reduce_mean(cur_wts, axis=-1)
            mean_wt_n.append(cur_wts)
        return mean_wt_n, wt_i


class IC3MultiAgentPolicy(NCMultiAgentPolicy):
    """Reference code: https://github.com/IC3Net/IC3Net/blob/master/comm.py.
       Note in IC3, the message is generated from hidden state only, so current state
       and neigbor policies are not included in the inputs."""
    def __init__(self, n_s, n_a, n_agent, n_step, neighbor_mask, n_fc=64, n_h=64):
        Policy.__init__(self, n_a, n_s, n_step, 'ic3', None)
        self._init_policy(n_agent, neighbor_mask, n_h)

    def _build_net(self, in_type):
        if in_type == 'forward':
            ob = self.ob_fw
            action = self.action_fw
            done = self.done_fw
        else:
            ob = self.ob_bw
            action = self.action_bw
            done = self.done_bw
        h, new_states = lstm_ic3(ob, done, self.neighbor_mask, self.states, 'lstm_ic3')
        pi_ls = []
        v_ls = []
        for i in range(self.n_agent):
            h_i = h[i] # Txn_h
            naction_i = tf.transpose(tf.boolean_mask(action, self.neighbor_mask[i])) # Txn_n
            pi = self._build_actor_head(h_i, agent_name='%d' % i)
            v = self._build_critic_head(h_i, naction_i, n_n=int(np.sum(self.neighbor_mask[i])),
                                        agent_name='%d' % i)
            pi_ls.append(tf.expand_dims(pi, axis=0))
            v_ls.append(tf.expand_dims(v, axis=0))
        return tf.squeeze(tf.concat(pi_ls, axis=0)), tf.squeeze(tf.concat(v_ls, axis=0)), new_states


class DIALMultiAgentPolicy(NCMultiAgentPolicy):
    def __init__(self, n_s, n_a, n_agent, n_step, neighbor_mask, n_fc=64, n_h=64):
        Policy.__init__(self, n_a, n_s, n_step, 'dial', None)
        self._init_policy(n_agent, neighbor_mask, n_h)

    def _build_net(self, in_type):
        if in_type == 'forward':
            ob = self.ob_fw
            policy = self.policy_fw
            action = self.action_fw
            done = self.done_fw
        else:
            ob = self.ob_bw
            policy = self.policy_bw
            action = self.action_bw
            done = self.done_bw
        h, new_states = lstm_dial(ob, policy, done, self.neighbor_mask, self.states, 'lstm_comm')
        pi_ls = []
        v_ls = []
        for i in range(self.n_agent):
            h_i = h[i] # Txn_h
            naction_i = tf.transpose(tf.boolean_mask(action, self.neighbor_mask[i])) # Txn_n
            pi = self._build_actor_head(h_i, agent_name='%d' % i)
            v = self._build_critic_head(h_i, naction_i, n_n=int(np.sum(self.neighbor_mask[i])),
                                        agent_name='%d' % i)
            pi_ls.append(tf.expand_dims(pi, axis=0))
            v_ls.append(tf.expand_dims(v, axis=0))
        return tf.squeeze(tf.concat(pi_ls, axis=0)), tf.squeeze(tf.concat(v_ls, axis=0)), new_states

