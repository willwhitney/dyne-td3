import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F
import utils

from EmbeddedTD3 import EmbeddedTD3, Actor, Critic

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
# device = "cpu"

# Implementation of Twin Delayed Deep Deterministic Policy Gradients (TD3)
# Paper: https://arxiv.org/abs/1802.09477

class Actor(nn.Module):
    def __init__(self, state_dim, action_dim, max_action):
        super(Actor, self).__init__()

        self.l1 = nn.Linear(state_dim, 400)
        self.l2 = nn.Linear(400, 300)
        self.l3 = nn.Linear(300, action_dim)

        self.max_action = max_action


    def forward(self, x):
        x = F.relu(self.l1(x))
        x = F.relu(self.l2(x))
        x = self.max_action * torch.tanh(self.l3(x))
        return x


class Critic(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Critic, self).__init__()

        # Q1 architecture
        self.l1 = nn.Linear(state_dim + action_dim, 400)
        self.l2 = nn.Linear(400, 300)
        self.l3 = nn.Linear(300, 1)

        # Q2 architecture
        self.l4 = nn.Linear(state_dim + action_dim, 400)
        self.l5 = nn.Linear(400, 300)
        self.l6 = nn.Linear(300, 1)


    def forward(self, x, u):
        xu = torch.cat([x, u], 1)

        x1 = F.relu(self.l1(xu))
        x1 = F.relu(self.l2(x1))
        x1 = self.l3(x1)

        x2 = F.relu(self.l4(xu))
        x2 = F.relu(self.l5(x2))
        x2 = self.l6(x2)
        return x1, x2


    def Q1(self, x, u):
        xu = torch.cat([x, u], 1)

        x1 = F.relu(self.l1(xu))
        x1 = F.relu(self.l2(x1))
        x1 = self.l3(x1)
        return x1


class InferredEmbeddingTD3(EmbeddedTD3):
    def __init__(self, state_dim, action_dim, max_action, decoder):
        self.decoder = decoder
        self.e_action_dim = decoder.embed_dim

        # set the maximum action in embedding space to the largest value the decoder saw during training
        self.max_e_action = self.decoder.max_embedding

        self.actor = Actor(state_dim, self.e_action_dim, self.max_e_action).to(device)
        self.actor_target = Actor(state_dim, self.e_action_dim, self.max_e_action).to(device)
        self.actor_target.load_state_dict(self.actor.state_dict())
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters())

        self.critic = Critic(state_dim, self.e_action_dim).to(device)
        self.critic_target = Critic(state_dim, self.e_action_dim).to(device)
        self.critic_target.load_state_dict(self.critic.state_dict())
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters())

        self.max_action = max_action
        self.pending_plan = torch.Tensor(0, 0, 0).to(device)
        self.current_e_action = None


    def train(self, replay_buffer, iterations, batch_size=100, discount=0.99, tau=0.005, policy_noise=0.2, noise_clip=0.5, policy_freq=2):

        for it in range(iterations):
            traj_len = self.decoder.traj_len

            # Sample replay buffer
            x, y, u, e, i, r, d = replay_buffer.sample_seq(batch_size, traj_len)

            state = torch.FloatTensor(x).to(device)
            action = torch.FloatTensor(u).to(device)
            e_action = torch.FloatTensor(e).to(device)
            next_state = torch.FloatTensor(y).to(device)
            plan_step = torch.FloatTensor(i).to(device)
            done = torch.FloatTensor(1 - d).to(device)
            reward = torch.FloatTensor(r).to(device)

            noise = torch.FloatTensor(batch_size, self.e_action_dim).data.normal_(0, policy_noise).to(device)
            noise = noise.clamp(-noise_clip, noise_clip)

            # indicates whether the reward at time t is from the same episode as state[:, 0]
            same_episode_mask = torch.zeros(plan_step.size()).to(device)
            same_episode_mask[:, 0] = 1
            for t in range(1, traj_len):
                same_episode_mask[:, t] = same_episode_mask[:, t-1] * done[:, t-1]

            # \sum_{j=0}^{k-1} \gamma^j r_{t+j}
            discount_exponent = torch.linspace(0, traj_len-1, traj_len).repeat(state.size(0), 1).to(device)
            discount_factor = discount ** discount_exponent
            discounted_reward = reward * discount_factor
            current_plan_reward = (discounted_reward * same_episode_mask).sum(1, keepdim=True)

            # find which state we next replan on
            # if there are 4 steps left in the plan, that means we replanned 
            #   on the state we got to after the 4th action (action[3])
            # that is, we replan on next_state[3]
            next_plan_state = next_state[:, traj_len-1]

            # Select action according to policy and add clipped noise
            # note this is now an embedded action
            # noise = torch.FloatTensor(batch_size, self.e_action_dim).data.normal_(0, policy_noise).to(device)
            # noise = noise.clamp(-noise_clip, noise_clip)
            next_action = (self.actor_target(next_plan_state) + noise).clamp(-self.max_e_action, self.max_e_action)
            
            # make a new done mask that is 0 if the episode ended during the current plan
            done_mask = done[:, :traj_len].prod(dim=1, keepdim=True)

            # Compute the target Q value
            # tell the target Q functions that we're on a new plan in this state
            target_Q1, target_Q2 = self.critic_target(next_plan_state, next_action)
            target_Q = torch.min(target_Q1, target_Q2)
            next_state_discount = discount ** remaining_plan_steps.unsqueeze(1)
            target_Q = current_plan_reward + (done_mask * next_state_discount * target_Q).detach()

            # infer the an embedding consistent with the next traj_len actions
            inferred_e_action = self.model.encode(action)

            # Get current Q estimates
            current_Q1, current_Q2 = self.critic(state[:, 0], inferred_e_action)
            target_Q = target_Q.reshape(-1, 1)
            critic_loss = F.mse_loss(current_Q1, target_Q) + F.mse_loss(current_Q2, target_Q)

            # Optimize the critic
            self.critic_optimizer.zero_grad()
            critic_loss.backward()
            self.critic_optimizer.step()

            # Delayed policy updates
            if it % policy_freq == 0:
                # Compute actor loss
                # If I was in this state, and I started following plan actor(state) right now, how would I do?
                # import ipdb; ipdb.set_trace()
                actor_loss = -self.critic.Q1(state[:, 0], self.actor(state[:, 0]), torch.zeros(batch_size, 1).to(device)).mean()
                # import ipdb; ipdb.set_trace()

                # Optimize the actor
                self.actor_optimizer.zero_grad()
                actor_loss.backward()
                self.actor_optimizer.step()

                # Update the frozen target models
                for param, target_param in zip(self.critic.parameters(), self.critic_target.parameters()):
                    target_param.data.copy_(tau * param.data + (1 - tau) * target_param.data)

                for param, target_param in zip(self.actor.parameters(), self.actor_target.parameters()):
                    target_param.data.copy_(tau * param.data + (1 - tau) * target_param.data)

