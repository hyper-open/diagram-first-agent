# Agent 学习手册

> **diagram-first-agent** · 从 ReAct 到 Agent 平台：架构分层、演进、网关、RAG、多 Agent、计费、内容安全与合规。

**在线阅读：** <https://hyper-open.github.io/diagram-first-agent/>

这不是又一份“Prompt 技巧合集”，也不是一张平铺的组件清单。它先给一张**架构分层总览**，再沿八级演进逐级加组件：

```text
一次调用 → ReAct → 工具调用 → 模型网关 → 工具网关
        → RAG → 意图路由 → 多 Agent → 平台化（计费/安全/评测）
```

每一级都回答同样四个问题：

1. 上一阶段长什么样？
2. 遇到了什么新矛盾？
3. 加了什么组件，它负责什么？
4. 这个组件又制造了什么新问题？

**全书以图为主线。** 每一章的第一屏都是同一件事：把组件、边界和请求流向画出来，再用文字解释箭头、取舍和故障。图不是配图，是正文；文字负责让你看懂这张图，而不是在图上盖一层名词。

配套一个**只用 Python 标准库**实现的 Mini Agent，所有核心概念都能跑起来验证。

## 内容地图

| 部分 | 章节 | 你会得到 |
| --- | --- | --- |
| 开始 | [学习路线](docs/start/roadmap.md) · [什么是 Agent](docs/start/what-is-agent.md) | 判断标准与阅读路径 |
| **架构主线** | [架构总览](docs/evolution/overview.md) · [ReAct](docs/evolution/react.md) · [工具调用](docs/evolution/tool-calling.md) · [模型网关](docs/evolution/model-gateway.md) · [工具网关](docs/evolution/tool-gateway.md) · [RAG](docs/evolution/rag.md) · [意图识别](docs/evolution/intent.md) · [多 Agent](docs/evolution/multi-agent.md) · [平台化](docs/evolution/platform.md) | 一条从最小 Agent 到可运营平台的演进路径 |
| 基础参考 | [核心循环](docs/foundations/loop.md) · [七层职责模型](docs/foundations/architecture.md) · [技术演进史](docs/foundations/evolution.md) | 职责视角与技术史背景 |
| 分层深入 | [模型](docs/layers/model.md) · [模型适配](docs/layers/model-adaptation.md) · [多模态](docs/layers/multimodal.md) · [上下文](docs/layers/context.md) · [检索](docs/layers/retrieval.md) · [工具](docs/layers/tools.md) · [记忆](docs/layers/memory.md) · [编排](docs/layers/orchestration.md) · [运行时](docs/layers/runtime.md) · [测试](docs/layers/testing.md) · [评测](docs/layers/evaluation.md) · [版本发布](docs/layers/release.md) · [安全](docs/layers/security.md) · [内容安全](docs/layers/content-safety.md) · [隐私](docs/layers/privacy.md) · [合规](docs/layers/compliance.md) | 每层的技术、故障与优化 |
| 动手 | [Mini Agent v1](docs/practice/mini-agent.md) · [v2](docs/practice/mini-agent-v2.md) · [v3](docs/practice/mini-agent-v3.md) · [部署形态](docs/practice/deployment.md) · [案例拆解](docs/practice/cases.md) · [交互设计](docs/practice/ux.md) | 可运行代码（三级递进）、可部署形态、真实系统分析 |
| 进阶 | [优化手册](docs/advanced/optimization.md) · [长任务](docs/advanced/durability.md) · [框架对照与迁移](docs/advanced/frameworks.md) · [前沿](docs/advanced/frontier.md) | 优化闭环、主流框架映射与迁移检查表 |
| 附录 | [术语表](docs/appendix/glossary.md) · [推荐来源](docs/appendix/sources.md) · [延伸阅读](docs/appendix/reading.md) | 速查、逐章出处与继续深入 |

## 怎么读：先图后文

1. **看主图。** 每章开头的「一图看懂」给出组件与流向，先建立坐标。
2. **读图注。** 把图上的箭头翻译成一句话：谁调用谁、在什么条件下、失败会怎样。
3. **再看文字。** 只读问题、取舍和验证方法；细节和代码按需展开。
4. **回到图上。** 如果某个概念说不出它落在哪个框里，就还没读懂——这一章的练习基本都在问这个。

## 快速开始

### 0. 离线阅读（不需要服务器）

直接双击打开：

```text
offline/index.html
```

这是独立的静态版本：相对路径、章节跳转、上一篇/下一篇、侧边栏筛选都可用，Mermaid 架构图由本地脚本渲染，不需要 `pnpm docs:dev`，也不需要联网。

重新生成：

```bash
pnpm build:offline
```

