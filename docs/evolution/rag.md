---
title: 阶段 5 · RAG
description: 把私有知识、时效信息和可引用证据接入 Agent
---

# 阶段 5：RAG 与知识层

> Agent 会调工具，但它仍然不知道公司内部的产品文档、昨天的价格、客户的合同。

## 一图看懂

```mermaid
flowchart LR
  Q["用户问题"] --> A["Agent"]
  A -->|"search_knowledge"| R["RAG 服务"]
  R --> A
  A --> L["LLM"]
  L --> O["回答 + 引用"]
  R --> T["Tool Gateway"]
```

<p class="diagram-caption"><strong>一图看懂</strong> · RAG 不是“把文档塞进 prompt”，而是一个可被 Agent 调用的知识服务</p>

<div class="flow-steps">
  <div class="flow-step"><b>1</b><strong>索引</strong><p>连接数据源、解析、切分、embedding、建索引。</p></div>
  <div class="flow-step"><b>2</b><strong>查询</strong><p>改写问题、混合检索、权限过滤、重排。</p></div>
  <div class="flow-step"><b>3</b><strong>组装</strong><p>片段 + 来源 + 时间 + 冲突提示。</p></div>
  <div class="flow-step"><b>4</b><strong>引用</strong><p>每个关键结论可追溯到来源。</p></div>
</div>

本章另有 4 张图：图 1 索引流水线，图 2 查询流水线，图 3 管线式与 Agentic 的取舍，图 4 权限过滤。

## 两条流水线

见**图 1**：

```mermaid
flowchart LR
  S["数据源"] --> C["连接器"]
  C --> P["解析 / OCR"]
  P --> K["切分"]
  K --> E["元数据 / ACL"]
  E --> V["Embedding"]
  V --> I[(索引)]
```

<p class="diagram-caption"><strong>图 1</strong> · 写入/索引流水线：元数据与 ACL 必须在这里就写入</p>


```mermaid
flowchart LR
  Q["问题"] --> R["查询改写"]
  R --> H["混合检索"]
  H --> F["权限过滤"]
  F --> RR["重排"]
  RR --> C["上下文组装"]
  C --> L["LLM"]
  L --> A["回答 + 引用"]
```

<p class="diagram-caption"><strong>图 2</strong> · 查询流水线：权限过滤必须在召回阶段，而不是让模型“别说”</p>

## 管线式 RAG vs Agentic RAG

见**图 3**：

```mermaid
flowchart TB
  subgraph P["管线式 RAG"]
    direction LR
    Q1["问题"] --> R1["检索"] --> G1["生成"]
  end
  subgraph A["Agentic RAG"]
    direction LR
    Q2["问题"] --> A2["Agent"]
    A2 --> R2["检索"]
    R2 --> A2
    A2 --> G2["生成"]
  end
```

<p class="diagram-caption"><strong>图 3</strong> · 路径固定用管线；需要多跳、判断“查够了没有”用 Agentic RAG</p>

## 权限感知：最容易漏的一环

见**图 4**：

```mermaid
flowchart LR
  Q["查询"] --> F["tenant / user / ACL 过滤"]
  F --> C["只召回有权访问的片段"]
  C --> R["重排"]
  R --> S["引用只展示可访问来源"]
  X["全量召回后再过滤"] -.->|"错误做法"| C
```

<p class="diagram-caption"><strong>图 4</strong> · 权限过滤发生在召回阶段；索引里必须有可过滤的元数据</p>

ACL 要在索引期写入元数据，查询时过滤，而不是取回结果再筛。删除、留存与跨境的要求见[隐私与数据治理](/layers/privacy)；召回质量怎么量化，见[检索层](/layers/retrieval)。

## 它解决了什么，又制造了什么

