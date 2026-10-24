"""Deterministic content guard: same contract as a hosted moderation API.

A real system puts a model or a vendor classifier behind check().  The
teaching point here is the contract around the classifier, not the classifier
itself:

    check(text, stage) -> GuardResult(action, text, findings, reason)

Four actions:

* allow   - pass through unchanged
* rewrite - pass through after masking (PII, secrets in output)
* review  - pass through but flag for sampling / human review
* block   - refuse; never reach the model, the user, or long-term memory

The same finding can map to different actions depending on stage.  A phone
number the user typed is fine; the same number in a model answer, a tool
result, or a memory write is not.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Pattern, Sequence, Tuple

ALLOW = "allow"
REWRITE = "rewrite"
REVIEW = "review"
BLOCK = "block"

_ORDER = {ALLOW: 0, REWRITE: 1, REVIEW: 2, BLOCK: 3}

REFUSAL = (
    "抱歉，这个请求涉及平台不允许提供的内容，我无法协助。"
    "你可以换一种方式描述目标，或联系人工支持。"
)

#: Default action per category and stage.  "*" matches any stage.
DEFAULT_ACTIONS: Dict[Tuple[str, str], str] = {
    ("secret", "*"): BLOCK,
    ("pii", "output"): REWRITE,
    ("pii", "tool_result"): REWRITE,
    ("pii", "memory"): REWRITE,
    ("pii", "*"): ALLOW,
    ("self_harm", "*"): REVIEW,
    ("weapons", "*"): BLOCK,
    ("illegal", "*"): BLOCK,
    ("harassment", "output"): REWRITE,
    ("harassment", "*"): REVIEW,
    ("prompt_injection", "tool_result"): REVIEW,
    ("prompt_injection", "*"): ALLOW,
}

#: Fallback when a category has no explicit rule: severity decides.
SEVERITY_DEFAULT = {3: BLOCK, 2: REVIEW, 1: ALLOW}


@dataclass(frozen=True)
class Rule:
    name: str
    category: str
    severity: int  # 1 low, 2 medium, 3 high
    pattern: Pattern[str]
    mask: str = ""  # replacement used by rewrite
    note: str = ""


@dataclass(frozen=True)
class Finding:
    rule: str
    category: str
    severity: int
    start: int
    end: int
    sample: str


@dataclass
class GuardResult:
    action: str
    text: str
    findings: List[Finding] = field(default_factory=list)
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.action == BLOCK

    @property
    def categories(self) -> List[str]:
        return sorted({f.category for f in self.findings})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "categories": self.categories,
            "findings": [f.__dict__ for f in self.findings],
        }


def _rx(pattern: str) -> Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


DEFAULT_RULES: List[Rule] = [
    # --- secrets: never leave the trust boundary -------------------------
    Rule("secret.openai", "secret", 3, _rx("sk-[A-Za-z0-9_-]{16,}"), "sk-***"),
    Rule("secret.aws", "secret", 3, _rx("AKIA[0-9A-Z]{16}"), "AKIA***"),
    Rule(
        "secret.private_key",
        "secret",
        3,
        _rx("-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "[private key]",
    ),
    # --- PII: allowed in, masked out -------------------------------------
    Rule("pii.cn_id", "pii", 2, _rx("[0-9]{17}[0-9Xx]"), "[身份证号]"),
    Rule("pii.cn_phone", "pii", 2, _rx("1[3-9][0-9]{9}"), "[手机号]"),
    Rule(
        "pii.email",
        "pii",
        1,
        _rx("[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}"),
        "[邮箱]",
    ),
    # --- harmful categories ----------------------------------------------
    Rule(
        "harm.weapons",
        "weapons",
        3,
        _rx("(制作|制造|合成).{0,6}(炸弹|爆炸物|燃烧瓶|神经毒剂)"),
    ),
    Rule(
        "harm.illegal",
        "illegal",
        3,
        _rx("(洗钱|毒品交易|买卖公民信息|伪造证件|入侵.{0,4}系统)"),
    ),
    Rule(
        "harm.self_harm",
        "self_harm",
        3,
        _rx("(自杀|自残|结束自己的生命|不想活了)"),
        note="高风险但需要帮助信号：走 review + 安全提示，而不是简单拒绝",
    ),
    Rule(
        "harm.harassment",
        "harassment",
        2,
        _rx("(去死|傻逼|废物|滚出去)"),
        "[不当用语]",
    ),
    # --- injected instructions inside untrusted text ----------------------
    Rule(
        "inject.ignore",
        "prompt_injection",
        2,
        _rx(
            "(ignore (all )?(previous|above) instructions"
            "|忽略(之前|以上|前面).{0,6}(指令|提示)"
            "|你现在是.{0,10}(开发者模式|DAN))"
        ),
    ),
    Rule(
        "inject.exfiltrate",
        "prompt_injection",
        2,
        _rx("(把|将).{0,8}(api ?key|密钥|令牌|token).{0,10}(发|上传|发送)"),
    ),
]


class Guard:
    """Rule-based guard with a stage-aware policy.

    Deterministic on purpose: policies must be testable, reviewable and
    versionable.  A model classifier can be added as another rule source
    without changing this contract.
    """

    def __init__(
        self,
        rules: Optional[Sequence[Rule]] = None,
        actions: Optional[Dict[Tuple[str, str], str]] = None,
        severity_default: Optional[Dict[int, str]] = None,
    ):
        self.rules = list(rules if rules is not None else DEFAULT_RULES)
        self.actions = dict(DEFAULT_ACTIONS)
        if actions:
            self.actions.update(actions)
        self.severity_default = dict(SEVERITY_DEFAULT)
        if severity_default:
            self.severity_default.update(severity_default)

    # -- public API ------------------------------------------------------

    def check(self, text: str, stage: str = "output") -> GuardResult:
        text = text or ""
        findings = self._scan(text)
        action = self._decide(findings, stage)
        result_text = text

        if action == REWRITE:
            result_text = self._apply_masks(text, findings)
        elif action == BLOCK:
            result_text = ""

        reason = ""
        if findings and action != ALLOW:
            reason = "命中规则: " + ", ".join(sorted({f.rule for f in findings}))

        return GuardResult(action=action, text=result_text, findings=findings, reason=reason)

    def check_json(self, payload: Any, stage: str = "tool_result") -> GuardResult:
        """Guard a structured payload by scanning its serialized form.

        Only block is enforceable on structured data: masking a field would
        silently change the schema.  Field-level rewrite needs per-field policy.
        """
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return self.check(text, stage=stage)

    # -- internals -------------------------------------------------------

    def _scan(self, text: str) -> List[Finding]:
        findings: List[Finding] = []
        for rule in self.rules:
            for match in rule.pattern.finditer(text):
                findings.append(
                    Finding(
                        rule=rule.name,
                        category=rule.category,
                        severity=rule.severity,
                        start=match.start(),
                        end=match.end(),
                        sample=match.group(0)[:24],
                    )
                )
        return findings

    def _decide(self, findings: Iterable[Finding], stage: str) -> str:
        action = ALLOW
        for finding in findings:
            candidate = self._action_for(finding, stage)
            if _ORDER[candidate] > _ORDER[action]:
                action = candidate
        return action

    def _action_for(self, finding: Finding, stage: str) -> str:
        for key in ((finding.category, stage), (finding.category, "*")):
            if key in self.actions:
                return self.actions[key]
        default = self.severity_default.get(finding.severity, REVIEW)
        if default == REWRITE and not self._mask_for(finding.rule):
            return REVIEW
        return default

    def _mask_for(self, rule_name: str) -> str:
        for rule in self.rules:
            if rule.name == rule_name:
                return rule.mask
        return ""

    def _apply_masks(self, text: str, findings: List[Finding]) -> str:
        # Replace right-to-left so earlier offsets stay valid.
        masked = text
        for finding in sorted(findings, key=lambda f: f.start, reverse=True):
            mask = self._mask_for(finding.rule)
            if not mask:
                continue
            masked = masked[: finding.start] + mask + masked[finding.end :]
        return masked


SENTENCE_ENDS = "。！？!?；;" + chr(10)


class StreamGuard:
    """Incremental guard for streaming output.

    Text that already left the server cannot be recalled, so the guard holds
    back the tail until a sentence boundary is complete, checks that segment,
    and only then releases it.  hold_back is the unavoidable risk window.
    """

    def __init__(self, guard: Optional[Guard] = None, hold_back: int = 8):
        self.guard = guard or Guard()
        self.hold_back = max(0, hold_back)
        self.buffer = ""
        self.blocked = False
        self.findings: List[Finding] = []

    def feed(self, delta: str) -> GuardResult:
        if self.blocked:
            return GuardResult(action=BLOCK, text="", reason="already_blocked")

        self.buffer += delta or ""
        emitted: List[str] = []

        while True:
            cut = self._next_boundary()
            if cut < 0:
                break
            segment, self.buffer = self.buffer[: cut + 1], self.buffer[cut + 1 :]
            verdict = self.guard.check(segment, stage="output")
            self.findings.extend(verdict.findings)
            if verdict.blocked:
                self.blocked = True
                return GuardResult(
                    action=BLOCK,
                    text="".join(emitted) + REFUSAL,
                    findings=self.findings,
                    reason=verdict.reason,
                )
            emitted.append(verdict.text)

        action = REVIEW if self.findings else ALLOW
        return GuardResult(action=action, text="".join(emitted), findings=self.findings)

    def flush(self) -> GuardResult:
        if self.blocked:
            return GuardResult(action=BLOCK, text="", reason="already_blocked")
        if not self.buffer:
            return GuardResult(action=ALLOW, text="")
        verdict = self.guard.check(self.buffer, stage="output")
        self.findings.extend(verdict.findings)
        self.buffer = ""
        if verdict.blocked:
            self.blocked = True
            return GuardResult(
                action=BLOCK,
                text=REFUSAL,
                findings=self.findings,
                reason=verdict.reason,
            )
        return GuardResult(
            action=REVIEW if self.findings else ALLOW,
            text=verdict.text,
            findings=self.findings,
        )

    def _next_boundary(self) -> int:
        for index, char in enumerate(self.buffer):
            if char in SENTENCE_ENDS and index + 1 >= self.hold_back:
                return index
        return -1
