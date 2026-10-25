"""Mini Agent v2: v1 + RAG + intent routing + memory hygiene.

Everything here builds on ``examples.mini_agent``; nothing in v1 is forked.
The new pieces are:

  * ``corpus``   — documents with ACL metadata, a deterministic embedder
  * ``retrieval``— chunking, hybrid (vector + BM25) search, RRF, ACL filter
  * ``router``   — intent classification with confidence and fallback
  * ``memory_hygiene`` — conflict resolution, provenance, redaction on write
  * ``pipeline`` — the request pipeline: route -> handler -> trace

See ``docs/practice/mini-agent-v2.md`` and ``examples/mini_agent_v2/cli.py``.
"""
from .corpus import Chunk, Document, InMemoryCorpus, embed
from .memory_hygiene import MemoryStore, WriteDecision
from .pipeline import PipelineRequest, PipelineResult, RequestPipeline
from .retrieval import HybridRetriever, SearchHit, chunk_document
from .router import Intent, IntentRouter, Route

__all__ = [
    "Chunk",
    "Document",
    "InMemoryCorpus",
    "embed",
    "MemoryStore",
    "WriteDecision",
    "PipelineRequest",
    "PipelineResult",
    "RequestPipeline",
    "HybridRetriever",
    "SearchHit",
    "chunk_document",
    "Intent",
    "IntentRouter",
    "Route",
]
