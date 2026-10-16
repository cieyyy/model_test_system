"""公开素材准备器，由 run_acceptance.py --prepare-materials 调用；不调用模型。"""
from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
import subprocess
from pathlib import Path

SOURCES = {
    "fruits.jpg": {
        "url": "https://raw.githubusercontent.com/opencv/opencv/4.10.0/samples/data/fruits.jpg",
        "source_page": "https://github.com/opencv/opencv/blob/4.10.0/samples/data/fruits.jpg",
        "license_source": "https://github.com/opencv/opencv/blob/4.10.0/LICENSE",
        "sha256": "9c031d80a1c52da5eca790db896baffec6a7e52bf786cdb7bbfca5c7f880e6a1",
        "bytes": 82429, "media_type": "image/jpeg",
    },
    "sample-5s-360p.mp4": {
        "url": "https://samplelib.com/mp4/sample-5s-360p.mp4",
        "source_page": "https://samplelib.com/sample-mp4.html",
        "license_source": "https://samplelib.com/license.html",
        "license_note": "来源页面说明示例视频可下载且无许可限制；保留来源供审计。",
        "sha256": "d385179acb9e43d7ff87d0e8fbff2074fb29bc2363b64206128383a17ca534a8", "bytes": 1137884, "media_type": "video/mp4",
    },
    "pride-and-prejudice-gutenberg.txt": {
        "url": "https://raw.githubusercontent.com/GITenberg/Pride-and-Prejudice_1342/master/1342-0.txt",
        "source_page": "https://www.gutenberg.org/ebooks/1342",
        "license_source": "https://www.gutenberg.org/policy/license.html",
        "license_note": "Jane Austen《傲慢与偏见》，Gutenberg #1342；原始文本含许可说明。",
        "sha256": "48e0522844402a86a3ea98f0947ba85ea54838db3c59022299298ed968a5a163",
        "bytes": 724725, "media_type": "text/plain; charset=utf-8",
    },
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(directory, name, source):
    path = directory / name
    if path.is_file() and digest(path) == source["sha256"]:
        return path
    if not shutil.which("curl"):
        raise ValueError("素材下载需要系统 curl")
    part = directory / (name + ".download")
    print(f"下载公开素材：{name}", flush=True)
    result = subprocess.run([
        "curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
        "--retry", "2", "--retry-delay", "1", "--connect-timeout", "20", "--max-time", "240",
        "--max-filesize", str(source["bytes"] + 1024), "--silent", "--show-error",
        source["url"], "-o", str(part),
    ], check=False)
    if result.returncode or not part.is_file() or digest(part) != source["sha256"]:
        part.unlink(missing_ok=True)
        raise ValueError(f"下载失败或 SHA256 不匹配：{name}；拒绝使用损坏或变更的素材")
    part.replace(path)
    return path


def prepare(root, c, encoding_name="o200k_base"):
    import tiktoken
    from run_acceptance import dumps, now, save

    directory = root / "fixtures" / "public"
    directory.mkdir(parents=True, exist_ok=True)
    for name, source in SOURCES.items():
        download(directory, name, source)
    enc = tiktoken.get_encoding(encoding_name)
    book = (directory / "pride-and-prejudice-gutenberg.txt").read_text(encoding="utf-8-sig")
    corpus = enc.encode(book)
    if not corpus:
        raise ValueError("公开文本为空")
    rng = random.Random(20261015)
    def text_tokens(count, offset=0):
        data = corpus[offset % len(corpus):] + corpus[:offset % len(corpus)]
        return enc.decode((data * (count // len(data) + 1))[:count], errors="ignore")

    generated = []
    def write_text(name, text):
        (directory / name).write_text(text, encoding="utf-8")
        generated.append(name)
    def write_json(name, doc):
        save(directory / name, doc)
        generated.append(name)

    # 1M 检索：公开文本重复扩展，头、中、尾放置不同随机答案。
    target = c["target_context_tokens"]
    answers = ["".join(rng.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=24)) for _ in range(3)]
    long_text = (f"KEY_1={answers[0]}\n" + text_tokens(target // 2)
                 + f"\nKEY_2={answers[1]}\n" + text_tokens(target - target // 2, len(corpus) // 3)
                 + f"\nKEY_3={answers[2]}\nReturn the exact values of KEY_1, KEY_2 and KEY_3 only.")
    write_text("context-1m.txt", long_text)
    write_json("context.public.json", dict(fixture_type="public_context_retrieval",
        body={"messages": [{"role": "user", "content": "fixture-text://context-1m.txt"}]},
        expected_contains=answers, encoding=encoding_name, local_content_tokens=len(enc.encode(long_text))))

    image = {"type": "image_url", "image_url": {"url": "fixture://fruits.jpg"}}
    video = {"type": "video_url", "video_url": {"url": "fixture://sample-5s-360p.mp4"}}
    write_json("body.public.json", dict(fixture_type="public_media_body", fill_media_to_bytes=True,
        body={"messages": [{"role": "user", "content": [
            {"type": "text", "text": "This is a payload acceptance test. Reply exactly BODY-OK."}, video, image]}]},
        expected_contains=["BODY-OK"]))

    # 预留约20%的Token负载余量给排空。共享前缀比例属于合成测试配置，绝不声称是业务事实。
    per_request = 80_000
    count = max(c["min_samples"], math.ceil(c["target_tpm"] * c["duration_seconds"] / 60 * 1.12 / per_request))
    if count + c["context_repeats"] + c["body_repeats"] > c["max_requests"]:
        raise ValueError("自动负载计划超过 max_requests；请增加上限或缩短窗口")
    groups, shared_tokens, tail_tokens = 8, 72_000, 8_000
    for i in range(groups):
        write_text(f"prefix-{i}.txt", f"PUBLIC BENCHMARK SHARED GROUP {i}. Reference book follows.\n" + text_tokens(shared_tokens, i * 997))
    media_counts = {"text": 0, "image": 0, "video": 0, "mixed": 0}
    layout = ["text", "text", "image", "image", "image", "video", "video", "video", "mixed", "mixed"]
    with (directory / "traffic.public.jsonl").open("w", encoding="utf-8") as f:
        for i in range(count):
            mode = layout[i % len(layout)]
            media_counts[mode] += 1
            content = [{"type": "text", "text": f"UNIQUE REQUEST {i:06d}. Additional reference:\n" + text_tokens(tail_tokens, i * 193)
                        + "\nPerformance test: inspect the supplied media if present and reply exactly LOAD-OK."}]
            if mode in ("image", "mixed"):
                content.append(image)
            if mode in ("video", "mixed"):
                content.append(video)
            row = dict(offset_seconds=i * c["duration_seconds"] / (count - 1), fixture_type="public_synthetic",
                       body={"messages": [{"role": "system", "content": f"fixture-text://prefix-{i % groups}.txt"},
                                          {"role": "user", "content": content}]}, expected_contains=["LOAD-OK"])
            f.write(dumps(row) + "\n")
    generated.append("traffic.public.jsonl")
    files = []
    for name in list(SOURCES) + generated:
        path = directory / name
        item = dict(file=name, bytes=path.stat().st_size, sha256=digest(path))
        if name in SOURCES:
            item.update(SOURCES[name])
        files.append(item)
    manifest = dict(schema_version="1.0", prepared_at=now(), dataset_kind="public_synthetic",
        seed=20261015, tokenizer=encoding_name, files=files,
        context_local_tokens=len(enc.encode(long_text)), requests=count, modality_counts=media_counts,
        shared_prefix_groups=groups, shared_prefix_target_tokens=shared_tokens,
        unique_tail_target_tokens=tail_tokens, estimated_text_tpm=count * per_request * 60 / c["duration_seconds"],
        note="公开语料重复扩展；8组共享前缀约90%，20%纯文本/30%图片/30%视频/20%图片视频同请求。不代表任何真实业务分布或缓存命中率。")
    save(directory / "materials_manifest.json", manifest)
    public_config = dict(c, cases=["body_100mb", "context_1m", "synthetic_traffic", "deployment"],
        asset_dir="fixtures/public", context_file="fixtures/public/context.public.json",
        traffic_file="fixtures/public/traffic.public.jsonl", body_file="fixtures/public/body.public.json",
        materials_manifest="fixtures/public/materials_manifest.json", media_transport="inline")
    save(root / "config.public.json", public_config)
    print(dumps(dict(config=str((root / "config.public.json").resolve()), requests=count,
                     modality_counts=media_counts, context_local_tokens=manifest["context_local_tokens"],
                     note="素材已准备。传入端点、模型和密钥即可运行；不需自行寻找或改写素材。")), flush=True)
