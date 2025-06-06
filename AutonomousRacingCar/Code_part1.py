# -*- coding: utf-8 -*-

# Commented out IPython magic to ensure Python compatibility.
# %load_ext autoreload
# %autoreload 2

"""# Install Dependencies"""

if "dependencies_installed" not in globals():
    !git clone https://github.com/MPC-Berkeley/barc_gym.git --depth 1
    !pip install -r barc_gym/requirements.txt
    !pip install -e barc_gym/gym-carla
    !pip install -e barc_gym/mpclab_common
    !pip install -e barc_gym/mpclab_controllers
    !pip install -e barc_gym/mpclab_simulation
    dependencies_installed = True

import site
import warnings
site.main()
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

import numpy as np
import gymnasium as gym
import gym_carla
from matplotlib import pyplot as plt
from loguru import logger

"""# Part 0: Defining the base PID controller
Standard PID controller has:
- Proportional (P) term responds to the current error
- Integral (I) term responds to the accumulation of past errors
- Derivative (D) term predicts future error based on the rate of change

It aims to minimize the error between a desired reference (`x_ref`) and the current measured state (`x`) by adjusting the control input (`u`).

The control law is defined as:

    u(t) = - [ Kp * e(t) + Ki * ∫e(t)dt + Kd * de(t)/dt ] + u_ref

Where:
- **e(t)** is the error between reference and current state: e(t) = x(t) - x_ref
- **Kp** is the proportional gain.
- **Ki** is the integral gain.
- **Kd** is the derivative gain.

## Preliminary: Vehicle State definition

We follow the same vehicle state definition as the ROS messages we use in the hardware experiments. The following snippet shows the definition of fields that are the most relevant to your task.

```python
@dataclass
class Position(PythonMsg):
    x: float = field(default=0)
    y: float = field(default=0)
    z: float = field(default=0)
    ...

@dataclass
class BodyLinearVelocity(PythonMsg):
    v_long: float = field(default=0)
    v_tran: float = field(default=0)
    v_n: float = field(default=0)

@dataclass
class BodyAngularVelocity(PythonMsg):
    w_phi: float = field(default=0)
    w_theta: float = field(default=0)
    w_psi: float = field(default=0)

@dataclass
class OrientationEuler(PythonMsg):
    phi: float = field(default=0)
    theta: float = field(default=0)
    psi: float = field(default=0)

@dataclass
class ParametricPose(PythonMsg):
    s: float = field(default=0)
    x_tran: float = field(default=0)
    n: float = field(default=0)
    e_psi: float = field(default=0)

@dataclass
class VehicleActuation(PythonMsg):
    t: float        = field(default=0)
    u_a: float      = field(default=0)
    u_steer: float  = field(default=0)
    ...

@dataclass
class VehicleState(PythonMsg):
    '''
    Complete vehicle state (local, global, and input)
    '''
    t: float                        = field(default=None)  # time in seconds
    x: Position                     = field(default=None)  # global position
    v: BodyLinearVelocity           = field(default=None)  # body linear velocity
    w: BodyAngularVelocity          = field(default=None)  # body angular velocity
    e: OrientationEuler             = field(default=None)  # global orientation (phi, theta, psi)
    p: ParametricPose               = field(default=None)  # parametric position (s, y, ths)
    u: VehicleActuation             = field(default=None)  # control inputs (u_a, u_steer)
    lap_num: int = field(default=None)
    ...
```

Use the dot notation to access an attribute of the data structure above. For example, to access the global y coordinate given a state object `vehicle_state`, you should do `vehicle_state.x.y`.

## Defining the PID Parameters
"""

from mpclab_common.pytypes import PythonMsg
from dataclasses import dataclass, field


@dataclass
class PIDParams(PythonMsg):
    """
    This is a template for PID Parameters.
    Make sure you understand what each parameter means,
    but DON'T MODIFY ANYTHING HERE!
    """
    dt: float = field(default=0.1)  # The frequency of the controller
    Kp: float = field(default=2.0)  # The P component.
    Ki: float = field(default=0.0)  # The I component.
    Kd: float = field(default=0.0)  # The D component.

    # Constraints
    int_e_max: float = field(default=100)
    int_e_min: float = field(default=-100)
    u_max: float = field(default=None)
    u_min: float = field(default=None)
    du_max: float = field(default=None)
    du_min: float = field(default=None)

    # References
    u_ref: float = field(default=0.0)  # Input reference
    x_ref: float = field(default=0.0)  # PID state reference

"""## Implementing the base class for PID controller

In this section, we implement the core logic of a PID controller.
The goal is to create a reusable controller that can adjust a system's behavior (such as speed or steering) based on
the difference between a desired reference value and the actual measured state.

This base PID class will be used by higher-level controllers to compute control inputs like throttle and steering angle to follow a lane or raceline.
"""

