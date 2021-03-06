"""
ϵ-greedy exploration strategy.

Random exploration according to the value of epsilon.
"""
import numpy as np

from garage.np.exploration_strategies.base import ExplorationStrategy


class EpsilonGreedyStrategy(ExplorationStrategy):
    """
    ϵ-greedy exploration strategy.

    Select action based on the value of ϵ. ϵ will decrease from
    max_epsilon to min_epsilon within decay_ratio * total_timesteps.

    At state s, with probability
    1 − ϵ: select action = argmax Q(s, a)
    ϵ    : select a random action from an uniform distribution.

    Args:
        env_spec (garage.envs.env_spec.EnvSpec): Environment specification.
        total_timesteps (int): Total steps in the training, equivalent to
            max_path_length * n_epochs.
        max_epsilon (float): The maximum(starting) value of epsilon.
        min_epsilon (float): The minimum(terminal) value of epsilon.
        decay_ratio (float): Fraction of total steps for epsilon decay.
    """

    def __init__(self,
                 env_spec,
                 total_timesteps,
                 max_epsilon=1.0,
                 min_epsilon=0.02,
                 decay_ratio=0.1,
                 episodical_decay=False,
                 exponential_decay_rate=None):
        self._env_spec = env_spec
        self._max_epsilon = max_epsilon
        self._min_epsilon = min_epsilon
        self._action_space = env_spec.action_space
        self._epsilon = self._max_epsilon
        self._episodical_decay = episodical_decay
        self._decay_rate = None
        self._decrement = None
        self._decay_period = None
        if exponential_decay_rate is None:
            self._decay_period = int(total_timesteps * decay_ratio)
            self._decrement = (self._max_epsilon -
                               self._min_epsilon) / self._decay_period
        else:
            self._decay_rate = exponential_decay_rate

    def get_action(self, t, observation, policy, **kwargs):
        """
        Get action from this policy for the input observation.

        Args:
            t: Iteration.
            observation: Observation from the environment.
            policy: Policy network to predict action based on the observation.

        Returns:
            opt_action: optimal action from this policy.

        """
        opt_action = policy.get_action(observation)
        self._decay()
        if np.random.random() < self._epsilon:
            opt_action = self._action_space.sample()

        return opt_action, dict()

    def get_actions(self, t, observations, policy, **kwargs):
        """
        Get actions from this policy for the input observations.

        Args:
            t: Iteration.
            observation: Observation from the environment.
            policy: Policy network to predict action based on the observation.

        Returns:
            opt_action: optimal actions from this policy.

        """
        opt_actions, agent_infos = policy.get_actions(observations)
        for itr in range(len(opt_actions)):
            self._decay()
            if np.random.random() < self._epsilon:
                opt_actions[itr] = self._action_space.sample()
        return opt_actions, agent_infos

    def _decay(self, episode_done=None):
        if self._epsilon > self._min_epsilon:
            if not self._episodical_decay or self._episodical_decay and episode_done:
                if self._decay_rate:
                # Do exponential decay
                    self._epsilon *= self._decay_rate
                # Do linear decay
                else:
                    self._epsilon -= self._decrement
