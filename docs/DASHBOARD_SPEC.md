# 测试数据驾驶舱规格

版本：0.1
状态：产品需求补充

## 1. 页面定位

测试数据驾驶舱用于展示一次或一组性能、稳定性和兼容性测试的结果。它不是原始日志页面，而是将运行结果转换成可快速判断的指标、达标状态、分布图和逐请求明细。

## 2. 页面布局

```text
筛选区：Provider / Model / Mode / 测试项目 / 时间 / Run

核心指标区：
KV Cache 命中率 | 有效成功率 | TTFT P90 | TPOT P90 | Total TPM

汇总指标区：
请求数 | RPM | Input TPM | Output TPM | Cached TPM | Cache 命中 | 命中率上限 | 链路策略 | Req TPS
Latency Avg/P50/P90 | TTFT Avg/P50/P90 | TPOT Avg/P50/P90

可视化区：
Latency 分布 | TTFT 分布 | 成功率/错误率趋势 | TPM/RPM 趋势

明细区：
序号 | 状态 | HTTP | Latency | TTFT | TPOT | Input | Output | Cached | Error | Evidence
```

## 3. 核心指标卡

每个指标卡必须显示：

- 当前值。
- 指标名称。
- 目标值或阈值。
- 达标/未达标状态。
- 差值或超出幅度。
- 指标定义和数据来源。

第一版核心指标：

| 指标 | 计算方式 | 示例目标 |
| --- | --- | --- |
| KV Cache 命中率 | 命中请求数 / 可判定请求数 | >= 90% |
| 有效成功率 | 满足 HTTP、响应、断言和结束条件的请求 / 总请求 | >= 99% |
| TTFT P90 | 首个有效文本 Token 的 90 分位耗时 | <= 30 秒 |
| TPOT P90 | 生成阶段每 Token 耗时的 90 分位 | <= 40 ms/token |
| Total TPM | 输入、输出和缓存 Token 的总速率 | >= 项目目标 |

阈值不能写死在前端，应属于测试项目或测试标准配置，并在报告中保存阈值快照。

## 4. 汇总指标

必须区分以下概念：

- `HTTP 200`：传输层返回成功。
- `有效成功`：HTTP、响应解析、断言、结束状态全部通过。
- `有效成功率`：用于验收的最终成功率。
- `Req TPS`：请求吞吐率。
- `生成 TPS`：模型输出 Token 吞吐率。
- `Input/Output/Cached/Total TPM`：不同 Token 类型的每分钟速率。

不能用 HTTP 200 代替有效成功率，也不能用平均延迟代替 P90/P95。

## 5. 图表

第一版使用柱状图和折线图：

- Latency 分布直方图。
- TTFT 分布直方图。
- 按时间或请求序号的成功率趋势。
- 按时间或请求序号的 TTFT、TPOT 趋势。
- RPM、TPM 和缓存命中率趋势。

图表必须支持悬停查看请求范围、样本数和证据 ID；没有有效样本时显示“暂无可计算数据”，不能显示 0 误导用户。

## 6. 逐请求明细

明细表支持排序、筛选、分页和导出。每行至少包含：

```text
request_id
sequence
ok
http_status
latency_ms
ttfb_ms
ttft_ms
tpot_ms
input_tokens
output_tokens
cached_tokens
finish_reason
error_type
evidence_id
```

点击一行后打开详情抽屉，显示脱敏后的请求头、完整请求体、响应头、响应体、原始 SSE 事件、执行命令和评测结果。

## 7. 状态颜色

- 绿色：达到目标或测试通过。
- 红色：未达到目标、断言失败或明确错误。
- 黄色：部分支持、数据不足或需要人工复核。
- 灰色：未执行或指标无法计算。

颜色不能是唯一表达方式，必须同时显示文字状态和数值。

## 8. 数据接口建议

```json
{
  "run_id": "run-20260922-001",
  "provider": "provider-a",
  "model": "model-a",
  "mode": "stream",
  "thresholds": {
    "success_rate": {"operator": ">=", "value": 0.99},
    "ttft_p90_ms": {"operator": "<=", "value": 30000}
  },
  "summary": {
    "success_rate": 0.897,
    "requests": 292,
    "successful_requests": 262,
    "ttft_p90_ms": 32071.1,
    "tpot_p90_ms": 16.61,
    "total_tpm": 10257000,
    "cached_tpm": 9413000,
    "cache_hit_rate": 0.919
  },
  "histograms": {
    "latency_ms": [],
    "ttft_ms": []
  },
  "rows": []
}
```

## 9. 报告联动

仪表盘、在线表格和 HTML 报告使用同一份结构化 Report 数据：

```text
修改表格状态/备注/阈值
  -> 更新 Report JSON
  -> 重新计算汇总指标
  -> 刷新图表
  -> 实时更新 HTML 预览
```

原始请求响应证据只读，报告编辑只能修改结论、备注、标签和展示字段。