from typing import Tuple

class PID():
    '''
    Base class for PID controller
    Meant to be packaged for use in actual controller (eg. ones that operate directly on vehicle state) since a PID controller by itself is not sufficient for vehicle control
    See PIDLaneFollower for a PID controller that is an actual controller
    '''
    def __init__(self, params: PIDParams = PIDParams()):
        self.dt             = params.dt

        self.Kp             = params.Kp             # proportional gain
        self.Ki             = params.Ki             # integral gain
        self.Kd             = params.Kd             # derivative gain

        # Integral action and control action saturation limits
        self.int_e_max      = params.int_e_max
        self.int_e_min      = params.int_e_min
        self.u_max          = params.u_max
        self.u_min          = params.u_min
        self.du_max         = params.du_max
        self.du_min         = params.du_min

        self.x_ref          = params.x_ref  # PID State reference
        self.u_ref          = params.u_ref  # Input reference
        self.u_prev         = 0             # Internal buffer for previous input

        self.e              = 0             # error
        self.de             = 0             # finite time error difference
        self.ei             = 0             # accumulated error

        self.initialized = True

    def solve(self, x: float,
                u_prev: float = None) -> Tuple[float, dict]:
        if not self.initialized:
            raise(RuntimeError('PID controller is not initialized, run PID.initialize() before calling PID.solve()'))

        if self.u_prev is None and u_prev is None: u_prev = 0
        elif u_prev is None: u_prev = self.u_prev

        info = {'success' : True}

        # Compute error terms
        e_t = x - self.x_ref
        de_t = (e_t - self.e)/self.dt
        ei_t = self.ei + e_t*self.dt

        # Anti-windup
        if ei_t > self.int_e_max:
            ei_t = self.int_e_max
        elif ei_t < self.int_e_min:
            ei_t = self.int_e_min

        # Compute control action terms
        P_val  = self.Kp * e_t
        I_val  = self.Ki * ei_t
        D_val  = self.Kd * de_t

        u = -(P_val + I_val + D_val) + self.u_ref

        # Compute change in control action from previous timestep
        du = u - u_prev

        # Saturate change in control action
        if self.du_max is not None:
            du = self._saturate_rel_high(du)
        if self.du_min is not None:
            du = self._saturate_rel_low(du)

        u = du + u_prev

        # Saturate absolute control action
        if self.u_max is not None:
            u = self._saturate_abs_high(u)
        if self.u_min is not None:
            u = self._saturate_abs_low(u)

        # Update error terms
        self.e  = e_t
        self.de = de_t
        self.ei = ei_t

        self.u_prev = u
        return u, info

    def set_x_ref(self, x_ref: float):
        self.x_ref = x_ref
        # reset error integrator
        self.ei = 0
        # reset error, otherwise de/dt will skyrocket
        self.e = 0

    def set_u_ref(self, u_ref: float):
        self.u_ref = u_ref

    def clear_errors(self):
        self.ei = 0
        self.de = 0

    def set_params(self, params:  PIDParams):
        self.dt             = params.dt

        self.Kp             = params.Kp             # proportional gain
        self.Ki             = params.Ki             # integral gain
        self.Kd             = params.Kd             # derivative gain

        # Integral action and control action saturation limits
        self.int_e_max      = params.int_e_max
        self.int_e_min      = params.int_e_min
        self.u_max          = params.u_max
        self.u_min          = params.u_min
        self.du_max         = params.du_max
        self.du_min         = params.du_min

    # Below are helper functions that might be helpful in debugging.
    def get_refs(self) -> Tuple[float, float]:
        return (self.x_ref, self.u_ref)

    def get_errors(self) -> Tuple[float, float, float]:
        return (self.e, self.de, self.ei)

    # Below are clipping functions to enforce hard constraints.
    def _saturate_abs_high(self, u: float) -> float:
        return np.minimum(u, self.u_max)

    def _saturate_abs_low(self, u: float) -> float:
        return np.maximum(u, self.u_min)

    def _saturate_rel_high(self, du: float) -> float:
        return np.minimum(du, self.du_max)

    def _saturate_rel_low(self, du: float) -> float:
        return np.maximum(du, self.du_min)

"""# Part 1: Implementing and tuning a PID lane follower
In this implementation, we follow a simple idea to control the speed and steering control separately by designing two separate PID controllers.

Specifically, the speed control PID tries to follow a reference speed, while the steering control PID tries to minimize the lateral deviation from the reference path.

Note that this PID controller only follows the center line of the track at a constant velocity.
Later, we will implement a better controller that can follow a race line.
"""

from mpclab_controllers.abstract_controller import AbstractController
from mpclab_common.pytypes import VehicleState

