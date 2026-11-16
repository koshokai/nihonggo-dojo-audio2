"""中文词典里 MeloTTS 几种读法都没读出声音的词（gen_zh.py 记为 failed，没有文件）：用 Kokoro-82M（https://huggingface.co/hexgrad/Kokoro-82M ，
Apache 2.0）的普通话女声补一条。文件名、索引的 key 和 MeloTTS 的一样（zh-d/<key 的 sha1 前 16 位>.mp3，key = 词本身），
输出 out/zh-d/*.mp3 和 out/index-dict-zh.part.json，由 merge.py 合并（和 MeloTTS 的分片一样）。以后再跑 MeloTTS 时这些词已经有文件，会跳过。

读音：按词典里的拼音（和 gen_zh.py 一样，多音字不读错），每个字拼音 → IPA 的写法和 misaki ZHG2P v1.0 一样；对不上时用 misaki 自己的读法。
音量：和 MeloTTS 中文同一套（gen_zh.norm_wav，有声部分 −21.5 dB），48 kbps；读出来没有声音的不放文件（下次再试）。

    python3 scripts/gen_zh_kokoro.py --words "某些 竟 整" --out out     只做这些词
    python3 scripts/gen_zh_kokoro.py --words all --out out              所有还没有录音的词（先让 MeloTTS 跑完，剩下的才是它读不出来的）
    python3 scripts/gen_zh_kokoro.py --words all --dry-run              只算数量
"""
import argparse
import os
import re
import subprocess
import tempfile
import wave
from pathlib import Path

import common
from gen_zh import HAN, KBPS, norm_wav, wav_speech, zh_forced_seq

REPO_ID = os.environ.get('KOKORO_ZH_REPO') or 'hexgrad/Kokoro-82M'
VOICE = os.environ.get('KOKORO_ZH_VOICE') or 'zf_xiaoxiao'
VOWEL = re.compile('[aeiouüvāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]')


def phonemes(text, seq):
    """词典拼音 → Kokoro v1.0 的音素（整个词连在一起，和 misaki 对一个词的写法一样）"""
    from misaki.zh import ZHG2P
    from pypinyin.contrib.tone_convert import to_tone3
    if seq and REPO_ID.rstrip('/').endswith('/Kokoro-82M'):
        try:
            return ''.join(ZHG2P.py2ipa(to_tone3(p, neutral_tone_with_five=True)) if p and VOWEL.search(p) else ZHG2P.word2ipa(c)
                           for c, p in seq).replace(chr(815), '')   # 和 misaki legacy_call 一样去掉 U+032F
        except Exception:   # noqa: BLE001
            pass
    return ZHG2P(version=None if REPO_ID.rstrip('/').endswith('/Kokoro-82M') else '1.1')(text)[0]


def synth(pipe, text, seq, wav):
    """几种读法（原样、加句号、慢一点）里第一个真的有声音的，写进 wav 并统一音量。返回 True 表示成功"""
    import numpy as np
    n = len(HAN.findall(text)) or 1
    ps = phonemes(text, seq)
    need = 0.15 if n == 1 else 0.11 * n
    for p, speed in ((ps, 0.8 if n == 1 else 0.85), (ps + '.', 0.8 if n == 1 else 0.85), (ps + '.', 0.7)):
        parts = [r.audio for r in pipe.generate_from_tokens(p, voice=VOICE, speed=speed)]
        parts = [x.numpy() if hasattr(x, 'numpy') else x for x in parts if x is not None]
        if not parts:
            continue
        with wave.open(str(wav), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes((np.clip(np.concatenate(parts), -1, 1) * 32767).astype('<i2').tobytes())
        pk, act = wav_speech(wav)[:2]
        if pk > -45 and act >= need:
            norm_wav(wav)
            return True
    return False


def main():
    ap = common.common_args(argparse.ArgumentParser())
    ap.add_argument('--words', default=os.environ.get('WORDS', ''), help='只做这些词（空格分开）；all = 所有还没有录音的词')
    a = ap.parse_args()
    if not a.words.strip():
        raise SystemExit('--words 必填（空格分开的词，或 all）')
    a.shard, a.shards = 0, 1
    r = common.start('zh', a)
    if r is None:
        return
    todo, idx, _need, out = r
    if a.words.strip() != 'all':
        want = set(a.words.split())
        todo = [e for e in todo if e['key'] in want]
        print(f'words: {len(want)} asked, {len(todo)} still without a recording', flush=True)
    if not todo:
        common.finish('zh', out, idx)
        return print('zh kokoro: nothing to do')
    from kokoro import KPipeline
    pipe = KPipeline(lang_code='z', repo_id=REPO_ID)
    prog = common.Progress(len(todo), every=50)
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'x.wav'
        for e in todo:
            file = common.file_for('zh', e['key'])
            try:
                if not synth(pipe, e['text'], zh_forced_seq(e['text'], e['py']), wav):
                    prog.bad()
                    continue
                (out / file).parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', str(wav), '-ar', '44100', '-ac', '1', '-b:a', f'{KBPS}k', str(out / file)], check=True)
            except Exception:   # noqa: BLE001  不打印词
                prog.bad()
                continue
            idx[e['key']] = file
            prog.ok()
    part = common.finish('zh', out, idx)
    prog.done('zh')
    print(f'zh kokoro ({VOICE}): part index {len(part)}')


if __name__ == '__main__':
    main()
