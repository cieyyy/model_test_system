"""Curated adapters for the separate script workspace; preview never executes code."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import itertools
import json
import math
import os
import sys
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .db import BASE_DIR

SCRIPTS_ROOT = Path(os.environ.get("MT_SCRIPTS_ROOT", BASE_DIR / "test-scripts")).resolve()
MEDIA_ROOT = Path(os.environ.get("MT_MEDIA_DIR", BASE_DIR / "media"))
IMAGE_FILE = os.environ.get("MT_IMAGE_FILE", str(MEDIA_ROOT / "test.png"))
VIDEO_FILE = os.environ.get("MT_VIDEO_FILE", str(MEDIA_ROOT / "test.mp4"))
KEY_ENV = "MT_SUITE_API_KEY"


def number(default, label, low=1, high=100000, kind="integer"):
    return dict(default=default, label=label, min=low, max=high, type=kind)


def text(default, label, choices=None):
    return dict(default=default, label=label, type="string", **({"choices": choices} if choices else {}))


TIMEOUT = number(180, "单请求超时 / 秒", 1, 3600)
CONCURRENCY = number(5, "并发数", 1, 2000)
OUTPUT = number(256, "最大输出 Token", 1, 1000000)
INPUT = number(1000, "输入 Token 目标", 1, 1000000)


def script(name, filename, fields=None, fixed=None, output=True, note="", required=()):
    return dict(name=name, entry="model-test-scripts/" + filename,
                fields={"timeout": TIMEOUT, **(fields or {})}, fixed=fixed or {},
                output=output, note=note, required=list(required), kind="script")


REGISTRY = {
    "benchmark": dict(name="流式并发压测 · 截图报告", entry="llm-benchmark/benchmark.py", kind="benchmark",
        note="TTFT 含正文或 reasoning；TPM 为全程折算，百分位为线性插值。输入为 cl100k_base 本地目标，实际以 Usage 为准。",
        fields={"total_requests": number(20, "每档请求总数", 1, 100000),
                "timeout_seconds": number(120, "单请求超时 / 秒", 1, 3600),
                "duration_seconds": number(0, "每档发起窗口 / 秒（0 不限）", 0, 86400),
                "max_tokens_field": text("max_tokens", "输出字段", ["max_tokens", "max_completion_tokens"])}),
    "basic": script("基础功能", "test_basic_functionality.py", {
        "cases": text("basic,reasoning,stream,json_object,json_schema,tools,parallel_tools,partial,cache,conversation,context", "用例 IDs"),
        "rounds": number(3, "对话 / 缓存轮数", 1, 200), "max_requests": number(20, "请求硬上限")}, output=False),
    "api_features": script("Responses / 工具 / 视觉", "test_api_features.py", {
        "cases": text("responses,tools", "用例 IDs"), "rounds": number(3, "轮数", 1, 200),
        "max_requests": number(20, "请求硬上限"), "image": text("", "图片绝对路径"), "video": text("", "视频绝对路径")}, output=False),
    "tool_loop": script("工具调用闭环", "test_tool_loop.py"),
    "video_understanding": script("原生视频理解", "test_video_understanding.py", {"video": text("", "视频绝对路径")}, required=("video",)),
    "long_conversation": script("完整历史长对话", "test_long_conversation_kvv.py", {
        "rounds": number(20, "轮数", 1, 200), "max_requests": number(20, "请求硬上限"), "max_completion_tokens": OUTPUT}),
    "kvv_rounds": script("KVV 分档轮数", "test_kvv_rounds_100_200_300.py", {
        "rounds": text("100,200,300", "轮数档位"), "max_requests": number(600, "请求硬上限"), "max_completion_tokens": OUTPUT}),
    "long_output": script("长输出与截断", "test_long_output_truncation.py", {
        "targets": text("512,1024,1808", "输出 Token 档位"), "max_requests": number(3, "请求硬上限")}),
    "thinking_temperature": script("推理与温度兼容性", "test_thinking_temperature_regression.py", {"cases": text("all", "用例 IDs")}),
    "cache": script("缓存与预热", "test_context_cache_multimodal.py", {
        "rounds": number(5, "轮数", 1, 1000)}, fixed={"mode": "cache"}, note="源脚本使用固定重复前缀，最大输出固定 64 Token。"),
    "context": script("上下文容量", "test_context_cache_multimodal.py", {
        "context_tokens": number(1000000, "本地上下文 Token 目标", 1, 2000000),
        "rounds": number(1, "轮数", 1, 1000), "max_completion_tokens": OUTPUT}, fixed={"mode": "context"}),
    "mixed_vision": script("图片 + 视频混合请求", "test_context_cache_multimodal.py", {
        "context_tokens": INPUT, "rounds": number(1, "轮数", 1, 1000), "max_completion_tokens": OUTPUT,
        "image": text("", "图片绝对路径"), "video": text("", "视频绝对路径")}, fixed={"mode": "mixed"}, required=("image", "video")),
    "body_100mb": script("大请求体（单档）", "test_context_cache_multimodal.py", {
        "body_mb": number(100, "请求体 MB", 1, 120), "context_tokens": INPUT,
        "rounds": number(1, "轮数", 1, 100), "max_completion_tokens": OUTPUT,
        "image": text("", "图片绝对路径"), "video": text("", "视频绝对路径")}, fixed={"mode": "mixed"},
        required=("image", "video"), note="metadata 填充的传输测试；不等同于 100MB 有效媒体理解。"),
    "fixed_rate": script("固定速率负载", "test_fixed_rate_performance.py", {
        "rate_rpm": number(10, "发起速率 RPM", 0.1, 100000, "number"), "duration": number(60, "持续秒数", 1, 86400),
        "concurrency": CONCURRENCY, "input_tokens": INPUT, "max_output_tokens": OUTPUT}),
    "tpm_benchmark": script("TPM 吞吐", "test_tpm_throughput_benchmark.py", {
        "requests": number(20, "请求总数"), "duration": number(60, "发起窗口 / 秒", 1, 86400),
        "concurrency": CONCURRENCY, "input_tokens": INPUT, "max_output_tokens": OUTPUT,
        "target_tpm": number(1000000, "验收目标 TPM", 1, 1000000000)}),
    "stream_metrics": script("流式 TTFT / TPOT", "test_stream_metrics.py", {
        "requests": number(20, "请求总数"), "concurrency": CONCURRENCY, "input_tokens": INPUT, "max_output_tokens": OUTPUT}),
    "ttft_long_context": script("长输入并发首字", "test_ttft_long_context_concurrency.py", {
        "case_tokens": text("16000,64000,100000", "输入 Token 档位"), "requests_per_case": number(10, "每档请求数"),
        "concurrency": CONCURRENCY, "max_output_tokens": OUTPUT, "max_requests": number(30, "请求硬上限")}),
    "latency": script("输入输出组合延迟验收", "test_latency_acceptance.py", {
        "input_tokens": text("1000,2000,4000", "输入 Token 档位"), "output_tokens": text("200,500,800", "输出 Token 档位"),
        "requests": number(3, "每组合请求数"), "concurrency": CONCURRENCY}, fixed={"mode": "latency"}),
    "stability": script("并发稳定性", "test_concurrency_stability.py", {
        "rounds": number(20, "轮数", 1, 1000), "concurrency": CONCURRENCY, "probes": number(5, "探测请求数"), "max_completion_tokens": OUTPUT}),
    "text_peak": dict(name="纯文本滚动 60 秒峰值 TPM", entry="model-test-scripts/text-peak-tpm/run_text_peak_tpm.py", kind="script", output=True,
        fields={"duration": number(120, "发起窗口 / 秒", 60, 1800), "rate_rps": number(5, "发起速率 RPS", 0.1, 2000, "number"),
                "concurrency": CONCURRENCY, "input_units": number(1000, "输入文本单元（非官方 Token）", 128, 131072),
                "max_output_tokens": number(256, "最大输出 Token", 16, 8192), "max_requests": number(601, "请求硬上限", 2, 100000),
                "timeout": number(180, "单请求超时 / 秒", 6, 300),
                "tpm_basis": text("total", "TPM 口径", ["total", "input", "output", "uncached_total"]),
                "cache_mode": text("shared", "前缀模式", ["shared", "unique"])},
        note="完整滚动 60 秒完成窗口；TTFT 仅正文；包含探测请求；保留脚本业务断言。"),
    "acceptance": dict(name="独立验收包", entry="standalone-acceptance/run_acceptance.py", kind="script", output=True,
        fields={"cases": text("synthetic_traffic", "用例 IDs"), "concurrency": CONCURRENCY,
                "max_requests": number(20, "请求硬上限"), "duration_seconds": number(10, "持续秒数", 1, 86400),
                "timeout_seconds": TIMEOUT, "max_output_tokens": OUTPUT},
        note="百万上下文、媒体与部署项依赖原包配置和素材；部署材料仍需人工核验。"),
    "az_probe": dict(name="渠道 / AZ 指纹探测", entry="az-channel-probe/run_az_channel_probe.py", kind="script", output=True,
        fields={"cases": text("models", "用例 IDs", ["models", "az", "concurrency", "all"]),
                "concurrency_levels": text("1,5,10", "并发档位"), "concurrency_requests": number(2, "每档请求倍数", 1, 100),
                "max_requests": number(30, "请求硬上限"), "timeout": TIMEOUT},
        note="响应头指纹仅作渠道观测，不能证明后台供应链。"),
}

# The common wrapper is the local acceptance profile; Python defaults fill its omitted flags.
COMMON_SUITES = list(REGISTRY)[1:19]
WRAPPER = SCRIPTS_ROOT / "run_model_all_tests.sh"
LOCAL_PROFILE = {
    "basic": dict(cases="basic,reasoning,stream,json_object,json_schema,tools,parallel_tools,partial,cache,conversation,context", rounds=5, max_requests=30, timeout=180),
    "api_features": dict(cases="responses,tools,vision_image,vision_video", image=IMAGE_FILE, video=VIDEO_FILE),
    "tool_loop": dict(timeout=180),
    "video_understanding": dict(video=VIDEO_FILE, timeout=300),
    "long_conversation": dict(rounds=100, max_requests=100, max_completion_tokens=512, timeout=600),
    "kvv_rounds": dict(rounds="100,200,300", max_requests=600, timeout=180),
    "long_output": dict(targets="512,1024,1808", max_requests=3, timeout=180),
    "thinking_temperature": dict(timeout=90),
    "cache": dict(rounds=10, timeout=180),
    "context": dict(context_tokens=1000000, max_completion_tokens=4096, rounds=1, timeout=600),
    "mixed_vision": dict(image=IMAGE_FILE, video=VIDEO_FILE, max_completion_tokens=2048, rounds=1, timeout=600),
    "body_100mb": dict(image=IMAGE_FILE, video=VIDEO_FILE, max_completion_tokens=512, rounds=1, timeout=600),
    "fixed_rate": dict(input_tokens=1000, max_output_tokens=200, rate_rpm=10, duration=60, concurrency=5),
    "tpm_benchmark": dict(requests=292, duration=96, concurrency=80, input_tokens=35000, max_output_tokens=256, target_tpm=10000000, timeout=180),
    "stream_metrics": dict(requests=90, concurrency=5, input_tokens=1000, max_output_tokens=200, timeout=180),
    "ttft_long_context": dict(case_tokens="16000,64000,100000", requests_per_case=10, concurrency=10, max_requests=30, max_output_tokens=32, timeout=300),
    "latency": dict(requests=10, concurrency=5, timeout=120),
    "stability": dict(rounds=50, concurrency=10, probes=10),
}


def cli_defaults(path):
    """Inspect literals without importing or executing a local test script."""
    result = {}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "add_argument":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant) or not str(node.args[0].value).startswith("--"):
            continue
        for kw in node.keywords:
            if kw.arg == "default":
                try:
                    value = ast.literal_eval(kw.value)
                    if isinstance(value, (str, int, float)):
                        result[node.args[0].value[2:].replace("-", "_")] = value
                except (ValueError, TypeError):
                    pass
    return result


for _sid, _item in REGISTRY.items():
    _item["fields"] = copy.deepcopy(_item["fields"])
    _path = SCRIPTS_ROOT / _item["entry"]
    _defaults = cli_defaults(_path) if _item["kind"] == "script" and _path.is_file() else {}
    for _key, _field in _item["fields"].items():
        if _key in _defaults:
            _field["default"] = _defaults[_key]
        if _key in LOCAL_PROFILE.get(_sid, {}):
            _field["default"] = LOCAL_PROFILE[_sid][_key]
    _item["group"] = "common" if _sid in COMMON_SUITES else "additional"
    _item["profile_source"] = "run_model_all_tests.sh + 脚本默认参数" if _sid in COMMON_SUITES else "专项配置预设"
    if _sid == "text_peak":
        _item["profile_source"] = "脚本默认参数"

_acceptance_config = SCRIPTS_ROOT / "standalone-acceptance/config.public.json"
if _acceptance_config.is_file():
    _preset = json.loads(_acceptance_config.read_text(encoding="utf-8"))
    for _key, _field in REGISTRY["acceptance"]["fields"].items():
        if _key in _preset:
            _value = _preset[_key]
            _field["default"] = ",".join(_value) if _key == "cases" and isinstance(_value, list) else _value
    REGISTRY["acceptance"]["profile_source"] = "standalone-acceptance/config.public.json"
REGISTRY["acceptance"]["fields"]["duration_seconds"].update(min=60, max=3600)
REGISTRY["acceptance"]["fields"]["max_output_tokens"]["max"] = 8192
REGISTRY["acceptance"]["fields"]["concurrency"]["max"] = 2048

REGISTRY["body_100mb"]["name"] = "大请求体阶梯"
REGISTRY["body_100mb"]["fields"].pop("body_mb")
REGISTRY["body_100mb"]["fields"]["body_mb_levels"] = text("50,70,80,90,95,100", "请求体 MB 档位")
REGISTRY["basic"]["fields"].update(context_chars=number(12000, "上下文字符数", 1, 10000000))
for _sid in ("kvv_rounds", "long_conversation"):
    REGISTRY[_sid]["fields"]["reasoning_effort"] = text("low", "推理强度", ["low", "high", "max"] + (["none"] if _sid == "kvv_rounds" else []))


def hash_plan(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

SOURCES = [
    ("通用脚本", SCRIPTS_ROOT / "model-test-scripts", "已适配 18 类入口"),
    ("压测与截图报告", SCRIPTS_ROOT / "llm-benchmark", "已接入"),
    ("独立验收", SCRIPTS_ROOT / "standalone-acceptance", "独立适配，保留原结论"),
    ("渠道诊断", SCRIPTS_ROOT / "az-channel-probe", "独立适配"),
    ("早期 K3 验收", SCRIPTS_ROOT / "model-test-scripts/k3-acceptance", "资料索引，未适配执行"),
    ("统一 API 旧用例", SCRIPTS_ROOT / "测试模型/统一模型API测试", "资料索引，未适配执行"),
    ("连续对话与恢复素材", SCRIPTS_ROOT / "测试模型/K3与GLM5.3-API测试素材", "原始 fixture 索引，按需选择素材"),
    ("GLM 专属", SCRIPTS_ROOT / "glm5.3", "资料索引，保留模型参数和历史证据"),
    ("Qwen 专属", SCRIPTS_ROOT / "qwen-3.8max", "资料索引，保留模型参数和历史证据"),
    ("35 项通用指标清单", SCRIPTS_ROOT / "通用测试项目与指标.xlsx", "需求清单，覆盖差异见整合基线"),
    ("模型专属与客户标准", SCRIPTS_ROOT / "客户的测试脚本", "资料索引；GLM / Qwen 等保留独立执行"),
    ("管理平台与模板", BASE_DIR, "复用渠道 / 模型 / 密钥 / 运行记录，保留原模板"),
]


@lru_cache(maxsize=1)
def benchmark_module():
    path = SCRIPTS_ROOT / REGISTRY["benchmark"]["entry"]
    spec = importlib.util.spec_from_file_location("workbuddy_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def catalog():
    suites = []
    for sid, item in REGISTRY.items():
        path = SCRIPTS_ROOT / item["entry"]
        suites.append(dict(id=sid, **item, path=str(path), available=path.is_file(),
                           sha256=digest(path) if path.is_file() else ""))
    return dict(suites=suites, common_suite_ids=COMMON_SUITES, wrapper=str(WRAPPER),
                wrapper_sha256=digest(WRAPPER) if WRAPPER.is_file() else "",
                sources=[dict(name=n, path=str(p), status=s, available=p.exists()) for n, p, s in SOURCES])


def validated_fields(item, params):
    fields = item["fields"]
    unknown = set(params) - set(fields)
    if unknown:
        raise ValueError("不支持的参数: " + ", ".join(sorted(unknown)))
    values = {}
    for key, field in fields.items():
        value = params.get(key, field["default"])
        if field["type"] in ("integer", "number"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} 必须是数字")
            if field["type"] == "integer" and not isinstance(value, int):
                raise ValueError(f"{key} 必须是整数")
            if not field["min"] <= value <= field["max"]:
                raise ValueError(f"{key} 范围 {field['min']}～{field['max']}")
        elif not isinstance(value, str) or len(value) > 2000 or "\x00" in value:
            raise ValueError(f"{key} 字符串无效")
        if "choices" in field and value not in field["choices"]:
            raise ValueError(f"{key} 不在允许值中")
        if key in ("image", "video") and value and not Path(value).is_file():
            raise ValueError(f"{key} 文件不存在")
        if key in item.get("required", []) and not value:
            raise ValueError(f"请指定 {key} 素材")
        values[key] = value
    return values


def axis(values, low, high):
    if not isinstance(values, list) or not 1 <= len(values) <= 24:
        raise ValueError("档位必须为含 1～24 个整数的列表")
    if any(isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high for v in values):
        raise ValueError(f"档位范围 {low}～{high}")
    return list(dict.fromkeys(values))


def make_plan(request, provider, model):
    if request.suite_ids:
        ids = request.suite_ids
        if len(ids) != len(set(ids)) or len(ids) > len(REGISTRY):
            raise ValueError("项目不能重复，且不能超过目录数量")
        unknown = set(request.suite_parameters) - set(ids)
        if unknown:
            raise ValueError("参数包含未选择的项目")
        projects = []
        for sid in ids:
            try:
                projects.append(make_plan(request.model_copy(update=dict(suite_ids=[], suite_id=sid,
                    parameters=request.suite_parameters.get(sid, request.parameters if sid == request.suite_id else {}),
                    repeats=request.repeats if sid == "benchmark" else 1)), provider, model))
            except (ValueError, OSError) as exc:
                raise ValueError(f"{REGISTRY.get(sid, {}).get('name', sid)}：{exc}") from exc
        plan = dict(projects[0])
        plan.pop("plan_hash", None)
        plan.pop("command", None)
        plan.update(suite_id="batch", name="多项目测试", kind="batch", projects=projects,
                    continue_on_error=request.continue_on_error, cooldown_seconds=request.cooldown_seconds,
                    stages=[dict(**stage, project_index=i) for i, p in enumerate(projects) for stage in p["stages"]])
        if len(plan["stages"]) > 128:
            raise ValueError("批量计划最多 128 个阶段")
        budgets = [p["request_budget"] for p in projects]
        plan.update(stage_count=len(plan["stages"]), project_count=len(projects),
                    request_budget=sum(budgets) if all(b is not None for b in budgets) else None,
                    known_request_budget=sum(b or 0 for b in budgets), unknown_budget_projects=sum(b is None for b in budgets),
                    token_target_upper_bound=None, budget_kind="各脚本计划合计（非计费保证）")
        if sum(b or 0 for b in budgets) > 100000:
            raise ValueError("批量计划累计请求数不得超过 100000")
        plan["plan_hash"] = hash_plan(plan)
        return plan
    sid = request.suite_id
    if sid not in REGISTRY:
        raise ValueError("未知测试套件")
    item = REGISTRY[sid]
    path = SCRIPTS_ROOT / item["entry"]
    if not path.is_file():
        raise ValueError("套件源文件不存在，请检查 MT_SCRIPTS_ROOT")
    if request.demo and sid != "benchmark":
        raise ValueError("本地模拟仅适用于流式并发压测套件")
    endpoint = benchmark_module().normalize_endpoint(provider.base_url) if provider else "http://localhost/v1/chat/completions"
    if provider and provider.protocol != "openai":
        raise ValueError("这些适配器需要 OpenAI 兼容协议")
    values = validated_fields(item, request.parameters)
    plan = dict(suite_id=sid, name=item["name"], kind=item["kind"], source=str(path), source_sha256=digest(path),
                provider_id=provider.id if provider else 0, provider_name=provider.name if provider else "本地模拟",
                model=model.model_name if model else "demo-model", model_id=model.id if model else None,
                credential_id=request.credential_id, endpoint=endpoint, parameters=values,
                demo=request.demo, note=item.get("note", ""), timeout_seconds=request.job_timeout_seconds, stages=[],
                profile_source=item["profile_source"], wrapper_sha256=digest(WRAPPER) if WRAPPER.is_file() else "")
    if sid == "api_features":
        cases = {v.strip() for v in values["cases"].split(",")}
        for case, asset in (("vision_image", "image"), ("vision_video", "video")):
            if case in cases and not values[asset]:
                raise ValueError(f"{case} 需要 {asset} 素材")
    plan["assets"] = [dict(parameter=k, path=str(Path(v).resolve()), sha256=digest(Path(v)))
                      for k, v in values.items() if k in ("image", "video") and v]
    config_path = _acceptance_config if sid == "acceptance" else SCRIPTS_ROOT / "az-channel-probe/config.local.json" if sid == "az_probe" else None
    if config_path and config_path.is_file():
        plan["assets"].append(dict(parameter="source_config", path=str(config_path), sha256=digest(config_path)))
    if sid == "benchmark":
        cc = axis(request.concurrency_levels, 1, 500)
        ii = axis(request.input_token_levels, 1, 1000000)
        oo = axis(request.output_token_levels, 1, 1000000)
        if request.strategy == "cartesian":
            count = len(cc) * len(ii) * len(oo)
            combos = itertools.product(cc, ii, oo)
        else:
            combos = list(dict.fromkeys([(c, ii[0], oo[0]) for c in cc] + [(cc[0], i, oo[0]) for i in ii] + [(cc[0], ii[0], o) for o in oo]))
            count = len(combos)
        if count * request.repeats > 64:
            raise ValueError("单计划最多 64 个阶段，请减少档位或重复次数")
        for c, i, o in combos:
            for repeat in range(1, request.repeats + 1):
                cfg = benchmark_module().validate_config(dict(values, concurrency=c, prompt_tokens=i, max_tokens=o,
                    endpoint=endpoint, model=plan["model"], demo=request.demo, name=item["name"]))
                plan["stages"].append(dict(config=cfg, repeat=repeat))
        budget = sum(s["config"]["total_requests"] for s in plan["stages"])
        plan.update(strategy=request.strategy, request_budget=budget, budget_kind="请求上限", token_target_upper_bound=sum(
            s["config"]["total_requests"] * (s["config"]["prompt_tokens"] + s["config"]["max_tokens"]) for s in plan["stages"]))
        if budget > 100000:
            raise ValueError("单计划累计请求上限为 100000")
    else:
        if request.repeats != 1:
            raise ValueError("外部脚本请在套件轮数参数中设置重复次数")
        plan["stages"] = [dict(config=values)]
        if sid == "body_100mb":
            try:
                sizes = list(dict.fromkeys(float(v) for v in values["body_mb_levels"].split(",")))
                if not sizes or len(sizes) > 24 or any(not math.isfinite(v) or not 1 <= v <= 120 for v in sizes):
                    raise ValueError()
            except ValueError as exc:
                raise ValueError("请求体档位需要 1～120 MB 的逗号分隔数值，最多 24 档") from exc
            plan["stages"] = [dict(config={k: v for k, v in values.items() if k != "body_mb_levels"} | {"body_mb": size}) for size in sizes]
        plan.update(request_budget=values.get("max_requests", values.get("requests")), budget_kind="脚本硬上限 / 计划值；空值表示由用例决定")
        if not endpoint.endswith("/chat/completions"):
            raise ValueError("外部脚本需要 /v1 基础地址或以 /chat/completions 结尾的接口地址")
        if sid in ("cache", "context", "mixed_vision", "body_100mb"):
            plan["request_budget"] = values["rounds"] * len(plan["stages"])
            if sid != "cache" and values["context_tokens"] < 1000:
                raise ValueError("该脚本的 context_tokens 至少为 1000")
        if sid == "stability":
            plan["request_budget"] = values["rounds"] + values["probes"]
        if sid == "video_understanding":
            plan["request_budget"] = 1
        if sid == "tool_loop":
            plan["request_budget"] = 2
        if sid == "latency":
            try:
                inputs = [int(v) for v in values["input_tokens"].split(",")]
                outputs = [int(v) for v in values["output_tokens"].split(",")]
                if not inputs or not outputs or min(inputs + outputs) < 1:
                    raise ValueError()
            except ValueError as exc:
                raise ValueError("输入输出档位需要逗号分隔的正整数") from exc
            plan["request_budget"] = len(inputs) * len(outputs) * values["requests"]
        if sid == "fixed_rate":
            plan["request_budget"] = max(1, math.ceil(values["rate_rpm"] * values["duration"] / 60))
        if sid == "text_peak":
            planned = math.ceil(values["rate_rps"] * values["duration"]) + 1
            if planned > values["max_requests"]:
                raise ValueError(f"峰值套件含 1 次探测，计划 {planned} 次，超过 max_requests")
        if plan["request_budget"] is not None and plan["request_budget"] > 100000:
            raise ValueError("单计划累计请求上限为 100000")
        plan["commands"] = [script_command({**plan, "parameters": stage["config"]}, Path("<运行目录>") / "artifacts") for stage in plan["stages"]]
        plan["command"] = plan["commands"][0]
    plan["stage_count"] = len(plan["stages"])
    plan["plan_hash"] = hash_plan(plan)
    return plan


def script_command(plan, output):
    item = REGISTRY[plan["suite_id"]]
    u = urlsplit(plan["endpoint"])
    path = u.path.removesuffix("/chat/completions")
    base = urlunsplit((u.scheme, u.netloc, path, "", ""))
    model_flag = "--models" if plan["suite_id"] == "az_probe" else "--model"
    command = [sys.executable, "-u", plan["source"], "--base-url", base,
               "--key-env", KEY_ENV, model_flag, plan["model"]]
    for key, value in {**plan["parameters"], **item.get("fixed", {})}.items():
        if key == "body_mb_levels":
            continue
        if value != "":
            command.extend(["--" + key.replace("_", "-"), str(value)])
    if item.get("output"):
        command.extend(["--output-dir", str(output)])
    command.extend(["--execute" if plan["suite_id"] == "az_probe" else "--run", "--confirm-paid"])
    return command
