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
# 句中分隔标点后需要插入停顿——单独合成确保有准确时间戳
SENT_SPLIT = "，；"


def _pause_at(line):
    """根据诗句字数返回[PAUSE]应插入的位置（从行首算起）。

    五言：位置2（「二/三」节奏）
    七言：位置4（「四/三」节奏）
    八言含逗号：逗号后（让逗号本身成为天然停顿点，不在中间切断）
    其他情况：按标准规则
    """
    import re
    # 如果包含中文逗号/分号，优先在标点后切分（保留逗号作为语义分隔）
    comma_pos = line.find('，')
    semi_pos = line.find('；')
    if comma_pos != -1 and comma_pos < len(line) - 1:
        # 逗号/分号前插入停顿（逗号是自然断点，不需要额外PAUSE标记）
        return comma_pos
    if semi_pos != -1 and semi_pos < len(line) - 1:
        return semi_pos
    ch_count = len(line)
    if ch_count == 5:
        return 2
    elif ch_count == 7:
        return 4
    elif ch_count == 8:
        return 4
    elif ch_count == 4:
        return 2
    elif 9 <= ch_count <= 12:
        return 4
    elif ch_count > 12:
        return 4
    return None


def pause_in_poem_lines(text):
    """为诗词类每行按意义单元插入 [PAUSE] 标记，供 speak_segmented 分段合成时使用。

    处理策略：所有含逗号的分句段，无句末标点的部分统一按字数规则插单个 [PAUSE]。
    - 七言（7字）：四三节奏，位置4，如"无限风光[PAUSE]尽被占"
    - 五言（5字）：二三节奏，位置2，如"众鸟高[PAUSE]飞尽"
    - 含句末标点的完整句子：不插 PAUSE（edge-tts SentenceBoundary 已自然断句）

    示例：
      "出师未捷身先死，长使英雄泪满襟。"
        → 第一段"出师未捷身先死"（7字）→ "出师未捷[PAUSE]身先死"
        → 第二段"长使英雄泪满襟。"（含句末标点）→ 原样
        → 结果："出师未捷[PAUSE]身先死，长使英雄泪满襟。"
      "不论平地与山尖，无限风光尽被占。"
        → 两段各7字、均无句末标点 → 统一四三节奏
        → "不论平地[PAUSE]与山尖，无限风光[PAUSE]尽被占。"
    """
    SENT_END = set("。！？!?")
    lines = text.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        # 含逗號 / 分號的行：拆分後分别處理
        if '，' in stripped or '；' in stripped:
            import re
            parts = re.split(r'([，；])', stripped)
            segs = []
            i = 0
            while i < len(parts):
                seg = parts[i].strip()
                sep = parts[i + 1] if i + 1 < len(parts) else ''
                if seg:
                    segs.append((seg, sep))
                i += 2
            processed = []
            for seg_text, seg_sep in segs:
                ends_with_sent = seg_text[-1] in SENT_END if seg_text else False
                if ends_with_sent:
                    processed.append(seg_text + seg_sep)
                    continue
                pos = _pause_at(seg_text)
                if pos is None:
                    processed.append(seg_text + seg_sep)
                    continue
                pauseline = seg_text[:pos] + '[PAUSE]' + seg_text[pos:]
                processed.append(pauseline + seg_sep)
            # 保持原行缩进
            prefix = line[:len(line) - len(stripped)]
            reconstructed = ''.join(processed)
            out.append(prefix + reconstructed)
        else:
            # 无逗号的分号行，直接判断
            ends_with_sent = stripped and stripped[-1] in SENT_END
            if ends_with_sent:
                out.append(line)
                continue
            pos = _pause_at(stripped)
            if pos is None:
                out.append(line)
                continue
            prefix = line[:len(line) - len(stripped)]
            new_stripped = stripped[:pos] + '[PAUSE]' + stripped[pos:]
            out.append(prefix + new_stripped)
    return '\n'.join(out)

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
        ("foreign", "foreign_poems"),
        ("modern", "modern_poems"),
        ("poem_hs", "highschool_poems"),
    ):
        for x in data.get(field) or []:
            item = dict(x)
            item["_kind"] = kind
            if kind == "poem_hs":
                item["level"] = "highschool"
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
PAUSE_CHARS = set("，。！？；：、,.!?;:\n…—～「」『』""''()（）《》〈〉【】[] \t")


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