class PIDLaneFollower(AbstractController):
    '''
    Class for PID throttle and steering control of a vehicle
    Incorporates separate PID controllers for maintaining a constant speed and a constant lane offset

    target speed: v_ref
    target lane offset: x_ref
    '''
    def __init__(self, dt: float,
                steer_pid_params: PIDParams = None,
                speed_pid_params: PIDParams = None):
        # If no steering parameters provided, create default ones
        if steer_pid_params is None:
            steer_pid_params = PIDParams()
            steer_pid_params.dt = dt
            steer_pid_params.default_steer_params()

        # If no speed parameters provided, create default ones
        if speed_pid_params is None:
            speed_pid_params = PIDParams()
            speed_pid_params.dt = dt
            speed_pid_params.default_speed_params()  # these may use dt so it is updated first

        # Store the time step
        self.dt = dt
        steer_pid_params.dt = dt
        speed_pid_params.dt = dt

        # Store the parameter objects
        self.steer_pid_params = steer_pid_params
        self.speed_pid_params = speed_pid_params

        # Create PID controller instances for steering and speed
        self.steer_pid = PID(self.steer_pid_params)
        self.speed_pid = PID(self.speed_pid_params)

        # Store the lateral (lane) reference position
        self.lat_ref = steer_pid_params.x_ref
        self.steer_pid.set_x_ref(0)

        self.requires_env_state = False
        return

    def reset(self):
        # Reinstantiate the two PID controllers.
        # This clears any accumulated errors and internal states.
        self.steer_pid = PID(self.steer_pid_params)
        self.speed_pid = PID(self.speed_pid_params)

    def initialize(self, **args):
        return

    def solve(self, *args, **kwargs):
        raise NotImplementedError

    def step(self, vehicle_state: VehicleState, env_state = None):
        '''
        Main control logic at each timestep.
        Computes throttle and steering commands based on vehicle state.

        Args:
            vehicle_state: Current state of the vehicle
            env_state: Environment state

        Returns:
            numpy array: [acceleration, steering] control commands
        '''

        # Extract the current longitudinal velocity
        v = vehicle_state.v.v_long

        # Compute acceleration command using speed PID controller
        vehicle_state.u.u_a, _ = self.speed_pid.solve(v)

        # Weighting factor: alpha*x_trans + beta*psi_diff
        alpha = 5.0 # Weight for lateral (cross-track) error
        beta = 1.0 # Weight for heading error

        # Compute steering command using steering PID controller
        vehicle_state.u.u_steer, _ = self.steer_pid.solve(alpha*(vehicle_state.p.x_tran - self.lat_ref) + beta*vehicle_state.p.e_psi)
        return np.array([vehicle_state.u.u_a, vehicle_state.u.u_steer])

    def get_prediction(self):
        return None

    def get_safe_set(self):
        return None

"""## Create an instance of your PID controller with the parameters

Initialize/Reset your controller.
If you decide to implement your own controller, it must have a step function with the following method signature:

```
def step(state: VehicleState) -> np.ndarray:
    ...
```

Also, make sure your controller also modify the action fields (`state.u.u_a` and `state.u.u_steer`) in place, so that it will work in the hardware experiments later.
"""

#@markdown ## Set PID parameters
#@markdown ### Steering PID Parameters
Kp_steer = 0.65 #@param {type:"slider", min:0.0, max:2.0, step:0.05}
Ki_steer = 0.1  #@param {type:"slider", min:0.0, max:2.0, step:0.05}
Kd_steer = 0  #@param {type:"slider", min:0.0, max:2.0, step:0.05}

#@markdown ### Speed PID Parameters
Kp_speed = 0.4 #@param {type:"slider", min:0.0, max:2.0, step:0.05}
Ki_speed = 0  #@param {type:"slider", min:0.0, max:2.0, step:0.05}
Kd_speed = 0  #@param {type:"slider", min:0.0, max:2.0, step:0.05}
reference_speed = 1.0  #@param

# Some other global parameters. Don't modify them!
seed = 42
dt = 0.1

pid_steer_params = PIDParams(dt=dt,
                             Kp=Kp_steer, # Use the parameters from the sliders above.
                             Ki=Ki_steer,
                             Kd=Kd_steer,
                             u_max=0.436,
                             u_min=-0.436,
                             du_max=4.5,
                             du_min=-4.5,
                             x_ref=0.0,  # Offset from the center line. Set to 0 to follow the center line.
                             )

pid_speed_params = PIDParams(dt=dt,
                             Kp=Kp_speed,
                             Ki=Ki_speed,
                             Kd=Kd_speed,
                             u_max=2.0,
                             u_min=-2.0,
                             du_max=20.0,
                             du_min=-20.0,
                             x_ref=reference_speed,  # Reference speed.
                             )
