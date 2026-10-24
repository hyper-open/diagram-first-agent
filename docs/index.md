---
layout: home

hero:
  name: "Agent 学习手册"
  text: "从 ReAct 到平台"
  tagline: 先建立架构分层总览，再沿八级演进逐层加组件：ReAct → 工具调用 → 模型网关 → 工具网关 → RAG → 意图路由 → 多 Agent → 计费与安全。
  image:
    src: /logo.svg
    alt: Agent 学习手册
  actions:
    - theme: brand
      text: 从架构总览开始
      link: /evolution/overview
    - theme: alt
      text: 直接看 ReAct
      link: /evolution/react

features:
  - icon: 🗺️
    title: 一条清晰的演进主线
    details: 不是零散知识点，而是“上一阶段遇到什么瓶颈 → 加什么组件 → 又引入什么新问题”的连续叙事。
  - icon: 🧱
    title: 每级都有架构图
    details: 从四个部件的 ReAct 循环，到控制面/数据面分离的 Agent 平台，逐级画清边界。
  - icon: 🚪
    title: 讲透网关与路由
    details: API Gateway、Model Gateway、Tool Gateway 各管什么；模型路由、回退、配额、计费怎么落地。
  - icon: 📚
    title: RAG 与意图识别
    details: 知识层如何做权限过滤与引用；为什么不是所有请求都该跑 Agent。
  - icon: 👥
    title: 多 Agent 与平台化
    details: 主管/工人、交接契约、A2A；以及计费、安全、评测、控制面如何收束。
  - icon: 🛡️
    title: 治理不止权限
    details: 内容审核、隐私、合规、发布门禁与长任务可靠性：从“能跑”走到“敢上线”。
  - icon: ⚙️
    title: 可运行的 Mini Agent
    details: 零第三方依赖的 Python 实现，带工具、记忆、预算、停滞检测、trace 和评测 harness。
---

<div class="hero-note">
  <strong>这不是“组件清单”，而是一条演化路径。</strong>
  每个阶段都只增加当前真正需要的组件；等新问题出现，再进入下一级。
</div>

## 怎么读：先图后文

<div class="flow-steps">
  <div class="flow-step"><b>1</b><strong>看主图</strong><p>先看清这一级有哪些组件、请求怎么流动。</p></div>
  <div class="flow-step"><b>2</b><strong>读图注</strong><p>用编号步骤把图上的箭头翻译成一句话。</p></div>
  <div class="flow-step"><b>3</b><strong>看时序</strong><p>理解一次请求在组件之间如何往返。</p></div>
  <div class="flow-step"><b>4</b><strong>再看文字</strong><p>只读问题、取舍和优化，代码收在折叠区。</p></div>
</div>

## 八级演进：每一级只解决一个新矛盾

