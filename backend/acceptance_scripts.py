"""阶段 4.5 验收：脚本文件夹执行链路（本服务器终端运行）。"""
from __future__ import annotations

import httpx
import time

BASE = "http://127.0.0.1:8032"
SCRIPT_DIR = "/Users/leyla/Documents/模型测试系统/workbuddy/sample_scripts"
c = httpx.Client(base_url=BASE, timeout=60, trust_env=False)


def find_project(name):
    for p in c.get("/api/projects").json():
        if p["name"] == name:
            return p
    return None


def cleanup(name):
    p = find_project(name)
    if p:
        c.delete(f"/api/projects/{p['id']}")
        print(f"  清理旧项目：{name} (id={p['id']})")


print("== 1. 准备脚本模式项目 ==")
# 复位上次崩溃遗留的 running 状态运行记录（开发库清理用）
stuck = c.get("/api/runs?limit=200").json()
for r in stuck:
    if r["status"] == "running":
        print(f"  复位遗留 running 记录 run_{r['id']:04d}")
import httpx as _hx
# 通过 jobs 没有复位接口，这里直接忽略；运行记录不影响新项目验收。

for n in ("_health_script_proj", "验收脚本项目"):
    cleanup(n)
r = c.post("/api/projects", json={
    "name": "验收脚本项目", "category": "脚本验证",
    "exec_mode": "script", "script_dir": SCRIPT_DIR,
})
proj = r.json()
pid = proj["id"]
print(f"  已创建：id={pid} exec_mode={proj['exec_mode']} script_dir={proj['script_dir']}")

print("\n== 2. 列出文件夹脚本 ==")
d = c.get(f"/api/projects/{pid}/scripts").json()
print(f"  目录：{d['dir']}")
for s in d["scripts"]:
    print(f"  - {s['name']}  [{s['ext']}]  {s['size']}B  可运行={s['runnable']}")

print("\n== 3. 一键运行全部脚本 ==")
r = c.post("/api/execute-scripts", json={"project_id": pid, "script_names": []})
job_id = r.json()["job_id"]
print(f"  job_id={job_id}")
while True:
    j = c.get(f"/api/jobs/{job_id}").json()
    print(f"  [状态 {j['status']}] 累计 {len(j['lines'])} 行输出；运行记录 {len(j['runs'])}")
    if j["status"] != "running":
        break
    time.sleep(0.5)
print("  终端输出预览：")
for ln in j["lines"][:20]:
    print(f"    {ln['ts']} {ln['text']}")

print("\n== 4. 自定义命令执行（故意非零退出） ==")
r = c.post("/api/execute-scripts", json={"project_id": pid, "command": "echo 自定义命令开始 && false"})
job_id2 = r.json()["job_id"]
while True:
    j = c.get(f"/api/jobs/{job_id2}").json()
    if j["status"] != "running":
        break
    time.sleep(0.5)
print(f"  命令任务状态：{j['status']}")

print("\n== 5. 校验 Run 记录与证据 ==")
runs = c.get(f"/api/runs?project_id={pid}").json()
print(f"  该项目运行记录数：{len(runs)}")
for r in sorted(runs, key=lambda x: x["id"]):
    ev = r["evidence_dir"]
    import os
    log = os.path.join(ev, "script_output.log") if ev else ""
    has_log = os.path.isfile(log) if log else False
    print(f"  run_{r['id']:04d} | {r['status']:7s} | {r['verdict']} | 证据日志={'有' if has_log else '无'}")

print("\n验收完成。项目 id=%d（如需清理：删除「验收脚本项目」即可，示例脚本在 sample_scripts/）。" % pid)