| 解决了 | 同时制造了 |
| --- | --- |
| 知识过时 | 切分破坏语义，答案跨段丢失 |
| 私有数据不可见 | 相似度 ≠ 相关性，top-k 全是噪声 |
| 幻觉 / 无依据 | 多跳问题单次检索不够 |
| 无法追溯 | 来源冲突、旧文档未下线 |
| 上下文装不下 | 检索内容注入、权限泄露 |
| 权限差异 | 索引/重排/长上下文成本高 |

## 优化矩阵

::: tip 可复制模板
本章的清单有对应的结构化版本：[`eval_case.json`](../../examples/templates/eval_case.json)。全部模板见 [examples/templates/README.md](../../examples/templates/README.md)。
:::

> 这张表只给方向。切分、混合检索、重排参数与评测方法，见[检索层](/layers/retrieval)。

| 症状 | 手段 |
| --- | --- |
| 召回不到 | 查询改写、混合检索、扩大候选、同义词 |
| 召回太杂 | 重排、元数据过滤、提高阈值 |
| 片段不完整 | 父文档/子片段、上下文扩展 |
| 多跳 | 查询分解、Agentic RAG、工具组合 |
| 权限泄露 | ACL 元数据 + 召回时过滤 |
| 时效 | 增量索引、TTL、版本号 |
| 引用不可信 | 引用只来自检索结果，逐条校验 |
| 成本高 | 缓存、分层检索、结果复用 |

## 度量

| 指标 | 说明 |
| --- | --- |
| Recall@k / Precision@k | 正确证据在不在候选里、候选有多纯 |
| 引用支持率 | 结论是否被引用支持 |
| 答案正确率 | 端到端任务成功 |
| 权限泄露 | 应为 0 |
| 索引新鲜度 | 索引延迟、过期文档比例 |
| 延迟 / 成本 | 检索 + 重排 + 生成 |

## 阶段边界

| 够用 | 进入下一阶段 |
| --- | --- |
| 问题类型单一、路径固定 | 请求类型差异大：FAQ/工单/Agent 混在一起 |
| 所有请求都值得检索 | 简单请求检索反而更慢更贵 |
| 统一入口、统一策略 | 需要按意图选择不同处理路径 |

## 来源

- Lewis et al., **Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks**（2020）——原始论文。[链接](https://arxiv.org/abs/2005.11401)
- Gao et al., **RAG for Large Language Models: A Survey**（2023）——管线式 / 高级 / Agentic 三阶段，本章的分类依据。[链接](https://arxiv.org/abs/2312.10997)
- Asai et al., **Self-RAG**（2023）——让模型自己判断“要不要检索、检索结果够不够”。[链接](https://arxiv.org/abs/2310.11511)
- Trivedi et al., **IRCoT**（2022）——多跳问题为何需要检索与推理交替。[链接](https://arxiv.org/abs/2212.10509)

完整清单见[推荐来源](/appendix/sources#05-rag)。

::: details 参考答案与自检
**1. 写一张索引元数据表**
要点：至少包含 `doc_id / tenant / acl / source / updated_at / section_path / page`。缺 `acl` 就无法在召回阶段过滤，缺 `section_path` 就无法给出可信引用。
自检：如果表里只有“内容 + 向量”，你的 RAG 只能做演示。

**2. 五条路由（FAQ / 查单 / RAG / Agent / 人工）**
要点：每条都要写出**低置信兜底**，且兜底不能是“再来一次”。例如 FAQ 低置信 → RAG；RAG 无命中 → Agent；Agent 涉及不可逆动作 → 人工。
参考设计见[意图识别](/evolution/intent)的置信度分叉；自检：五条路径里若没有“人工”，说明高风险出口缺失。

**3. “无权限但语义相关”的检索用例**
要点：测试要断言**召回结果不含该文档**（而不是“回答里不出现”）。后者仍然泄露了存在性。
自检：过滤发生在召回之后（如重排后再删）属于未通过——见[检索层](/layers/retrieval)第七节“权限过滤：越早越好”。
:::

下一步：[阶段 6：意图识别与路由](/evolution/intent)。
