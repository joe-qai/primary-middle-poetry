# -*- coding: utf-8 -*-
"""生成「故事动画」示范页的分镜朗读。

用法：python tools/gen_story_audio.py
输出：story/audio/s1.mp3 ... s6.mp3 + story/audio/manifest.json
依赖：edge-tts + ffmpeg（与 gen_audio.py 相同）
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_audio as ga  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "story" / "audio"

VOICE = "zh-CN-YunxiNeural"   # 云希：明亮、适合讲故事
RATE = "-8%"                  # 比日常稍慢，留出画面呼吸感

# 每幕：旁白（朗读） / 字幕（屏幕上的一行） / 分镜标题
SCENES = [
    {
        "id": "s1",
        "name": "墓地旁安家",
        "say": "很久以前，孟子小时候，和母亲住在墓地旁边。",
        "sub": "孟子小时候，家住在墓地旁边",
    },
    {
        "id": "s2",
        "name": "学做丧事游戏",
        "say": "他常常学着大人的样子，玩抬棺材、挖坑埋葬的游戏。",
        "sub": "他学着大人的样子，玩埋葬的游戏",
    },
    {
        "id": "s3",
        "name": "迁到集市",
        "say": "孟母说：这里不适合我的孩子居住。于是，她搬到了集市旁边。",
        "sub": "孟母说：这里不适合孩子居住",
    },
    {
        "id": "s4",
        "name": "学商人叫卖",
        "say": "可是，孟子又学起了商人叫卖、讨价还价的样子。",
        "sub": "搬到集市，孟子又学起了叫卖",
    },
    {
        "id": "s5",
        "name": "再迁学宫",
        "say": "孟母说：这里也不适合。她再一次搬家，搬到了学宫旁边。",
        "sub": "孟母再搬家，搬到了学宫旁边",
    },
    {
        "id": "s6",
        "name": "学礼，定居",
        "say": "孟子开始学习祭祀和揖让进退的礼节。孟母说：这里，才是适合我孩子居住的地方。",
        "sub": "学礼知节，孟母终于安心定居",
    },
]


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"voice": VOICE, "rate": RATE, "scenes": []}
    for sc in SCENES:
        audio, _ = await ga.speak(sc["say"], VOICE, RATE)
        x = ga._decode(audio)
        x, _ = ga._trim(x, pad=0.04)
        # 首尾 8ms 淡入淡出，避免爆音
        n = min(int(0.008 * ga.AUDIO_SR), len(x) // 2)
        if n > 1:
            x[:n] *= [i / n for i in range(n)]
            x[-n:] *= [i / n for i in range(n - 1, -1, -1)]
        data = ga._encode(x)
        (OUT / f"{sc['id']}.mp3").write_bytes(data)
        dur = round(len(x) / ga.AUDIO_SR, 3)
        manifest["scenes"].append({**sc, "dur": dur})
        print(f"[ok] {sc['id']} {sc['name']:<8s} {dur:5.2f}s {len(data) // 1024}KB  {sc['say']}")

    total = round(sum(s["dur"] for s in manifest["scenes"]), 2)
    manifest["total"] = total
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\n共 {len(SCENES)} 幕，朗读总长 {total}s")


asyncio.run(main())