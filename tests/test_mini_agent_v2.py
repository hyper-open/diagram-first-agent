"""Tests for Mini Agent v2 (RAG + intent routing + memory hygiene).

They follow the same principle as the v1 suite: assert **mechanisms**, not model
quality.  The interesting tests are the ones that would fail if someone
"optimised" away a contract — ACL filtering, citation verification, supersede
chains, secret refusal.
"""
from __future__ import annotations

import unittest

from examples.mini_agent_v2.corpus import Document, InMemoryCorpus, embed, cosine
from examples.mini_agent_v2.fixtures import seed_cases, seed_documents
from examples.mini_agent_v2.memory_hygiene import MemoryStore, redact, scan_text
from examples.mini_agent_v2.pipeline import PipelineRequest, RequestPipeline
from examples.mini_agent_v2.retrieval import (
    HybridRetriever,
    chunk_document,
    evaluate_retrieval,
)
from examples.mini_agent_v2.router import (
    AGENT,
    CLARIFY,
    FAQ,
    HUMAN,
    RAG,
    WORKFLOW,
    IntentRouter,
    default_intents,
)


def build() -> tuple:
    corpus = InMemoryCorpus(seed_documents())
    retriever = HybridRetriever(corpus)
    retriever.build()
    router = IntentRouter(default_intents())
    memory = MemoryStore(tenant_id="acme")
    pipeline = RequestPipeline(router=router, retriever=retriever, memory=memory)
    return corpus, retriever, router, memory, pipeline


# ---------------------------------------------------------------------------
# Chunking & retrieval
# ---------------------------------------------------------------------------


class ChunkingTests(unittest.TestCase):
    def test_chunks_never_cross_sections(self):
        document = seed_documents()[0]
        chunks = chunk_document(document)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            # The first line is the section heading; the body must belong to it.
            self.assertTrue(chunk.text.startswith(chunk.section))
            self.assertNotIn("##", chunk.text)

    def test_chunk_carries_acl_and_metadata(self):
        document = seed_documents()[1]  # policy-vip, acl={vip, staff}
        for chunk in chunk_document(document):
            self.assertEqual(chunk.acl, {"vip", "staff"})
            self.assertEqual(chunk.doc_id, "policy-vip")
            self.assertIn("source", chunk.to_citation())

    def test_document_title_is_not_embedded_into_every_chunk(self):
        # A shared prefix flattens similarity scores into noise; the title must
        # travel as metadata instead.
        document = seed_documents()[4]
        for chunk in chunk_document(document):
            self.assertNotIn(document.title, chunk.text)


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        _, self.retriever, _, _, _ = build()

    def test_acl_filtering_happens_before_ranking(self):
        hits = self.retriever.search("VIP 会员退款上限", {"public"}, k=20)
        docs = {hit.chunk.doc_id for hit in hits}
        self.assertNotIn("policy-vip", docs)
        self.assertNotIn("internal-ops", docs)

    def test_privileged_principal_sees_restricted_docs(self):
        hits = self.retriever.search("VIP 会员退款上限", {"staff"}, k=20)
        self.assertIn("policy-vip", {hit.chunk.doc_id for hit in hits})

    def test_exact_identifier_prefers_bm25_route(self):
        hits = self.retriever.search("GW-4021 是什么错误", {"public"}, k=3)
        self.assertEqual(hits[0].chunk.doc_id, "manual-gateway")
        self.assertEqual(hits[0].chunk.section, "错误码")

    def test_hybrid_beats_vector_only_on_identifiers(self):
        visible = self.retriever.chunks
        vector_top = HybridRetriever._rank_vector("GW-4021 是什么错误", visible, 1)[0][0]
        hybrid_top = self.retriever.search("GW-4021 是什么错误", {"public"}, k=1)[0].chunk
        self.assertNotEqual(
            hybrid_top.chunk_id,
            vector_top.chunk_id,
            "if this fails, the fixture no longer demonstrates hybrid retrieval",
        )

    def test_rrf_is_rank_based_not_score_based(self):
        hit = self.retriever.search("退款多久到账", {"public"}, k=3)[0]
        self.assertIn("vector", hit.ranks)
        self.assertIn("bm25", hit.ranks)
        # RRF score must equal sum(weight/(k+rank)).
        expected = sum(
            self.retriever.weights[name] / (self.retriever.rrf_k + rank)
            for name, rank in hit.ranks.items()
        )
        self.assertAlmostEqual(hit.score, expected, places=6)

    def test_eval_reports_zero_acl_leaks(self):
        report = evaluate_retrieval(self.retriever, seed_cases())
        self.assertEqual(report.forbidden_hits, 0)
        self.assertTrue(report.passed)
        self.assertGreaterEqual(report.recall_at_20, 0.95)

    def test_embedding_is_deterministic_and_normalised(self):
        a = embed("退款政策")
        b = embed("退款政策")
        self.assertEqual(a, b)
        self.assertAlmostEqual(sum(x * x for x in a) ** 0.5, 1.0, places=6)
        self.assertGreater(cosine(embed("退款政策"), embed("退款政策是什么")), 0.0)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.router = IntentRouter(default_intents())

    def test_routes_are_dispatched_correctly(self):
        cases = {
            "退款多久到账": FAQ,
            "订单 A1024 到哪了": WORKFLOW,
            "解释一下退款时效条款": RAG,
            "GW-4021 是什么错误": RAG,
            "帮我把退款整理成表格": AGENT,
            "我要投诉你们": HUMAN,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.router.route(text).route, expected)

    def test_low_confidence_clarifies_instead_of_guessing(self):
        route = self.router.route("你好")
        self.assertEqual(route.route, CLARIFY)
        self.assertTrue(route.needs_clarification)

    def test_confidence_is_bounded(self):
        for text in ["退款多久到账", "我要投诉你们", "违约金比例是多少"]:
            with self.subTest(text=text):
                route = self.router.route(text)
                self.assertLessEqual(route.confidence, 0.95)

    def test_every_route_has_a_reason(self):
        for text in ["嗯嗯那个东西", "订单 A1024", "我要投诉"]:
            with self.subTest(text=text):
                self.assertTrue(self.router.route(text).reason)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class PipelineTests(unittest.TestCase):
    def setUp(self):
        *_, self.pipeline = build()

    def _handle(self, text, principals=None):
        return self.pipeline.handle(
            PipelineRequest(text=text, principals=principals or {"public"})
        )

    def test_faq_path_does_not_call_a_model(self):
        result = self._handle("退款多久到账")
        self.assertEqual(result.route.route, FAQ)
        self.assertEqual(result.status, "success")
        self.assertTrue(result.citations)

    def test_workflow_binds_order_to_the_calling_user(self):
        result = self._handle("订单 A1024 到哪了")
        self.assertIn("A1024", result.output)
        self.assertIn("user_88", result.output)

    def test_workflow_without_order_id_asks_a_question(self):
        result = self._handle("我想查一下订单")
        self.assertEqual(result.status, "clarify")

    def test_rag_answer_carries_verified_citations(self):
        result = self._handle("解释一下退款时效条款")
        self.assertEqual(result.status, "success")
        self.assertTrue(result.output.rstrip().endswith("[c1]"))
        self.assertEqual(len(result.citations), 1)
        self.assertEqual(result.citations[0].doc_id, "policy-refund")

    def test_ungrounded_answer_is_downgraded_not_returned(self):
        # A model that cites a ref it was never given must not be trusted.
        self.pipeline.llm_factory = _BadLLM
        result = self._handle("解释一下退款时效条款")
        self.assertEqual(result.status, "ungrounded")
        self.assertEqual(result.handoff["reason"], "ungrounded_answer")

    def test_public_user_cannot_reach_vip_documents_via_rag(self):
        result = self._handle("解释一下 VIP 会员退款上限条款")
        for citation in result.citations:
            self.assertNotEqual(citation.doc_id, "policy-vip")
        self.assertNotIn("20000", result.output)

    def test_complaint_becomes_handoff_with_context(self):
        result = self._handle("我要投诉你们")
        self.assertEqual(result.status, "handoff")
        self.assertEqual(result.handoff["request"], "我要投诉你们")

    def test_every_result_has_a_route_trace(self):
        result = self._handle("退款多久到账")
        self.assertEqual(result.trace[0]["kind"], "route")
        self.assertIn("confidence", result.trace[0])

    def test_result_is_json_serialisable(self):
        import json

        result = self._handle("解释一下退款时效条款")
        json.dumps(result.to_dict(), ensure_ascii=False)


