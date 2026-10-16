"""阶段 4 兼容性矩阵 端到端验收脚本（本地 Mock，不产生费用）。

场景：同名模型 mock-normal 分别登记在两个中转站：
  - 好站A（8090，正常）：四类全部通过 → 兼容
  - 坏站B（8091，全站假成功）：四类全部假成功 → 不支持
验证矩阵能展示同一模型跨中转站的结果差异，以及人工复核覆盖。
"""
import json
import time

import httpx

BASE = "http://127.0.0.1:8032"
c = httpx.Client(base_url=BASE, timeout=30, trust_env=False)  # 本地验收：绕过环境代理直连


def jpost(path, payload):
    r = c.post(path, json=payload)
    print(f"POST {path} -> {r.status_code}")
    if r.status_code >= 300:
        print(r.text)
        raise SystemExit(f"失败: {path}")
    return r.json()


def jget(path):
    r = c.get(path)
    if r.status_code >= 300:
        print(f"GET {path} -> {r.status_code}", r.text)
        raise SystemExit(f"失败: {path}")
    return r.json()


# 1. 两个中转站（同名模型可重复登记，验证组合唯一迁移）
pa = jpost("/api/providers", {"name": "验收·好站A", "base_url": "http://127.0.0.1:8090/v1", "api_key": "sk-mock-good"})
pb = jpost("/api/providers", {"name": "验收·坏站B", "base_url": "http://127.0.0.1:8091/v1", "api_key": "sk-mock-bad"})
ma = jpost("/api/models", {"provider_id": pa["id"], "model_name": "mock-normal"})
mb = jpost("/api/models", {"provider_id": pb["id"], "model_name": "mock-normal"})
print(f"同名模型登记成功：好站A model#{ma['id']} / 坏站B model#{mb['id']}")

# 2. 每站四类测试项目
pid_a, pid_b = pa["id"], pb["id"]
mid_a, mid_b = ma["id"], mb["id"]
cats = ["chat", "stream", "json", "tools"]
proj_ids = []
for st, pv, mv in (("A", pid_a, mid_a), ("B", pid_b, mid_b)):
    for cat in cats:
        p = jpost("/api/projects", {
            "name": f"验收{st}-{cat}", "category": cat,
            "provider_id": pv, "model_id": mv,
        })
        proj_ids.append(p["id"])

# 3. 真实执行（本地 Mock，不产生费用）
job = jpost("/api/execute", {"project_ids": proj_ids, "confirmed": True})
job_id = job["job_id"]
print(f"任务已启动: {job_id}")
while True:
    time.sleep(0.5)
    j = jget(f"/api/jobs/{job_id}")
    if j["status"] != "running":
        break
print(f"任务结束: {j['status']}")
for r in j["runs"]:
    print(f"  {r['project_name']:<12} HTTP {r['http_status']}  {'通过' if r['passed'] else '失败'}  {r['verdict'][:40]}")

# 4. 矩阵：同一模型跨中转站对比
m = jget("/api/matrix")
print("\n=== 兼容性矩阵 ===")
print("分类列:", m["categories"])
for row in m["rows"]:
    if row["model_name"] != "mock-normal":
        continue
    line = "  ".join(
        f"{m['categories'][i]}={row['cells'][cat]['effective_status']}"
        for i, cat in enumerate(m["categories"]))
    print(f"{row['model_name']} @ {row['provider_name']}: {line}")

good = next(r for r in m["rows"] if r["model_name"] == "mock-normal" and r["provider_name"] == "验收·好站A")
bad = next(r for r in m["rows"] if r["model_name"] == "mock-normal" and r["provider_name"] == "验收·坏站B")
for cat in cats:
    assert good["cells"][cat]["effective_status"] == "compatible", f"好站A {cat} 应为 compatible"
    assert bad["cells"][cat]["effective_status"] == "unsupported", f"坏站B {cat} 应为 unsupported，实际 {bad['cells'][cat]['effective_status']}"
print("\n验收①：同一模型在两个中转站下 chat/stream/json/tools 结果差异正确展示（兼容 vs 不支持）")

# 5. 单元格明细
d = jget(f"/api/matrix/detail?provider_id={bad['provider_id']}&model_name=mock-normal&category=stream")
assert len(d["runs"]) == 1 and d["runs"][0]["error_class"] == "silent_failure"
print("验收②：单元格明细返回运行记录，坏站 stream 为 silent_failure（假成功）")

# 6. 人工复核覆盖 + 备注
jpost("/api/matrix/review", {
    "provider_id": bad["provider_id"], "model_name": "mock-normal", "category": "chat",
    "override_status": "manual_review", "note": "坏站已反馈工单，等待修复后复测",
})
m2 = jget("/api/matrix")
bad2 = next(r for r in m2["rows"] if r["model_name"] == "mock-normal" and r["provider_name"] == "验收·坏站B")
cell = bad2["cells"]["chat"]
assert cell["effective_status"] == "manual_review" and "复测" in cell["note"]
assert m2["summary"]["manual_review"] >= 1
print("验收③：人工复核覆盖生效（chat 覆盖为待复核，带备注），汇总计数正确")

# 7. 清除复核
jpost("/api/matrix/review", {
    "provider_id": bad["provider_id"], "model_name": "mock-normal", "category": "chat",
    "override_status": "", "note": "",
})
m3 = jget("/api/matrix")
bad3 = next(r for r in m3["rows"] if r["model_name"] == "mock-normal" and r["provider_name"] == "验收·坏站B")
assert bad3["cells"]["chat"]["effective_status"] == "unsupported"
print("验收④：清除复核后回到自动聚合结论（不支持）")

print("\n===== 阶段 4 端到端验收全部通过 =====")
print("矩阵汇总:", json.dumps(m3["summary"], ensure_ascii=False))
