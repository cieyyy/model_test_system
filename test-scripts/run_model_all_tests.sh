#!/usr/bin/env bash
# 多模型通用测试项目统一入口
#
# 用法：
#   export API_BASE='https://agiapi.asia/v1'
#   export API_KEY_ENV='AGIAPI_API_KEY'
#   export MODEL='your-model'
#   # 测试项目可用 TEST_CASE 设置，也可用命令行 --case/--all 覆盖
#   export TEST_CASE='stream_metrics'
#   bash test-scripts/run_model_all_tests.sh all
#
# 也可以只运行一个项目，例如：
#   bash .../run_model_all_tests.sh --case stream_metrics
#   bash .../run_model_all_tests.sh --all
#
# 测试脚本本身仍保持独立；本文件只负责统一环境变量和运行入口。

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$PROJECT_ROOT/model-test-scripts"
REPORT_SCRIPT="$PROJECT_ROOT/generate_test_report.py"
PYTHON_BIN="${PYTHON_BIN:-python3}"
API_BASE="${API_BASE:-http://127.0.0.1:8000/v1}"
API_KEY_ENV="${API_KEY_ENV:-API_KEY}"
MODEL="${MODEL:-test-model}"
MODEL_SLUG="$(printf '%s' "$MODEL" | tr '/: ' '___' | tr -cd '[:alnum:]_.-')"
export TEST_RESULT_ROOT="${MT_RESULT_ROOT:-$PROJECT_ROOT}/${MODEL_SLUG:-unknown-model}/functional-test-results"
# 素材固定在脚本中，避免不同模型/中转站使用不同样本。
IMAGE_FILE="${MT_IMAGE_FILE:-${MT_MEDIA_DIR:-$PROJECT_ROOT/../media}/test.png}"
VIDEO_FILE="${MT_VIDEO_FILE:-${MT_MEDIA_DIR:-$PROJECT_ROOT/../media}/test.mp4}"
PUBLIC_IMAGE_URL='https://dashscope.oss-cn-beijing.aliyuncs.com/images/tiger.png'
PUBLIC_VIDEO_URL='https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4'
DRY_RUN="${DRY_RUN:-0}"
CACHE_MIN_TOKENS="${CACHE_MIN_TOKENS:-}"
CACHE_HIT_RATE="${CACHE_HIT_RATE:-}"
RATE_LIMIT_COOLDOWN_SECONDS="${RATE_LIMIT_COOLDOWN_SECONDS:-70}"
LONG_CONVERSATION_MAX_REQUESTS="${LONG_CONVERSATION_MAX_REQUESTS:-100}"
LONG_CONVERSATION_MAX_COMPLETION_TOKENS="${LONG_CONVERSATION_MAX_COMPLETION_TOKENS:-512}"

if [[ "$DRY_RUN" != '1' ]]; then
  PAID_FLAGS=(--run --confirm-paid)
else
  PAID_FLAGS=()
  echo '预览模式：未发送真实请求。'
fi

# Fail fast when a copied command accidentally concatenates the URL and next option.
if [[ "$API_BASE" == *'--key-env'* || "$API_BASE" == *'--model'* || "$API_BASE" == *' --'* ]]; then
  echo "API_BASE 参数疑似拼接错误：$API_BASE" >&2
  echo '请确认 --base-url 和 --key-env 之间保留空格。' >&2
  exit 2
