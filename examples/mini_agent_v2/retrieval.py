"""Chunking, hybrid retrieval and reciprocal rank fusion.

The three ideas this module makes concrete:

1. **ACL filtering happens before ranking.**  The candidate set is built from
   documents the caller can see, so an invisible chunk can never leak through
   a score, a count, or an error message.
2. **Hybrid beats vector-only.**  Exact identifiers (error codes, order ids)
   are what dense-ish retrieval is worst at; BM25 is what it is best at.
3. **RRF fuses by rank, not by score.**  Cosine similarity and BM25 scores are
   not comparable; adding them is a classic bug.

The numbers printed by the CLI are measured locally against the toy embedder in
``corpus.py`` — treat them as a demonstration of the *method*, not a benchmark.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .corpus import Chunk, Document, InMemoryCorpus, cosine, embed

_SENTENCE = re.compile(r"(?<=[。！？!?\.])\s*|\n+")


def chunk_document(
    document: Document,
    *,
    max_chars: int = 160,
    overlap: int = 24,
    max_chunks: int = 6,
) -> List[Chunk]:
    """Structure-first, then length, then put the section back.

    Order matters:

    1. **Structure first** — split on ``## heading`` lines, because a refund
       paragraph must not be glued to an incident paragraph;
    2. **then length** — pack sentences up to ``max_chars`` with a small
       overlap, so a chunk stays inside one section;
    3. **then context** — prepend ``title / section`` to every chunk and copy
       ACL/updated_at onto it, so the chunk is self-contained and filterable.

    ``max_chunks`` keeps the toy corpus small; production code would keep
    parent/child links instead of dropping chunks.
    """
    sections: List[tuple] = []
    current_heading = ""
    current_lines: List[str] = []
    for raw in document.text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            if current_lines:
                sections.append((current_heading, current_lines))
            current_heading, current_lines = line[3:].strip(), []
        elif line:
            current_lines.append(line)
    if current_lines:
        sections.append((current_heading, current_lines))

    pieces: List[tuple] = []
    for heading, lines in sections:
        for line in lines:
            for sentence in _SENTENCE.split(line):
                sentence = sentence.strip()
                if sentence:
                    pieces.append((heading, sentence))

    chunks: List[Chunk] = []
    buffer: List[str] = []
    buffer_heading = pieces[0][0] if pieces else ""
    ordinal = 0

    def flush() -> None:
        nonlocal ordinal, buffer, buffer_heading
        if buffer:
            chunks.append(_make_chunk(document, " ".join(buffer), buffer_heading, ordinal))
            ordinal += 1
            buffer = []

    for heading, sentence in pieces:
        if heading != buffer_heading:
            flush()
            buffer_heading = heading
        candidate = " ".join(buffer + [sentence])
        if buffer and len(candidate) > max_chars:
            flush()
        buffer.append(sentence)
    flush()
    return chunks[:max_chunks]


def _make_chunk(document: Document, text: str, section: str, ordinal: int) -> Chunk:
    """The chunk the model/searcher sees is ``section + text``.

    Note what is **not** in the embedded text: the document title.  Prepending
    it to every chunk of the same document gives all of them an identical
    prefix, which flattens the similarity scores into noise and makes ranking
    essentially arbitrary.  The title still reaches the reader through
    ``doc_id``/``section`` metadata, where it belongs.
    """
    body = f"{section}\n{text}" if section else text
    return Chunk(
        chunk_id=f"{document.doc_id}#{ordinal}",
        doc_id=document.doc_id,
        text=body,
        section=section,
        ordinal=ordinal,
        acl=set(document.acl),
        updated_at=document.updated_at,
        source=document.source or document.doc_id,
        vector=embed(body),
    )


@dataclass
class SearchHit:
    chunk: Chunk
    score: float
    ranks: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        data = self.chunk.to_citation()
        data["score"] = round(self.score, 5)
        data["ranks"] = dict(self.ranks)
        data["text"] = self.chunk.text
        return data


class HybridRetriever:
    """Vector + BM25 candidates, fused with RRF, filtered by ACL first."""

    def __init__(
        self,
        corpus: InMemoryCorpus,
        *,
        rrf_k: int = 60,
        weights: Optional[Dict[str, float]] = None,
    ):
        self.corpus = corpus
        self.rrf_k = rrf_k
        # Lexical evidence is worth more when the query contains an exact
        # identifier (error code, order id).  Pure RRF throws that signal away:
        # rank 1 and rank 2 count almost the same, even when BM25 scores are
        # 6.99 vs 1.78.  A weighted RRF keeps the "fuse by rank" safety (scores
        # from different scales are never added) while letting us say which
        # route we trust more.
        self.weights = {"vector": 1.0, "bm25": 2.5}
        if weights:
            self.weights.update(weights)
        self._chunks: List[Chunk] = []
        self._built = False

    # -- index -----------------------------------------------------------

    def build(self) -> int:
        self._chunks = []
        for document in self.corpus.documents.values():
            self._chunks.extend(chunk_document(document))
        self._built = True
        return len(self._chunks)

    @property
    def chunks(self) -> List[Chunk]:
        if not self._built:
            self.build()
        return self._chunks

    # -- search ----------------------------------------------------------

    def search(
        self,
        query: str,
        principals: Iterable[str],
        k: int = 5,
        candidate_k: int = 20,
    ) -> List[SearchHit]:
        principals = set(principals)
        # 1. Filter before ranking.  This is the contract, not an optimisation.
        visible = [chunk for chunk in self.chunks if chunk.acl & principals]

        vector_ranking = self._rank_vector(query, visible, candidate_k)
        lexical_ranking = self._rank_bm25(query, visible, candidate_k)

        # 2. Fuse by rank (RRF).  Scores from two different scales are never summed.
        fused: Dict[str, Tuple[float, Dict[str, int]]] = {}
        for name, ranking in (("vector", vector_ranking), ("bm25", lexical_ranking)):
            weight = self.weights.get(name, 1.0)
            for rank, (chunk, _score) in enumerate(ranking, start=1):
                current, ranks = fused.get(chunk.chunk_id, (0.0, {}))
                fused[chunk.chunk_id] = (
                    current + weight / (self.rrf_k + rank),
                    {**ranks, name: rank},
                )

        by_id = {chunk.chunk_id: chunk for chunk in visible}
        hits = [
            SearchHit(chunk=by_id[chunk_id], score=score, ranks=ranks)
            for chunk_id, (score, ranks) in fused.items()
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:k]

    def recall_at_k(self, hits: List[SearchHit], doc_ids: Set[str]) -> float:
        if not doc_ids:
            return 1.0
        found = {hit.chunk.doc_id for hit in hits}
        return len(found & doc_ids) / len(doc_ids)

    # -- rankings --------------------------------------------------------

    @staticmethod
    def _rank_vector(
        query: str, chunks: Sequence[Chunk], limit: int
    ) -> List[Tuple[Chunk, float]]:
        query_vector = embed(query)
        scored = [(chunk, cosine(query_vector, chunk.vector)) for chunk in chunks]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

    @staticmethod
    def _rank_bm25(
        query: str, chunks: Sequence[Chunk], limit: int, k1: float = 1.5, b: float = 0.75
    ) -> List[Tuple[Chunk, float]]:
        query_terms = _bm25_terms(query)
        if not query_terms or not chunks:
            return []
        doc_terms = [_bm25_terms(chunk.text) for chunk in chunks]
        avg_len = sum(len(terms) for terms in doc_terms) / max(1, len(doc_terms))
        df: Dict[str, int] = {}
        for terms in doc_terms:
            for term in set(terms):
                df[term] = df.get(term, 0) + 1
        total = len(doc_terms)
        scored: List[Tuple[Chunk, float]] = []
        for chunk, terms in zip(chunks, doc_terms):
            counts: Dict[str, int] = {}
            for term in terms:
                counts[term] = counts.get(term, 0) + 1
            score = 0.0
            for term in query_terms:
                if term not in counts:
                    continue
                idf = math.log(1 + (total - df[term] + 0.5) / (df[term] + 0.5))
                tf = counts[term]
                norm = tf + k1 * (1 - b + b * len(terms) / max(1e-9, avg_len))
                score += idf * (tf * (k1 + 1)) / norm
            if score > 0:
                scored.append((chunk, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]


def _bm25_terms(text: str) -> List[str]:
    from .corpus import _tokens

    return _tokens(text)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class RetrievalCase:
    """One retrieval eval sample.  ``expected_docs`` are the only ground truth."""

    case_id: str
    query: str
    expected_docs: Set[str]
    principals: Set[str] = field(default_factory=lambda: {"public"})
    forbid_docs: Set[str] = field(default_factory=set)


@dataclass
class RetrievalReport:
    samples: int
    recall_at_5: float
    recall_at_20: float
    forbidden_hits: int

    @property
    def passed(self) -> bool:
        return self.forbidden_hits == 0

    def to_dict(self) -> Dict[str, object]:
        return {
            "samples": self.samples,
            "recall@5": round(self.recall_at_5, 3),
            "recall@20": round(self.recall_at_20, 3),
            "forbidden_hits": self.forbidden_hits,
            "passed": self.passed,
        }


def evaluate_retrieval(
    retriever: HybridRetriever, cases: Iterable[RetrievalCase]
) -> RetrievalReport:
    """Recall@5 / Recall@20 plus a hard zero-tolerance ACL leak counter.

    The ``forbidden_hits`` counter is the one that must never be non-zero:
    it means a chunk the caller may not see made it into the results.
    """
    total_r5 = total_r20 = 0.0
    leaks = 0
    count = 0
    for case in cases:
        hits = retriever.search(case.query, case.principals, k=20)
        top5 = hits[:5]
        total_r5 += retriever.recall_at_k(top5, case.expected_docs)
        total_r20 += retriever.recall_at_k(hits, case.expected_docs)
        for hit in hits:
            if hit.chunk.doc_id in case.forbid_docs:
                leaks += 1
        count += 1
    count = max(1, count)
    return RetrievalReport(
        samples=count,
        recall_at_5=total_r5 / count,
        recall_at_20=total_r20 / count,
        forbidden_hits=leaks,
    )