### 1. 读文档站

线上版：<https://hyper-open.github.io/diagram-first-agent/>（push 到 `main` 自动部署）。

本地启动：

```bash
pnpm install
pnpm docs:dev       # http://localhost:5173/diagram-first-agent/
```

构建静态站点：

```bash
pnpm docs:build
pnpm docs:preview
```

### 2. 跑 Mini Agent（不需要 API Key）

```bash
# v1：一个循环（工具、记忆、预算、审核、评测）
python3 -m examples.mini_agent.cli --demo
python3 -m examples.mini_agent.cli --demo --trace
python3 -m examples.mini_agent.eval          # 评测报告：pass@1 / pass^k

# v2：六条路径（FAQ / 查单 / RAG / Agent / 澄清 / 人工）
python3 -m examples.mini_agent_v2.cli --demo
python3 -m examples.mini_agent_v2.cli --eval # 检索评测：Recall + ACL 泄漏
python3 -m examples.mini_agent_v2.eval       # 端到端：路由准确率 + 转人工率

# v3：能力令牌 / 工具网关 / 账本 / 审计 / 断点续跑
python3 -m examples.mini_agent_v3.cli --demo         # 含非法主体被拒 + 审计记录
python3 -m examples.mini_agent_v3.cli --eval         # 平台不变量：配额、幂等、审计无 PII
python3 -m examples.mini_agent_v3.cli --resume-demo  # 模拟崩溃后续跑

# 起一个可部署的服务（零依赖，标准库 HTTP）
MINI_AGENT_PORT=8000 TENANT_LIMITS="acme:5" python3 -m examples.mini_agent_v3.app
curl -s http://127.0.0.1:8000/health
# 或者容器：docker compose up --build   （端口 8000 读写 / 8001 只读）
```

演示覆盖：工具调用、读写文件、验证器拒绝、记忆注入、停滞检测、输入拦截、输出打码、流式审核。

### 3. 跑测试

```bash
python3 -m unittest discover -s tests -v
# 或
pnpm test
```

### 4. 接入真实模型（可选）

任何 OpenAI 兼容端点：

```bash
export OPENAI_API_KEY="..."
export OPENAI_MODEL="gpt-4.1-mini"
export OPENAI_BASE_URL="https://api.openai.com/v1"   # 可选
python3 -m examples.mini_agent.cli --live "用计算器算 123 * 456" --trace
```

## 项目结构

```text
.
├── docs/                       # VitePress 教程
│   ├── start/                  # 路线、定义
│   ├── evolution/              # 架构演进主线（9 篇）
│   ├── foundations/            # 循环、七层职责、技术史
│   ├── layers/                 # 十六层深入（含适配、多模态、检索、测试、发布、治理）
│   ├── practice/               # 实战与案例
│   ├── advanced/               # 优化、前沿
│   ├── appendix/               # 术语、推荐来源、书单
│   └── .vitepress/             # 站点配置、主题、共享侧边栏
├── offline/                    # 离线静态版（双击 index.html）
├── tools/
│   ├── build-offline.mjs       # 离线构建脚本
│   └── offline/                # 离线 CSS/JS
├── Dockerfile / docker-compose.yml   # 教学镜像：非 root、健康检查、/data 卷
├── .github/workflows/ci.yml    # 测试矩阵 + 冒烟 + 镜像构建
├── examples/
│   ├── mini_agent/             # v1：无依赖 Agent 实现
│   │   ├── types.py            # Decision / ToolCall / Budget / Trace
│   │   ├── llm.py              # Mock + OpenAI 兼容客户端
│   │   ├── tools.py            # Schema 校验 / 工作区 / 错误契约
│   │   ├── memory.py           # 带来源与时效的简单记忆
│   │   ├── guard.py            # 内容审核：阶段策略 + 流式护栏
│   │   ├── agent.py            # 循环 / 上下文 / 验证 / 停滞检测
│   │   ├── eval.py             # 迷你评测 harness
│   │   └── cli.py              # 八个演示场景
│   ├── mini_agent_v2/          # v2：RAG + 意图路由 + 记忆治理（复用 v1）
│   ├── mini_agent_v3/          # v3：能力令牌 + 网关 + 账本 + 审计 + 持久化
│   │   ├── config.py           # 环境变量 → 启动即校验
│   │   ├── server.py           # 标准库 HTTP（/health /v1/answer /metrics）
│   │   └── app.py              # 进程入口（SIGTERM 优雅退出）
│   └── templates/              # 10 个可复制 JSON 模板（清单的结构化版本）
├── tests/                      # 129 个测试（v1 26 + v2 32 + v3 43 + 部署 28）
└── package.json                # VitePress 站点脚本
```

