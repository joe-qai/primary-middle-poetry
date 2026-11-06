#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
墨韵 · 朗读音频生成脚本

把 data.js 中的篇目用 edge-tts 合成为 mp3，并输出逐句时间轴。
产物全部落在仓库 audio/ 目录，页面上线后只读这些静态文件，运行时不联网。

用法：
    python tools/gen_audio.py --ids p001,w001,c001
    python tools/gen_audio.py --kind poem --level primary
    python tools/gen_audio.py --kind prose
    python tools/gen_audio.py --all
    python tools/gen_audio.py --ids p001 --force

依赖：pip install edge-tts
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import edge_tts

ROOT = Path(__file__).resolve().parent.parent
DATA_JS = ROOT / "data.js"
AUDIO_DIR = ROOT / "audio"
INDEX_JSON = AUDIO_DIR / "index.json"

DEFAULT_VOICE = "zh-CN-YunjianNeural"
DEFAULT_RATE = "-10%"

# 与前端 bodyText / isProseLayout 保持一致
PROSE_KINDS = {"prose", "story", "classics"}
# 句末标点：edge-tts 的 SentenceBoundary 只在 。！？!? 处断句
SENT_END = "。！？!?"

DYN_SINGLE = set("唐宋元明清汉秦晋隋魏")
NOISE_CHARS = "《》〈〉「」『』“”‘’()（）[]【】"


# ---------------------------------------------------------------- data.js

def load_items():
    """读取 data.js 里的 window.POETRY_DATA。"""
    raw = DATA_JS.read_text(encoding="utf-8")
    key = "window.POETRY_DATA"
    start = raw.index(key)
    start = raw.index("{", start)
    depth, i, in_str, quote, esc = 0, start, False, "", False
    while i < len(raw):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
        else:
            if ch in "\"'":
                in_str, quote = True, ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        i += 1
    data = json.loads(raw[start:i + 1])

    items = []
    for kind, field in (
        ("poem", "poems"),
        ("prose", "prose"),
        ("story", "stories"),
        ("extra", "extras"),
        ("classics", "classics"),
    ):
        for x in data.get(field) or []:
            item = dict(x)
            item["_kind"] = kind
            items.append(item)
    return items


def is_prose_layout(item):
    if item["_kind"] in PROSE_KINDS:
        return True
    if item["_kind"] == "extra":
        return bool(item.get("content")) and len(item["content"]) > len(item.get("pinyin") or "")
    return False


def body_text(item):
    if is_prose_layout(item):
        return item.get("content") or item.get("pinyin") or item.get("original") or ""
    return item.get("pinyin") or item.get("original") or item.get("content") or ""


def strip_ann(s):
    return re.sub(r"\{[^}]*\}", "", s or "")


# ---------------------------------------------------------------- 文本处理

def tts_clean(s):
    s = strip_ann(s)
    for ch in NOISE_CHARS:
        s = s.replace(ch, "")
    s = s.replace("·", "，")
    s = re.sub(r"[，、；：]{2,}", "，", s)
    s = re.sub(r"[ \t\u3000]+", "", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def dynasty_text(d):
    t = tts_clean(d)
    if not t:
        return ""
    return t + "代" if t in DYN_SINGLE else t


def split_sentences(body):
    """与前端 splitRawSentences 完全一致：按行、按句末标点切分。"""
    out = []
    for line in body.splitlines():
        buf = ""
        for ch in line:
            buf += ch
            if ch in SENT_END:
                if buf.strip():
                    out.append(buf.strip())
                buf = ""
        if buf.strip():
            out.append(buf.strip())
    return out


def compose(item):
    """拼出朗读文本，返回 (text, 头部行数, 正文句数)。"""
    title = tts_clean(item.get("title"))
    who = "，".join(x for x in (dynasty_text(item.get("dynasty")), tts_clean(item.get("author"))) if x)
    body = tts_clean(body_text(item))
    header = [x for x in (title, who) if x]
    text = "\n".join(header + ([body] if body else []))
    return text, len(header), len(split_sentences(body))


# ---------------------------------------------------------------- 合成

async def synth(item, voice, rate, out_mp3, retries=3):
    text, hdr, body_count = compose(item)
    if not text:
        raise ValueError("empty text")

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            cues = []
            audio = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
                elif chunk["type"] == "SentenceBoundary":
                    cues.append({
                        "t": round(chunk["offset"] / 1e7, 3),
                        "d": round(chunk["duration"] / 1e7, 3),
                        "s": chunk["text"],
                    })
            if not audio:
                raise RuntimeError("no audio received")
            out_mp3.write_bytes(bytes(audio))
            dur = round(max(c["t"] + c["d"] for c in cues), 3) if cues else 0.0
            meta = {
                "id": item["id"],
                "title": item.get("title", ""),
                "voice": voice,
                "rate": rate,
                "dur": dur,
                "hdr": hdr,
                "bodyCues": body_count,
                "cues": cues,
            }
            return meta
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"{item['id']} 合成失败：{last_err}")


