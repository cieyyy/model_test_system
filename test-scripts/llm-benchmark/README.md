# 模型并发性能测试

一套本地运行的 OpenAI Chat Completions 兼容接口测试脚本。包含命令行压测、网页配置、实时进度、历史报告，以及与参考截图对应的 5 列指标网格、延迟百分位、逐请求延迟曲线和请求明细。

本项目是整体测试范围中的性能报告模块。后续整合同时覆盖“模型测试”和“模型测试系统”两个目录，范围、来源及指标差异见 [双目录整合基线](../../模型测试系统/workbuddy/docs/INTEGRATION_BASELINE.md)。通用功能、专项验收和现有管理平台需按该清单分别接入。

支持文本 / reasoning_content 流式输出。模型 ID 由你填写；示例名称不保证在某家供应商处可用。原有模型测试目录和脚本不受影响。

## 快速运行

macOS 双击 `start.command`，首次运行会创建 `.venv` 并安装依赖，随后打开本地控制台。也可在本目录运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py --open
```

默认地址是 `http://127.0.0.1:8765`；占用时自动尝试后续 19 个端口，以终端打印地址为准。需要 Python 3.9 以上。关闭终端或按 Ctrl+C 停止服务；已有报告保留在 `results/`。

先点击“运行模拟测试”可验证页面和导出，不访问外部 API。模拟报告会显示“模拟数据”，数值由本地模拟生成，不代表模型的真实性能。

真实测试填写接口地址、实际模型 ID、API Key 和测试参数，再点击“开始测试”。接口地址同时支持 `https://api.example.com/v1` 基础地址和 `https://api.example.com/v1/chat/completions` 完整地址：基础地址自动补全 `/chat/completions`，完整地址不重复追加；只有域名时使用 `/v1/chat/completions`。其他自定义路径按原样请求。报告配置的 `endpoint` 保存实际请求地址，网页报告模型名称的悬浮提示也显示该地址。

API Key 仅用于运行期间，不写入配置、报告、浏览器存储或日志。也可以让 API Key 输入框留空，使用环境变量：

```sh
read -s MODEL_TEST_API_KEY
export MODEL_TEST_API_KEY
.venv/bin/python server.py --open
```

输入 Key 后回车。默认共享同一份长提示词；配置并发 50、请求 160、输入目标 32000 Token，可执行参考截图对应规模的测试。真实调用会消耗所填渠道的额度，具体成本由模型计费决定。

## 命令行模式

```sh
# 本地模拟，生成 HTML / CSV / JSON。
.venv/bin/python benchmark.py --demo

# 编辑 config.example.json 中的 Endpoint 和模型 ID 后运行。
.venv/bin/python benchmark.py --config config.example.json

# 指定结果目录。
.venv/bin/python benchmark.py --config config.example.json --output ./my-results
```

每轮生成一个独立目录：

```text
results/<run_id>/
  run.json          运行元数据及配置，不包含 Key
  requests.jsonl    每完成一条请求就追加，便于中断后恢复
  report.json       完整配置、汇总、逐请求记录
  requests.csv      UTF-8 BOM，可用 Excel 打开
  report.html       单文件离线报告，无 CDN 或外部资源依赖
```

网页历史读取默认的 `results/`；自定义 `--output` 目录可直接打开其中的 HTML。自动生成的长上下文不落盘，只保存本地 Token 数、SHA256 和请求中的提示词摘要；自定义提示词作为配置的一部分保留于报告。输出仅保存最多 240 字符的摘要。

网页可以导出 HTML、CSV、JSON。点击“导出 PDF”会打开浏览器打印窗口，选择“另存为 PDF”。打印时包含当前状态筛选下的全部明细，不受屏幕分页限制；PDF 是浏览器打印导出，并非服务端生成。离线 HTML 同样可以导出 CSV、JSON 和打印。

## 指标口径

所有统计来自请求记录。延迟分位数仅包含成功请求，采用线性插值，并对每个指标独立过滤空值；百分位表的悬浮提示包含有效样本数。

