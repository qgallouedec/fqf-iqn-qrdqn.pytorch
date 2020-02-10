import os
import numpy as np
import torch
from torch.optim import Adam, RMSprop
from torch.utils.tensorboard import SummaryWriter

from .memory import DummyMultiStepMemory
from .model import FQF
from .utils import update_params, calculate_huber_loss, RunningMeanStats


class FQFAgent:

    def __init__(self, env, test_env, log_dir, num_steps=2*(10**8),
                 batch_size=32, num_taus=32, num_cosines=64, ent_coef=1.0,
                 kappa=1.0, fraction_lr=2.5e-9, quantile_lr=5e-5,
                 memory_size=10**6, gamma=0.99, multi_step=1,
                 update_period=4, target_update_period=10000,
                 start_steps=50000, epsilon_train=0.01, epsilon_eval=0.001,
                 log_interval=50, eval_interval=1000,
                 cuda=True, seed=0):

        self.env = env
        self.test_env = test_env

        torch.manual_seed(seed)
        np.random.seed(seed)
        self.env.seed(seed)
        self.test_env.seed(seed)
        # torch.backends.cudnn.deterministic = True  # It harms a performance.
        # torch.backends.cudnn.benchmark = False  # It harms a performance.

        self.device = torch.device(
            "cuda" if cuda and torch.cuda.is_available() else "cpu")

        # DQN-like feature extractor.
        self.fqf = FQF(
            num_channels=self.env.observation_space.shape[0],
            num_actions=self.env.action_space.n, num_taus=num_taus,
            num_cosines=num_cosines, device=self.device)

        self.fraction_optim = RMSprop(
            self.fqf.fraction_net.parameters(),
            lr=fraction_lr, eps=1e-2/batch_size)
        self.quantile_optim = Adam(
            list(self.fqf.dqn_base.parameters())
            + list(self.fqf.quantile_net.parameters()),
            lr=quantile_lr, eps=1e-2/batch_size)

        # Memory-efficient replay memory.
        self.memory = DummyMultiStepMemory(
            memory_size, self.env.observation_space.shape,
            self.device, gamma, multi_step)

        self.log_dir = log_dir
        self.model_dir = os.path.join(log_dir, 'model')
        self.summary_dir = os.path.join(log_dir, 'summary')
        if not os.path.exists(self.model_dir):
            os.makedirs(self.model_dir)
        if not os.path.exists(self.summary_dir):
            os.makedirs(self.summary_dir)

        self.writer = SummaryWriter(log_dir=self.summary_dir)
        self.train_rewards = RunningMeanStats(log_interval)

        self.steps = 0
        self.learning_steps = 0
        self.episodes = 0
        self.num_actions = self.env.action_space.n
        self.num_steps = num_steps
        self.batch_size = batch_size
        self.num_taus = num_taus
        self.num_cosines = num_cosines
        self.ent_coef = ent_coef
        self.kappa = kappa
        self.update_period = update_period
        self.target_update_period = target_update_period
        self.gamma_n = gamma ** multi_step
        self.start_steps = start_steps
        self.epsilon_train = epsilon_train
        self.epsilon_eval = epsilon_eval
        self.log_interval = log_interval
        self.eval_interval = eval_interval

    def run(self):
        while True:
            self.train_episode()
            if self.steps > self.num_steps:
                break

    def is_update(self):
        return self.steps % self.update_period == 0\
            and self.steps >= self.start_steps

    def explore(self):
        # Act with randomness.
        action = self.env.action_space.sample()
        return action

    def exploit(self, state):
        # Act without randomness.
        state = torch.ByteTensor(
            state).unsqueeze(0).to(self.device).float() / 255.
        with torch.no_grad():
            # Calculate state embeddings.
            state_embedding = self.fqf.dqn_base(state)
            # Calculate proposals of fractions.
            tau, hat_tau, _ = self.fqf.fraction_net(state_embedding)
            # Calculate Q and get greedy action.
            action = self.fqf.calculate_q(
                state_embedding, tau, hat_tau).argmax().item()
        return action

    def train_episode(self):
        self.episodes += 1
        episode_reward = 0.
        episode_steps = 0
        done = False
        state = self.env.reset()

        while not done:
            if self.steps < self.start_steps or\
                    np.random.rand() < self.epsilon_train:
                action = self.explore()
            else:
                action = self.exploit(state)

            next_state, reward, done, _ = self.env.step(action)
            self.steps += 1
            episode_steps += 1
            episode_reward += reward

            self.memory.append(
                state, action, reward, next_state, done)

            if self.is_update():
                self.learn()

            if self.steps % self.eval_interval == 0:
                self.evaluate()
                self.fqf.save(self.model_dir)

            state = next_state

        # We log running mean of training rewards.
        self.train_rewards.append(episode_reward)

        if self.episodes % self.log_interval == 0:
            self.writer.add_scalar(
                'reward/train', self.train_rewards.get(), self.steps)

        print(f'episode: {self.episodes:<4}  '
              f'episode steps: {episode_steps:<4}  '
              f'reward: {episode_reward:<5.1f}')

    def learn(self):
        self.learning_steps += 1

        if self.learning_steps % self.target_update_period == 0:
            self.fqf.update_target()

        states, actions, rewards, next_states, dones =\
            self.memory.sample(self.batch_size)

        state_embeddings = self.fqf.dqn_base(states)
        taus, hat_taus, entropies = self.fqf.fraction_net(state_embeddings)

        fraction_loss = self.calculate_fraction_loss(
            state_embeddings, taus, hat_taus, actions)

        entropy_loss = -self.ent_coef * entropies.mean()

        quantile_loss = self.calculate_quantile_loss(
            state_embeddings, taus, hat_taus, actions, rewards,
            next_states, dones)

        update_params(self.fraction_optim, fraction_loss + entropy_loss, True)
        update_params(self.quantile_optim, quantile_loss + entropy_loss)

        if self.learning_steps % self.log_interval == 0:
            self.writer.add_scalar(
                'loss/fraction_loss', fraction_loss.detach().item(),
                self.learning_steps)
            self.writer.add_scalar(
                'loss/quantile_loss', quantile_loss.detach().item(),
                self.learning_steps)
            self.writer.add_scalar(
                'loss/entropy_loss', entropy_loss.detach().item(),
                self.learning_steps)

            with torch.no_grad():
                curr_q = self.fqf.calculate_q(
                    state_embeddings, taus, hat_taus)
                mean_q = curr_q.mean(dim=0).sum()

            self.writer.add_scalar(
                'stats/mean_Q', mean_q, self.learning_steps)
            self.writer.add_scalar(
                'stats/entropy', entropies.mean().detach().item(),
                self.learning_steps)

    def calculate_fraction_loss(self, state_embeddings, taus, hat_taus,
                                actions):

        gradient_of_taus = self.fqf.calculate_gradients_of_tau_s(
            state_embeddings, taus, hat_taus)
        assert gradient_of_taus.shape == (
            self.batch_size, self.num_taus-1, self.num_actions)

        # action_index = actions[..., None].expand(
        #     self.batch_size, self.num_taus-1, 1)
        # gradient_of_taus = self.fqf.calculate_gradients_of_tau_sa(
        #     state_embeddings, taus, hat_taus, action_index)
        # assert gradient_of_taus.shape == (
        #     self.batch_size, self.num_taus-1, 1)

        fraction_loss = (
            gradient_of_taus * taus[:, 1:-1, None]).mean(dim=0).sum()

        return fraction_loss

    def calculate_quantile_loss(self, state_embeddings, taus, hat_taus,
                                actions, rewards, next_states, dones):

        # (batch_size, num_taus, num_actions)
        current_s_quantiles = self.fqf.quantile_net(
            state_embeddings, hat_taus)

        action_index = actions[..., None].expand(
            self.batch_size, self.num_taus, 1)

        # (batch_size, 1, num_taus)
        current_sa_quantiles = current_s_quantiles.gather(
            dim=2, index=action_index).view(self.batch_size, 1, self.num_taus)

        with torch.no_grad():
            # (batch_size, embedding_dim)
            next_state_embeddings = self.fqf.dqn_base(next_states)

            next_taus, next_hat_taus, _ =\
                self.fqf.fraction_net(next_state_embeddings)

            # (batch_size, num_taus, num_actions)
            next_s_quantiles = self.fqf.target_net(
                next_state_embeddings, hat_taus)

            # (batch_size, 1, 1)
            next_actions = torch.argmax(self.fqf.calculate_q(
                next_state_embeddings, next_taus, next_hat_taus), dim=1
                ).view(-1, 1, 1)
            assert next_actions.shape == (self.batch_size, 1, 1)

            # (batch_size, num_taus, 1)
            next_action_index = next_actions.expand(
                self.batch_size, self.num_taus, 1)

            # (batch_size, num_taus, 1)
            next_sa_quantiles = next_s_quantiles.gather(
                dim=2, index=next_action_index).view(-1, self.num_taus, 1)

            # (batch_size, num_taus, 1)
            target_sa_quantiles = rewards[..., None] + (
                1.0 - dones[..., None]) * self.gamma_n * next_sa_quantiles
            assert target_sa_quantiles.shape == (
                self.batch_size, self.num_taus, 1)

        # (batch_size, num_taus, num_taus)
        td_errors = target_sa_quantiles - current_sa_quantiles
        assert td_errors.shape == (
            self.batch_size, self.num_taus, self.num_taus)

        # (batch_size, num_taus, num_taus)
        huber_loss = calculate_huber_loss(td_errors, self.kappa)
        assert huber_loss.shape == (
            self.batch_size, self.num_taus, self.num_taus)

        quantile_huber_loss = (torch.abs(
            hat_taus[..., None]-(td_errors < 0).float()
            ) * huber_loss / self.kappa).sum(dim=-1).mean()

        return quantile_huber_loss

    def evaluate(self):
        episodes = 5
        returns = np.zeros((episodes,), dtype=np.float32)

        for i in range(episodes):
            state = self.test_env.reset()
            episode_reward = 0.
            done = False
            while not done:
                if np.random.rand() < self.epsilon_eval:
                    action = self.explore()
                else:
                    action = self.exploit(state)
                next_state, reward, done, _ = self.test_env.step(action)
                episode_reward += reward
                state = next_state
            returns[i] = episode_reward

        mean_return = np.mean(returns)
        std_return = np.std(returns)

        self.writer.add_scalar(
            'reward/test', mean_return, self.steps)
        print('-' * 60)
        print(f'Num steps: {self.steps:<5}  '
              f'reward: {mean_return:<5.1f} +/- {std_return:<5.1f}')
        print('-' * 60)

    def __del__(self):
        self.env.close()
        self.test_env.close()
        self.writer.close()