# 中文 TTS 偶发错误合并词边界问题修复。
#
# edge-tts / Microsoft TTS 的中文分词器对以下情况容易出错：
#   1. 单字名词+常见动词/连词 → 错误合并为一个词
#      例：佛于是 → 错误切成「佛是 / 于」而非「佛 / 于是」
#      例：年岁 → 错误切成「年 / 岁」或合为「年纪」的读音
#   2. 专有名词+后续虚词 → 跨词合并
#      例：向晚 → 误读为地名「向晚」而非「向/晚」
#
# 解决思路（不修改 data.js）：
#   方案 A：在歧义词后插入可见逗号「佛，于是」→ 会导致朗读时出现不自然停顿
#   方案 B：插入不可见 Unicode 占位符 U+2060（Word Joiner）→ TTS 会在此处停顿
#           但渲染到页面时该字符不可见，audio/m10.json 的时间轴标注也恢复原文
#
# 方案 B 优势：数据干净、朗读自然、时间轴与页面原文一致。

# 触发位置：在该字符之后插入 U+2060，让 TTS 将其视为韵律边界
SILENT_BREAK_BEFORE = {
    # 佛 + 于是/是 → 避免读出「佛是」
    "佛": {"于", "是", "乎"},
    # 年/岁/朝/暮等时间词单字 + 易合并的后缀
    "年": {"纪", "代", "少"},
    "岁": {"月", "星", "华"},
    "朝": {"夕", "代", "暮", "阳"},
    "暮": {"朝", "春"},
}

# 多字词保护列表：这些组合不能被拆散，必须整体合成一整段
PROTECTED_WORDS = {
    "于是", "但是", "所以", "然而", "虽然", "如果",
    "然后", "至于", "关于", "对于", "因为", "以及",
    "忽然", "依然", "仍然", "正在", "能够", "可以",
}

def silence_break_before(s):
    """在已知歧义边界前插入零宽连接符（\\u2060），使 TTS 在此停顿但不影响显示文本。"""
    out = []
    for i, ch in enumerate(s):
        out.append(ch)
        if ch in SILENT_BREAK_BEFORE:
            # 检查后面是否紧跟易被错误合并的字（且不以保护词开头）
            nxt = s[i+1] if i+1 < len(s) else ""
            if nxt in SILENT_BREAK_BEFORE.get(ch, set()):
                out.append('\u2060')  # WORD JOINER：可见为空，TTS 视为停顿
    return "".join(out)


def protect_words_in_text(s):
    """用空格将受保护的复合词前后隔开，防止 TTS 将它们与上下文错误合并。
    注：edge-tts 对空格敏感度有限，此函数主要用于配合 silence_break_before 使用。"""
    for word in PROTECTED_WORDS:
        if word in s:
            # 不实际替换，仅在需要时对整体分段处理有参考意义
            pass
    return s


def pause_at_boundary(s):
    """综合边界修正：先处理沉默断点，再处理「而」吞音暂停，返回送入 TTS 的文本。
    输出中的 \\u2060 会在 time-annotator 还原时被剥离，因此 cues/s 保持原文。"""
    result = silence_break_before(s)
    result = pause_before_er(result)
    return result


