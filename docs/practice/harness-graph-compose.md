---
title: Harness 与 Graph 结合：一个可中断的 Plan-Execute 工作流
description: 图管阶段（顺序/检查点/审批中断），harness 管每个阶段内部的循环
---

# Harness 与 Graph 结合

## 为什么要结合，以及谁管什么

前面的讨论澄清过：**harness 和 LangGraph 在"单 Agent 循环"上是重叠的**。所以结合不是"两层都写业务逻辑"，而是**分层**：

| 层 | 管什么 | 对应 |
| --- | --- | --- |
| **Graph** | 阶段之间的**控制流**：先做什么、后做什么、条件分支、**检查点**、**审批中断与恢复** | 类比 LangGraph |
| **Harness** | 一个阶段**内部**怎么跑：规划 → 执行 → 验证 → 重试 → 重规划 | v7 的 `PlanExecuteHarness` |

**一个图节点 = 一次 harness run。** 图不重新实现循环，harness 也不知道"阶段"和"审批"。

本项目受"零第三方依赖"约束，所以没有真的 `import langgraph`——而是**用标准库复刻它的核心概念**（State / Node / Edge / 条件边 / Checkpointer / interrupt），让结合是真实的，不是贴标签。

## 一图看懂

```
   ┌──────────────── Graph（控制流 + 检查点 + 中断/恢复）────────────────┐
   │                                                                    │
   │  gather ──▶ draft ──▶ approve ┄┄(需人工)┄┄▶ summarize              │
   │    │          │          │                    │                     │
   │    ▼          ▼          ▼                    ▼                     │
   │  [harness] [harness]  [harness]            [harness]               │
   │  规划→执行  规划→执行   审批门          规划→执行                    │
   │  →验证      →验证      (可能 interrupt)    →验证                     │
   │                                                                    │
   │  每个节点返回 state 更新，图负责合并 + 每节点存 checkpoint          │
   └────────────────────────────────────────────────────────────────────┘
```

<p class="diagram-caption"><strong>一图看懂</strong> · 图负责阶段与中断，节点内部是 harness；节点只返回更新，图合并并存档</p>

## 跑起来

```bash
python3 -m examples.mini_agent_v8.cli --demo     # 中断 → 批准 → 完成
python3 -m examples.mini_agent_v8.cli --trace    # 打印完整 state 与 checkpoint 历史
python3 -m unittest tests.test_mini_agent_v8 -v  # 15 个测试
```

输出（第一趟在审批闸门中断）：

```text
【第一趟】跑到审批闸门 → 中断
  status=interrupted  path=gather → draft → approve
  中断于: approve  提示: 退款金额 58 元，是否批准？
  checkpoint 数: 3（每节点一份，可恢复）
```

第二趟批准后跑完全部四阶段。

## 分层怎么实现的

### 1. 图引擎（`graph.py`）——对齐 LangGraph 的核心

```python
class Graph:
    def add_node(name, fn)          # 节点：(state) -> partial_update
    def add_edge(src, to)           # 静态边
    def add_conditional_edge(src, router)  # 条件边
    def run(state, max_steps)       # 执行
```

三个关键约束，都是 LangGraph 也有的：

- **节点只返回部分更新，由执行器合并**——不直接改共享 state。这是检查点和时间旅行能成立的前提。
- **每执行一个节点就存一份 checkpoint**。
- **节点可抛 `GraphInterrupt`**——图在该节点停下，state 已存档，可从此恢复。

### 2. 把 harness 包成节点（`compose.py`）

```python
def run_stage_as_node(spec, state_key):
    def node(state):
        harness = PlanExecuteHarness(...)     # ← 每个节点内部一个 harness
        result = harness.run(goal)
        if spec.requires_approval and not state["approved"]:
            raise GraphInterrupt(spec.name, spec.approval_prompt)  # 审批闸门
        return {state_key: {...}, "tokens_total": ...}  # 只返回更新
    return node
```

**注意节点做三件事**：跑 harness、必要时中断、返回更新。它不管顺序（图管）、不管恢复（图管）。

### 3. 阶段之间用 state 传值

```python
gather  → state["gather_result"]
draft   → 读前面的结果，产出 state["draft_result"]
approve → 若未批准，中断
summarize → 汇总
```

这就是"图"作为编排层的价值：**阶段之间怎么串、失败怎么停、人工怎么插**，全是显式控制流。

## 开发中踩的两个真实问题

### 问题一：空 Checkpointer 被悄悄换掉

```python
self.checkpointer = checkpointer or Checkpointer()   # ❌ 有 bug
```

`Checkpointer` 实现了 `__len__`，**空对象是 falsy**，于是 `or` 把调用方传入的 checkpointer 换成了新的——测试里 `len(cp)` 一直是 0。改为 `if ... is not None`。

**教训**：实现了 `__len__` / `__bool__` 的对象，别用 `x or default`。

### 问题二：中断必须在"跑完 harness 之后"

最初想先判审批再跑。但正确顺序是**先跑完这一阶段、记录结果，再中断**——否则恢复时会丢掉这一阶段已经做完的工作。所以 `GraphInterrupt` 抛在 `return` 之前、`harness.run()` 之后。

## 什么时候用这套，什么时候不用

| 场景 | 用什么 |
| --- | --- |
| 单阶段、线性循环（v7 原样） | 只用 harness，**别套图** |
| 多阶段、需要检查点/审批中断/恢复 | **harness + graph**（本文） |
| 阶段之间有并行分支、汇合 | graph（条件边/多出边） |
| 生产环境、要持久化到库 | 换成真 LangGraph（checkpointer 写 Postgres） |

**判断标准还是复杂度**：控制流简单就只写 harness；一旦需要"阶段、检查点、人工中断、恢复"，图的收益就出现了。

## 关于"零依赖"的取舍

本 demo 手写了迷你图引擎，好处是**能跑、能测、CI 不破、能看清机制**；代价是它不是 LangGraph——**没有真 LangGraph 的持久化后端、流式事件、可视化**。

生产建议在[框架对照](/advanced/frameworks)里说得很清楚：概念（节点契约、幂等、可控性）仍然要自己补，图引擎这种基础设施才交给 LangGraph。

## 自检清单

- [ ] 能说出 Graph 和 Harness 各管什么，以及为什么不是"两层都写业务"。
- [ ] 能解释为什么节点必须返回更新而不是直接改 state。
- [ ] 能说出检查点在哪里存、为什么中断前要存。
- [ ] 能解释中断为什么要在"跑完 harness 之后"。
- [ ] 能判断一个流程该只写 harness，还是该加图。
- [ ] 能说出这个迷你图引擎和真 LangGraph 的差距。

相关：[用 Harness 写 Plan-Execute](/practice/plan-execute-harness)、[长任务与可靠执行](/advanced/durability)、[框架对照](/advanced/frameworks)。
