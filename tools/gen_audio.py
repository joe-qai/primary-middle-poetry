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

依赖：pip install edge-tts numpy parselmouth
      （ffmpeg 需带 rubberband；parselmouth 仅《咏鹅》音高修正用到，缺失时自动跳过）
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import edge_tts

try:  # 表达力增强（分句拼接）需要 numpy；缺失时自动退回整句合成
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

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


# 「而」在连读中容易被语音引擎吞掉：前文连续 6 字以上时补一个逗号，强制读出。
# 只作用于送入 TTS 的文本，页面展示与时间轴切分不受影响。
PAUSE_CHARS = set("，。！？；：、,.!?;:\n…—～「」『』“”‘’()（）《》〈〉【】[] \t")


def pause_before_er(s):
    out = []
    run = 0
    for ch in s:
        if ch == "而" and run >= 6:
            out.append("，")
            run = 0
        out.append(ch)
        if ch in PAUSE_CHARS:
            run = 0
        else:
            run += 1
    return "".join(out)


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


def compose(item, join_header=False):
    """拼出朗读文本，返回 (text, 头部行数, 正文句数)。"""
    title = tts_clean(item.get("title"))
    who = "，".join(x for x in (dynasty_text(item.get("dynasty")), tts_clean(item.get("author"))) if x)
    body = pause_before_er(tts_clean(body_text(item)))
    header = [x for x in (title, who) if x]
    if join_header and len(header) > 1:
        # 单字标题单独起句容易硬起/破音，连成一句读更顺
        text = "\n".join(["，".join(header)] + ([body] if body else []))
    else:
        text = "\n".join(header + ([body] if body else []))
    return text, len(header), len(split_sentences(body))


# ---------------------------------------------------------------- 表达力增强

AUDIO_SR = 24000  # edge-tts 固定输出 24kHz 单声道 48kbps

# 重复单字开头的分句增强（音高 / 节奏）。
#
# 《咏鹅》开头三个「鹅」的问题：edge-tts 按「列举语调」会把每个「鹅」读成
# 降调（≈第四声 è），听感像「饿、饿、饿」。这里不再用整段合成，而是：
#   1. 合成一个把「鹅」放在自然第二声位置的载体句（「鹅的毛」），
#   2. 用 WordBoundary 精确切出这个「鹅」（实测 F0 137→172Hz，真阳平），
#   3. 复制三份，做轻微的音高/时长变化后按正常停顿拼回全篇。
# 这样每个「鹅」都是模型自己读出的第二声，没有变调失真，也不拖尾。
EXPR_OPENINGS = {
    "p001": {
        "match": "鹅，鹅，鹅，",
        "text": "鹅",
        "mode": "carrier",
        "carrier": "鹅的毛",     # 这个上下文里「鹅」是完整的第二声
        "carrier_rate": "-40%", # 放慢载体句，切出的单字约 0.23s，不显得仓促
        "detune": [1.00, 1.02, 0.98],
        "tempo": [1.00, 1.00, 0.90],
        "stress": 1.05,          # 相对正文的音量，轻微重读
        "gaps": [0.33, 0.31],
        "lead_gap": 0.35,
        "tail_gap": 0.38,
    },
}


def _tool(name):
    """找 ffmpeg/ffprobe：环境变量 → PATH → 本机常见安装路径。"""
    env = os.environ.get(name.upper())
    if env and Path(env).exists():
        return env
    exe = shutil.which(name)
    if exe:
        return exe
    suffix = ".exe" if os.name == "nt" else ""
    cand = Path(r"D:\ffmpeg-7.0.2-full_build\bin") / (name + suffix)
    return str(cand) if cand.exists() else None