# 专名 / 多音字：只替换送进 TTS 的文本，读出来后再把 cue 文本还原成页面原文。
#
# 规则：
#   1. 键和值**字数必须相同**，否则时间轴会错位；
#   2. 键要带上下文（至少两个字），避免把同篇里字音不同的同形字一起换掉；
#   3. 值（同音字）不能本来就是页面原文里的常见写法，否则还原时会误伤。
#
# 覆盖的是教材规定的古读 / 专名读音。引擎对多音字常按今读处理（例如「亡赖」读 wáng、
# 「朝奏」读 cháo），靠提示词无法稳定纠正，所以直接用同音字把它读对，页面显示不变。
PRONOUNCE = {
    # ---- 专名
    "张僧繇": "张僧由",   # 繇 yóu（教育部《重編國語辭典》注 zhāng sēng yóu），避免引擎改读 yáo

    # ---- 小学
    "草低见牛羊": "草低现牛羊",   # 见 xiàn
    "朝辞白帝": "招辞白帝",       # 朝 zhāo
    "亡赖": "无赖",               # 亡 wú
    "卧剥": "卧包",               # 剥 bāo
    "未肯降": "未肯详",           # 降 xiáng
    "朝雨": "招雨",               # 朝 zhāo（渭城朝雨）
    "轻骑": "轻记",               # 骑 jì
    "满蹊": "满西",               # 蹊 xī
    "坚劲": "坚静",               # 劲 jìng
    "重抖擞": "虫抖擞",           # 重 chóng
    "返景": "返影",               # 景 yǐng
    "藉秋风": "借秋风",           # 藉 jiè
    "才了": "才蓼",               # 了 liǎo
    "泊烟渚": "博烟渚",           # 泊 bó
    "忽见": "忽现",               # 见 xiàn
    "胡骑": "胡记",               # 骑 jì
    "旧时裳": "旧时常",           # 裳 cháng
    "夜泊": "夜博",               # 泊 bó
    "难着": "难酌",               # 着 zhuó
    "两骑": "两记",               # 骑 jì
    "系向": "记向",               # 系 jì
    "的卢": "敌卢",               # 的 dí
    "千骑": "千记",               # 骑 jì
    "朝奏": "招奏",               # 朝 zhāo
    "九重天": "九虫天",           # 重 chóng

    # ---- 初中诗词
    "春朝": "春招",               # 朝 zhāo（胜春朝）
    "候骑": "候记",               # 骑 jì
    "没马蹄": "墨马蹄",           # 没 mò
    "遗所思": "位所思",           # 遗 wèi
    "试倩": "试庆",               # 倩 qìng
    "挑兮": "掏兮",               # 挑 tāo
    "人省": "人醒",               # 省 xǐng
    "更著": "更酌",               # 著 zhuó
    "阿房": "婀旁",               # 阿房 ē páng
    "铁骑": "铁记",               # 骑 jì
    "千乘": "千剩",               # 乘 shèng
    "万骑": "万记",               # 骑 jì
    "万仞": "万任",               # 仞 rèn
    "会稽": "快基",               # 会稽 kuài jī
    "属客": "嘱客",               # 属 zhǔ
    "冯虚": "平虚",               # 冯 píng
    "佛狸": "必狸",               # 佛 bì
    "曾不能": "增不能",           # 曾 zēng

    # ---- 初中文言文（本轮新增篇目）
    "尊君在不": "尊君在否",       # 不 fǒu
    "身亡所寄": "身无所寄",       # 亡 wú
    "亡处亡气": "无处无气",
    "亡处亡块": "无处无块",
    "亡以应": "无以应",
    "舍然大喜": "释然大喜",       # 舍 shì
    "中伤": "众伤",               # 中 zhòng
    "跐蹈": "此蹈",               # 跐 cǐ
    "千转": "千啭",               # 转 zhuàn
    "缥碧": "飘碧",               # 缥 piāo
    "轩邈": "轩秒",               # 邈 miǎo
    "戾天": "利天",               # 戾 lì
    "往之女家": "往之汝家",       # 女 rǔ
    "丈夫之冠也": "丈夫之贯也",   # 冠 guàn
    "公孙衍": "公孙演",           # 衍 yǎn
    "差可拟": "叉可拟",           # 差 chā
    "之塞": "之涩",               # 塞 sè
    "荷担": "贺担",               # 荷 hè
    "箕畚": "基本",               # 畚 běn
    "始龀": "始趁",               # 龀 chèn
    "一厝": "一错",               # 厝 cuò
    "陇断": "垄断",               # 陇 lǒng
    "选贤与能": "选贤举能",       # 与 jǔ
    "矜、寡": "关、寡",           # 矜 guān
    "男有分": "男有份",           # 分 fèn
    "货恶其": "货误其",           # 恶 wù
    "力恶其": "力误其",
    "不教": "不叫",                   # 教 jiào（出塞：不教胡马度阴山）
    "人未还": "人未环",               # 还 huán（出塞）
    "终不还": "终不环",               # 还 huán（从军行）
    "相与还": "相与环",               # 还 huán（饮酒）
    "照我还": "照我环",               # 还 huán（泊船瓜洲）
    "一日还": "一日环",               # 还 huán（早发白帝城）
    "还就菊": "环就菊",               # 还 huán（过故人庄）
    "还过女墙": "环过女墙",           # 还 huán（石头城）
    "好读书": "号读书",               # 好 hào（五柳先生传：好读书，不求甚解）
}