fi
if [[ "$API_BASE" != http://* && "$API_BASE" != https://* ]]; then
  echo "API_BASE 必须是 http(s) 地址：$API_BASE" >&2
  exit 2
fi

run_cmd() {
  echo "+ $*"
  if [[ "$DRY_RUN" != '1' ]]; then
    "$@"
  fi
}

result_dir() {
  printf '%s/%s-%s-%s' "$TEST_RESULT_ROOT" "$(date +%Y%m%d-%H%M%S)" "$RANDOM" "$1"
}

cooldown_for_rate_limit() {
  if [[ "$DRY_RUN" == '1' || "${RATE_LIMIT_COOLDOWN_SECONDS:-0}" -le 0 ]]; then
    return
  fi
  echo "等待 ${RATE_LIMIT_COOLDOWN_SECONDS}s，避开中转站 RPM 限流窗口。"
  sleep "$RATE_LIMIT_COOLDOWN_SECONDS"
}

# 1. 通用功能兼容性：基础调用、推理、流式、JSON、工具、缓存、连续对话和上下文。
test_basic() {
  CACHE_FLAGS=()
  [[ -n "$CACHE_MIN_TOKENS" ]] && CACHE_FLAGS+=(--cache-min-tokens "$CACHE_MIN_TOKENS")
  run_cmd "$PYTHON_BIN" "$ROOT/test_basic_functionality.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --cases basic,reasoning,stream,json_object,json_schema,tools,parallel_tools,partial,cache,conversation,context \
    --rounds 5 --max-requests 30 --timeout 180 "${CACHE_FLAGS[@]}" "${PAID_FLAGS[@]}"
}

# 2. Responses、工具闭环、图片和视频理解；输出 x-request-id 证据。
test_api_features() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_api_features.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --cases responses,tools,vision_image,vision_video \
    --image "$IMAGE_FILE" --video "$VIDEO_FILE" "${PAID_FLAGS[@]}"
}

# 3. 工具结果闭环：先拿 tool_calls，再回传工具结果验证最终回答。
test_tool_loop() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_tool_loop.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --timeout 180 --output-dir "$(result_dir tool-loop)" "${PAID_FLAGS[@]}"
}

# 4. 独立视频理解：使用 SDK 发送 video_url/data URL，并记录结果。
test_video_understanding() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_video_understanding.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --video "$VIDEO_FILE" --timeout 300 --output-dir "$(result_dir video-understanding)" "${PAID_FLAGS[@]}"
}

# 5. 真正携带完整历史的长对话：验证连续上下文和 KVV，而不是独立重复请求。
test_long_conversation() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_long_conversation_kvv.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --rounds 100 --max-requests "$LONG_CONVERSATION_MAX_REQUESTS" \
    --max-completion-tokens "$LONG_CONVERSATION_MAX_COMPLETION_TOKENS" \
    --timeout 600 --output-dir "$(result_dir long-conversation)" "${PAID_FLAGS[@]}"
}

# 6. KVV 100/200/300 轮专项：每档独立会话，并持续携带完整历史。
test_kvv_rounds() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_kvv_rounds_100_200_300.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --rounds 100,200,300 --max-requests 600 --timeout 180 \
    --output-dir "$(result_dir kvv-100-200-300)" "${PAID_FLAGS[@]}"
}

# 7. 长输出：检查不同输出档位是否被 finish=length 截断或超时。
test_long_output() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_long_output_truncation.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --targets 512,1024,1808 --max-requests 3 --timeout 180 \
    --output-dir "$(result_dir long-output)" "${PAID_FLAGS[@]}"
}

# 8. thinking/temperature 参数兼容性回归。
test_thinking_temperature() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_thinking_temperature_regression.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --timeout 90 --output-dir "$(result_dir thinking-temperature-regression)" "${PAID_FLAGS[@]}"
}

# 9. 缓存命中率阶梯：首轮预热不计入分母，观察 cached_tokens 和命中率。
test_cache() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_context_cache_multimodal.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --mode cache --rounds 10 --timeout 180 \
    --output-dir "$(result_dir sophnet-load-cache)" "${PAID_FLAGS[@]}"
}

# 10. 1M 上下文边界：验证接近模型上下文上限时是否超时或被拒绝。
test_context() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_context_cache_multimodal.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --mode context --context-tokens 1000000 --max-completion-tokens 4096 \
    --rounds 1 --timeout 600 --output-dir "$(result_dir sophnet-load-context)" "${PAID_FLAGS[@]}"
}

# 11. 混合图文视频：图片和视频同时进入一个多模态请求。
test_mixed_vision() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_context_cache_multimodal.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --mode mixed --image "$IMAGE_FILE" --video "$VIDEO_FILE" \
    --max-completion-tokens 2048 --rounds 1 --timeout 600 \
    --output-dir "$(result_dir sophnet-load-mixed)" "${PAID_FLAGS[@]}"
}