class _BadLLM:
    """A model that invents a citation reference."""

    def decide(self, messages, tools):  # noqa: ARG002
        from examples.mini_agent.types import Decision

        return Decision(content="VIP 可以退 20000 元 [c99]", final=True)


# ---------------------------------------------------------------------------
# Memory hygiene
# ---------------------------------------------------------------------------


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.memory = MemoryStore(tenant_id="acme")

    def test_inferred_content_is_not_stored_as_fact(self):
        decision = self.memory.write(
            "用户的手机号是 13800138000", source="model_inferred"
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "inferred_content_requires_confirmation")
        self.assertEqual(len(self.memory), 0)

    def test_secret_is_refused(self):
        decision = self.memory.write("api_key: sk-abcdef1234567890", source="user")
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "secret_refused")

    def test_pii_is_redacted_before_storage(self):
        decision = self.memory.write("电话 13800138000", source="user")
        self.assertTrue(decision.accepted)
        self.assertIn("phone", decision.redactions)
        self.assertNotIn("13800138000", decision.item.content)
        self.assertIn("[手机号]", decision.item.content)

    def test_conflict_supersedes_but_keeps_history(self):
        self.memory.write("收货地址：北京朝阳", source="user")
        self.memory.write("收货地址：上海浦东", source="user")
        active = self.memory.retrieve("收货地址", k=5)
        self.assertEqual(len(active), 1)
        self.assertIn("上海", active[0].content)
        history = self.memory.records
        self.assertEqual(len(history), 2)
        self.assertIsNotNone(history[0].superseded_by)
        self.assertEqual(history[1].supersedes, history[0].id)

    def test_explain_answers_why_it_changed(self):
        self.memory.write("收货地址：北京朝阳", source="user")
        self.memory.write("收货地址：上海浦东", source="user")
        explanation = self.memory.explain("收货地址")
        self.assertIn("北京", explanation)
        self.assertIn("上海", explanation)

    def test_cross_tenant_write_is_rejected(self):
        with self.assertRaises(ValueError):
            self.memory.write("地址：深圳", tenant_id="other")

    def test_purge_subject_delivers_erasure(self):
        self.memory.write("收货地址：北京朝阳", source="user")
        self.memory.write("收货地址：上海浦东", source="user")
        removed = self.memory.purge_subject("收货地址")
        self.assertEqual(removed, 2)
        self.assertEqual(len(self.memory.records), 0)

    def test_scan_text_finds_secrets_and_pii(self):
        findings = scan_text("sk-abcdef1234567890 13800138000 a@b.com")
        self.assertIn("secret", findings)
        self.assertIn("phone", findings)
        self.assertIn("email", findings)

    def test_redact_masks_all_known_categories(self):
        text, categories = redact("13800138000 / a@b.com")
        self.assertIn("phone", categories)
        self.assertIn("email", categories)
        self.assertNotIn("13800138000", text)


if __name__ == "__main__":
    unittest.main()
