# Paper-Context 宿主指令（AGENTS.md）

本仓库以 MCP 服务形态对外提供论文知识库与记忆库（Claude Code 读 AGENTS.md/CLAUDE.md，Codex 读 AGENTS.md）。宿主负责思考与编排，本服务负责"知道"和"记住"。

## 检索规范

- 论文/学术问题：先调 `rag_search`（默认 auto：向量优先，embedding 不可达自动降级全文检索）；空结果时明确告知"未在知识库中找到相关文献"，勿凭模型记忆编造。
- FTS 模式下检索英文文献建议用英文查询词（全文检索不做跨语言）。
- 阅读新论文：先 `rag_ingest(file_path)` 入库再检索；入库结果携带 `reverse` 描述符，可用 `rag_ingest(action="delete", paper_id=…)` 或 `source=…` 撤销。
- 引用规范：引用论文时注明 title/section/page 出处（检索结果每条 hit 自带）。
- 撰写约束：关键论断必须有 `rag_search` 命中的证据支撑，无证据处明示局限。

## 记忆规范

- 重要偏好、结论、约束变更用 `memory_save`（默认 scope=global 跨会话）；会话内临时状态用 scope=session。
- 新会话开始：先 `memory_prepare(query=本次任务)` 注入相关记忆，或 `handoff_pending` 查待认领的工作转交。
- 会话结束（有未完成工作）：`handoff_prepare`（objective/next_steps/blockers/progress_bullets）→ `handoff_commit` 转交，供下一次会话或另一宿主认领。
- 记忆纠错：`memory_update`（改值）/ `memory_forget`（删单条或按范围）。
- 工具返回可能截断；重要结论交叉核对或说明依据局限。勿泄露密钥与 `.env` 内容。
