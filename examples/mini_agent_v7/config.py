"""Harness configuration: the control knobs come from config, not hard-coded.

This closes the gap the reader pointed out: a harness that "manages everything"
must also **manage its own settings** — otherwise `max_replans=2` is a magic
number buried in code, and changing it means a redeploy.

Design:
  * one ``HarnessConfig`` dataclass holds every knob the harness reads;
  * ``from_env`` parses strings with validation (same discipline as v3);
  * ``for_scenario`` lets different tasks get different budgets/policies;
  * the harness takes a config and reads it in ONE place — no magic numbers.

Everything here is testable without touching the process environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional


class ConfigError(ValueError):
    """Invalid harness configuration.  Always names the field."""


def _int(env: Dict[str, str], key: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("%s 必须是整数，当前为 %r" % (key, raw))
    if value < minimum:
        raise ConfigError("%s 不能小于 %d，当前为 %d" % (key, minimum, value))
    return value


def _float(env: Dict[str, str], key: str, default: float, *, minimum: float = 0.0) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("%s 必须是数字，当前为 %r" % (key, raw))
    if value < minimum:
        raise ConfigError("%s 不能小于 %s，当前为 %s" % (key, minimum, value))
    return value


def _bool(env: Dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError("%s 必须是 true/false，当前为 %r" % (key, raw))


@dataclass
class HarnessConfig:
    """Every knob the Plan-Execute harness reads.  One place, no magic numbers."""

    # budget
    max_steps: int = 16
    max_tool_calls: int = 16
    max_tokens: int = 60_000
    max_seconds: float = 60.0
    # loop policy
    max_replans: int = 2
    max_attempts_per_step: int = 2
    early_stop: bool = True  # stop as soon as the verifier passes
    # observability
    trace_enabled: bool = True
    # metadata (which deployment/tenant this config is for)
    profile: str = "default"

    def validate(self) -> None:
        if self.max_steps < 1:
            raise ConfigError("max_steps 必须 >= 1")
        if self.max_attempts_per_step < 1:
            raise ConfigError("max_attempts_per_step 必须 >= 1")
        if self.max_replans < 0:
            raise ConfigError("max_replans 不能为负")
        if self.max_tokens < 1:
            raise ConfigError("max_tokens 必须 >= 1")
        if self.max_seconds <= 0:
            raise ConfigError("max_seconds 必须 > 0")

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "HarnessConfig":
        env = dict(os.environ if env is None else env)
        cfg = cls(
            max_steps=_int(env, "HARNESS_MAX_STEPS", 16, minimum=1),
            max_tool_calls=_int(env, "HARNESS_MAX_TOOL_CALLS", 16, minimum=1),
            max_tokens=_int(env, "HARNESS_MAX_TOKENS", 60_000, minimum=1),
            max_seconds=_float(env, "HARNESS_MAX_SECONDS", 60.0, minimum=0.1),
            max_replans=_int(env, "HARNESS_MAX_REPLANS", 2, minimum=0),
            max_attempts_per_step=_int(env, "HARNESS_MAX_ATTEMPTS", 2, minimum=1),
            early_stop=_bool(env, "HARNESS_EARLY_STOP", True),
            trace_enabled=_bool(env, "HARNESS_TRACE_ENABLED", True),
            profile=env.get("HARNESS_PROFILE", "default") or "default",
        )
        cfg.validate()
        return cfg

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "HarnessConfig":
        """For loading from JSON/YAML config files (control plane downlink)."""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ConfigError("未知配置项：%s" % ", ".join(sorted(unknown)))
        cfg = cls(**data)  # type: ignore[arg-type]
        cfg.validate()
        return cfg

    def public_dict(self) -> Dict[str, object]:
        return {
            "profile": self.profile,
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds,
            "max_replans": self.max_replans,
            "max_attempts_per_step": self.max_attempts_per_step,
            "early_stop": self.early_stop,
            "trace_enabled": self.trace_enabled,
        }


# ---------------------------------------------------------------------------
# Named profiles: different tasks get different policies.
# ---------------------------------------------------------------------------

PROFILES: Dict[str, HarnessConfig] = {
    # interactive: fast, cheap, fail early
    "interactive": HarnessConfig(
        profile="interactive", max_steps=8, max_tool_calls=8, max_tokens=20_000,
        max_seconds=15.0, max_replans=1, max_attempts_per_step=2,
    ),
    # batch / long task: more budget, more patience
    "batch": HarnessConfig(
        profile="batch", max_steps=40, max_tool_calls=60, max_tokens=200_000,
        max_seconds=600.0, max_replans=3, max_attempts_per_step=3,
    ),
    # strict / high-risk: minimal retries, no early-stop to force full verify
    "strict": HarnessConfig(
        profile="strict", max_steps=12, max_tool_calls=12, max_tokens=40_000,
        max_replans=1, max_attempts_per_step=1, early_stop=False,
    ),
}


def for_scenario(name: str) -> HarnessConfig:
    """Look up a named profile; unknown name falls back to default with a guard."""
    if name in PROFILES:
        # return a copy so callers can't mutate the shared profile
        base = PROFILES[name]
        return HarnessConfig(**base.public_dict())  # type: ignore[arg-type]
    if name in ("", "default"):
        return HarnessConfig()
    raise ConfigError("未知 profile：%s（可选：%s）" % (name, ", ".join(PROFILES)))
