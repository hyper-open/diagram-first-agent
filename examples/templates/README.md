# 可复制模板

这些 JSON 是书中各章「清单」的可执行版本：把勾选项变成**有字段、有约束、能被工具读取**的结构。

| 文件 | 对应章节 | 用在什么时候 |
| --- | --- | --- |
| `trace_step.json` | [评测与可观测性](/layers/evaluation) · [评测与会话 7.2](/layers/evaluation) | 每一步决策与工具调用要落哪些字段 |
| `usage_event.json` | [平台化](/evolution/platform) · 计费 | 一次调用如何计量、如何避免重复计费 |
| `eval_case.json` | [评测与可观测性](/layers/evaluation) · [检索层](/layers/retrieval) | 评测集的一条样本长什么样 |
| `tool_spec.json` | [工具层](/layers/tools) | 工具定义（给模型看、给宿主校验、给审计用） |
| `capability_token.json` | [工具网关](/evolution/tool-gateway) | 短期、限权的工具凭证 |
| `agent_task_packet.json` | [多 Agent](/evolution/multi-agent) · [编排层](/layers/orchestration) | 父 Agent 交给子 Agent 的任务包 |
| `release_record.json` | [版本与发布](/layers/release) | 一次发布必须记录的六件事 |
| `compliance_four_tables.json` | [合规与法律](/layers/compliance) | 上线前的四张表（资产/模型/场景/变更） |
| `preflight_checklist.json` | [平台化](/evolution/platform) · [安全与权限](/layers/security) | 上线前的门禁清单 |
| `mini_agent_config.json` | [从零实现 Mini Agent](/practice/mini-agent) | Mini Agent 的预算与审核参数 |

## 用法

1. 复制到你的项目，按注释里的“必填 / 可选”裁剪。
2. 字段说明以对应章节为准；模板只负责让清单**可被程序读取**。
3. 改字段要先想清楚：**谁消费这个字段**（模型 / 宿主 / 审计 / 计费），没人消费的字段就该删掉。

> 这些模板是**结构约定**，不是 schema 的最终版。真实系统里请用 JSON Schema 描述并做校验，参见 `examples/mini_agent/tools.py` 的 `validate_args()`。