# 12. 100MB 请求体：50/70/80/90/95/100MB 阶梯验证入口限制和 413/429/524。
test_100mb() {
  for size in 50 70 80 90 95 100; do
    run_cmd "$PYTHON_BIN" "$ROOT/test_context_cache_multimodal.py" \
      --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
      --mode mixed --image "$IMAGE_FILE" --video "$VIDEO_FILE" --body-mb "$size" \
      --max-completion-tokens 512 --rounds 1 --timeout 600 \
      --output-dir "$(result_dir "sophnet-load-body-${size}mb")" "${PAID_FLAGS[@]}"
  done
}

# 13. 常规固定速率性能：采集 HTTP 成功率、RPM/TPM、TTFT 和分位数。
test_fixed_rate() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_fixed_rate_performance.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --input-tokens 1000 --max-output-tokens 200 --rate-rpm 10 \
    --duration 60 --concurrency 5 --output-dir "$(result_dir load-test)" "${PAID_FLAGS[@]}"
}

# 14. 1000万 TPM 高吞吐基准：独立脚本，统计成功率、缓存、TPM、TTFT/TPOT。
test_tpm_benchmark() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_tpm_throughput_benchmark.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --requests 292 --duration 96 --concurrency 80 --input-tokens 35000 \
    --max-output-tokens 256 --target-tpm 10000000 --timeout 180 \
    --output-dir "$(result_dir tpm-benchmark)" "${PAID_FLAGS[@]}"
}

# 15. 独立流式指标：补采 AVG/P99 TTFT、AVG/P99 TPOT、GEN TOK/S 和成功率。
test_stream_metrics() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_stream_metrics.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --requests 90 --concurrency 5 --input-tokens 1000 --max-output-tokens 200 \
    --timeout 180 --output-dir "$(result_dir stream-metrics)" "${PAID_FLAGS[@]}"
}

# 16. 长上下文 TTFT 专项：16K/64K/100K 输入，固定 10 并发、每档 10 请求。
test_ttft_long_context() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_ttft_long_context_concurrency.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --case-tokens 16000,64000,100000 --requests-per-case 10 --concurrency 10 \
    --max-requests 30 --max-output-tokens 32 --timeout 300 \
    --output-dir "$(result_dir ttft-long-context)" "${PAID_FLAGS[@]}"
}

# 17. 严格验收：latency 模式用于多档输入/输出的流式 TTFT 验收。
test_latency_acceptance() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_latency_acceptance.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --mode latency --requests 10 --concurrency 5 --timeout 120 \
    --output-dir "$(result_dir acceptance)" "${PAID_FLAGS[@]}"
}

# 18. 并发稳定性：多会话和并发探测，验证连续响应和错误率。
test_stability() {
  run_cmd "$PYTHON_BIN" "$ROOT/test_concurrency_stability.py" \
    --base-url "$API_BASE" --key-env "$API_KEY_ENV" --model "$MODEL" \
    --rounds 50 --concurrency 10 --probes 10 \
    --output-dir "$(result_dir stability-concurrency)" "${PAID_FLAGS[@]}"
}