def _decode(data):
    """mp3 字节 → float 采样（24kHz 单声道）。"""
    ff = _tool("ffmpeg")
    proc = subprocess.run(
        [ff, "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(AUDIO_SR), "pipe:1"],
        input=data, capture_output=True, check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float64) / 32768.0


def _encode(samples):
    """float 采样 → 48kbps mp3 字节（与 edge-tts 输出一致）。"""
    ff = _tool("ffmpeg")
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    proc = subprocess.run(
        [ff, "-v", "error", "-f", "s16le", "-ac", "1", "-ar", str(AUDIO_SR), "-i", "pipe:0",
         "-b:a", "48k", "-f", "mp3", "pipe:1"],
        input=pcm, capture_output=True, check=True,
    )
    return proc.stdout


def _sil(seconds):
    return np.zeros(int(round(seconds * AUDIO_SR)), dtype=np.float64)


# 标题与作者连读的篇目（单字标题硬起容易破音）
JOIN_HEADER = {"p006"}

# 单篇朗读参数覆盖（音色 / 语速 / 音高）。
# 《咏鹅》是一年级的活泼小诗，云健偏低沉，单独换成「云希」（Lively, Sunshine），
# 语速略提、音高略抬，读起来更朝气；其余篇目仍用全站默认的云健。
SPEECH_OVERRIDES = {
    "p001": {"voice": "zh-CN-YunxiNeural", "rate": "+0%", "pitch": "+0Hz"},
}


def split_header_cue(audio, cue):
    """把「标题，朝代，作者」这一条 cue 按第一个明显停顿切成两段。"""
    x = _decode(audio)
    a = max(0, int(cue["t"] * AUDIO_SR))
    b = min(len(x), int((cue["t"] + cue["d"]) * AUDIO_SR))
    seg = x[a:b]
    win = int(0.01 * AUDIO_SR)
    n = len(seg) // win
    mid = None
    if n > 10:
        energy = np.sqrt((seg[: n * win].reshape(n, win) ** 2).mean(axis=1))
        voiced = np.where(energy > 0.012)[0]
        run = None
        for i in range(int(voiced[0]) if len(voiced) else 0, n):
            if energy[i] < 0.012:
                if run is None:
                    run = i
            else:
                if run is not None and (i - run) >= 12:  # ≥120ms 的停顿
                    mid = cue["t"] + (run + (i - run) / 2) * 0.01
                    break
                run = None
    if mid is None:  # 没找到停顿就按字数估
        mid = cue["t"] + cue["d"] * 0.25
    first = {"t": cue["t"], "d": round(max(0.1, mid - cue["t"]), 3), "s": cue["s"].split("，")[0]}
    second = {"t": round(mid, 3),
              "d": round(max(0.1, cue["t"] + cue["d"] - mid), 3),
              "s": cue["s"]}
    return [first, second]


def _trim(x, th=0.012, pad=0.03):
    """去掉首尾静音，返回 (新采样, 被裁掉的头部时长秒数)。"""
    win = int(0.01 * AUDIO_SR)
    if len(x) < win * 2:
        return x, 0.0
    n = len(x) // win
    energy = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(axis=1))
    idx = np.where(energy > th)[0]
    if not len(idx):
        return x, 0.0
    a = max(0, int(idx[0] * win - pad * AUDIO_SR))
    b = min(len(x), int((idx[-1] + 1) * win + pad * AUDIO_SR))
    return x[a:b], a / AUDIO_SR


def _rubber(x, tmpdir, pitch=1.0, tempo=1.0):
    """rubberband 变调/变速（保持共振峰，不改变音色）。"""
    import wave

    ff = _tool("ffmpeg")
    src = Path(tmpdir) / "in.wav"
    dst = Path(tmpdir) / "out.wav"
    w = wave.open(str(src), "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(AUDIO_SR)
    w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    w.close()
    subprocess.run([ff, "-y", "-v", "error", "-i", str(src),
                    "-af", f"rubberband=pitch={pitch:.4f}:tempo={tempo:.4f}", str(dst)], check=True)
    w = wave.open(str(dst), "rb")
    y = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64) / 32768.0
    w.close()
    return y


def _xfade(a, b, fade):
    """等功率交叉淡化拼接。"""
    fade = min(fade, len(a), len(b))
    if fade < 8:
        return np.concatenate([a, b])
    t = np.linspace(0, 1, fade)
    mix = a[-fade:] * np.cos(t * np.pi / 2) + b[:fade] * np.sin(t * np.pi / 2)
    return np.concatenate([a[:-fade], mix, b[fade:]])


def _bend(x, k0, k1, slices, tmpdir):
    """分片变调：系数从 k0 线性升到 k1，片间交叉淡化，音高曲线变平滑上扬。"""
    bounds = np.linspace(0, len(x), slices + 1).astype(int)
    fade = int(0.02 * AUDIO_SR)
    out = None
    for i in range(slices):
        a = max(0, bounds[i] - fade)
        b = min(len(x), bounds[i + 1] + fade)
        if b - a < int(0.02 * AUDIO_SR):
            continue
        k = k0 + (k1 - k0) * (i + 0.5) / max(1, slices - 1)
        try:
            seg = _rubber(x[a:b], tmpdir, pitch=k)
        except Exception:  # noqa: BLE001  极短片段直接跳过变调
            seg = x[a:b]
        out = seg if out is None else _xfade(out, seg, fade)
    return x if out is None else out


