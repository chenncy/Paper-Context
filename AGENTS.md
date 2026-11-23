# Paper-Context AI 上手指南

Paper-Context 是面向 Codex、Claude Code 等宿主的本地论文知识库与长期记忆 MCP 服务。用户导入论文，在宿主对话中提问，再从回答后的引用打开 PDF 原页核对证据。宿主负责组织回答，Paper-Context 提供证据和显式保存的研究记忆。功能现状和更新记录见[项目说明](docs/PROJECT.md)。

## 从哪里读代码

| 任务 | 入口 |
| --- | --- |
| 检索、入库、索引统计 | [rag_mcp_server.py](rag_mcp_server.py) 注册 MCP 工具，[app/rag_service.py](app/rag_service.py) 实现检索模式、入库和返回字段 |
| 解析、切块、全文索引 | [app/ingestion/pipeline.py](app/ingestion/pipeline.py)、[app/ingestion/chunking.py](app/ingestion/chunking.py)、[app/rag_fts.py](app/rag_fts.py) |
| 对话引用打开原 PDF | [app/rag_service.py](app/rag_service.py) 生成引用，[evidence_server.py](evidence_server.py) 核对源文件并跳转到本地 PDF.js |
| 研究记忆和工作转交 | [memory_mcp_server.py](memory_mcp_server.py) 启动服务，[app/memory_mcp_server.py](app/memory_mcp_server.py) 注册工具，[app/memory_service.py](app/memory_service.py) 处理记忆，[app/handoff.py](app/handoff.py) 处理转交 |
| 安装与 MCP 配置 | [中文快速开始](docs/getting-started.zh-CN.md)、[scripts/configure_mcp.py](scripts/configure_mcp.py)、[scripts/check_install.py](scripts/check_install.py)、[scripts/smoke_mcp.py](scripts/smoke_mcp.py) |

## 检索规范

- 论文/学术问题：先调 `rag_search`（默认 auto：向量优先，embedding 不可达自动降级全文检索）；空结果时明确告知"未在知识库中找到相关文献"，勿凭模型记忆编造。
- FTS 模式下检索英文文献建议用英文查询词（全文检索不做跨语言）。
- 阅读新论文：先 `rag_ingest(file_path)` 入库再检索；入库结果携带 `reverse` 描述符，可用 `rag_ingest(action="delete", paper_id=…)` 或 `source=…` 撤销。
- 引用规范：引用论文时注明相应检索结果中可用的 title/section/page。
- PDF 原页直链：需要能点击的出处时，调用 `rag_search(mode="fts")`，把该证据的 `citation` 放在对应结论后。保持服务器返回的 URL 与页码；无 `citation` 时只注明已有的出处，不拼接链接。
- 撰写约束：关键论断必须有 `rag_search` 命中的证据支撑，无证据处明示局限。

## 记忆规范

- 重要偏好、结论、约束变更用 `memory_save`（默认 scope=global 跨会话）；会话内临时状态用 scope=session。
- 新会话开始：先 `memory_prepare(query=本次任务)` 注入相关记忆，或 `handoff_pending` 查待认领的工作转交。
- 会话结束（有未完成工作）：`handoff_prepare`（objective/next_steps/blockers/progress_bullets）→ `handoff_commit` 转交，供下一次会话或另一宿主认领。
- 记忆纠错：`memory_update`（改值）/ `memory_forget`（删单条或按范围）。
- 工具返回可能截断；重要结论交叉核对或说明依据局限。勿泄露密钥与 `.env` 内容。

## 修改代码时

- 检索与记忆的默认本地流程使用 SQLite。保留无 API Key 时的全文检索，以及 `auto` 模式降级时的说明。更多结构与工具约定见[检索架构](docs/rag_structure_and_mcp.md)和[记忆契约](docs/memory_contract_v1.md)。
- 修改引用链路时保留旧的 `page` 元数据语义。对于确认过的 PDF 证据，外部字段 `page_number` 从 1 起算，且与 PDF.js 的 `#page=N` 一致。旧索引缺少可靠定位信息时仍可检索，重新入库后才可生成直链。
- 修改后使用项目虚拟环境运行 `python -m pytest tests/ -q`。检索和页码改动可先跑 [入库测试](tests/test_rag_ingestion.py)、[全文检索测试](tests/test_rag_fts.py)和[切块测试](tests/test_rag_chunking.py)。
- 涉及用户操作或可见结果的改动时，在[项目说明](docs/PROJECT.md#更新记录)的「未发布」下记一条。安装或试用流程变化时同步对应的 README 和快速开始。
