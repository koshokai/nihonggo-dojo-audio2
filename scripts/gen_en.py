"""英语词典（lang/en-dict.json，CEFR 以外的常用词）里还没有录音的词：Kokoro-82M（https://huggingface.co/hexgrad/Kokoro-82M ，Apache 2.0），
声音 af_heart（和网站英语单词同一个声音）。

合成部分（synth / write_wav / to_mp3）照搬主站 tools/gen_audio_kokoro.py：单词稍慢（0.9），loudnorm 统一响度，44.1 kHz，48 kbps。

    python3 scripts/gen_en.py --shard 3 --shards 8 --out out     生成这一片（→ out/en-d/*.mp3、out/index-dict-en.part.json）
    python3 scripts/gen_en.py --dry-run                           只算数量（不需要 Kokoro）
"""
import argparse
import subprocess
import tempfile
import wave
from pathlib import Path

import common

WORD_VOICE = 'af_heart'
KBPS = 48


def to_mp3(wav_path, mp3_path, kbps=KBPS):
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    # 音量统一（和日语配音差不多响）；44.1kHz 兼容性更好
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', str(wav_path), '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11', '-ar', '44100', '-ac', '1', '-b:a', f'{kbps}k', str(mp3_path)], check=True)


def synth(pipe, text, voice, speed=1.0):
    import numpy as np
    audio = [a for _, _, a in pipe(text, voice=voice, speed=speed)]
    return np.concatenate([a.numpy() if hasattr(a, 'numpy') else a for a in audio]) if audio else None


def write_wav(path, samples, rate=24000):
    import numpy as np
    pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)


def main():
    a = common.common_args(argparse.ArgumentParser()).parse_args()
    r = common.start('en', a)
    if r is None:
        return
    todo, idx, need, out = r
    from kokoro import KPipeline
    pipe = KPipeline(lang_code='a')
    budget, prog = common.Budget(a.max_minutes), common.Progress(len(todo), every=500)
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'x.wav'
        for e in todo:
            if budget.over():
                print('time budget reached', flush=True)
                break
            text = e['text']
            file = common.file_for('en', e['key'])
            try:
                s = synth(pipe, text, WORD_VOICE, speed=0.9 if len(text) < 14 else 1.0)
                if s is None:
                    prog.bad()
                    continue
                write_wav(wav, s)
                to_mp3(wav, out / file)
            except Exception:
                prog.bad()
                continue
            idx[e['key']] = file
            prog.ok()
            if prog.n % 500 == 0:
                common.finish('en', out, idx)
    part = common.finish('en', out, idx)
    prog.done('en')
    print(f'en: part index {len(part)}')


if __name__ == '__main__':
    main()
