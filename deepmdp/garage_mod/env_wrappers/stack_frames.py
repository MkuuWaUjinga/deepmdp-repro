"""Stack frames wrapper for gym.Env."""
from collections import deque

import gym
import gym.spaces
import numpy as np


class StackFrames(gym.Wrapper):
    """gym.Env wrapper to stack multiple frames.

    Useful for training feed-forward agents on dynamic games.

    Args:
        env: gym.Env to wrap.
        n_frames: number of frames to stack.
        do_noops: indicate whether env should do n_frames - 1 many noops in the beginning to prefill the observation
        queue with different observations.

    Raises:
        ValueError: If observation space shape is not 2 or
        environment is not gym.spaces.Box.

    """

    def __init__(self, env, n_frames, do_noops=False):
        if not isinstance(env.observation_space, gym.spaces.Box):
            raise ValueError('Stack frames only works with gym.spaces.Box '
                             'environment.')

        super().__init__(env)

        self._n_frames = n_frames
        self._frames = deque(maxlen=n_frames)
        self._obs_space_length = len(env.observation_space.shape)
        self._do_noops = do_noops
        self._noop_action = 0

        new_obs_space_shape = env.observation_space.shape + (n_frames, )
        _low = env.observation_space.low.flatten()[0]
        _high = env.observation_space.high.flatten()[0]
        self._observation_space = gym.spaces.Box(
            _low,
            _high,
            shape=new_obs_space_shape,
            dtype=env.observation_space.dtype)

    @property
    def observation_space(self):
        """gym.Env observation space."""
        return self._observation_space

    @observation_space.setter
    def observation_space(self, observation_space):
        self._observation_space = observation_space

    def _stack_frames(self):
        stacked_frames =  np.stack(self._frames, axis=self._obs_space_length)
        return stacked_frames if self._obs_space_length == 2 else stacked_frames.flatten()

    def reset(self):
        """gym.Env reset function."""
        observation = self.env.reset()
        self._frames.clear()
        if self._do_noops:
            self._frames.append(observation)
            for _ in range(self._n_frames - 1):
                obs, _, done, _ = self.env.step(self._noop_action)
                self._frames.append(obs)
        else:
            for i in range(self._n_frames):
                self._frames.append(observation)

        return self._stack_frames()

    def step(self, action):
        """gym.Env step function."""
        new_observation, reward, done, info = self.env.step(action)
        self._frames.append(new_observation)

        return self._stack_frames(), reward, done, info