<div class="stage-grid">
  <a class="stage-card" href="/evolution/overview">
    <span class="stage-num">00 · OVERVIEW</span>
    <h3>架构总览</h3>
    <p>六层架构、控制面/数据面、三类网关的分工，以及一张八级演进表。</p>
  </a>
  <a class="stage-card" href="/evolution/react">
    <span class="stage-num">01 · REACT</span>
    <h3>ReAct，最小可用 Agent</h3>
    <p>LLM + 本地工具 + 循环。理解“观察—思考—行动”为什么是 Agent 的心跳。</p>
  </a>
  <a class="stage-card" href="/evolution/tool-calling">
    <span class="stage-num">02 · TOOL CALLING</span>
    <h3>工具调用与注册</h3>
    <p>用 JSON Schema 和 Tool Registry 替代脆弱的文本解析，建立错误契约。</p>
  </a>
  <a class="stage-card" href="/evolution/model-gateway">
    <span class="stage-num">03 · MODEL GATEWAY</span>
    <h3>模型网关与路由</h3>
    <p>多模型接入、路由、回退、配额与成本：把模型访问收拢成一个内部服务。</p>
  </a>
  <a class="stage-card" href="/evolution/tool-gateway">
    <span class="stage-num">04 · TOOL GATEWAY</span>
    <h3>工具网关</h3>
    <p>跨团队工具的鉴权、限流、凭证托管、审计与协议适配，防止 Agent 变成超级账号。</p>
  </a>
  <a class="stage-card" href="/evolution/rag">
    <span class="stage-num">05 · RAG</span>
    <h3>RAG 与知识层</h3>
    <p>索引/查询两条流水线、权限感知检索、Agentic RAG，以及引用与冲突处理。</p>
  </a>
  <a class="stage-card" href="/evolution/intent">
    <span class="stage-num">06 · INTENT</span>
    <h3>意图识别与路由</h3>
    <p>FAQ、工作流、RAG、Agent、人工各走其路；用置信度和兜底控制误路由。</p>
  </a>
  <a class="stage-card" href="/evolution/multi-agent">
    <span class="stage-num">07 · MULTI-AGENT</span>
    <h3>多 Agent 协作</h3>
    <p>主管/工人、分层、辩论；交接契约、共享状态，以及什么时候不该用。</p>
  </a>
  <a class="stage-card" href="/evolution/platform">
    <span class="stage-num">08 · PLATFORM</span>
    <h3>平台化与治理</h3>
    <p>计费流水线、安全边界、评测与可观测、控制面，以及最小可用平台清单。</p>
  </a>
</div>

## 先看总览：Agent 平台的六层（组件视角）

<div class="layer-stack">
  <div class="layer-row"><span class="layer-index">L6</span><strong>接入层</strong><span>API Gateway、身份、租户、限流、WAF</span></div>
  <div class="layer-row"><span class="layer-index">L5</span><strong>编排层</strong><span>意图路由、Agent Runtime、工作流、多 Agent、终止与预算</span></div>
  <div class="layer-row"><span class="layer-index">L4</span><strong>能力层</strong><span>Model Gateway、Tool Gateway、RAG、Memory</span></div>
  <div class="layer-row"><span class="layer-index">L3</span><strong>执行层</strong><span>沙箱、浏览器、代码解释器、Computer Use</span></div>
  <div class="layer-row"><span class="layer-index">L2</span><strong>数据层</strong><span>状态、向量库、对象存储、计费账本、审计</span></div>
  <div class="layer-row"><span class="layer-index">L1</span><strong>治理面</strong><span>可观测、评测、策略、配额、版本与发布</span></div>
</div>

> 数据面负责“把这次请求做完”，控制面负责“所有请求按什么规则做”。平台化的核心，是把散落的规则收拢到控制面。

这六层是**组件视角**。想知道某个功能该由谁负责、出问题先查哪一层，看[分层架构](/foundations/architecture)的**七层职责模型**——两套编号的对应关系在那一页末尾有换算表。

## 这本书怎么读

<div class="arch-grid">
  <div class="arch-card">
    <h3>第一次接触 Agent</h3>
    <p>按「架构总览 → ReAct → 工具调用 → Mini Agent Lab 1–2」走，先跑通一个能调用工具的最小循环。</p>
  </div>
  <div class="arch-card">
    <h3>已经在做系统</h3>
    <p>重点看「模型网关 → 工具网关 → 平台化」，把鉴权、配额、计费、回退和审计补齐。</p>
  </div>
  <div class="arch-card">
    <h3>在做产品/平台</h3>
    <p>从「RAG → 意图识别 → 多 Agent → 平台化」切入，同时读评测与安全，先立指标和护栏。</p>
  </div>
</div>

## 你会得到什么

- 一条能解释“为什么系统长成今天这样”的架构演进主线。
- 每级架构的组件边界、请求时序、故障模式与优化手段。
- 对网关、路由、RAG、意图识别、多 Agent、计费与安全的工程判断。
- 一个不依赖框架的 Mini Agent 实现，以及可运行的评测 harness。

准备好了？从 [架构总览](/evolution/overview) 开始。
