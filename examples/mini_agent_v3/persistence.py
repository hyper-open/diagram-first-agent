"""Durable run state: checkpoints, resume and leases.

The lesson is a definition change: a long task is **not** a function call that
runs for three hours — it is a sequence of recorded steps that can be replayed
after the process dies.

Three mechanisms, smallest useful version of each:

* **checkpoint** — after every step the state is written to disk;
* **resume** — ``load`` returns the last checkpoint plus the steps already done,
  so the loop continues instead of starting over;
* **lease** — a worker must hold a non-expired lease to run a step.  A crashed
  worker's lease expires and another worker can take over — and a fencing token
  (``version``) prevents the old worker from writing after it lost the lease.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class LeaseLost(RuntimeError):
    """Raised when a worker tries to commit without holding the lease."""


@dataclass
class RunState:
    run_id: str
    goal: str
    status: str = "running"  # running | done | failed | partial
    step: int = 0
    outputs: List[str] = field(default_factory=list)
    completed_steps: List[str] = field(default_factory=list)
    lease_owner: Optional[str] = None
    lease_expires_at: float = 0.0
    version: int = 0  # fencing token
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: Dict[str, Any]) -> "RunState":
        return cls(**row)


class FileStore:
    """One JSON file per run.  Crash-safe enough for a tutorial.

    Each save writes to a temporary file and renames it, so a crash mid-write
    cannot leave a half-written checkpoint (the same reason databases use WAL).
    """

    def __init__(self, root: str):
        self.root = Path(root)
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self.runs_dir / ("%s.json" % run_id)

    def load(self, run_id: str) -> Optional[RunState]:
        path = self._path(run_id)
        if not path.exists():
            return None
        return RunState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, state: RunState) -> RunState:
        state.updated_at = time.time()
        path = self._path(state.run_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)  # atomic on POSIX
        return state

    def list_runs(self) -> List[str]:
        return sorted(p.stem for p in self.runs_dir.glob("*.json"))


class RunManager:
    """Create, lease, checkpoint and resume runs."""

    def __init__(self, store: FileStore, *, lease_ttl_s: float = 30.0, worker_id: str = ""):
        self.store = store
        self.lease_ttl_s = lease_ttl_s
        self.worker_id = worker_id or "worker-" + uuid.uuid4().hex[:6]

    # -- lifecycle -------------------------------------------------------

    def create(self, goal: str) -> RunState:
        state = RunState(run_id="run_" + uuid.uuid4().hex[:10], goal=goal)
        return self.store.save(state)

    def acquire(self, run_id: str) -> RunState:
        state = self.store.load(run_id)
        if state is None:
            raise KeyError(run_id)
        now = time.time()
        if state.lease_owner and state.lease_expires_at > now and state.lease_owner != self.worker_id:
            raise LeaseLost(
                "run %s 仍被 %s 持有（剩余 %.0fs）"
                % (run_id, state.lease_owner, state.lease_expires_at - now)
            )
        state.lease_owner = self.worker_id
        state.lease_expires_at = now + self.lease_ttl_s
        state.version += 1
        return self.store.save(state)

    def renew(self, state: RunState) -> RunState:
        if state.lease_owner != self.worker_id:
            raise LeaseLost("当前 worker 不是租约持有者")
        state.lease_expires_at = time.time() + self.lease_ttl_s
        return self.store.save(state)

    def checkpoint(self, state: RunState, step_name: str, output: str = "") -> RunState:
        """Persist progress.  Guarded by the lease *and* the fencing token."""
        current = self.store.load(state.run_id)
        assert current is not None
        if current.lease_owner != self.worker_id:
            raise LeaseLost(
                "租约已被 %s 接管（fencing version %d），拒绝写入"
                % (current.lease_owner, current.version)
            )
        if current.version != state.version:
            raise LeaseLost(
                "fencing token 不匹配：本地 %d，存储 %d" % (state.version, current.version)
            )
        if current.lease_expires_at <= time.time():
            raise LeaseLost("租约已过期，需重新 acquire")

        state.step += 1
        state.completed_steps.append(step_name)
        if output:
            state.outputs.append(output)
        return self.store.save(state)

    def finish(self, state: RunState, status: str = "done") -> RunState:
        state.status = status
        state.lease_owner = None
        state.lease_expires_at = 0.0
        return self.store.save(state)

    # -- inspection ------------------------------------------------------

    def resume_plan(self, run_id: str) -> Dict[str, Any]:
        """What remains to be done — the input to a resuming worker."""
        state = self.store.load(run_id)
        if state is None:
            raise KeyError(run_id)
        now = time.time()
        lease_active = bool(state.lease_owner) and state.lease_expires_at > now
        return {
            "run_id": state.run_id,
            "goal": state.goal,
            "status": state.status,
            # ``done_steps`` / ``next_step_index`` let a new worker continue
            # instead of redoing work — this is the whole point of checkpointing.
            "done_steps": len(state.completed_steps),
            "completed_steps": list(state.completed_steps),
            "next_step_index": state.step,
            "lease_owner": state.lease_owner,
            "lease_remaining_s": (
                round(state.lease_expires_at - now, 3) if lease_active else 0.0
            ),
            # A second worker may *plan* a resume immediately; it may only
            # *commit* once the lease is free.  Conflating the two makes the
            # plan look useless right after a checkpoint.
            "can_resume": state.status == "running",
            "can_take_over": state.status == "running" and not lease_active,
        }
