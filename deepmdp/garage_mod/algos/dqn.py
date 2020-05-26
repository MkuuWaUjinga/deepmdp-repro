import torch
import numpy as np

from typing import List
from dowel import tabular, logger
from garage.np.algos.off_policy_rl_algorithm import OffPolicyRLAlgorithm
from garage.misc.tensor_utils import normalize_pixel_batch
from garage.torch.utils import np_to_torch
from deepmdp.experiments.utils import Visualizer
from deepmdp.garage_mod.algos.auxiliary_objective import AuxiliaryObjective
from deepmdp.garage_mod.q_functions.discrete_cnn_q_function import DiscreteCNNQFunction
from deepmdp.garage_mod.algos.reward_auxiliary_objective import RewardAuxiliaryObjective
from deepmdp.garage_mod.algos.transition_auxiliary_objective import TransitionAuxiliaryObjective

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

class DQN(OffPolicyRLAlgorithm):

    def __init__(self,
                 env_spec,
                 policy,
                 qf: DiscreteCNNQFunction,
                 replay_buffer,
                 experiment_id : int,
                 plot_list : List,
                 exploration_strategy=None,
                 n_epoch_cycles=20,
                 min_buffer_size=int(1e4),
                 buffer_batch_size=32,
                 rollout_batch_size=1,
                 n_train_steps=50,
                 max_path_length=None,
                 qf_lr=0.0002,
                 qf_optimizer=torch.optim.Adam,
                 discount=0.99,
                 reward_scale=1.,
                 target_network_update_freq=5,
                 input_include_goal=False,
                 smooth_return=True,
                 auxiliary_objectives: List[AuxiliaryObjective] = None,
                 penalty_lambda=0.01
                 ):
        super(DQN, self).__init__(env_spec=env_spec,
                                  policy=policy,
                                  qf=qf ,
                                  exploration_strategy=exploration_strategy,
                                  min_buffer_size=min_buffer_size,
                                  n_train_steps=n_train_steps,
                                  n_epoch_cycles=n_epoch_cycles,
                                  buffer_batch_size=buffer_batch_size,
                                  rollout_batch_size=rollout_batch_size,
                                  replay_buffer=replay_buffer,
                                  max_path_length=max_path_length,
                                  discount=discount,
                                  reward_scale=reward_scale,
                                  input_include_goal=input_include_goal,
                                  smooth_return=smooth_return)
        self.qf_lr = qf_lr
        self.target_network_update_freq = target_network_update_freq
        self.episode_rewards : List = []
        self.episode_mean_q_vals : List = []
        self.episode_qf_losses : List = []
        self.episode_std_q_vals : List = []
        self.penalty_lambda = penalty_lambda

        # Clone target q-network
        self.target_qf = self.qf.clone()

        self.target_qf.to(device)
        self.qf.to(device)

        # Init visualizer
        self.experiment_id = experiment_id
        self.plot_list = plot_list
        self.visualizer = Visualizer(self.experiment_id, self.plot_list)
        self.auxiliary_objectives = auxiliary_objectives

        self.qf_optimizer = qf_optimizer(qf.parameters(), lr=qf_lr) # TODO add deepmdp params to optimizer!!!


        logger.log(f"Number of parameter of q-network are: {sum(p.numel() for p in qf.parameters() if p.requires_grad)}")


    def optimize_policy(self, itr, samples):
        """Optimize q-network."""
        del itr
        del samples
        action_dim = self.env_spec.action_space.n
        transitions = self.replay_buffer.sample(self.buffer_batch_size)

        # Obs. are stored in uint8 format in replay buffer to optimize memory.
        # Convert pixel values to [0,1] for training if env's obs are images.
        transitions["observation"] = np.array(normalize_pixel_batch(self.env_spec, transitions["observation"]))
        transitions["next_observation"] = np.array(normalize_pixel_batch(self.env_spec, transitions["next_observation"]))
        # Garage's normalize pixel batch returns list primitive. Converting it to numpy array makes FloatTensor
        # creation around 10 times faster.
        transitions = np_to_torch(transitions)
        observations = transitions['observation']
        rewards = transitions['reward'].to(device)
        actions = transitions['action'].to(device)
        next_observations = transitions['next_observation']
        dones = transitions['terminal'].to(device)

        with torch.no_grad():
            target_qvals = self.target_qf(next_observations)
            target_qvals, _ =  torch.max(target_qvals, dim=1)
            assert target_qvals.size(0) == self.buffer_batch_size, "number of target qvals has to equal batch size"

        # if done, it's just reward else reward + discount * target_qvals
        target = rewards + (1.0 - dones) * self.discount * target_qvals

        qval, embedding = self.qf(observations, return_embedding=True)
        actions_one_hot = self.one_hot(actions, action_dim)
        q_selected = torch.sum(qval * actions_one_hot, axis=1)

        loss = 0
        for auxiliary_objective in self.auxiliary_objectives:
            if isinstance(auxiliary_objective, RewardAuxiliaryObjective):
                flattened_embedding = embedding.view(embedding.size(0), -1)
                reward_loss = auxiliary_objective.compute_loss(flattened_embedding, rewards, actions_one_hot)
                loss += reward_loss
                self.visualizer.save_aux_loss(reward_loss.item(), "reward loss")
            elif isinstance(auxiliary_objective, TransitionAuxiliaryObjective):
                _, embedding_next_obs = self.qf(next_observations, return_embedding=True)
                transition_loss = auxiliary_objective.compute_loss(embedding, embedding_next_obs, actions)
                loss += transition_loss
                self.visualizer.save_aux_loss(transition_loss.item(), "transition loss")

        self.visualizer.save_latent_space(self, next_observations, transitions.get("ground_truth_state"))

        # compute gradient penalty if we have auxiliary objectives i.e. we train a DeepMDP
        if self.auxiliary_objectives:
            gradient_penalty = 0
            gradient_penalty += self.compute_gradient_penalty(self.qf.encoder, observations)
            for head in [self.qf.head] + [aux.net for aux in self.auxiliary_objectives]:
                gradient_penalty += self.compute_gradient_penalty(head, embedding)
            loss += self.penalty_lambda * gradient_penalty

        loss_func = torch.nn.SmoothL1Loss()
        qval_loss = loss_func(q_selected, target)
        loss += qval_loss
        self.qf_optimizer.zero_grad()
        loss.backward()
        self.qf_optimizer.step()
        return qval_loss.cpu().detach()

    @staticmethod
    def one_hot(actions, action_dim) -> torch.Tensor:
        return torch.zeros((len(actions), action_dim)).scatter_(1, actions.long().unsqueeze(1), 1).to(device)

    def train_once(self, itr, paths):
        """Perform one step of policy optimization given one batch of samples.

        Args:
            itr (int): Iteration number.
            paths (list[dict]): A list of collected paths.

        """
        paths = self.process_samples(itr, paths)
        epoch = itr / self.n_epoch_cycles

        # ? log correlation between reward and q-value to see whether the agent's estimation of value was correct.
        self.episode_rewards.extend(paths['undiscounted_returns'])
        self.episode_mean_q_vals.extend(paths['episode_mean_q_vals'])
        self.episode_std_q_vals.extend(paths['episode_std_q_vals'])

        self.visualizer.visualize_episodical_stats(self, len(paths["undiscounted_returns"]))

        # Decay epsilon of exploration strategy manually for each finished episode.
        if self.es._episodical_decay:
            for complete in paths["complete"]:
                if complete:
                    self.es._decay(episode_done=True)
                    path_length = paths["path_lengths"]
                    logger.log(f"Episode: {len(self.episode_rewards)} --- Episode length: {path_length} --- Epsilon: {self.es._epsilon}")

        last_average_return = np.mean(self.episode_rewards) if self.episode_rewards else 0
        for _ in range(self.n_train_steps):
            if self._buffer_prefilled:
                qf_loss = self.optimize_policy(itr, None)
                self.episode_qf_losses.append(qf_loss.item())

        self.visualizer.visualize_training_results(self.n_train_steps * itr)

        if self._buffer_prefilled:
            if itr % self.target_network_update_freq == 0:
                self.update_target()

        if itr % self.n_epoch_cycles == 0:
            if self._buffer_prefilled:
                mean100ep_rewards = round(np.mean(self.episode_rewards[-100:]), 1)
                mean100ep_q_vals = round(np.mean(self.episode_mean_q_vals[-100:]), 1)
                mean100ep_qf_loss = np.mean(self.episode_qf_losses[-100:])
                tabular.record('Epoch', epoch)
                tabular.record("Episode100QValuesMean", mean100ep_q_vals)
                tabular.record('Episode100RewardMean', mean100ep_rewards)
                tabular.record('Episode100LossMean', mean100ep_qf_loss)
                tabular.record('CurrentEpsilon', self.es._epsilon)
        return last_average_return

    def process_samples(self, itr, paths):
        """Return processed sample data based on the collected paths.

        Args:
            itr (int): Iteration number.
            paths (list[dict]): A list of collected paths.

        Returns:
            dict: Processed sample data, with keys
                * undiscounted_returns (list[float])
                * episode_mean_q_vals (list[float])
                * episode_std_q_vals (list[float])
                * complete (list[float])
        """
        undiscounted_returns = [path['undiscounted_return'] for path in paths if path['dones'][-1]]
        episode_mean_q_vals = [np.mean(path['q_vals']) for path in paths if path['dones'][-1]]
        episode_std_q_vals = [np.std(path["q_vals"]) for path in paths if path['dones'][-1]]
        path_lengths = [len(path['q_vals']) for path in paths if path['dones'][-1]]

        complete = [path['dones'][-1] for path in paths]

        samples_data = dict(undiscounted_returns=undiscounted_returns,
                            episode_mean_q_vals=episode_mean_q_vals,
                            episode_std_q_vals=episode_std_q_vals,
                            path_lengths=path_lengths,
                            complete=complete)

        return samples_data

    def update_target(self):
        self.target_qf.load_state_dict(self.qf.state_dict())

    @property
    def _buffer_prefilled(self):
        """Flag whether first min buffer size steps are done."""
        return self.replay_buffer.n_transitions_stored >= self.min_buffer_size

    def __getstate__(self):
        """Return state values to be pickled. Pickling visdom throws an error using pickle or cloudpickle.
        --> Remove it from state values."""
        data = self.__dict__.copy()
        del data['visualizer']
        return data

    def __setstate__(self, state):
        """Restore state from the unpickled state values."""
        self.__dict__ = state
        self.visualizer =  Visualizer(self.experiment_id, self.plot_list)

    @staticmethod
    def compute_gradient_penalty(net, samples):
        """Calculates the gradient penalty loss for WGAN GP, adapt for WGAN-LP
        https://github.com/eriklindernoren/PyTorch-GAN/blob/master/implementations/wgan_gp/wgan_gp.py"""
        # Random weight term for interpolation between real and fake samples
        batch_size = samples.size(0)
        samples_a, samples_b = torch.split(samples, int(batch_size/2))
        alpha = torch.rand_like(samples_a)
        # Get random interpolation between real and fake samples
        interpolated_obs = samples_a * alpha + ((1.0 - alpha) * samples_b)
        interpolated_obs = torch.autograd.Variable(interpolated_obs, requires_grad=True)

        d_interpolates = net(interpolated_obs)
        grad = torch.ones(d_interpolates.size(), requires_grad=False).to(device)

        # Get gradient w.r.t. interpolates
        gradients = torch.autograd.grad(
            outputs=d_interpolates,
            inputs=interpolated_obs,
            grad_outputs=grad
        )[0]

        gradients = gradients.view(int(batch_size/2), -1)
        gradients_norm = gradients.norm(2, dim=1)
        penalty = (gradients_norm ** 2).mean()
        return penalty