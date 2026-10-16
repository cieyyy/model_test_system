# 模型测试平台 TRD

## 1. 技术选型

- 后端：Python 3.12、FastAPI、Pydantic、SQLite（单机阶段）。
- 后续扩展：PostgreSQL、Redis、后台 Worker。
- 前端：React、TypeScript、TanStack Table、Monaco Editor。
- 报告：结构化 Report JSON 作为唯一数据源，Jinja2 渲染 HTML，Excel 作为导出格式。
- 部署：Ubuntu 24.04 LTS、Docker Compose、Caddy/Nginx。

## 2. 核心实体

```text
Provider       中转站和协议
Model          Provider 下的模型
Credential     加密后的 API Key 和脱敏展示信息
Mode           chat、stream、json、tools、vision 等测试模式
TestProject   可复用测试项目
TestCase      单个测试个例和评测规则
Run            一次模型/端点/项目执行
Evidence      不可覆盖的请求响应证据
Compatibility 模式和能力的兼容性结论
Report        可编辑报告数据和版本
Artifact      测试脚本、日志、证据、表格、HTML 和附件
TerminalJob   受控终端执行任务、状态、输出和退出码
```

## 3. 证据结构

每次执行生成独立目录：

```text
evidence/<run-id>/
  manifest.json
  request.json
  request_headers.json
  response.json
  response_headers.json
  stream.ndjson
  source.py 或 command.sh
  timing.json
  evaluation.json
```

原始证据写入后不可被报告编辑覆盖。报告只引用证据 ID。

## 4. 文件命名和归档

运行标识使用：

```text
YYYYMMDD-HHmmss-中转站-模型
```

归档目录：

```text
<run-name>/source
<run-name>/evidence
<run-name>/results
<run-name>/reports
```

报告文件默认使用：

```text
<run-name>.xlsx
<run-name>.html
```

模板从 `/Users/leyla/Documents/模型测试系统/workbuddy/docs/models` 加载，并记录模板版本或哈希。

## 5. 在线文件查看和编辑

按文件类型选择查看器：

```text
JSON       格式化树 + 源码编辑
Markdown   渲染预览 + 源码编辑
CSV/TSV    在线表格
XLSX       工作表和单元格编辑
HTML       安全 iframe 预览 + 源码
TXT/PY/SH  代码编辑器
```

原始证据挂载只读权限；编辑操作生成新版本并记录操作者、时间和变更摘要。

## 6. 终端执行

终端任务必须由后端创建，前端通过 WebSocket 或 SSE 查看输出。任务状态包括：`queued`、`running`、`passed`、`failed`、`cancelled`。

执行器必须限制工作目录、环境变量、运行时间、输出大小和可执行命令；真实付费测试需要二次确认。

## 7. 兼容性判定

能力声明、协议兼容性和实际个例结果分开存储。兼容性结论使用：

```text
compatible | partial | unsupported | unknown | manual_review
```

HTTP 200 只表示传输层成功，不能自动转为 `compatible`。

## 8. 安全边界

- 默认只监听 `127.0.0.1`。
- API Key 通过环境变量读取并在持久化前脱敏。
- 平台录入的 Key 使用密钥加密存储，列表和文件默认只显示掩码；查看明文需要主密码重新认证并设置短时会话。
- 明文 Key 不得进入终端输出、请求证据、报告、导出文件或应用日志。
- shell/Python 执行器使用明确的执行确认、超时、输出上限和工作目录。
- 生产部署使用 HTTPS、登录和按项目授权。