| 指标 | 计算方式 |
| --- | --- |
| 成功率 | 成功请求 / 已发出并结束的请求，取消计入分母；未发出的请求不计入 |
| 请求数 | 已结束的请求 / 配置请求总数；运行中另有在途请求 |
| 总延迟 | 发起 HTTP 调用到流读取结束，包含连接、响应头和 Usage 尾包等待 |
| TTFT | 发起调用到首个非空 content 或 reasoning_content delta；角色及空包不算 |
| TPOT | `(最后文本片段时间 - 首个文本片段时间) / (供应商 output_tokens - 1)`；仅输出大于 1 Token 且成功时计算 |
| Req TPS（成功） | 成功请求数 / 整个测试执行窗口 |
| Req TPS（全部） | 已结束请求数（包含失败和取消）/ 整个测试执行窗口 |
| RPM | 成功 Req TPS × 60 |
| Input / Output / Cached / Total TPM | 对应成功请求的 Token 总和 / 测试窗口 × 60 |
| 系统 Gen TPS | 成功请求 output_tokens 总和 / 测试窗口 |
| 缓存命中率 | 成功请求 cached_input_tokens 总和 / input_tokens 总和，按 Token 加权 |

参考截图中两种 Req TPS 的原始定义未知。本版本明确区分“成功”和“全部”，成功率 100% 时相等。TPS / TPM / RPM 表示当前测试窗口的实测速率折算，不代表供应商允许的限额。

自动生成提示词使用 `cl100k_base` 计算本地文本 Token 数，不包含消息封装开销；实际模型的分词器可能不同，输入统计以供应商 Usage 为准。首次真实测试初始化 tiktoken 时可能需要下载其编码表。各请求共享提示词，易产生缓存命中；默认不预热，所有发出的请求都纳入测试窗口。要比较冷、热缓存，应分别设计测试提示词和运行批次。

缓存字段兼容 OpenAI 的 `prompt_tokens_details.cached_tokens` / `input_tokens_details.cached_tokens`，以及 DeepSeek 的 `prompt_cache_hit_tokens`。Token 数不以字符数或 SSE 包数估算。任何成功请求缺少某类 Usage 时，对应汇总和吞吐显示“—”，完整度保存在 `summary.token_coverage`；缓存返回 0 与未提供缓存数据分别处理。未知输出 Token 时 TPOT 不可用。

SSE 可能一次返回多个 Token，所以 TTFT / TPOT 是从客户端观察到的近似值，包含网络影响。reasoning Token 是否计入 output_tokens 由供应商定义。流必须提供 finish_reason 以及非空文本，否则视为不完整响应；`length` 表示达到输出上限，仍视为成功并保留结束原因。本版本不测试纯工具调用响应或非流式接口。

## 调度和错误处理

- 使用异步 HTTP 连接池和固定数量 Worker；请求完成后补位，最多运行配置的并发数。
- 总请求数达到上限或限制时长到达后停止派发；限制时长到达时等待在途请求结束，因此实际测试时长可能长于限制时长。
- 不自动重试、不自动跟随重定向；429、HTTP 错误、超时、无效 SSE、缺少结束原因都保留为失败记录。
- 单请求超时约束整个调用，不仅是两次网络读取之间的间隔。点击取消会停止派发，并取消在途请求。
- 默认直连且忽略系统 HTTP_PROXY 等环境变量；只有显式填入代理时才使用 HTTP/HTTPS 代理。
- 服务仅绑定本机地址，同时检查 Host / Origin。每次只运行一个测试任务，避免互相影响。
- 请求完成后立即写 JSONL；异常退出后网页历史可恢复已落盘记录，标识为“已中断”，不将其伪装成正常完成。

该版本使用本地文件存储，适合单人操作和截图规模的压测；未实现账号、多人调度、分布式 Worker、数据库或跨机器恢复。客户端资源、上游限流和共享渠道负载会影响结果，建议相同配置独立重复测量。

## 验证

```sh
.venv/bin/python -m unittest discover -s tests -v
```

测试使用本地 HTTP/SSE 模型接口，覆盖并发上限、UTF-8 分块、TTFT、TPOT、缺失 Usage、缓存 0、HTTP 429、提前断流、超时、取消、时间限制及离线报告。不消耗真实 API 额度。

另有可选的 `tests/browser_smoke.cjs`，需要 Node.js 和 Playwright（含 Chromium）。启动本地服务后运行 `node tests/browser_smoke.cjs`，会检查桌面 / 手机布局、曲线像素、分页、导出和离线 HTML，并生成截图及 PDF 到 `results/ui-validation/`。默认服务地址为 8765，其他端口通过 `BENCHMARK_URL` 指定。

本地服务接口：

```text
POST /api/runs                  {config: {...}, api_key: "..."}
GET  /api/runs                  历史列表
GET  /api/runs/<id>             完整报告
GET  /api/runs/<id>/events      SSE 进度，实时明细保留最近 500 条
POST /api/runs/<id>/cancel      {}
GET  /api/runs/<id>/csv         CSV
GET  /api/runs/<id>/html        离线 HTML
GET  /api/runs/<id>/json        JSON
```
