"""Runtime configuration: environment in, validated settings out.

Three rules, all learned from production incidents:

1. **Fail fast on startup.**  A missing secret or a malformed limit must stop
   the process, not surface as a confusing error on the first request.
2. **Parsing is total and explicit.**  ``MINI_AGENT_PORT=abc`` is an error with
   the variable name in it, not a silent fallback to 8080.
3. **Secrets never have defaults.**  An API key defaulting to a placeholder is
   how you end up calling a provider with ``"changeme"`` in production.

The config is a plain dataclass, so it is trivially testable without touching
the process environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class ConfigError(ValueError):
    """Raised on invalid or missing configuration.  Always names the variable."""


def _get(env: Dict[str, str], key: str, default: Optional[str]) -> Optional[str]:
    value = env.get(key)
    if value is None or value == "":
        return default
    return value


def _int(env: Dict[str, str], key: str, default: int, *, minimum: int = 0) -> int:
    raw = _get(env, key, None)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("%s 必须是整数，当前为 %r" % (key, raw))
    if value < minimum:
        raise ConfigError("%s 不能小于 %d，当前为 %d" % (key, minimum, value))
    return value


def _float(env: Dict[str, str], key: str, default: float, *, minimum: float = 0.0) -> float:
    raw = _get(env, key, None)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("%s 必须是数字，当前为 %r" % (key, raw))
    if value < minimum:
        raise ConfigError("%s 不能小于 %s，当前为 %s" % (key, minimum, value))
    return value


def _bool(env: Dict[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key, None)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError("%s 必须是 true/false，当前为 %r" % (key, raw))


def _csv(env: Dict[str, str], key: str, default: str = "") -> List[str]:
    raw = _get(env, key, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _tenant_limits(env: Dict[str, str]) -> Dict[str, float]:
    """``TENANT_LIMITS=acme:5,globex:2`` -> {"acme": 5.0, "globex": 2.0}"""
    limits: Dict[str, float] = {}
    for item in _csv(env, "TENANT_LIMITS", ""):
        if ":" not in item:
            raise ConfigError(
                "TENANT_LIMITS 的每一项必须是 tenant:limit，当前为 %r" % item
            )
        tenant, _, value = item.partition(":")
        try:
            limits[tenant.strip()] = float(value)
        except ValueError:
            raise ConfigError(
                "TENANT_LIMITS 中 %r 的额度不是数字：%r" % (tenant.strip(), value)
            )
    return limits


@dataclass
class Settings:
    # server
    host: str = "127.0.0.1"
    port: int = 8000
    request_timeout_s: float = 15.0
    max_body_bytes: int = 64 * 1024

    # model
    model: str = "gpt-4.1-mini"
    api_key: str = ""  # empty means "no live model"; the stand-in is used
    base_url: str = "https://api.openai.com/v1"

    # platform
    default_tenant: str = "acme"
    tenant_limits: Dict[str, float] = field(default_factory=lambda: {"acme": 5.0})
    tenant_allowlist: List[str] = field(default_factory=list)  # empty = all
    default_limit_usd: float = 1.0

    # safety switches
    readonly: bool = False  # refuse every tool with side_effect != read
    guard_enabled: bool = True

    # storage
    data_dir: str = "./.data"

    # metadata
    env: str = "dev"  # dev | staging | prod

    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        settings = cls(
            host=_get(env, "MINI_AGENT_HOST", "127.0.0.1") or "127.0.0.1",
            # Port 0 is legal: it asks the OS for a free port, which is how the
            # test suite starts servers without hard-coding numbers.  Anything
            # outside 0..65535 is still rejected in ``validate``.
            port=_int(env, "MINI_AGENT_PORT", 8000, minimum=0),
            request_timeout_s=_float(env, "MINI_AGENT_TIMEOUT_S", 15.0, minimum=0.1),
            max_body_bytes=_int(env, "MINI_AGENT_MAX_BODY_BYTES", 64 * 1024, minimum=1024),
            model=_get(env, "MINI_AGENT_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini",
            api_key=_get(env, "OPENAI_API_KEY", "") or "",
            base_url=_get(env, "OPENAI_BASE_URL", "https://api.openai.com/v1")
            or "https://api.openai.com/v1",
            default_tenant=_get(env, "DEFAULT_TENANT", "acme") or "acme",
            tenant_limits=_tenant_limits(env),
            tenant_allowlist=_csv(env, "TENANT_ALLOWLIST", ""),
            default_limit_usd=_float(env, "DEFAULT_TENANT_LIMIT_USD", 1.0),
            readonly=_bool(env, "MINI_AGENT_READONLY", False),
            guard_enabled=_bool(env, "MINI_AGENT_GUARD", True),
            data_dir=_get(env, "MINI_AGENT_DATA_DIR", "./.data") or "./.data",
            env=_get(env, "MINI_AGENT_ENV", "dev") or "dev",
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.env not in ("dev", "staging", "prod"):
            raise ConfigError("MINI_AGENT_ENV 必须是 dev/staging/prod，当前为 %r" % self.env)
        if self.env == "prod" and self.readonly is False and self.api_key == "":
            # Running "prod" with no model silently degrades to the stand-in;
            # that is a configuration mistake, not a feature.
            raise ConfigError(
                "prod 环境必须配置 OPENAI_API_KEY（否则会退化为确定性替身并给出假答案）"
            )
        if self.default_tenant not in self.tenant_limits:
            self.tenant_limits[self.default_tenant] = self.default_limit_usd
        unknown = [t for t in self.tenant_allowlist if t not in self.tenant_limits]
        if unknown:
            # An allowlisted tenant with no limit is a billing hole, not a default.
            raise ConfigError(
                "TENANT_ALLOWLIST 中的租户缺少额度配置：%s" % ", ".join(unknown)
            )
        if self.port < 0 or self.port > 65535:
            raise ConfigError("MINI_AGENT_PORT 必须在 0..65535，当前为 %d" % self.port)

    # ------------------------------------------------------------------

    def tenant_allowed(self, tenant_id: str) -> bool:
        if not self.tenant_allowlist:
            return True
        return tenant_id in self.tenant_allowlist

    def limit_for(self, tenant_id: str) -> float:
        return self.tenant_limits.get(tenant_id, self.default_limit_usd)

    def public_dict(self) -> Dict[str, object]:
        """Everything safe to expose on /health.  Never includes the API key."""
        return {
            "env": self.env,
            "model": self.model,
            "live_model": bool(self.api_key),
            "readonly": self.readonly,
            "guard": self.guard_enabled,
            "tenants": sorted(self.tenant_limits),
            "allowlist_enabled": bool(self.tenant_allowlist),
        }
