"""Documents, chunks and a deterministic "embedding".

Why a hand-written embedding instead of a real model?

The tutorial has a hard constraint: **no third-party runtime dependencies**, so
the examples run anywhere.  That forces us to be honest about what the vector
part actually does: in this toy it is a hashed bag-of-words.  Keep that in mind
when reading the numbers — the *contracts* (ACL filtering at recall time,
metadata on every chunk, filtering before ranking) are what you are meant to
learn here, not the retrieval quality.

A production system swaps ``embed()`` for a real embedding API or local model.
Everything else in ``retrieval.py`` stays the same, because it only depends on
"a function that maps text to a unit vector".
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

_WORD = re.compile(r"[a-zA-Z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")

DIM = 256


def _tokens(text: str) -> List[str]:
    """Lowercased words plus CJK character bigrams.

    Chinese has no spaces; character bigrams are the cheapest lexical signal
    that still works without a tokenizer dependency.
    """
    lowered = text.lower()
    words = _WORD.findall(lowered)
    cjk = "".join(_CJK.findall(lowered))
    grams = [cjk[i : i + 2] for i in range(max(0, len(cjk) - 1))]
    return words + grams


def embed(text: str, dim: int = DIM) -> List[float]:
    """Hashed bag-of-words embedding, L2-normalised.

    Deterministic: the same text always maps to the same vector, in any process.
    Two texts that share tokens land closer together.  That is all.
    """
    vector = [0.0] * dim
    tokens = _tokens(text)
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


@dataclass
class Document:
    """A source document.

    ``acl`` is the whole point: the retrieval layer filters on it *before*
    ranking, because a chunk that the caller may not see must never enter the
    candidate set — not even to be ranked and then dropped.
    """

    doc_id: str
    title: str
    text: str
    acl: Set[str] = field(default_factory=lambda: {"public"})
    updated_at: float = 0.0
    source: str = ""
    version: str = "1"

    def visible_to(self, principals: Iterable[str]) -> bool:
        return bool(self.acl & set(principals))


@dataclass
class Chunk:
    """A retrievable unit.  Metadata travels with the text, always."""

    chunk_id: str
    doc_id: str
    text: str
    section: str
    ordinal: int
    acl: Set[str]
    updated_at: float
    source: str = ""
    vector: List[float] = field(default_factory=list, repr=False)

    def to_citation(self) -> Dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "section": self.section,
            "source": self.source,
            "updated_at": self.updated_at,
        }


class InMemoryCorpus:
    """A tiny document store with ACL-aware listing."""

    def __init__(self, documents: Optional[List[Document]] = None):
        self.documents: Dict[str, Document] = {}
        for document in documents or []:
            self.add(document)

    def add(self, document: Document) -> None:
        self.documents[document.doc_id] = document

    def remove(self, doc_id: str) -> bool:
        return self.documents.pop(doc_id, None) is not None

    def visible_documents(self, principals: Iterable[str]) -> List[Document]:
        principals = set(principals)
        return [
            document
            for document in self.documents.values()
            if document.acl & principals
        ]

    def __len__(self) -> int:
        return len(self.documents)