for _k, _v in PRONOUNCE.items():
    if len(_k) != len(_v):
        raise ValueError(f"PRONOUNCE 键值字数不一致：{_k} -> {_v}")


def tts_pronounce(s):
    for k, v in PRONOUNCE.items():
        s = s.replace(k, v)
    return s


# TTS 不可见占位符：插入在 TTS 输入中用于停顿，输出 cue 文本时剥离
_INVISIBLE_CHARS = set('\u200b\u200c\u200d\u2060\ufeff')


def restore_pronounce(s):
    for k, v in PRONOUNCE.items():
        s = s.replace(v, k)
    return ''.join(ch for ch in s if ch not in _INVISIBLE_CHARS)


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
    body = pause_at_boundary(pause_before_er(tts_clean(body_text(item))))
    header = [x for x in (title, who) if x]
    if join_header and len(header) > 1:
        # 单字标题单独起句容易硬起/破音，连成一句读更顺
        text = "\n".join(["，".join(header)] + ([body] if body else []))
    else:
        text = "\n".join(header + ([body] if body else []))
    return text, len(header), len(split_sentences(body))


# ---------------------------------------------------------------- 表达力增强

AUDIO_SR = 24000  # edge-tts 固定输出 24kHz 单声道 48kbps

# edge-tts 输出是 24kHz / 48kbps / 单声道 CBR，字节数与时长严格成正比
# （实测 6000.0 字节 = 1 秒）。用它换算时长比 SoundBoundary/WordBoundary 的
# offset+duration 可靠：后者会略微高估，累积后会把最后一句 cue 推到音频末尾之后，
# 表现为「最后一句没高亮」。
MP3_BYTES_PER_SEC = 6000.0
GAP_SEC = 0.30  # 歧义词边界处的静音间隙（秒）

# 歧义词边界列表：遇到这些模式时，在 TTS 合成后手动拆分并插入静音
# 格式：(前缀词, 后缀词) → 在该位置断开，生成两段独立音频，中间补 GAP_SEC 秒静音
DISAMBIG_PATTERNS = [
    ("佛", "于是"),   # 佛于是 → 佛 / 于是（避免读成「佛是」）
]


def mp3_dur(data: bytes) -> float:
    """按 CBR 字节数换算 mp3 真实时长（秒）。"""
    return len(data) / MP3_BYTES_PER_SEC


