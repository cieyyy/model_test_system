# 实施计划

本计划遵循“先文档、后骨架、再垂直切片”的流程。每个阶段都需要有可运行结果和验收标准。

## 阶段 1：文档和边界

状态：已完成

- [x] 建立 README、CONTEXT、PRD、TRD。
- [x] 明确 Provider、Model、Mode、Project、Case、Run、Evidence、Report 对象。
- [x] 明确原始证据不可覆盖和 API Key 默认脱敏。
- [x] 根据服务器环境确定部署方式（8 核 / 16GB / 500GB NVMe / Ubuntu 24.04 + Docker，阶段 6 部署）。

## 阶段 2：最小登记闭环

状态：已完成（2026-09-30 验收通过，代码位于 `workbuddy/backend` 与 `workbuddy/frontend`）

目标：不发送请求，也能登记和浏览测试资产。

- [x] Provider 和模型登记。
- [x] 中转站网站和协议登记。
- [x] Key 加密保存（Fernet）、默认隐藏（掩码展示）和重新认证查看（平台主密码）。
- [x] 测试项目登记。
- [x] 从现有目录导入脚本、结果文件和测试项目表（只读扫描，不移动不复制）。
- [x] SQLite 数据库初始化（`workbuddy/data/app.db`）。
- [x] 简单 Web 管理页（`http://127.0.0.1:8032`）。

验收：新建一个 Provider、一个模型和一个 Kimi/GLM/DeepSeek 测试项目，并能重新打开查看。——已通过：666api 中转站 + kimi-k3/glm-5.3/deepseek-v4.1 三模型 + 三个专项测试项目，重启后数据完整；Key 掩码 `sk-********5678`，错误密码拒绝、正确密码可查看明文；旧资料目录扫描出 24 项，试导入 qwen-3.8max（16 个文件）。

## 阶段 3：测试执行和证据

状态：基本完成（2026-09-30，curl 命令执行器待补，代码位于 `workbuddy/backend/app/runner.py` 与 `jobs.py`）

目标：先支持 OpenAI 兼容接口。

- [x] Python 请求执行器（httpx 原生请求，保留真实请求头/体用于取证）。
- [ ] curl 命令解析和执行器（待补）。
- [x] 单个测试项目执行和全部项目执行（勾选批量）。
- [x] 受控终端窗口、实时输出、取消和退出状态（Web 终端面板 + 任务线程 + 取消信号）。
- [x] 预览模式和明确执行确认（预览不发送；真实执行必须 confirmed=true + 前端二次确认）。
- [x] 脱敏请求头、原始请求体、响应头、响应体（证据目录 run_XXXX/）。
- [x] SSE 流式响应保存（stream.ndjson 含逐 chunk 时间戳与 TTFT）。
- [x] 超时（60s）、错误分类（timeout/connect_error/auth_error/rate_limit/server_error）和运行指标（耗时/TTFT/Token/判定）。
- [x] 静默失效判定：HTTP 200 但 JSON 无效 / 无 tool_calls / 流式无内容 → failed（error_class=silent_failure）。

验收：用一个不含真实密钥的测试配置完成一次模拟运行；使用真实接口时能生成独立证据目录。——已通过：本地 Mock 服务（`backend/mock_openai.py`）验证 chat/stream/json/tools 四类 + 静默失效检测，5 次运行全部生成独立证据目录（含 sha256 manifest）。

## 阶段 4：兼容性矩阵（已完成 2026-10-02）

目标：从“单次结果”形成“能力结论”。

- [x] 模式和能力声明（矩阵自动聚合：兼容/部分/不支持/未知 + 未测试，人工复核可覆盖）。
- [x] 项目级兼容性（行 = 模型×中转站，列 = chat/stream/json/tools + 自定义分类）。
- [x] 个例级例外（单元格明细列出全部支撑 Run，可跳转证据）。
- [x] 人工复核和备注（matrix_reviews 表：override_status + note，可清除）。
- [x] 同一模型跨 Provider 对比（model_name 全局唯一 → (provider_id, model_name) 组合唯一，含旧表迁移）。
- [x] 配套调整：界面术语全中文（假成功），mock 支持全站假成功模式（8091 silent）。

验收：能够展示同一模型在两个中转站下的 stream、JSON、tools 和 context 结果差异。（已通过：mock-normal 在好站 4 类全兼容、坏站 4 类全部假成功 → 不支持；人工复核覆盖/清除验证通过，脚本 backend/acceptance_phase4.py）

## 阶段 5：报告编辑和导出

- 结构化 Report JSON。
- 可编辑结果表格。
- 性能数据驾驶舱：核心指标卡、阈值状态、分布图和逐请求明细。
- HTML 实时预览。
- 按“时间-中转站-模型”自动命名和归档文件。
- 基于 `docs/models` 默认模板生成表格和 HTML。
- 在线文件查看器和基础编辑器。
- JSON、Markdown、CSV、TXT、HTML、Python、Shell 和 Excel 文件的查看/编辑/导出。
- Excel、CSV、JSON 导出。
- 证据文件链接和哈希。
- 报告版本和修改记录。

验收：修改表格中的状态或备注后，HTML 预览立即反映修改，原始证据保持不变。

驾驶舱验收：导入一组包含成功、失败、超时和缓存字段的结果后，能够正确计算有效成功率、TTFT/TPOT 分位数、TPM、缓存命中率，并在图表和明细中定位到对应证据。

## 阶段 6：服务器化

- Ubuntu + Docker Compose。
- PostgreSQL 和 Redis Worker。
- 登录、权限和 HTTPS。
- 定时任务、并发队列和限流。
- 备份、归档和审计日志。

## 备忘：多 Key 轮询/切换测试（阶段 4 之后评估）

用户已确认的方向（2026-09-30）：中转站可保存多个 Key（互不覆盖），当前执行测试默认使用第一个。后续需要支持"同一测试用不同 Key 各跑一遍"的调度能力，用于对比不同 Key/分组/账号下的可用性与性能差异。实现时注意：

- 执行请求支持按 Key 维度展开（Run 记录关联 credential_id）。
- 结果对比视图按 Key 分组展示。
- Key 永不落入日志与报告，仅显示标签和掩码。
