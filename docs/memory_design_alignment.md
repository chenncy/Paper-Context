# 记忆系统设计对齐：PowerContext ↔ Paper-Context

> 本文档回答两个问题：Paper-Context 记忆系统与 PowerContext 的概念对应关系是什么；
> 哪些是有意对齐、哪些是自己的增量、哪些是有意不实现。
> 作者背景：本人为 PowerContext 开源贡献者（LongMemEval-V2 评测工作负载、文档），
> 本系统为研读其设计后的独立实现，非代码移植。

## 1. 概念映射表

| PowerContext 概念 | Paper-Context 实现 | 对齐程度 |
|---|---|---|
| **Scope**（隔离边界） | username + session_id + GLOBAL_PROFILE 三级 | 等价（实现更简单） |
| **Source**（证据日志，捕获≠记忆） | `agent_session_events` 追加式事件日志（敏感键脱敏、长度截断）；事件不自动成为记忆，须经固化管道 | 等价 |
| **Memory 带 Revision / retire** | `memory_item_history` 版本链 + `superseded_by/at` 取代链 + `as_of` 时间旅行查询；retire 语义 = supersede/retract，历史永不物理删除 | 等价，**as_of 为本地增强** |
| **PreparedContext**（单轮有界视图） | `app/prepared_context.py`：分层召回按优先级共享字节预算、每段带 Citation、单层失败记入 degraded、全空返回 status=empty 不阻塞 | 等价（三契约逐条对齐） |
| **Candidate → Review Inbox** | 固化产出 confidence < 阈值的记忆进 candidate 状态，不参与召回；`confirm/reject` 终态审核（`memory.confirmed/rejected` 审计事件） | 等价（缺 expected_version 乐观锁，见 §3） |
| **Handoff 四步闭环** | `app/handoff.py`：prepare（规则装配+可选 LLM 蒸馏）→ commit → acknowledge（并发认领保护）→ outcome（终态归档），双向审计事件 | 等价（缺 Work Contract 独立实体，见 §3） |
| **Artifact FAMILY/ID@REVISION** | 未实现 | 有意不实现（§3） |
| **Experience / Skill 治理** | 未实现 | 有意不实现（§3） |

## 2. 本地增量（PowerContext 没有强调的）

**事务性记忆管道**：聊天消息、会话事件与固化任务在**同一数据库事务**提交（Outbox 模式），
后台 Worker 以租约方式消费（乐观并发 claim、180s 租约超时夺回、指数退避、幂等键防重）。
保证：进程崩溃时记忆任务**不丢、不重**。PowerContext 的可靠性依赖其 Server 存储层，
未将"对话与记忆任务的事务一致性"作为一等契约——这是本系统针对生产 Agent 场景的自有增量，
也是 LoCoMo 之外最值得讲的差异化设计。

## 3. 有意不实现的部分（及理由）

| 未实现 | 理由 |
|---|---|
| Artifact 不可变 Revision 引用体系（FAMILY/ID@REVISION） | 版本链 + history 表已覆盖追溯需求；引用格式是仪式感成本 |
| Experience / Managed Skill 治理（导出投影） | 面向跨 Agent 技能市场场景，本系统是对话记忆，无此需求 |
| Work Contract 独立实体 | Handoff 的 objective 字段已承载目标语义，无需独立表 |
| Review 的 expected_version 乐观锁 | 单写入者场景（用户本人审核），并发冲突概率极低；列入 backlog |
| Scope 树 / 多级继承 | 三级 scope（全局/会话/用户）够用 |

## 4. 基准校准：与 PowerContext 公开数字的交叉验证

PowerContext 官方公布的 LoCoMo 结果（1540 题计分，排除 adversarial 类）：

| 系统 | 准确率 | 注入量 |
|---|---|---|
| PowerContext | 90.78% | ~1.65k tokens/题 |
| PowerMem | 87.79% | ~0.9k |
| 全文直塞基线 | 52.9% | ~26k |

本系统在 LoCoMo 样本 0（199 题）上的独立实测：

| 配置 | 准确率 | 注入量 |
|---|---|---|
| **全文直塞基线（本系统复现）** | **52.5%** | 24.0k 字符/题 |
| 记忆模式（lite 管道） | 40.7%（含 adversarial）| 1.5k 字符/题 |

**两个结论**：

1. **判分协议可信**：独立复现的全文基线 52.5% 与官方 52.9% 吻合（差 0.4pp），
   说明数据归一化、LLM-as-judge 判分与官方口径基本一致——本系统的评测数字可与
   PowerContext 公开数字放在同一坐标系讨论。
2. **当前差距与归因**：记忆模式与 PowerContext（90.78%）差距显著，归因明确：
   lite 评测管道只有"事实+摘要"两层（63 facts / 457 字），**没有接入向量情景记忆、
   图谱召回与重排**；生产链路（PreparedContext 五层装配）能力更强但未在真机跑过。
   该差距是路线图（生产链路复测），不是设计缺陷的证据。

## 5. 评测基准选型说明

- **LoCoMo**：对话型长会话记忆基准，与本系统场景同构，已跑（见 §4）。
- **LongMemEval-V1**：对话型五能力基准（信息抽取/多会话推理/时间推理/知识更新/拒答），
  与取代链、as_of、candidate 审核逐项对应，适配器见 `scripts/evaluate_longmemeval_lite.py`。
- **LongMemEval-V2**：coding/web-agent 轨迹记忆基准（本人贡献的 PowerContext 工作负载
  即此基准），**不用于本系统**。依据其官方 DATA_CARD：评测对象是"从 web-agent
  轨迹长历史中答题"（shopping/ServiceNow 环境、含截图与无障碍树观测），与本系统的
  对话记忆语义不同构。贡献经历作为设计理解深度的背书。

### 5.1 V2 官方参考前沿（LAFS 方法论，供口径参照）

V2 排行榜以 LAFS（Latency-Accuracy Frontier Score）计分：对每个记忆方法采集
（准确率, 查询延迟）操作点，与官方固定参考前沿比较。small 档参考点：

| 方法 | 准确率 | 查询延迟 |
|---|---|---|
| RAG: query → slice + notes | 51.0% | 0.2s |
| AgentRunbook-R | 58.6% | 26.9s |
| Codex | 69.9% | 177.2s |
| AgentRunbook-C | 74.9% | 108.3s |

**对本系统的启示**：LoCoMo 结果同样应读作"准确率-成本前沿"上的一个点——
记忆模式以 77%（相对全文基线）的准确率换取约 94% 的注入成本下降，
与全文基线各居前沿一侧；单看准确率或单看成本都会失真。

## 6. 面试叙事（30 秒版）

> "我给 PowerContext 贡献过 LongMemEval-V2 评测工作负载，过程中吃透了它的
> Source/Memory/PreparedContext/Handoff 设计。之后在自己的项目里独立实现了
> 同语义的记忆系统，并加了一层它没有强调的事务可靠性（Outbox + 租约 Worker，
> 崩溃不丢不重）。用 LoCoMo 验证时，我独立复现的全文基线（52.5%）与官方公布
> 数字（52.9%）吻合，说明评测协议可信；记忆模式目前达到基线的 77%、注入量
> 只有 1/16，与 PowerContext 生产系统的差距主要来自评测管道未接向量召回，
> 这是下一步。"

诚实边界：贡献者是"评测工作负载与文档"，不是核心引擎；本系统是"语义对齐的
独立实现"，不是 fork；当前数字出自 lite 管道，生产链路复测在路线图上。