async def run(args):
    items = load_items()
    if args.kind:
        items = [x for x in items if x["_kind"] in set(args.kind.split(","))]
    if args.level:
        items = [x for x in items if x.get("level") in set(args.level.split(","))]
    if args.ids:
        wanted = [x.strip() for x in args.ids.split(",") if x.strip()]
        by_id = {x["id"]: x for x in items}
        items = [by_id[i] for i in wanted if i in by_id]
    if args.limit:
        items = items[: args.limit]
    if not items:
        print("没有匹配到篇目")
        return 1

    AUDIO_DIR.mkdir(exist_ok=True)
    index = {"voice": args.voice, "rate": args.rate, "items": {}}
    if INDEX_JSON.exists():
        try:
            old = json.loads(INDEX_JSON.read_text(encoding="utf-8"))
            index.update({k: v for k, v in old.items() if k not in ("items",)})
            index["items"] = old.get("items") or {}
        except Exception:  # noqa: BLE001
            pass
    if args.force:
        for x in items:
            index["items"].pop(x["id"], None)

    sem = asyncio.Semaphore(max(1, args.workers))
    done = {"ok": 0, "skip": 0, "fail": 0}
    total = len(items)

    async def one(item):
        async with sem:
            mp3 = AUDIO_DIR / f"{item['id']}.mp3"
            meta_path = AUDIO_DIR / f"{item['id']}.json"
            if not args.force and mp3.exists() and meta_path.exists():
                done["skip"] += 1
                print(f"[skip] {item['id']} {item.get('title')}")
                return
            t0 = time.time()
            try:
                meta = await synth(item, args.voice, args.rate, mp3)
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
                index["items"][item["id"]] = {
                    "dur": meta["dur"],
                    "hdr": meta["hdr"],
                    "bodyCues": meta["bodyCues"],
                }
                done["ok"] += 1
                warn = ""
                if len(meta["cues"]) - meta["hdr"] != meta["bodyCues"]:
                    warn = f" ⚠ cue {len(meta['cues']) - meta['hdr']} != body {meta['bodyCues']}"
                print(f"[ok]   {item['id']} {item.get('title')} {meta['dur']:.1f}s "
                      f"{mp3.stat().st_size // 1024}KB {time.time() - t0:.1f}s{warn}")
            except Exception as exc:  # noqa: BLE001
                done["fail"] += 1
                print(f"[fail] {item['id']} {item.get('title')}: {exc}", file=sys.stderr)

    await asyncio.gather(*(one(x) for x in items))

    index["count"] = len(index["items"])
    index["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    INDEX_JSON.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n完成 {done['ok']} 篇，跳过 {done['skip']} 篇，失败 {done['fail']} 篇 / 共 {total} 篇")
    print(f"索引：{INDEX_JSON.relative_to(ROOT)}  共 {index['count']} 篇")
    return 0 if done["fail"] == 0 else 2


def main():
    p = argparse.ArgumentParser(description="生成古诗词朗读音频与逐句时间轴")
    p.add_argument("--ids", help="指定篇目 id，逗号分隔")
    p.add_argument("--kind", help="poem,prose,story,extra,classics")
    p.add_argument("--level", help="primary,middle（仅诗词有效）")
    p.add_argument("--limit", type=int, help="只取前 N 篇")
    p.add_argument("--all", action="store_true", help="全量生成")
    p.add_argument("--force", action="store_true", help="覆盖已存在的音频")
    p.add_argument("--voice", default=DEFAULT_VOICE)
    p.add_argument("--rate", default=DEFAULT_RATE)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args()
    if not any((args.ids, args.kind, args.level, args.limit, args.all)):
        p.print_help()
        return 1
    return asyncio.run(run(args)) or 0


if __name__ == "__main__":
    sys.exit(main())