usage() {
  cat <<'EOF'
环境变量：
  API_BASE       中转站 API 地址
  API_KEY_ENV    API Key 所在环境变量名
  MODEL          被测模型名
  CACHE_MIN_TOKENS 可选的缓存最小块标注值，例如 8960
  CACHE_HIT_RATE  可选的报告自定义缓存命中率，例如 95.5
  TEST_CASE      默认测试项目；可被 --case 覆盖
  DRY_RUN=1       仅预览命令，不发送真实请求；默认发送请求
  RATE_LIMIT_COOLDOWN_SECONDS  all 模式中高压项目后的等待秒数，默认 70；设为 0 可关闭

固定素材：
  本地图片：$IMAGE_FILE
  本地视频：$VIDEO_FILE
  公网图片：https://dashscope.oss-cn-beijing.aliyuncs.com/images/tiger.png
  公网视频：https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4

可选项目：
  basic              通用功能兼容性
  api_features       Responses/工具/视觉
  tool_loop          Function Calling 工具结果闭环
  video_understanding 独立视频理解
  long_conversation  100轮真实连续对话
  kvv_rounds         KVV 100/200/300轮专项
  long_output        长输出截断与超时
  thinking_temperature thinking/temperature 参数兼容性
  cache              缓存命中率
  context            1M上下文
  mixed_vision       混合图文视频
  body_100mb         100MB请求体阶梯
  fixed_rate         固定速率性能
  tpm_benchmark      高吞吐/1000万TPM基准
  stream_metrics     AVG/P99 TTFT、TPOT、GEN TOK/S
  ttft_long_context  TTFT 16K/64K/100K，10并发
  latency            严格延迟验收
  stability          并发稳定性
  all                依次运行全部项目

示例：
  TEST_CASE=latency bash run_model_all_tests.sh
  bash run_model_all_tests.sh --case stream_metrics
  bash run_model_all_tests.sh --all
EOF
}

selected="${TEST_CASE:-}"; run_all=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --all) run_all=1; shift;;
    --case) selected="${2:-}"; shift 2;;
    -h|--help|help) usage; exit 0;;
    *) selected="$1"; shift;;
  esac
done
if [[ $run_all -eq 1 ]]; then selected='all'; fi
if [[ -z "$selected" ]]; then selected='all'; fi

case "$selected" in
  basic)
    test_basic
    ;;
  api_features)
    test_api_features
    ;;
  tool_loop)
    test_tool_loop
    ;;
  video_understanding)
    test_video_understanding
    ;;
  long_conversation)
    test_long_conversation
    ;;
  kvv_rounds)
    test_kvv_rounds
    ;;
  long_output)
    test_long_output
    ;;
  thinking_temperature)
    test_thinking_temperature
    ;;
  cache)
    test_cache
    ;;
  context)
    test_context
    ;;
  mixed_vision)
    test_mixed_vision
    ;;
  body_100mb)
    test_100mb
    ;;
  fixed_rate)
    test_fixed_rate
    ;;
  tpm_benchmark)
    test_tpm_benchmark
    ;;
  stream_metrics)
    test_stream_metrics
    ;;
  ttft_long_context)
    test_ttft_long_context
    ;;
  latency)
    test_latency_acceptance
    ;;
  stability)
    test_stability
    ;;
  all)
    test_basic
    test_api_features
    test_tool_loop
    test_video_understanding
    test_long_conversation
    test_kvv_rounds
    test_long_output
    test_thinking_temperature
    test_cache
    test_context
    test_mixed_vision
    test_100mb
    test_fixed_rate
    test_tpm_benchmark
    cooldown_for_rate_limit
    test_stream_metrics
    cooldown_for_rate_limit
    test_ttft_long_context
    test_latency_acceptance
    test_stability
    ;;
  *)
    echo "未知项目：$selected"
    usage
    exit 2
    ;;
esac

# 直接运行本脚本完成测试后，自动在当前模型目录导出 Excel 和 HTML。
# 设置 SKIP_REPORT=1 可仅运行测试而跳过报告导出。
if [[ "${SKIP_REPORT:-0}" != '1' && "$DRY_RUN" != '1' ]]; then
  REPORT_FLAGS=()
  [[ "${REPORT_FAILED_ONLY:-0}" == '1' ]] && REPORT_FLAGS=(--failed-only)
  [[ -n "$CACHE_HIT_RATE" ]] && REPORT_FLAGS+=(--cache-hit-rate "$CACHE_HIT_RATE")
  "$PYTHON_BIN" "$REPORT_SCRIPT" --model "$MODEL" --base-url "$API_BASE" \
    --results-root "$PROJECT_ROOT/${MODEL_SLUG:-unknown-model}" \
    --output-dir "$PROJECT_ROOT/${MODEL_SLUG:-unknown-model}/reports" \
    --run-date "$(date +%Y%m%d)" "${REPORT_FLAGS[@]}"
fi
