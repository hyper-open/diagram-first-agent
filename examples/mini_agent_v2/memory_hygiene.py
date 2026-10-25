"""Memory with provenance, conflict resolution and redaction on write.

v1 (``memory.SimpleMemory``) shows the *contract* of a memory item.  This module
answers the three questions that decide whether a memory system is safe:

1. **Who said it?**  Every item carries ``source`` and ``confidence``; a model
   inference can never be stored as a user fact.
2. **What happens when it contradicts something?**  New evidence supersedes old
   (``supersedes``), old items are kept for audit but excluded from retrieval.
   Nothing is silently overwritten — you must be able to answer "why is it B
   now?".
3. **What must never be written?**  Secrets are refused; PII is redacted before
   it reaches storage.  A memory system is a second copy of your data, so it
   needs the same (or stricter) rules as your logs.

Tenant isolation is enforced structurally: a store only ever sees its tenant's
items, and cross-tenant writes raise.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..mini_agent.memory import _tokens

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S+"),
    re.compile(r"(?i)password\s*[:=]\s*\S+"),
]
_PII_PATTERNS = {
    "phone": re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)"),
    "id_card": re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)"),
    "email": re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"),
}
_MASKS = {"phone": "[手机号]", "id_card": "[证件号]", "email": "[邮箱]"}


@dataclass
class WriteDecision:
    """The result of attempting a memory write.  Never raise for policy."""

    accepted: bool
    reason: str = ""
    item: Optional["MemoryRecord"] = None
    redactions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "redactions": list(self.redactions),
            "item_id": self.item.id if self.item else None,
        }


@dataclass
class MemoryRecord:
    content: str
    kind: str = "fact"  # fact | preference | episode | procedure
    source: str = "user"
    confidence: float = 0.8
    importance: float = 0.6
    tenant_id: str = "default"
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def expired(self, now: Optional[float] = None) -> bool:
        return self.expires_at is not None and self.expires_at <= (now or time.time())

    @property
    def active(self) -> bool:
        return self.superseded_by is None

    def explain(self) -> str:
        state = "生效" if self.active else "已被 %s 取代" % self.superseded_by
        return "[%s] %s（来源: %s，置信度 %.2f，%s）" % (
            self.kind,
            self.content,
            self.source,
            self.confidence,
            state,
        )


def scan_text(text: str) -> Dict[str, List[str]]:
    """Return detected secrets and PII categories.  Used by write and by tests."""
    findings: Dict[str, List[str]] = {}
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            findings.setdefault("secret", []).append(pattern.pattern)
    for name, pattern in _PII_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            findings.setdefault(name, []).extend(
                m if isinstance(m, str) else m[0] for m in matches
            )
    return findings


def redact(text: str) -> tuple:
    """Mask PII, return (redacted_text, categories_redacted)."""
    redactions: List[str] = []
    for name, pattern in _PII_PATTERNS.items():
        if pattern.search(text):
            text = pattern.sub(_MASKS[name], text)
            redactions.append(name)
    return text, redactions


class MemoryStore:
    """Tenant-scoped memory with an explicit write policy."""

    def __init__(self, tenant_id: str = "default"):
        self.tenant_id = tenant_id
        self.records: List[MemoryRecord] = []

    # -- write -----------------------------------------------------------

    def write(
        self,
        content: str,
        *,
        kind: str = "fact",
        source: str = "user",
        confidence: float = 0.8,
        importance: float = 0.6,
        tags: Optional[Sequence[str]] = None,
        tenant_id: Optional[str] = None,
        allow_inferred: bool = False,
    ) -> WriteDecision:
        tenant_id = tenant_id or self.tenant_id
        if tenant_id != self.tenant_id:
            raise ValueError("cross-tenant write rejected")

        content = (content or "").strip()
        if not content:
            return WriteDecision(False, "empty")
        if source in ("model_inferred", "inferred") and not allow_inferred:
            # A guess is not a fact.  This is the single most effective guard
            # against memory poisoning.
            return WriteDecision(False, "inferred_content_requires_confirmation")
        if source == "unknown":
            return WriteDecision(False, "missing_source")

        findings = scan_text(content)
        if "secret" in findings:
            return WriteDecision(False, "secret_refused")

        clean, redactions = redact(content)

        existing = self._find_conflict(clean, kind)
        record = MemoryRecord(
            content=clean,
            kind=kind,
            source=source,
            confidence=confidence,
            importance=importance,
            tenant_id=tenant_id,
            tags=list(tags or []),
        )
        if existing is not None:
            if existing.content == clean:
                existing.confidence = max(existing.confidence, confidence)
                return WriteDecision(
                    False, "duplicate", item=existing, redactions=redactions
                )
            existing.superseded_by = record.id
            record.supersedes = existing.id

        self.records.append(record)
        return WriteDecision(True, "added" if not record.supersedes else "superseded", item=record, redactions=redactions)

    def _find_conflict(self, content: str, kind: str) -> Optional[MemoryRecord]:
        """A conflict is the same *subject* with a different *value*.

        We approximate the subject with token overlap on a fixed key set
        (``key:`` prefix or the first few tokens).  Production systems use an
        explicit slot/triple instead of guessing.
        """
        subject = self._subject(content)
        for record in self.history(kind=kind):
            if record.active and self._subject(record.content) == subject:
                return record
        return None

    @staticmethod
    def _subject(content: str) -> str:
        match = re.search(r"([\w\u4e00-\u9fff]+)\s*[:：]\s*(.+)", content)
        if match:
            return match.group(1)
        # Fall back to the first two content tokens, which is crude but stable.
        tokens = [t for t in _tokens(content) if len(t) > 1]
        return "|".join(sorted(tokens[:2]))

    # -- read ------------------------------------------------------------

    def retrieve(self, query: str, k: int = 4, now: Optional[float] = None) -> List[MemoryRecord]:
        now = now or time.time()
        query_tokens = _tokens(query)
        scored = []
        for record in self.records:
            if record.tenant_id != self.tenant_id or record.expired(now):
                continue
            if not record.active:
                continue  # superseded items are auditable, not retrievable
            overlap = len(query_tokens & _tokens(record.content)) / (
                len(query_tokens) + 1
            ) ** 0.5
            age_days = max(0.0, (now - record.created_at) / 86400)
            recency = 1.0 / (1.0 + 0.1 * age_days)
            score = (0.65 * overlap + 0.20 * record.importance + 0.15 * recency) * record.confidence
            if score > 0:
                scored.append((score, record))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [record for _, record in scored[:k]]

    def history(self, kind: Optional[str] = None) -> List[MemoryRecord]:
        """All records including superseded ones — the audit view."""
        return [r for r in self.records if kind is None or r.kind == kind]

    def explain(self, subject_query: str) -> str:
        """Answer 'why is it B now?' from the supersede chain."""
        lines = []
        relevant = self.history()
        for record in relevant:
            if subject_query in record.content or not subject_query:
                lines.append("  " + record.explain())
        return "\n".join(lines) if lines else "  （无相关记忆）"

    # -- delete ----------------------------------------------------------

    def delete(self, record_id: str) -> bool:
        before = len(self.records)
        self.records = [r for r in self.records if r.id != record_id]
        return len(self.records) != before

    def purge_subject(self, subject: str) -> int:
        """Delete every record about a subject — the 'right to erasure' path."""
        keep = [r for r in self.records if self._subject(r.content) != subject]
        removed = len(self.records) - len(keep)
        self.records = keep
        return removed

    def __len__(self) -> int:
        return len([r for r in self.records if r.active])
