#!/usr/bin/env python
import torch
import torch.multiprocessing as mp
import random
import numpy as np
from collections import namedtuple
from duelling_network import DuellingDQN
from env import make_local_env

Transition = namedtuple('Transition', ['S', 'A', 'R', 'Gamma', 'q'])
N_Step_Transition = namedtuple('N_Step_Transition', ['St', 'At', 'R_ttpB', 'Gamma_ttpB', 'qS_t', 'S_tpn', 'qS_tpn', 'key'])
Prioritized_N_Step_Transition = namedtuple('Prioritized_N_Step_Transition', ['St', 'At', 'R_ttpB', 'Gamma_ttpB',
                                                                             'S_tpn', 'key'])
class ExperienceBuffer(object):
    def __init__(self, n, actor_id):
        """
        Implements a circular/ring buffer to store n-step transition data used by the actor
        :param n:
        """
        self.buffer = list()
        self.idx = -1
        self.capacity = n
        self.local_memory = list()  #  To store n-step transitions b4 they r batched, prioritized and sent to replay mem
        self.gamma = 0.99
        self.id = actor_id
        self.n_step_seq_num = 0  # Used to compose the unique key per per-actor and per n-step transition stored

    def update_buffer(self):
        """
        Updates the accumulated per-step discount and the partial return for every item in the buffer. This should be
        called after every new transition is added to the buffer
        :return: None
        """
        for i in range(self.B - 1):
            R = self.buffer[i].R
            Gamma = 1
            for k in range(i + 1, self.B ):
                Gamma *= self.gamma
                R += Gamma * self.buffer[k].R
            self.buffer[i] = Transition(self.buffer[i].S, self.buffer[i].A, R, Gamma, self.buffer[i].q)

    def add(self, data):
        """
        Add transition data to the Experience Buffer and calls update_buffer
        :param data: tuple containing a transition data of type Transition(s, a, r, gamma, q)
        :return: None
        """
        if self.idx  + 1 < self.capacity:
            self.idx += 1
            self.buffer.append(None)
            self.buffer[self.idx] = data
            self.update_buffer()  #  calculate the accumulated per-step disc & partial return for all entries
        else:  # Buffer has reached its capacity, n
            #  Construct the n-step transition
            key = str(self.id) + str(self.n_step_seq_num)
            n_step_transition = N_Step_Transition(*self.buffer[0], data.S, data.q, key)
            self.n_step_seq_num += 1
            #  Put the n_step_transition into a local memory store
            self.local_memory.append(n_step_transition)
            #  Free-up the buffer
            self.buffer.clear()
            self.idx = -1

    def get(self, batch_size):
        assert batch_size <= self.size, "Requested n-step transitions batch size is more than available"
        batch_of_n_step_transitions = self.local_memory[: batch_size]
        del self.local_memory[: batch_size]
        return batch_of_n_step_transitions

    @property
    def B(self):
        """
        The current size of buffer. B follows the same notation as in the Ape-X paper(TODO: insert link to paper)
        :return: The current size of the buffer
        """
        return len(self.buffer)

    @property
    def size(self):
        """
        The current size of the local experience memory
        :return:
        """
        return len(self.local_memory)