def _with_tail(x, factor, start=0.58, tmpdir=None, src=(0.50, 0.85), target=0.038, fade=0.18):
    """取字尾元音拉长成持续音（补增益 + 淡出），做出真正听得见的拖尾。"""
    if factor <= 1.01:
        return x
    s = int(len(x) * start)
    a, b = int(len(x) * src[0]), int(len(x) * src[1])
    if b - a < int(0.02 * AUDIO_SR):
        return x
    try:
        tail = _rubber(x[a:b], tmpdir, tempo=1.0 / factor)
    except Exception:  # noqa: BLE001
        return x
    rms = float(np.sqrt(np.mean(tail ** 2)))
    if rms > 1e-5:
        tail = tail * min(3.5, target / rms)
    f = int(fade * AUDIO_SR)
    if f < len(tail):
        f = min(f, int(0.45 * len(tail)))
        tail[-f:] *= np.linspace(1, 0, f) ** 1.3
    fi = int(0.03 * AUDIO_SR)
    tail[:fi] *= np.linspace(0, 1, fi)
    return _xfade(x[:s], tail, int(0.03 * AUDIO_SR))


def _speech_rms(x, win=0.01):
    """取有声帧的中位 RMS，用来把拼接的单字音量对齐到正常语句。"""
    w = max(1, int(win * AUDIO_SR))
    n = len(x) // w
    if not n:
        return 0.0
    e = np.sqrt((np.asarray(x[: n * w]).reshape(n, w) ** 2).mean(axis=1))
    e = e[e > max(1e-4, e.max() * 0.15)]
    return float(np.median(e)) if len(e) else float(np.sqrt(np.mean(np.asarray(x) ** 2)))


def _pitch_curve(x, f_start, f_end, tmpdir=None):
    """用 Praat Manipulation 把整段音节的基频改成 f_start → f_end 的上扬曲线。

    与分片 rubberband 不同，这里重写整条 PitchTier 再做 PSOLA 重合成：
    共振峰与时长都保持原样，只改音高走向，听感更接近真人把字读成第二声。
    缺 parselmouth 时原样返回。
    """
    if np is None:
        return x
    try:
        import parselmouth
        from parselmouth import praat
    except ImportError:  # pragma: no cover
        return x

    x = np.asarray(x, dtype=np.float64)
    try:
        snd = parselmouth.Sound(x, sampling_frequency=AUDIO_SR)
        dur = float(snd.get_total_duration())
        manip = praat.call(snd, "To Manipulation", 0.01, 70, 500)
        tier = praat.call(manip, "Extract pitch tier")
        praat.call(tier, "Remove points between", 0.0, dur + 1.0)
        # 只改有声段（首尾各留 4%），线性上升
        a, b = dur * 0.04, dur * 0.96
        for i in range(6):
            frac = i / 5.0
            praat.call(tier, "Add point", a + (b - a) * frac,
                       f_start + (f_end - f_start) * frac)
        praat.call([manip, tier], "Replace pitch tier")
        out = praat.call(manip, "Get resynthesis (overlap-add)")
        y = np.asarray(out.values[0], dtype=np.float64)
    except Exception:  # noqa: BLE001  Praat 调用失败就退回原音
        return x

    if len(y) >= len(x):
        return y[:len(x)]
    return np.pad(y, (0, len(x) - len(y)))


async def speak(text, voice, rate, pitch="+0Hz", volume="+0%", retries=3):
    """合成一段文本，返回 (mp3 字节, cues)。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
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
            return bytes(audio), cues
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"合成失败：{last_err}")


async def speak_words(text, voice, rate, pitch="+0Hz", retries=3):
    """按词边界合成，返回 (mp3 字节, [{t,d,s}])。保留给需要精确词边界的场景。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            communicate = edge_tts.Communicate(
                text, voice, rate=rate, pitch=pitch, boundary="WordBoundary"
            )
            words = []
            audio = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    words.append({
                        "t": chunk["offset"] / 1e7,
                        "d": chunk["duration"] / 1e7,
                        "s": chunk["text"],
                    })
            if not audio:
                raise RuntimeError("no audio received")
            return bytes(audio), words
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"合成失败：{last_err}")


