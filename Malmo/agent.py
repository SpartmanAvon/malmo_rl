from abc import ABC, abstractmethod
import malmo.MalmoPython as MalmoPython
import random
import os
from pathlib import Path
import subprocess
import time
import re
from PIL import Image
import numpy as np


class Agent(ABC):
    """An agent abstract base class.
    Defines the methods which are required to be implemented by all agents deriving from this class.
    This class also defines the base behavior for communication with the environment.
    """
    def __init__(self, params):
        self.params = params

        # Malmo interface.
        self.MalmoPython = MalmoPython
        self.agent_host = self.MalmoPython.AgentHost()
        # If no port is given, start a Malmo instance with a random port number.
        if self.params.malmo_port is None:
            self.params.malmo_port = 10000 + random.randint(0, 999)
            self.minecraft = None
            self._start_malmo()

        # Mapping from policy action id's to actual game commands.
        # These are the commands supported by our simulator, different policies can support different actions as long as
        # they are sub-sets of this action set.
        self.action_id_to_action_command = {
            0: 'move 1',      # W
            1: 'move -1',     # S
            2: 'turn -1',     # A
            3: 'turn 1',      # D
            4: 'attack 1',    # Q
            9: 'newgame'      # 9
        }

        # Add the default client - on the local machine:
        self.client = MalmoPython.ClientInfo("127.0.0.1", int(self.params.malmo_port))
        self.client_pool = MalmoPython.ClientPool()
        self.client_pool.add(self.client)

        # Start the mission.
        self.tick_regex = re.compile('<MsPerTick>([0-9]*)</MsPerTick>', re.I)
        self.tick_time = 200  # Default value, in milliseconds.
        self.experiment_id = None
        self.max_mission_start_retries = 10

    def _start_malmo(self):
        # Minecraft directory is found via environment variable, this is required as a part of the Malmo installation
        # process. The MALMO_XSD_PATH points to the ..../Malmo/Schemas folder whereas Minecraft is a subdirectory of
        # Malmo.
        minecraft_directory = Path(os.environ['MALMO_XSD_PATH']).parent.joinpath('Minecraft')
        if self.params.platform is 'linux':
            minecraft_path = str(minecraft_directory) + '\launchClient.sh'
        elif self.params.platform is 'win':
            minecraft_path = str(minecraft_directory) + '\launchClient.bat'
        else:
            raise NotImplementedError('Only windows and linux are currently supported.')

        # Keep CWD as a pointer of where to return to.
        working_directory = os.getcwd()
        # Loading malmo is required from within the Minecraft folder (otherwise we will encounter issues with gradle).
        os.chdir(str(minecraft_directory))
        self.minecraft = subprocess.Popen([minecraft_path, '-port', str(self.params.malmo_port)],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        print('Starting Malmo:')
        current_line = ''
        # Emit Malmo console logs to console up to the point where it is fully loaded.
        for c in iter(lambda: self.minecraft.stdout.read(1), ''):
            if c == b'\n':
                print(current_line)
                # Ugly workaround to check that Malmo has properly loaded.
                if 'CLIENT enter state: DORMANT' in current_line:
                    break
                current_line = ''
            else:
                current_line += c.decode('utf-8')

        print('Malmo loaded!')

        os.chdir(working_directory)

    @abstractmethod
    def _restart_world(self):
        # Restart world is called upon new mission (new game, mission stuck, agent dead, etc...).
        pass

    def _load_mission_from_xml(self, mission_xml):
        # Given a string variable (containing the mission XML), will reload the mission itself.
        tick_time = self.tick_regex.search(mission_xml)
        if tick_time is not None:
            self.tick_time = int(tick_time.group(1))

        mission = self.MalmoPython.MissionSpec(mission_xml, True)
        mission.forceWorldReset()
        mission_record = self.MalmoPython.MissionRecordSpec()
        for retry in reversed(range(self.max_mission_start_retries)):
            try:
                self.agent_host.startMission(mission, self.client_pool, mission_record, 0, str(self.experiment_id))
                break
            except RuntimeError as e:
                if retry == 0:
                    print('Error starting mission', e)
                    print('Is the game running?')
                    exit(1)
                else:
                    time.sleep(2)

    def _wait_for_mission_to_begin(self):
        world_state = self.agent_host.getWorldState()
        while not world_state.has_mission_begun:
            time.sleep(0.1)
            world_state = self.agent_host.getWorldState()
            for error in world_state.errors:
                print('Error: ' + error.text)

    def perform_action(self, action_id):
        action_command = self.action_id_to_action_command[action_id]
        if action_command is 'newgame':
            self._restart_world()
            reward, terminal, state, world_state = self._get_new_state(True)
            return self._manual_reward_and_terminal(reward, terminal, state, world_state)
        else:
            self.agent_host.sendCommand(action_command)
            reward, terminal, state, world_state = self._get_new_state(False)
            return self._manual_reward_and_terminal(reward, terminal, state, world_state)

    def _get_new_state(self, new_game):
        current_r = 0
        while True:
            time.sleep(self.tick_time / 1000.0)
            world_state, r = self._get_updated_world_state()
            current_r += r

            if current_r != 0 or new_game:
                if world_state.is_mission_running and len(world_state.observations) > 0 \
                        and not (world_state.observations[-1].text == "{}") and len(world_state.video_frames) > 0:
                    frame = world_state.video_frames[-1]
                    state = Image.frombytes('RGB', (frame.width, frame.height), bytes(frame.pixels))
                    preprocessed_state = self._preprocess_state(state, self.params.image_width,
                                                                self.params.image_height, not self.params.retain_rgb)
                    return current_r, False, preprocessed_state, world_state
                elif not world_state.is_mission_running and not new_game:
                    return current_r, True, None, world_state

    def _get_updated_world_state(self):
        world_state = self.agent_host.getWorldState()
        r = 0
        for error in world_state.errors:
            print('Error: ' + error.text)
        for reward in world_state.rewards:
            r += reward.getValue()
        return world_state, r

    def _manual_reward_and_terminal(self, reward, terminal, state, world_state):
        del world_state  # Not used in the base implementation.
        return reward, terminal, state

    @staticmethod
    def _preprocess_state(state, width, height, gray_scale):
        preprocessed_state = state.resize((width, height))
        if gray_scale:
            preprocessed_state = preprocessed_state.convert('L')  # Grayscale conversion.
        else:
            preprocessed_state = preprocessed_state.convert('RGB')  # Any convert op. is required for numpy parsing.
        return np.array(preprocessed_state)