pid_controller = PIDLaneFollower(dt, pid_steer_params, pid_speed_params)

"""## Simulate the controller
The environment is implemented following the standard definition of OpenAI gym.

**Explanation of the parameters:**
- `truncated` indicates whether it is at a terminal state (collision with the boundary, going too slow (slower than 0.25 m/s), going in the wrong way, or finished the requested number of laps)
- `terminated` indicates whether the car just finished a lap.
- `info` is a dictionary that contains the ground truth vehicle_state and other helpful debugging information.

"""

# Create an instance of the simulator.
env = gym.make('barc-v0',
               track_name='L_track_barc',
               do_render=True,
               max_n_laps=2,  # Feel free to modify this.
               in_colab=True,
               )

# Connect our PID controller to the environment
env.unwrapped.bind_controller(pid_controller)

# Reset the car to the starting line, with an initial velocity of 0.5 m/s.
_, info = env.reset(seed=seed, options={'spawning': 'fixed'})

truncated = False # Flag indicating whether the car is at a terminal state.

# Reset the PID controller (clear the errors).
pid_controller.reset()

# Create a list to track completion time for each lap.
lap_time = []

# Main simulation loop - runs until a terminal state is reached.
while not truncated:
    action = pid_controller.step(vehicle_state=info['vehicle_state']) # Call the controller to get the steering and speed commands.
    _, _, terminated, truncated, info = env.step(action) # Apply the action on the car and get the updated state.

    if terminated:
        lap_time.append(info['lap_time'])  # Keep track of the time it took to finish a lap.

# After simulation ends, display statistics about lap performance
logger.info(f"Average lap time: {np.mean(lap_time):.1f} s. Std: {np.std(lap_time):.1f} s.")
logger.info("Rollout truncated.")

"""## Playback of the race
This cell generates a matplotlib figure of the trajectory and statistics of the race that is just simulated.
Use this to debug and tune your controller!
"""

env.unwrapped.show_debug_plot()

"""## Animation (Optional)
Render an animation of the race.

Note that this may take a long time (about 4 frames per second). (FYI the simulation runs at 10 frames per second)

The playback speed is **2x compared to real time.**
"""

render_animation = False  #@param {type:"boolean"}

if render_animation:
    from IPython.display import HTML
    animation = env.unwrapped.visualizer.get_animation()
    print(f"Please wait for the animation to be rendered. Estimated total wait time: {len(env.unwrapped.v_buffer) * 0.25:.1f} s. ")
    display(HTML(animation.to_html5_video()))

"""## Compare with hardware data (Optional)
**Warning:** Upload the csv file converted from the ROS bag that is generated on your hardware test first!

Replace TODO with your group name (e.g., group_1). Your csv file should have the same name.
"""

compare_with_hardware_data = False  #@param {type:"boolean"}
group_name = "TODO" #@param {type:"string"}

if compare_with_hardware_data:
    import pandas as pd
    from google.colab import files
    import os

    while not os.path.exists(f'{group_name}.csv'):
        print(f"Data file {group_name}.csv not found. Please upload it here.")
        uploaded = files.upload()
    df = pd.read_csv(f'{group_name}.csv', delimiter=',')

    fig, axes = plt.subplots(2, 2, figsize=(9, 9))
    ((ax_traj, ax_v), (ax_u_a, ax_u_steer)) = axes
    start_idx = np.where((df['u_a'] != 0) & (df['u_steer'] != 0) & (df['v_long'] >= 0.5))[0][0]
    t = df['t'][start_idx:]
    t = (t - t.min()) / 1e9

    env.unwrapped.get_track().plot_map(ax=ax_traj)
    ax_traj.plot(df['x'][start_idx:], df['y'][start_idx:], label='hardware')
    ax_traj.set_title("Trajectory Playback")
    ax_traj.set_xlabel('x(m)')
    ax_traj.set_ylabel('y(m)')

    ax_v.plot(t, df['v_long'][start_idx:], label='hardware')
    ax_v.set_title("Velocity playback")
    ax_v.set_xlabel('t(s)')
    ax_v.set_ylabel('v(m/s)')

    ax_u_a.plot(t, df['u_a'][start_idx:], label='hardware')
    ax_u_a.set_title("Acceleration input playback")
    ax_u_a.set_xlabel('t(s)')
    ax_u_a.set_ylabel('$u_a$')

    ax_u_steer.plot(t, df['u_steer'][start_idx:], label='hardware')
    ax_u_steer.set_title('Steering input playback')
    ax_u_steer.set_xlabel('t(s)')
    ax_u_steer.set_ylabel('$u_{steer}$')

    env.unwrapped.show_debug_plot(axes)

    for ax in axes.flatten():
        ax.legend()

    plt.show()