def _first_syllable(x, thresh=0.28, hold=3, pad=0.012):
    """切出一段语音的第一个音节：首峰之后能量跌到峰值 28% 并保持 15ms 处收尾。

    edge-tts 的 WordBoundary 偏移和真实波形有约 0.1s 的错位，直接按能量切更稳。
    """
    win = int(0.005 * AUDIO_SR)
    n = len(x) // win
    if not n:
        return np.asarray(x, dtype=np.float64)
    e = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(axis=1))
    peak = int(np.argmax(e))
    k, run = peak, 0
    while k < n:
        if e[k] < e[peak] * thresh:
            run += 1
            if run >= hold:
                break
        else:
            run = 0
        k += 1
    end = min(len(x), int((k - run + 1) * win + pad * AUDIO_SR))
    seg = np.asarray(x[:end], dtype=np.float64).copy()
    m = min(int(0.012 * AUDIO_SR), len(seg) // 2)
    if m > 1:
        seg[-m:] *= np.linspace(1.0, 0.0, m)
    nf = min(int(0.006 * AUDIO_SR), len(seg) // 2)
    if nf > 1:
        seg[:nf] *= np.linspace(0.0, 1.0, nf)
    return seg


async def synth_expr(item, voice, rate, out_mp3, spec):
    """分句拼接版：重复字开头做音高/节奏起伏，其余部分照常合成。"""
    title = tts_clean(item.get("title"))
    who = "，".join(x for x in (dynasty_text(item.get("dynasty")), tts_clean(item.get("author"))) if x)
    header = [x for x in (title, who) if x]
    body = pause_before_er(tts_clean(body_text(item)))
    opening = spec["match"]
    if not body.startswith(opening):
        return await synth_plain(item, voice, rate, out_mp3)

    rest = body[len(opening):].lstrip("\n")
    head_bytes, head_cues = await speak("\n".join(header), voice, rate)
    rest_bytes, rest_cues = await speak(rest, voice, rate)

    head_x, head_lead = _trim(_decode(head_bytes))
    rest_x, rest_lead = _trim(_decode(rest_bytes))

    n_syl = max(1, opening.count(spec.get("text") or opening))
    detune = list(spec.get("detune") or [1.0])[:n_syl]
    while len(detune) < n_syl:
        detune.append(1.0)
    tempo_list = list(spec.get("tempo") or [1.0])[:n_syl]
    while len(tempo_list) < n_syl:
        tempo_list.append(1.0)

    with tempfile.TemporaryDirectory() as tmpdir:
        if spec.get("mode") == "carrier":
            # 从载体句里切出模型自己读出的第二声单字，再轻微变调复制
            carrier_bytes, _ = await speak(
                spec["carrier"], voice, spec.get("carrier_rate", rate)
            )
            carrier_x, _ = _trim(_decode(carrier_bytes), pad=0.0)
            base = _first_syllable(carrier_x)
            if len(base) < int(0.08 * AUDIO_SR):
                return await synth_plain(item, voice, rate, out_mp3)
            ref = _speech_rms(rest_x)
            stress = spec.get("stress", 1.05)
            seg_x = []
            for m, tp in zip(detune, tempo_list):
                if abs(m - 1) < 0.001 and abs(tp - 1) < 0.001:
                    y = base.copy()
                else:
                    y = _rubber(base, tmpdir, pitch=m, tempo=tp)
                r = _speech_rms(y)
                if r > 1e-6 and ref > 1e-6:
                    y = y * (ref * stress / r)
                seg_x.append(y)
        elif spec.get("mode") == "tone2":
            base_bytes, _ = await speak(spec["text"], voice, spec["rate"])
            base_x, _ = _trim(_decode(base_bytes))
            # 正常朗诵：每个字都重读成第二声（升调），不加拖尾、不拆停顿
            f0_start, f0_end = spec["f0"]
            stress = spec.get("stress", 1.12)
            ref = _speech_rms(rest_x)
            seg_x = []
            for m in detune:
                y = _pitch_curve(base_x, f0_start * m, f0_end * m)
                r = _speech_rms(y)
                if r > 1e-6 and ref > 1e-6:
                    y = y * (ref * stress / r)
                seg_x.append(y)
        else:
            base_bytes, _ = await speak(spec["text"], voice, spec["rate"])
            base_x, _ = _trim(_decode(base_bytes))
            k0, k1 = spec["bend"]
            seg = _with_tail(_bend(base_x, k0, k1, spec.get("slices", 10), tmpdir),
                             spec.get("tail", 1.0), spec.get("tail_from", 0.50), tmpdir,
                             src=tuple(spec.get("tail_src") or (0.50, 0.85)),
                             target=spec.get("tail_level", 0.038),
                             fade=spec.get("tail_fade", 0.18))
            seg_x = []
            for m in detune:
                if abs(m - 1) < 0.001:
                    seg_x.append(seg)
                else:
                    seg_x.append(_rubber(seg, tmpdir, pitch=m))

    gaps = spec.get("gaps") or [0.20] * (len(seg_x) - 1)
    parts = [head_x, _sil(spec.get("lead_gap", 0.28))]
    open_start = sum(len(p) for p in parts) / AUDIO_SR
    for i, x in enumerate(seg_x):
        parts.append(x)
        if i < len(gaps):
            parts.append(_sil(gaps[i]))
    parts.append(_sil(spec.get("tail_gap", 0.34)))
    rest_start = sum(len(p) for p in parts) / AUDIO_SR
    parts.append(rest_x)

    audio = _encode(np.concatenate(parts))
    out_mp3.write_bytes(audio)
    total = sum(len(p) for p in parts) / AUDIO_SR

    cues = [{"t": round(max(0.0, c["t"] - head_lead), 3), "d": c["d"], "s": c["s"]} for c in head_cues]
    for i, c in enumerate(rest_cues):
        # 第一句的 cue 从开头第一个「鹅」算起，覆盖到「曲项向天歌。」结束，
        # 这样前端逐句高亮的句数仍与正文一致。
        t = rest_start + c["t"] - rest_lead
        if i == 0:
            cues.append({"t": round(open_start, 3),
                         "d": round(max(0.0, t + c["d"] - open_start), 3),
                         "s": opening + c["s"]})
        else:
            cues.append({"t": round(t, 3), "d": c["d"], "s": c["s"]})

    # edge-tts 的末句 cue 会把尾部静音也算进去，这里按真实音频裁掉，
    # 同时避免相邻 cue 重叠。
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["t"] if i + 1 < len(cues) else total
        c["d"] = round(max(0.05, min(c["t"] + c["d"], nxt) - c["t"]), 3)

    return {
        "id": item["id"],
        "title": item.get("title", ""),
        "voice": voice,
        "rate": rate,
        "pitch": "+0Hz",
        "dur": round(total, 3),
        "hdr": len(header),
        "bodyCues": len(rest_cues),
        "v": hashlib.md5(audio).hexdigest()[:8],
        "cues": cues,
    }


# ---------------------------------------------------------------- 合成

async def synth(item, voice, rate, out_mp3):
    ov = SPEECH_OVERRIDES.get(item["id"]) or {}
    voice = ov.get("voice", voice)
    rate = ov.get("rate", rate)
    pitch = ov.get("pitch", "+0Hz")
    spec = EXPR_OPENINGS.get(item["id"])
    if spec and np is not None and _tool("ffmpeg"):
        return await synth_expr(item, voice, rate, out_mp3, spec)
    return await synth_plain(item, voice, rate, out_mp3, pitch)


async def synth_plain(item, voice, rate, out_mp3, pitch="+0Hz"):
    join = item["id"] in JOIN_HEADER
    text, hdr, body_count = compose(item, join_header=join)
    if not text:
        raise ValueError("empty text")
    audio, cues = await speak(text, voice, rate, pitch)
    out_mp3.write_bytes(audio)
    if join and hdr > 1 and cues:
        cues = split_header_cue(audio, cues[0]) + cues[1:]
    dur = round(max(c["t"] + c["d"] for c in cues), 3) if cues else 0.0
    return {
        "id": item["id"],
        "title": item.get("title", ""),
        "voice": voice,
        "rate": rate,
        "pitch": pitch,
        "dur": dur,
        "hdr": hdr,
        "bodyCues": body_count,
        "v": hashlib.md5(audio).hexdigest()[:8],
        "cues": cues,
    }


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
                    "v": meta["v"],
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
    index.pop("updated", None)   # 索引里不写年份/时间戳，避免出现具体年份
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