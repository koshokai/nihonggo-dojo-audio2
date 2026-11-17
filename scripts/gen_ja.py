"""日语词典（dict.json，JMdict 常用词）里还没有录音的词：VOICEVOX:春日部つむぎ（style 8）。

合成部分（query / voice_word / render / synth / norm_wav / to_mp3）照搬主站 tools/gen_audio.py（单词模式、统一响度、40 kbps）：
先用写法合成（重音更自然），引擎读出来的假名和词典的读音对不上时改用读音，避免读错。

先启动引擎：docker run -d --log-driver none -p 50021:50021 voicevox/voicevox_engine:cpu-latest
    python3 scripts/gen_ja.py --shard 3 --shards 8 --out out     生成这一片（→ out/ja-d/*.mp3、out/index-dict-ja.part.json）
    python3 scripts/gen_ja.py --dry-run                           只算数量（不需要引擎）
"""
import argparse
import array
import json
import math
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import wave
from io import BytesIO
from pathlib import Path

import common

ENGINE = 'http://127.0.0.1:50021'
SPEAKER = 14          # 冥鳴ひまり（ノーマル）：和网站其他日语录音同一个声音
KBPS = 40


def api(engine, path, params=None, body=None, raw=False):
    url = engine.rstrip('/') + path + ('?' + urllib.parse.urlencode(params) if params else '')
    data = json.dumps(body).encode() if body is not None else (b'' if path in ('/audio_query', '/synthesis') else None)
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'},
                                 method='POST' if data is not None else 'GET')
    with urllib.request.urlopen(req, timeout=120) as r:
        b = r.read()
    return b if raw else json.loads(b)


def wait_engine(engine, timeout=300):
    t = time.time()
    while time.time() - t < timeout:
        try:
            return api(engine, '/version')
        except Exception:
            time.sleep(3)
    sys.exit('VOICEVOX engine not reachable at ' + engine)


def hira(s):
    return re.sub(r'[ァ-ヶ]', lambda c: chr(ord(c.group()) - 0x60), s)


def norm(s):
    return hira(re.sub(r'[\s、。！？!?「」・〜～ー]', '', s))


def mora_text(q):
    return ''.join(m['text'] for ap in q['accent_phrases'] for m in ap['moras'])


# 助词「は・へ・を」读作「わ・え・お」：比较读音时当成一样
SAME = str.maketrans('わえお', 'はへを')


def query(engine, speaker, surface, kana):
    """先用汉字原文（重音更自然），读音和我们的假名对不上时改用假名，避免读错。"""
    q = api(engine, '/audio_query', {'text': surface, 'speaker': speaker})
    used = 'surface'
    if kana and norm(mora_text(q)).translate(SAME) != norm(kana).translate(SAME):
        q = api(engine, '/audio_query', {'text': re.sub(r'\s+', '', kana), 'speaker': speaker})
        used = 'kana'
    return q, used


DEVOICED = {'A': 'a', 'I': 'i', 'U': 'u', 'E': 'e', 'O': 'o'}


def voice_word(engine, speaker, q):
    """两拍以内的短词单独读时，元音不清音化（「しき」「くつ」单独听几乎全是气声，初学者听不清）。返回是否改动过。"""
    moras = [m for ap in q['accent_phrases'] for m in ap['moras']]
    hit = False
    for m in (moras if len(moras) <= 2 else []):
        if m.get('vowel') in DEVOICED:
            m['vowel'] = DEVOICED[m['vowel']]
            hit = True
    if hit:
        q['accent_phrases'] = api(engine, '/mora_data', {'speaker': speaker}, body=q['accent_phrases'])
    return hit


def render(engine, speaker, q, speed=1.0, style=None):
    q['speedScale'] = speed
    q.update(style or {})
    q['outputSamplingRate'] = 24000
    return api(engine, '/synthesis', {'speaker': speaker}, body=q, raw=True)


def synth(engine, speaker, surface, kana, speed=1.0, style=None, word=False):
    q, used = query(engine, speaker, surface, kana)
    if word:
        voice_word(engine, speaker, q)
    return render(engine, speaker, q, speed, style), used


LOUD_DB = -21.5   # 站内录音的统一响度（有声部分的平均 RMS）


def norm_wav(wav, target_db=LOUD_DB, ceil_db=-1.0):
    """把音量调到统一水平，峰值不超过 ceil_db。只看有声音的部分，前后的静音不算。"""
    with wave.open(BytesIO(wav)) as w:
        prm = w.getparams()
        data = array.array('h', w.readframes(prm.nframes))
    if sys.byteorder == 'big':
        data.byteswap()
    N, hop, gate = int(prm.framerate * .03), int(prm.framerate * .01), (0.01 * 32768) ** 2
    acc = cnt = 0
    for st in range(0, max(0, len(data) - N), hop):
        e = sum(x * x for x in data[st:st + N]) / N
        if e > gate:
            acc += e
            cnt += 1
    if not cnt:
        return wav
    rms = math.sqrt(acc / cnt) / 32768
    peak = max(abs(x) for x in data) / 32768
    gain = min(10 ** ((target_db - 20 * math.log10(rms)) / 20), 10 ** (ceil_db / 20) / max(peak, 1e-6))
    out = array.array('h', (max(-32768, min(32767, round(x * gain))) for x in data))
    if sys.byteorder == 'big':
        out.byteswap()
    buf = BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(prm.nchannels)
        w.setsampwidth(prm.sampwidth)
        w.setframerate(prm.framerate)
        w.writeframes(out.tobytes())
    return buf.getvalue()


def to_mp3(wav, path, kbps=KBPS, af=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', '-', '-ac', '1'] + (['-af', af] if af else []) + ['-b:a', f'{kbps}k', str(path)],
                   input=wav, check=True)


def main():
    ap = common.common_args(argparse.ArgumentParser())
    ap.add_argument('--engine', default=ENGINE)
    ap.add_argument('--speaker', type=int, default=SPEAKER)
    a = ap.parse_args()
    r = common.start('ja', a)
    if r is None:
        return
    todo, idx, need, out = r
    wait_engine(a.engine)
    budget, prog, by_kana = common.Budget(a.max_minutes), common.Progress(len(todo)), 0
    for e in todo:
        if budget.over():
            print('time budget reached', flush=True)
            break
        file = common.file_for('ja', e['key'])
        try:
            # 每个词条用自己的写法合成（同音词「箸 / 橋」各自的重音），读音不对时改用词典的读音
            wav, used = synth(a.engine, a.speaker, e['text'], e['kana'], 1.0, word=True)
            to_mp3(norm_wav(wav), out / file)
        except Exception:
            prog.bad()
            continue
        idx[e['key']] = file
        by_kana += used == 'kana'
        prog.ok()
        if prog.n % 200 == 0:
            common.finish('ja', out, idx, need, a.shard, a.shards)
    part = common.finish('ja', out, idx, need, a.shard, a.shards)
    prog.done('ja')
    print(f'ja: {by_kana} synthesised from kana, part index {len(part)}')


if __name__ == '__main__':
    main()
