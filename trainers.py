
import os 
import gym
import utils
import wandb 
import torch
import agents 
import numpy as np 
from PIL import Image
from collections import deque
from augmentations import get_transform


def to_numpy(value):
    if isinstance(value, torch.Tensor):
        return value.detach().cpu().numpy()
    elif isinstance(value, (float, bool)):
        return np.array([[float(value)]], dtype=np.float32)
    elif isinstance(value, int):
        return np.array([[value]], dtype=np.int64)


class ReplayMemory:
    
    def __init__(self, memory_size, device, resolution=(125, 80)):
        self.device = device
        self.size = memory_size 
        self.height, self.width = resolution 
        
        self.obs = deque(maxlen=memory_size)
        self.next_obs = deque(maxlen=memory_size)
        self.actions = deque(maxlen=memory_size)
        self.rewards = deque(maxlen=memory_size)
        self.episode_done = deque(maxlen=memory_size) 
    
    def _collect(self, deck, idx):
        tensors = []
        for i in idx:
            if deck[i] is None:
                print([deck[j] for j in np.arange(i-2, i+2)])
                print(len(self.obs))
            else:
                tensors.append(torch.from_numpy(deck[i]))
        return torch.cat(tensors, 0)
        
    def add_sample(self, obs, action, next_obs, reward, done):
        self.obs.append(obs)
        self.next_obs.append(next_obs)
        self.actions.append(action)
        self.rewards.append(reward)
        self.episode_done.append(done)
        
    def get_batch(self, batch_size, device, spacing=None):
        if spacing is None:
            idx = np.random.choice(np.arange(len(self.obs)), size=batch_size, replace=False)
        elif isinstance(spacing, int):
            start_idx = np.random.choice(np.arange(len(self.obs) - batch_size * spacing), size=1)
            idx = np.array([start_idx + i * spacing for i in range(batch_size)])
        else:
            raise ValueError(f"Argument spacing expected to be NoneType or int, got {type(spacing)}")
        
        obs = self._collect(self.obs, idx).to(device)
        next_obs = self._collect(self.next_obs, idx).to(device)
        action = self._collect(self.actions, idx).to(device)
        reward = self._collect(self.rewards, idx).to(device)
        done = self._collect(self.episode_done, idx).to(device)
        return obs, action, next_obs, reward, done
    
    