## 设计原则

- **图是正文，不是配图**：38 章全部以「一图看懂」开篇，统一按“图 → 解释 → 症状与手段 → 清单”展开。
- **一张图贯穿全书**：六层平台图、八级演进图、七层职责图是同一套系统的三种视角，术语与编号全书一致。
- **离线可用**：生成纯静态 HTML，使用相对路径和本地 Mermaid，双击即可阅读。
- **演进视角**：不只讲“有什么”，更讲“为什么需要它、它又制造了什么”。
- **框架无关**：先讲清循环与契约，再谈框架。所有示例不依赖 Agent 框架。
- **每层落到问题**：不只列技术，还给症状、根因、验证方法和修复手段。
- **可运行**：Mini Agent 有测试、trace、预算、错误契约，不是伪代码；v2 接上 RAG、路由与记忆治理，v3 接上能力令牌、计量账本、审计与断点续跑，并可直接以容器形态启动。
- **可替换**：LLM 是接口，工具是注册表，运行时可以换成容器/微虚拟机。
- **承认局限**：示例中的线程超时、简单裁剪、关键词记忆、规则式内容审核都标注了生产化差距。

## 适合谁

- 会调用 LLM API、想系统理解 Agent 的工程师。
- 正在做 Agent 产品，需要定位“为什么不稳定/太贵/太慢”的团队。
- 想从 Prompt 技巧进入 Agent 系统工程的学习者。

不需要先学某个 Agent 框架。需要基础 Python 与 HTTP/JSON 知识。

**这本书不教什么**（避免预期落差）：

- 不教主流 Agent 框架（LangGraph、OpenAI Agents SDK、CrewAI、Dify 等）的用法——只讲它们背后的循环、契约与编排模式，选型判断需要你自己补。
- 不提供生产级实现：Mini Agent 是**教学实现**，线程超时、简单裁剪、规则式审核都标注了生产化差距。
- 不覆盖模型原理（Transformer、训练、RLHF 的数学）、GPU 部署与推理优化。
- 不是新闻追踪：`frontier` 章给的是判断框架，不是最新产品清单。

## 状态

- 文档：38 篇、95 张图；每张图都有图注，每章以「一图看懂」开篇、其余按章内序号编号，正文用「见**图 N**」引用图表。
- 出处：38 章各有「来源」小节（每章 3–5 条），逐章完整清单汇总在[推荐来源](docs/appendix/sources.md)。
- 练习：26 处「练习」均配「参考答案与自检」（可折叠），给出要点与判定标准而非标准答案。
- 模板：`examples/templates/` 提供 10 个可复制 JSON（trace、usage event、评测用例、工具定义、能力令牌、任务包、发布记录、四张表、上线门禁、Mini Agent 配置），31 章的清单与之互引。
- 代码：Mini Agent **v1（单循环）→ v2（RAG / 路由 / 记忆治理）→ v3（网关 / 账本 / 审计 / 持久化）→ 可部署服务**，**129 个测试**，Python 3.8+，零第三方运行时依赖；CI 在 3.8/3.11/3.12 上跑测试与评测，并构建镜像、冒烟探测 HTTP 端点。
- 站点：VitePress 1.x + Mermaid 图表；另提供 `offline/` 纯静态离线版。
- 文档侧已补：每章来源、练习参考答案、可复制模板、主线与分层章的对照表、术语写法约定。

## 联系作者

- 技术问题 / Bug 反馈：请提 [Issue](../../issues)，方便沉淀给后来人
- 交流 / 合作 / 授权：扫码添加微信（备注 GitHub）

<p align="center"><img src="docs/images/wechat-qrcode.jpg" width="220" alt="作者微信"></p>

## English

**diagram-first-agent** — a diagram-first handbook for building agent systems.

Every chapter opens with one architecture diagram, then explains the arrows, the trade-offs and the failure modes behind it. Together they form one continuous path: a single LLM call → ReAct → tool calling → model/tool gateways → RAG → intent routing → multi-agent → a billable, auditable, compliant platform.

- **38 chapters, 95 diagrams.** Layers covered: model, model adaptation, multimodal, context, retrieval, tools, memory, orchestration, runtime, testing, evaluation, release, security, content safety, privacy, compliance.
- **A dependency-free Python Mini Agent** (26 tests): budgets, tracing, memory, content guard, eval harness.
- **Offline first.** Open `offline/index.html` — no server, no network.

Start here: [docs/evolution/overview.md](docs/evolution/overview.md).

Questions, bugs or collaboration: open an [issue](../../issues), or scan the WeChat QR code above.

## License

MIT。见 [LICENSE](LICENSE)。
