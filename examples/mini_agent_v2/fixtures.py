"""Seed corpus and eval cases for Mini Agent v2.

The corpus is deliberately small and has an **ACL leak trap** built in:

* ``policy-refund`` — public, every tenant can read it;
* ``policy-vip``    — only the ``vip`` principal may read it;
* ``internal-ops``  — only ``staff`` may read it;
* ``manual-gateway``— public, contains exact identifiers (error codes) that
  dense retrieval is bad at and BM25 is good at.

Any retrieval result that contains ``policy-vip`` or ``internal-ops`` for a
non-privileged principal is a leak, and the eval counts it as a hard failure.
"""
from __future__ import annotations

from typing import Dict, List

from .corpus import Document
from .retrieval import RetrievalCase


def seed_documents() -> List[Document]:
    return [
        Document(
            doc_id="policy-refund",
            title="退款政策",
            source="kb://policy/refund-v3",
            version="3",
            updated_at=1_700_000_000.0,
            acl={"public"},
            text="""## 退款条件
商品签收后 7 天内，且未拆封使用，可申请无理由退款。
定制类商品不支持 7 天无理由退款，但质量问题仍可申请。
## 退款时效
退款审核通过后 1 到 3 个工作日原路退回，具体到账时间以支付渠道为准。
## 退款金额
若使用了优惠券，退款金额按实付金额计算，优惠券不返还。""",
        ),
        Document(
            doc_id="policy-vip",
            title="VIP 会员专享政策",
            source="kb://policy/vip-internal",
            version="1",
            updated_at=1_700_100_000.0,
            acl={"vip", "staff"},
            text="""## 会员权益
VIP 会员享有 30 天无理由退款，以及优先客服通道。
## 内部说明
VIP 退款由专属团队审批，单笔上限 20000 元。""",
        ),
        Document(
            doc_id="internal-ops",
            title="运维手册",
            source="kb://internal/ops",
            version="2",
            updated_at=1_700_200_000.0,
            acl={"staff"},
            text="""## 事故分级
P0 事故需在 5 分钟内拉起作战室，并通知值班负责人。
## 密钥轮换
所有服务密钥每 90 天轮换一次，轮换记录保存在内部审计系统。""",
        ),
        Document(
            doc_id="manual-gateway",
            title="工具网关使用手册",
            source="kb://docs/gateway",
            version="1",
            updated_at=1_700_300_000.0,
            acl={"public"},
            text="""## 错误码
工具调用失败时返回 GW-4021 表示能力令牌过期，GW-4022 表示能力范围不足。
遇到 GW-4021 请重新申请短期令牌，不要重试原请求。
## 超时
默认超时为 30 秒，超时后网关返回可重试错误，并记录 trace 字段 tool_timeout。""",
        ),
        Document(
            doc_id="faq-shipping",
            title="配送常见问题",
            source="kb://faq/shipping",
            version="4",
            updated_at=1_700_150_000.0,
            acl={"public"},
            text="""## 配送时效
大部分地区 48 小时内发货，偏远地区需要 3 到 5 天。
## 运费
单笔订单满 99 元包邮，否则收取 8 元运费。""",
        ),
        Document(
            doc_id="contract-sample",
            title="服务合同（模板）",
            source="kb://docs/contract-template",
            version="1",
            updated_at=1_700_050_000.0,
            acl={"public"},
            text="""## 违约责任
任何一方违约，应向守约方支付合同总额 20% 的违约金。
## 争议解决
争议提交北京仲裁委员会按其规则仲裁，裁决为终局裁决。""",
        ),
    ]


def seed_cases() -> List[RetrievalCase]:
    """Retrieval eval set: normal, exact-identifier, and ACL-trapped queries."""
    return [
        RetrievalCase("r1", "七天无理由退款的条件是什么", {"policy-refund"}),
        RetrievalCase("r2", "定制商品能退吗", {"policy-refund"}),
        RetrievalCase("r3", "退款多久到账", {"policy-refund"}),
        RetrievalCase("r4", "用了优惠券退款多少钱", {"policy-refund"}),
        RetrievalCase("r5", "GW-4021 是什么错误", {"manual-gateway"}),
        RetrievalCase("r6", "能力令牌过期怎么办", {"manual-gateway"}),
        RetrievalCase("r7", "工具调用超时是多久", {"manual-gateway"}),
        RetrievalCase("r8", "偏远地区多久发货", {"faq-shipping"}),
        RetrievalCase("r9", "多少钱包邮", {"faq-shipping"}),
        RetrievalCase("r10", "违约金比例是多少", {"contract-sample"}),
        RetrievalCase("r11", "争议怎么解决", {"contract-sample"}),
        # ACL traps: a non-vip user must never see policy-vip or internal-ops.
        RetrievalCase(
            "acl1",
            "VIP 会员退款有什么专享政策",
            set(),
            principals={"public"},
            forbid_docs={"policy-vip", "internal-ops"},
        ),
        RetrievalCase(
            "acl2",
            "密钥轮换多久一次，事故怎么分级",
            set(),
            principals={"public"},
            forbid_docs={"policy-vip", "internal-ops"},
        ),
        RetrievalCase(
            "acl3",
            "退款政策",
            {"policy-refund"},
            principals={"public"},
            forbid_docs={"policy-vip", "internal-ops"},
        ),
        # Positive control: staff may see them.
        RetrievalCase(
            "acl4",
            "VIP 会员退款上限",
            {"policy-vip"},
            principals={"staff"},
        ),
    ]


def build_corpus_from_dict(rows: List[Dict[str, object]]):
    """Load documents from ``examples/templates``-style JSON (ACL as a list)."""
    return [
        Document(
            doc_id=str(row["doc_id"]),
            title=str(row.get("title", "")),
            text=str(row.get("text", "")),
            acl=set(row.get("acl", ["public"])),
            updated_at=float(row.get("updated_at", 0.0)),
            source=str(row.get("source", "")),
        )
        for row in rows
    ]