def clamp_cues(cues, dur):
    """把越界的 cue 收回到音频实际长度内。

    - 起点已经超过音频末尾的 cue：并入前一句（它属于同一口气读出来的内容）
    - 其余 cue 的时长裁到不越过音频结尾
    """
    out = []
    for c in cues or []:
        c = dict(c)
        if out and c["t"] >= dur - 0.02:
            prev = out[-1]
            prev["d"] = round(max(prev["d"], dur - prev["t"]), 3)
            continue
        out.append(c)
    for c in out:
        c["d"] = round(max(0.03, min(c["d"], dur - c["t"])), 3)
    return out

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
            communicate = edge_tts.Communicate(tts_pronounce(text), voice, rate=rate, pitch=pitch, volume=volume)
            cues = []
            audio = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
                elif chunk["type"] == "SentenceBoundary":
                    cues.append({
                        "t": round(chunk["offset"] / 1e7, 3),
                        "d": round(chunk["duration"] / 1e7, 3),
                        "s": restore_pronounce(chunk["text"]),
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


async def speak_segmented(text, voice, rate, pitch="+0Hz", volume="+0%", retries=3):
    """按句末标点（。！？）分段合成，段间短停0.12s，保证朗诵流畅感。
    逗号/分号不强制切分，由TTS自然处理韵律。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            # 只按句末标点分割，保持整句完整性
            SENT_END = set('。！？!?')
            segments = []
            buf = ''
            for ch in text:
                buf += ch
                if ch in SENT_END:
                    if buf.strip():
                        segments.append(buf.strip())
                    buf = ''
            if buf.strip():
                segments.append(buf.strip())

            # 纯标点段（如结尾的「……」）会让 edge-tts 返回空音频并报
            # 「No audio was received」，直接丢掉
            segments = [s for s in segments if re.search(r"[\u4e00-\u9fff0-9A-Za-z]", s)]

            if not segments:
                raise RuntimeError("empty segments")

            all_audio = bytearray()
            all_cues = []
            offset = 0.0
            for seg_text in segments:
                seg_clean = tts_pronounce(seg_text)
                
                # 检查是否需要拆分歧义词
                split_info = None
                for prefix, suffix in DISAMBIG_PATTERNS:
                    idx = seg_clean.find(prefix + suffix)
                    if idx >= 0:
                        # 找到歧义边界，拆分成两段
                        part_a = seg_clean[:idx + len(prefix)]
                        part_b = seg_clean[idx + len(prefix):]
                        split_info = (part_a, part_b)
                        break
                
                if split_info:
                    part_a, part_b = split_info
                    # 分别合成两段
                    a_audio, a_cues = await speak(part_a, voice, rate, pitch, volume, retries)
                    b_audio, b_cues = await speak(part_b, voice, rate, pitch, volume, retries)
                    
                    # 构造静音字节
                    sil_bytes = int(GAP_SEC * MP3_BYTES_PER_SEC)
                    silence = bytes(sil_bytes)
                    
                    # 合并音频
                    seg_audio = bytearray(a_audio) + silence + bytearray(b_audio)
                    seg_dur_a = mp3_dur(a_audio)
                    seg_dur_b = mp3_dur(b_audio)
                    seg_dur = seg_dur_a + GAP_SEC + seg_dur_b
                    
                    # 合并 cues：第一段正常，第二段偏移
                    for c in clamp_cues(a_cues, seg_dur_a):
                        all_cues.append({"t": round(offset + c["t"], 3),
                                         "d": c["d"],
                                         "s": restore_pronounce(c["s"])})
                    # 添加一个标记 cue 表示静音间隙（可选）
                    all_cues.append({"t": round(offset + seg_dur_a, 3),
                                     "d": GAP_SEC,
                                     "s": ""})  # 空文本，仅占位
                    for c in clamp_cues(b_cues, seg_dur_b):
                        all_cues.append({"t": round(offset + seg_dur_a + GAP_SEC + c["t"], 3),
                                         "d": c["d"],
                                         "s": restore_pronounce(c["s"])})
                    all_audio.extend(seg_audio)
                    offset += seg_dur
                else:
                    # 正常合成整段
                    seg_audio, seg_cues = await speak(seg_clean, voice, rate, pitch, volume, retries)
                    seg_dur = mp3_dur(seg_audio)
                    for c in clamp_cues(seg_cues, seg_dur):
                        all_cues.append({"t": round(offset + c["t"], 3),
                                         "d": c["d"],
                                         "s": restore_pronounce(c["s"])})
                    all_audio.extend(seg_audio)
                    offset += seg_dur

            total = offset
            return bytes(all_audio), all_cues, total
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"分段合成失败：{last_err}")


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
    body = pause_at_boundary(pause_before_er(tts_clean(body_text(item))))
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
    # 对诗词类（含逗号/分号，或5/7字古体句）使用分段合成，确保每小句有准确时间戳
    is_poem = item["_kind"] not in PROSE_KINDS
    body_raw = tts_clean(body_text(item))
    # pause_before_er 只为古典诗词服务（处理「而」吞音问题）；现代自由诗不应用，
    # 否则会在"而你..."等处插入逗号，导致被误判为古典诗词走 speak_segmented 路径
    if item.get("_kind") == "modern":
        body_text_for_check = silence_break_before(body_raw)
    else:
        body_text_for_check = pause_at_boundary(pause_before_er(body_raw))
    has_comma_semicolon = any(ch in body_text_for_check for ch in '，；')
    if is_poem and (has_comma_semicolon or any(len(line.strip()) in (4, 5, 7, 8) for line in body_text_for_check.splitlines() if line.strip())):
        # 诗词类按逗号/分号分段合成，段间短停0.15s保持流畅朗诵感
        audio, cues, total = await speak_segmented(body_text_for_check, voice, rate, pitch)
        # 合并 header cues（从完整文本合成）
        header_parts = [x for x in (tts_clean(item.get("title")),
                                     "，".join(x for x in (dynasty_text(item.get("dynasty")),
                                                           tts_clean(item.get("author"))) if x)) if x]
        header_text = "\n".join(header_parts)
        h_audio, h_cues = await speak(header_text, voice, rate)
        full_audio = h_audio + audio
        out_mp3.write_bytes(full_audio)
        # 真实时长按字节数换算；header 占用的时长同样用字节数，避免与正文错位
        dur = round(mp3_dur(full_audio), 3)
        offset = round(mp3_dur(h_audio), 3)
        h_final = clamp_cues(h_cues, dur)
        b_final = clamp_cues([dict(c, t=round(c["t"] + offset, 3)) for c in cues], dur)
        cues = h_final + b_final
        hdr = len(h_final)
        body_count = len(b_final)
        return {
            "id": item["id"],
            "title": item.get("title", ""),
            "voice": voice,
            "rate": rate,
            "pitch": pitch,
            "dur": dur,
            "hdr": hdr,
            "bodyCues": body_count,
            "v": hashlib.md5(full_audio).hexdigest()[:8],
            "cues": cues,
        }
    else:
        # 自由诗/散文类：检测歧义词并拆分合成
        text_clean = pause_at_boundary(pause_before_er(tts_clean(body_text(item))))
        # 检查是否需要拆分（处理可能已插入的零宽字符）
        split_point = None
        for prefix, suffix in DISAMBIG_PATTERNS:
            # 搜索原始模式或带零宽字符的模式
            pat_raw = prefix + suffix
            pat_zwj = prefix + '\u2060' + suffix
            idx = text_clean.find(pat_raw)
            if idx < 0:
                idx = text_clean.find(pat_zwj)
            if idx >= 0:
                # 找到位置，计算分割点（跳过可能的 ZWJ）
                split_pos = idx + len(prefix)
                # 确认分割点后确实是 suffix（跳过 ZWJ）
                after_split = text_clean[split_pos:].lstrip('\u2060')
                if after_split.startswith(suffix):
                    split_point = (split_pos, prefix, suffix)
                    break
        
        # 合成 header（标题 + 作者）
        header_parts = [x for x in (tts_clean(item.get("title")),
                                     "，".join(x for x in (dynasty_text(item.get("dynasty")),
                                                           tts_clean(item.get("author"))) if x)) if x]
        header_text = "\n".join(header_parts)
        h_audio, h_cues = await speak(header_text, voice, rate)
        
        if split_point:
            pos, pre, suf = split_point
            # 移除 part_b 开头的零宽字符（如果有）
            part_a = text_clean[:pos]
            part_b = text_clean[pos:].lstrip('\u2060')
            a_audio, a_cues = await speak(part_a, voice, rate, pitch)
            b_audio, b_cues = await speak(part_b, voice, rate, pitch)
            sil_bytes = int(GAP_SEC * MP3_BYTES_PER_SEC)
            silence = bytes(sil_bytes)
            body_audio = bytearray(a_audio) + silence + bytearray(b_audio)
            dur_a = mp3_dur(a_audio)
            dur_b = mp3_dur(b_audio)
            body_dur = round(dur_a + GAP_SEC + dur_b, 3)
            # 合并 body cues
            all_body_cues = list(clamp_cues(a_cues, dur_a))
            all_body_cues.append({"t": round(dur_a, 3), "d": GAP_SEC, "s": ""})
            for c in clamp_cues(b_cues, dur_b):
                all_body_cues.append({"t": round(dur_a + GAP_SEC + c["t"], 3),
                                     "d": c["d"],
                                     "s": restore_pronounce(c["s"])})
            body_cues = clamp_cues(all_body_cues, body_dur)
        else:
            body_audio, body_cues = await speak(text_clean, voice, rate, pitch)
            body_dur = mp3_dur(body_audio)
        
        full_audio = bytearray(h_audio) + bytearray(body_audio)
        out_mp3.write_bytes(full_audio)
        dur = round(mp3_dur(full_audio), 3)
        offset = round(mp3_dur(h_audio), 3)
        h_final = clamp_cues(h_cues, dur)
        b_final = clamp_cues([dict(c, t=round(c["t"] + offset, 3)) for c in body_cues], dur)
        cues = h_final + b_final
        hdr = len(h_final)
        body_count = len(b_final)
        return {
            "id": item["id"],
            "title": item.get("title", ""),
            "voice": voice,
            "rate": rate,
            "pitch": pitch,
            "dur": dur,
            "hdr": hdr,
            "bodyCues": body_count,
            "v": hashlib.md5(bytes(full_audio)).hexdigest()[:8],
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
    p.add_argument("--kind", help="poem,prose,story,extra,classics,foreign,modern")
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