class Actor(object):
    def __init__(self, actor_id, env_conf, shared_state, shared_replay_mem, actor_params):
        self.actor_id = actor_id  # Used to compose a unique key for the transitions generated by each actor
        state_shape = env_conf['state_shape']
        action_dim = env_conf['action_dim']
        self.params = actor_params
        self.shared_state = shared_state
        self.Q = DuellingDQN(state_shape, action_dim)
        self.Q.load_state_dict(shared_state["Q_state_dict"])
        self.env = make_local_env(env_conf['name'])
        self.policy = self.epsilon_greedy_Q
        self.local_experience_buffer = ExperienceBuffer(self.params["local_experience_buffer_capacity"], self.actor_id)
        self.global_replay_queue = shared_replay_mem
        eps = self.params['epsilon']
        N = self.params['num_actors']
        alpha = self.params['alpha']
        self.epsilon = eps**(1 + alpha * self.actor_id / (N-1))
        self.gamma = self.params['gamma']
        self.num_buffered_steps = 0  # Used to compose a unique key for the transitions generated by each actor

    def epsilon_greedy_Q(self, qS_t):
        if random.random() >= self.epsilon:
            return np.argmax(qS_t)
        else:
            return random.choice(list(range(len(qS_t))))

    def compute_priorities(self, n_step_transitions):
        n_step_transitions = N_Step_Transition(*zip(*n_step_transitions))
        # Convert tuple to numpy array
        rew_t_to_tpB = np.array(n_step_transitions.R_ttpB)
        gamma_t_to_tpB = np.array(n_step_transitions.Gamma_ttpB)
        qS_tpn = np.array(n_step_transitions.qS_tpn)
        At = np.array(n_step_transitions.At, dtype=np.int)
        qS_t = np.array(n_step_transitions.qS_t)

        print("qS_t.shape:", qS_t.shape)
        print("np.max(qS_tpn,1):", np.max(qS_tpn, 1))
        #  Calculate the absolute n-step TD errors
        n_step_td_target =  rew_t_to_tpB + gamma_t_to_tpB * np.max(qS_tpn, 1)
        print("td_target:", n_step_td_target)
        n_step_td_error = n_step_td_target - np.array([ qS_t[i, At[i]] for i in range(At.shape[0])])
        print("td_err:", n_step_td_error)
        priorities = {k: val for k in n_step_transitions.key for val in abs(n_step_td_error) }
        #prioritized_xp = [Prioritized_N_Step_Transition(*xp) for xp in
        #                  list(zip(n_step_transitions.St, n_step_transitions.At, n_step_transitions.R_ttpB,
        #                              n_step_transitions.Gamma_ttpB, n_step_transitions.S_tpn, keys))]
        return priorities

    def gather_experience(self, T):
        # 3. Get initial state from environment
        obs = self.env.reset()
        for t in range(T):
            qS_t = self.Q(torch.from_numpy(np.resize(obs, (1, 84,84))).unsqueeze_(0).float())[2].detach().numpy().squeeze()
            # 5. Select the action using the current policy
            action = self.policy(qS_t)
            # 6. Apply action in the environment
            next_obs, reward, done, _ = self.env.step(action)
            # 7. Add data to local buffer
            self.local_experience_buffer.add(Transition(obs, action, reward, self.gamma, qS_t))
            obs = next_obs
            print("t=", t, "action=", action, "xp_buf_size:", self.local_experience_buffer.size)
            # 8. Periodically send data to replay
            if self.local_experience_buffer.size >= self.params['n_step_transition_batch_size']:
                # 9. Get batches of multi-step transitions
                n_step_experience_batch = self.local_experience_buffer.get(self.params['n_step_transition_batch_size'])
                # 10.Calculate the priorities for experience
                priorities = self.compute_priorities(n_step_experience_batch)
                print("Priorities:", priorities)
                # 11. Send the experience to the global replay memory
                [self.global_replay_queue.put(item) for item in zip(priorities.items(), n_step_experience_batch)]

            if t % self.params['Q_network_sync_freq'] == 0:
                # 13. Obtain latest network parameters
                self.Q.load_state_dict(self.shared_state["Q_state_dict"])

if __name__ == "__main__":
    env_conf = {"state_shape": (1, 84, 84),
                "action_dim": 4,
                "name": "Breakout-v0"}
    params= {"local_experience_buffer_capacity": 10,
             "epsilon": 0.4,
             "alpha": 7,
             "gamma": 0.99,
             "num_actors": 2,
             "n_step_transition_batch_size": 5,
             "Q_network_sync_freq": 10
             }
    dummy_q = DuellingDQN(env_conf['state_shape'], env_conf['action_dim'])
    mp_manager = mp.Manager()
    shared_state = mp_manager.dict()
    shared_state["Q_state_dict"] = dummy_q.state_dict()
    shared_replay_mem = mp_manager.Queue()
    actor = Actor(1, env_conf, shared_state, shared_replay_mem, params)
    actor.gather_experience(101)
