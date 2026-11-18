"""T4 policy interface and the hybrid-mode scripted expert.

Hybrid mode (spec V2 fallback): the chip model charges T4 with the cost of a real
vision-language-action model's operator graph, while the arm's behavior comes from this
scripted expert. The expert is deliberately simple and honest about what it knows:

* object positions and velocities come only from T6 tracks, which carry the capture time of
  the detection behind them, so the expert knows how old its information is;
* it predicts where the target will be at the grasp moment by extrapolating the track
  (position + velocity * age), choosing a grasp time it can still make given its measured
  goal-to-activation latency;
* it refines the intercept on every tick while the refinement can still land before the
  grasp, and stops refining once its own pipeline latency says the update would arrive too
  late. With a slow chip, the last usable refinement is older, so prediction error grows.

Any other policy (for example a real VLA producing end-effector action chunks) can slot in
by implementing Policy.act and returning Goal objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from cosim.envs import build_scene as layout
from cosim.stack.estimator import Track
from cosim.stack.planner import Goal, PlannerConfig, grasp_yaw


@dataclass
class PlanStatus:
    """What the robot reports about its active trajectory (read from controller state)."""

    kind: str
    plan_id: int
    target_uid: int
    t_grasp: float
    t_release: float
    t_end: float
    t_act: float
    goal_release_t: float


@dataclass
class PolicyObservation:
    t: float
    tracks: dict[int, Track]
    tcp: np.ndarray
    gripper_width: float
    plan: PlanStatus | None
    alpha: float                 # current controller speed scale
    alpha_target: float          # latest safety output
    lag: float                   # trajectory clock lag (s)
    belt_speed_setpoint: float
    rejected_plan_ids: frozenset = frozenset()   # pick episodes the planner found infeasible


class Policy(Protocol):
    """Anything that maps an observation to a goal (or None for no change)."""

    name: str

    def act(self, obs: PolicyObservation) -> Goal | None: ...


class VLAPolicy:
    """Placeholder for a real VLA in the loop (cinematic mode).

    A VLA would consume camera frames plus a language instruction and emit end-effector action
    chunks. To slot it in, convert each chunk into a Goal (or a new goal kind tracked by the
    planner) and return it from act(). Not implemented here: on this machine no open VLA runs
    closed loop at a useful rate, so the co-sim uses ScriptedExpert in hybrid mode.
    """

    name = "vla"

    def act(self, obs: PolicyObservation) -> Goal | None:  # pragma: no cover
        raise NotImplementedError("VLA-in-the-loop is not available; use ScriptedExpert (hybrid mode)")


@dataclass
class ExpertConfig:
    min_track_n: int = 3
    refine_tol_m: float = 0.002
    reach_speed: float = 0.7          # m/s, rough TCP speed used to estimate time to the pre-grasp
    max_retries: int = 1
    latency_init: float = 0.08
    latency_margin: float = 0.03
    activation_timeout: float = 1.5


@dataclass
class ScriptedExpert:
    """Picks the next reachable object and keeps refining the intercept while it can."""

    planner_cfg: PlannerConfig = field(default_factory=PlannerConfig)
    cfg: ExpertConfig = field(default_factory=ExpertConfig)
    name: str = "scripted_expert"

    def __post_init__(self) -> None:
        self.next_plan_id = 1
        self.latency_est = self.cfg.latency_init
        self.seen_plans: set[int] = set()
        self.failed: dict[int, int] = {}
        self.done_uids: set[int] = set()
        self.last_goal: Goal | None = None
        self.last_goal_t = -1e9
        self.top = layout.CONVEYOR["top_z"]
        self.miss_checked: set[int] = set()
        self.theta: dict[int, float] = {}

    # ------------------------------------------------------------------ helpers
    def _new_id(self) -> int:
        pid = self.next_plan_id
        self.next_plan_id += 1
        return pid

    def _waiting(self, obs: PolicyObservation) -> bool:
        """True while the last emitted new-episode goal has not become the active plan yet."""
        g = self.last_goal
        if g is None:
            return False
        if obs.plan is not None and obs.plan.plan_id == g.plan_id:
            return False
        if g.plan_id in obs.rejected_plan_ids:
            if g.uid >= 0:
                self.failed[g.uid] = self.failed.get(g.uid, 0) + 1
            self.last_goal = None
            return False
        return obs.t - self.last_goal_t < self.cfg.activation_timeout

    def _on_belt(self, tr: Track) -> bool:
        return abs(tr.z - (self.top + tr.half_h)) < 0.025 and abs(tr.x[0] - layout.CONVEYOR["x"]) < 0.17

    def _theta(self, tr: Track, plan_id: int) -> float:
        """Closing direction for this episode, kept continuous across refinements."""
        th = grasp_yaw(tr.kind, tr.yaw, self.planner_cfg.grasp_axis)
        prev = self.theta.get(plan_id)
        if prev is not None and tr.kind == "box":
            # equivalent closing directions for a box differ by 90 degrees; stay near the previous one
            cands = [th + k * np.pi / 2 for k in (-2, -1, 0, 1, 2)]
            th = min(cands, key=lambda c: abs(c - prev))
            if abs(th - prev) < np.deg2rad(8):
                th = prev
        self.theta[plan_id] = th
        return th

    def _pick_goal(self, obs: PolicyObservation, tr: Track, t_g: float, plan_id: int, refine: bool = False) -> Goal:
        return Goal(theta=self._theta(tr, plan_id), place_slot=plan_id, refine=refine, kind="pick", uid=tr.uid, p_obs=tr.pos, v_obs=tr.vel, t_obs=tr.t, yaw=tr.yaw, kind_obj=tr.kind,
                    half_h=tr.half_h, t_grasp=t_g, latency_budget=self.latency_est + self.cfg.latency_margin,
                    plan_id=plan_id, origin=tr.origin, release_t=obs.t)

    def _select(self, obs: PolicyObservation) -> Goal | None:
        pc = self.planner_cfg
        best = None
        L = self.latency_est + self.cfg.latency_margin
        for tr in obs.tracks.values():
            if tr.n < self.cfg.min_track_n or tr.uid in self.done_uids:
                continue
            if self.failed.get(tr.uid, 0) > self.cfg.max_retries or not self._on_belt(tr):
                continue
            vy = max(tr.x[3], 0.02)
            p_now = tr.pos + tr.vel * (obs.t - tr.t)
            hover = np.array([p_now[0], p_now[1], tr.z + pc.h_pre])
            t_reach = float(np.linalg.norm(hover - obs.tcp)) / self.cfg.reach_speed + 0.25
            t_g = obs.t + L + t_reach + pc.t_hover + pc.t_desc
            y_g = p_now[1] + vy * (t_g - obs.t)
            y_lo = pc.y_min + vy * (pc.t_hover + pc.t_desc) + 0.02   # hover point inside the window
            if y_g < y_lo:
                t_g = obs.t + (y_lo - p_now[1]) / vy
                y_g = y_lo
            if y_g > pc.y_max - 0.03:
                continue
            if best is None or t_g < best[0]:
                best = (t_g, tr)
        if best is None:
            return None
        return self._pick_goal(obs, best[1], best[0], self._new_id())

    def _emit(self, g: Goal, obs: PolicyObservation) -> Goal:
        self.last_goal = g
        self.last_goal_t = obs.t
        return g

    # ------------------------------------------------------------------ main
    def act(self, obs: PolicyObservation) -> Goal | None:
        p = obs.plan
        if p is not None and p.plan_id not in self.seen_plans and not np.isnan(p.goal_release_t):
            self.seen_plans.add(p.plan_id)
            meas = p.t_act - p.goal_release_t
            self.latency_est = 0.7 * self.latency_est + 0.3 * meas if self.seen_plans else meas
        # Safety slowdown: let the controller scale speed; do not start anything new.
        if obs.alpha_target < 0.999 or obs.alpha < 0.999:
            return None
        holding = obs.gripper_width > 0.008
        if self._waiting(obs):
            # A new episode was requested; only refine it if it is the active pick.
            return None
        # Recover from a safety stop: the old plan's timing is stale.
        if obs.lag > 0.03:
            if p is not None and p.kind in ("pick", "place") and holding and obs.t > p.t_grasp:
                return self._emit(Goal("place", plan_id=self._new_id(), uid=p.target_uid, release_t=obs.t), obs)
            g = self._select(obs)
            if g is None:
                g = Goal("home", plan_id=self._new_id(), release_t=obs.t)
            return self._emit(g, obs)
        if p is None or p.kind in ("home", "hold"):
            g = self._select(obs)
            return self._emit(g, obs) if g is not None else None
        if p.kind == "pick":
            pc = self.planner_cfg
            if obs.t < p.t_grasp:
                # Refine while the refinement can still land before the grasp.
                if obs.t + self.latency_est + self.cfg.latency_margin < p.t_grasp - 0.02:
                    tr = obs.tracks.get(p.target_uid)
                    if tr is not None and tr.n >= self.cfg.min_track_n:
                        g = self._pick_goal(obs, tr, p.t_grasp, p.plan_id, refine=True)
                        lg = self.last_goal
                        if lg is not None and lg.plan_id == p.plan_id and lg.kind == "pick":
                            a = lg.p_obs + lg.v_obs * (p.t_grasp - lg.t_obs)
                            b = g.p_obs + g.v_obs * (p.t_grasp - g.t_obs)
                            if np.linalg.norm(a - b) < self.cfg.refine_tol_m:
                                return None
                        return self._emit(g, obs)
                return None
            t_check = p.t_grasp + pc.t_close + 0.6 * pc.t_lift
            if obs.t < t_check:
                return None
            if obs.t < p.t_release:
                if not holding and p.plan_id not in self.miss_checked:
                    self.miss_checked.add(p.plan_id)
                    self.failed[p.target_uid] = self.failed.get(p.target_uid, 0) + 1
                    g = self._select(obs) or Goal("home", plan_id=self._new_id(), release_t=obs.t)
                    return self._emit(g, obs)
                if holding:
                    self.done_uids.add(p.target_uid)
                return None
            if obs.t > p.t_release + 0.15:
                self.done_uids.add(p.target_uid)
                g = self._select(obs)
                return self._emit(g, obs) if g is not None else None
            return None
        if p.kind == "place":
            if obs.t > p.t_release + 0.15:
                g = self._select(obs)
                return self._emit(g, obs) if g is not None else None
        return None