class Trainer:
    
    def __init__(self, args):
        self.config, self.output_dir, self.logger, self.device = utils.initialize_experiment(args, output_root="outputs/atari/double_q_basic/")
        self.env = gym.make(self.config["env_name"])
        self.model = agents.DoubleDQN(self.config["model"], self.env.action_space.n, self.device)
        self.memory = ReplayMemory(self.config["memory_size"], self.device)
        self.obs_transform = get_transform(self.config["transform"])
        self.stack_size = self.config.get("frames_per_sample", 4)
        self.best_return = 0
        
        run = wandb.init(project="atari-experiments")
        self.logger.write("Wandb url: {}".format(run.get_url()), mode="info")
        
    def save_checkpoint(self):
        state = {"model": self.model, "memory": self.memory}
        torch.save(state, os.path.join(self.output_dir, "checkpoint.pt"))
    
    def load_checkpoint(self, ckpt_dir):
        file = os.path.join(ckpt_dir, "checkpoint.pt")
        if os.path.exists(file):
            state = torch.load(file, map_location=self.device)
            self.model = state["model"]
            self.memory = state["memory"]
            self.logger.print("Successfully loaded model checkpoint", mode="info")
        else:
            raise FileNotFoundError(f"Could not find checkpoint at {ckpt_dir}")
        
    def process_state(self, frames, from_reset=False):
        if from_reset:
            assert len(frames) == 1, f"Observation from reset should have 1 frame, received {len(frames)}"
            frames = frames * self.stack_size
        elif len(frames) < self.stack_size:
            frames = frames[:-1] + [frames[-1]] * (self.stack_size - len(frames) + 1)
            
        frames = [self.obs_transform(Image.fromarray(f.astype(np.uint8))).mean(0) for f in frames]
        return torch.stack(frames, 0).to(self.device).unsqueeze(0)
        
    @torch.no_grad()
    def initialize_memory(self):
        obs = self.process_state([self.env.reset()], True)
        for step in range(self.config["random_action_steps"]):
            frames = [] 
            total_reward = 0
            action = self.env.action_space.sample()
            for _ in range(self.stack_size):
                next_frame, reward, done, _ = self.env.step(action)
                total_reward += reward 
                if not done:
                    frames.append(next_frame)
                else:
                    break
            if len(frames) > 0:
                new_obs = self.process_state(frames)
                self.memory.add_sample(to_numpy(obs), to_numpy(action), to_numpy(new_obs), to_numpy(total_reward), to_numpy(done))
                obs = new_obs if not done else self.process_state([self.env.reset()], True)
                utils.progress_bar(progress=(step+1)/self.config["random_action_steps"], desc="Initializing memory", status="")
            else:
                obs = self.process_state([self.env.reset()], True)
        print()
        
    @torch.no_grad()
    def eval_episode(self):
        obs = self.process_state([self.env.reset()], True)
        episode_finished = False 
        total_reward = 0
        while not episode_finished:
            frames = [] 
            action = self.model.select_action(obs)
            for _ in range(self.stack_size):
                next_frame, reward, done, _ = self.env.step(action)
                total_reward += reward
                if not done:
                    frames.append(next_frame)
                else:
                    episode_finished = True
                    break
            if len(frames) > 0:
                new_obs = self.process_state(frames)
                obs = new_obs if not done else None
        return total_reward 
    
    def train_episode(self):
        obs = self.process_state([self.env.reset()], True)
        episode_finished = False 
        total_reward = 0
        step = 0
        
        while not episode_finished:
            frames = [] 
            action = self.model.select_action(obs)
            for _ in range(self.stack_size):
                next_frame, reward, done, _ = self.env.step(action)
                total_reward += reward
                if not done:
                    frames.append(next_frame)
                else:
                    episode_finished = True
                    break
                
            if len(frames) > 0:
                next_obs = self.process_state(frames)
                self.memory.add_sample(to_numpy(obs), to_numpy(int(action)), to_numpy(next_obs), to_numpy(total_reward), to_numpy(done))
                obs = next_obs if not done else None
                step += 1
            
            # Model update 
            batch = self.memory.get_batch(self.config["batch_size"], self.device)
            metrics = self.model.learning_step(step, batch)
        return {**metrics, "reward": total_reward} 
    
    def train(self):
        self.logger.print("Initializing memory", mode="info")
        self.initialize_memory()
        print()
        self.logger.print("Beginning training", mode="info")
        
        for episode in range(1, self.config["train_episodes"]+1):    
            self.model.train()            
            train_metrics = self.train_episode()
            wandb.log({f"Train {key}": value for key, value in train_metrics.items()})
            self.logger.record("Episode {:4d}/{:4d} {}".format(
                episode, self.config["train_episodes"], " ".join(["[{}] {:.4f}".format(k, v) for k, v in train_metrics.items()])), mode="train")

            if episode % self.config["eval_every"] == 0:
                val_rewards = [] 
                for i in range(self.config["eval_episodes"]):
                    reward = self.eval_episode()
                    val_rewards.append(reward)
                    utils.progress_bar(progress=(i+1)/self.config["eval_episodes"], desc="Validation", status="")
                print()
                avg_reward = np.mean(val_rewards)
                wandb.log({"Val reward": avg_reward, "Episode": episode})
                self.logger.record("Episode {:4d}/{:4d} [Reward] {:.4f}".format(episode, self.config["train_episodes"], avg_reward), mode="val")
                
                if avg_reward > self.best_return:
                    self.best_return = avg_reward
                    self.save_checkpoint()
            
            self.model.decay_epsilon()        
            self.model.update_target_critic()
        print()
        self.logger.print("Completed training", mode="info")