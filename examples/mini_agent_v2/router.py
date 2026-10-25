"""Intent routing: not every request deserves an Agent.

The router answers exactly one question — *which handler should own this
request* — and it is deliberately cheap and explainable:

* **rules first** (regex/keyword), because they are free, deterministic and
  testable;
* **semantic fallback** to the nearest labelled example;
* **confidence decides the path**: high -> route, medium -> clarify, low ->
  human/fallback.

The most important property is not accuracy, it is that every route has a
defined low-confidence behaviour.  A router without a fallback is a single
point of failure in front of your entire product.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .corpus import cosine, embed


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

FAQ = "faq"
WORKFLOW = "workflow"
RAG = "rag"
AGENT = "agent"
HUMAN = "human"
CLARIFY = "clarify"  # internal pseudo-route, never a business destination

ALL_ROUTES = (FAQ, WORKFLOW, RAG, AGENT, HUMAN)


@dataclass
class Intent:
    name: str
    route: str
    description: str
    examples: List[str] = field(default_factory=list)
    patterns: List[str] = field(default_factory=list)
    vectors: List[List[float]] = field(default_factory=list, repr=False)

    def compile(self) -> None:
        self.vectors = [embed(example) for example in self.examples]


@dataclass
class Route:
    route: str
    confidence: float
    intent: str = ""
    reason: str = ""

    @property
    def needs_clarification(self) -> bool:
        return self.route == CLARIFY

    def to_dict(self) -> Dict[str, object]:
        return {
            "route": self.route,
            "confidence": round(self.confidence, 3),
            "intent": self.intent,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class IntentRouter:
    """Rules + exemplar similarity, with an explicit confidence ladder."""

    def __init__(
        self,
        intents: Sequence[Intent],
        *,
        high: float = 0.62,
        low: float = 0.34,
        fallback: str = HUMAN,
    ):
        self.intents = list(intents)
        for intent in self.intents:
            intent.compile()
        self.high = high
        self.low = low
        self.fallback = fallback
        self._patterns = [
            (intent, [re.compile(p, re.IGNORECASE) for p in intent.patterns])
            for intent in self.intents
        ]

    # -- public ----------------------------------------------------------

    def route(self, request: str) -> Route:
        text = request.strip()
        if not text:
            return Route(CLARIFY, 0.0, reason="空请求")

        rule_route = self._match_rules(text)
        intent, similarity = self._match_exemplars(text)
        semantic_route = intent.route if intent else self.fallback

        if rule_route is not None and rule_route.route == semantic_route:
            # Agreement between two independent signals is evidence, but the
            # number must still mean something: it is the rule floor blended
            # with the semantic score, never a hard-coded 1.0.  A confidence
            # that cannot move cannot be used for thresholds or monitoring.
            confidence = min(0.95, 0.55 * 0.9 + 0.45 * similarity)
            return Route(
                semantic_route,
                confidence,
                intent.name if intent else "",
                "规则与语义一致：" + rule_route.reason,
            )

        if rule_route is not None and rule_route.route != HUMAN:
            route = rule_route.route
            # Rule wins over a disagreed semantic label, but the score stays
            # bounded: two sources disagreeing is *less* certain, not more.
            confidence = min(0.9, max(rule_route.confidence, self.high))
            return Route(
                route,
                confidence,
                intent.name if intent else "",
                "命中规则（语义判为 %s）：%s" % (semantic_route, rule_route.reason),
            )

        if semantic_route == HUMAN and similarity < self.low:
            return Route(
                HUMAN, similarity, intent.name if intent else "", "语义置信度过低"
            )

        if similarity >= self.high:
            # The hashed toy embedding can reach exactly 1.0 on a near-duplicate
            # example; cap it so "certainty" stays comparable with rule matches.
            return Route(
                semantic_route,
                min(0.95, similarity),
                intent.name if intent else "",
                "语义相似度高于阈值 %.2f" % self.high,
            )
        if similarity >= self.low:
            return Route(
                CLARIFY,
                similarity,
                intent.name if intent else "",
                "置信度处于澄清区间 [%.2f, %.2f)" % (self.low, self.high),
            )
        return Route(
            self.fallback,
            similarity,
            intent.name if intent else "",
            "置信度低于兜底阈值 %.2f" % self.low,
        )

    # -- internals -------------------------------------------------------

    def _match_rules(self, text: str) -> Optional[Route]:
        for intent, patterns in self._patterns:
            for pattern in patterns:
                if pattern.search(text):
                    return Route(intent.route, 0.9, intent.name, "matched %r" % pattern.pattern)
        return None

    def _match_exemplars(self, text: str) -> Tuple[Optional[Intent], float]:
        query = embed(text)
        best: Optional[Intent] = None
        best_score = 0.0
        for intent in self.intents:
            for vector in intent.vectors:
                score = cosine(query, vector)
                if score > best_score:
                    best_score, best = score, intent
        return best, best_score


# ---------------------------------------------------------------------------
# Default intent table for a support-style product
# ---------------------------------------------------------------------------


def default_intents() -> List[Intent]:
    return [
        Intent(
            name="faq.policy",
            route=FAQ,
            description="政策、规则、常见问题：有标准答案，不需要查业务数据",
            examples=[
                "退货政策是什么",
                "超过七天还能退吗",
                "退款多久到账",
                "多少钱包邮",
                "运费怎么算",
                "发票怎么开",
                "支持哪些付款方式",
                "偏远地区多久能送到",
            ],
            patterns=[r"政策|规则|怎么开|支持哪些|多久|多少钱|运费|包邮|发票"],
        ),
        Intent(
            name="query.order_status",
            route=WORKFLOW,
            description="查订单、查物流、改地址：步骤固定，参数明确",
            examples=[
                "我的订单到哪了",
                "帮我查一下物流",
                "订单 A1024 什么时候发货",
                "我想改收货地址",
            ],
            patterns=[r"[A-Z]\d{3,}|订单号|物流|快递|查.*单"],
        ),
        Intent(
            name="doc.explain",
            route=RAG,
            description="解释文档、手册、条款：需要检索证据并给出引用",
            examples=[
                "解释一下退款条款第三条",
                "手册里关于超时重试是怎么说的",
                "帮我看看这份合同里的违约金规定",
                "违约金比例是多少",
                "合同怎么约定争议解决",
                "GW-4021 是什么错误",
                "错误码 GW-4022 是什么意思",
            ],
            patterns=[r"条款|手册|文档|解释一下|合同|约定|错误码|是什么错误|GW-?\d+"],
        ),
        Intent(
            name="task.multi_step",
            route=AGENT,
            description="需要多步操作或工具组合的复杂任务",
            examples=[
                "帮我把这个月的退款都整理成表格并汇总金额",
                "对比一下这两个订单的差异然后发起退款",
                "分析一下最近一周的投诉都集中在哪些环节",
            ],
            patterns=[r"整理成|汇总|分析一下|对比一下|一步一步"],
        ),
        Intent(
            name="risk.complaint",
            route=HUMAN,
            description="投诉、法律威胁、情绪激烈：必须有人接手",
            examples=[
                "我要投诉你们",
                "再不解决我就起诉",
                "已经联系媒体了",
                "你们这是欺诈",
            ],
            patterns=[r"投诉|起诉|律师|监管|媒体|欺诈|退款都退不了"],
        ),
    ]
