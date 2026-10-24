"""A tiny long-term memory with keyword retrieval and explicit provenance.

This is intentionally simple.  The tutorial uses it to make the *contract*
visible: memory items need a source, confidence, importance and an expiry.
Production systems usually add embeddings, a database, consolidation jobs and
tenant isolation.
"""
from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


_WORD = re.compile(r"[a-zA-Z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokens(text: str) -> set:
    lowered = text.lower()
    tokens = set(_WORD.findall(lowered))
    cjk = "".join(_CJK.findall(lowered))
    # Character bigrams give a cheap lexical signal for Chinese text.
    tokens.update(cjk[i : i + 2] for i in range(len(cjk) - 1))
    return tokens


@dataclass
class MemoryItem:
    content: str
    kind: str = "fact"  # fact | preference | episode | procedure
    source: str = "unknown"
    confidence: float = 0.7
    importance: float = 0.5
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    tenant_id: str = "default"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def expired(self, now: Optional[float] = None) -> bool:
        return self.expires_at is not None and self.expires_at <= (now or time.time())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SimpleMemory:
    """In-memory store with optional JSON persistence."""

    def __init__(self, path: Optional[str] = None, tenant_id: str = "default"):
        self.path = Path(path) if path else None
        self.tenant_id = tenant_id
        self.items: List[MemoryItem] = []
        if self.path and self.path.exists():
            self.load()

    def write(self, item: MemoryItem) -> bool:
        if item.tenant_id != self.tenant_id:
            raise ValueError("cross-tenant write rejected")
        if not item.source or item.source == "unknown":
            raise ValueError("memory requires a source")
        if item.expired():
            return False
        # Exact duplicate: keep the newer provenance, do not grow forever.
        for existing in self.items:
            if existing.kind == item.kind and existing.content == item.content:
                existing.importance = max(existing.importance, item.importance)
                existing.confidence = max(existing.confidence, item.confidence)
                return False
        self.items.append(item)
        self.save()
        return True

    def remember(
        self,
        content: str,
        kind: str = "fact",
        source: str = "user",
        confidence: float = 0.8,
        importance: float = 0.6,
        tags: Optional[List[str]] = None,
        ttl_s: Optional[float] = None,
    ) -> MemoryItem:
        item = MemoryItem(
            content=content,
            kind=kind,
            source=source,
            confidence=confidence,
            importance=importance,
            tags=tags or [],
            tenant_id=self.tenant_id,
            expires_at=(time.time() + ttl_s) if ttl_s else None,
        )
        self.write(item)
        return item

    def retrieve(self, query: str, k: int = 4) -> List[MemoryItem]:
        query_tokens = _tokens(query)
        now = time.time()
        scored = []
        for item in self.items:
            if item.tenant_id != self.tenant_id or item.expired(now):
                continue
            item_tokens = _tokens(item.content)
            if not item_tokens:
                continue
            overlap = len(query_tokens & item_tokens) / math.sqrt(
                len(query_tokens) + 1
            )
            age_days = max(0.0, (now - item.created_at) / 86400)
            recency = 1.0 / (1.0 + 0.1 * age_days)
            score = (
                0.65 * overlap
                + 0.20 * item.importance
                + 0.15 * recency
            ) * item.confidence
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:k]]

    def delete(self, item_id: str) -> bool:
        before = len(self.items)
        self.items = [item for item in self.items if item.id != item_id]
        self.save()
        return len(self.items) != before

    def clear(self) -> None:
        self.items = []
        self.save()

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [item.to_dict() for item in self.items]
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load(self) -> None:
        assert self.path is not None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.items = [MemoryItem(**row) for row in raw]

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterable[MemoryItem]:
        return iter(self.items)
