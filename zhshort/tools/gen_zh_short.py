"""中文的单字、短词录音：MeloTTS 读单字、两个字的词常常含糊（语音识别听写检查：单字九成、两个字的词一半听不出是什么字，
真人录音的单字九成都能听出来，所以是录音的问题）。这里给每个短词准备几条候选录音——原来的 MeloTTS 录音、Kokoro 的几个普通话女声
（按网页显示的拼音读，tools/zh/audio_py.json）——用语音识别（tools/asr_check_zh.py，FunASR Paraformer）逐条听，留下听得最清楚的一条。

    python3 tools/gen_zh_short.py test  --root <录音根目录> --index merged.json             抽样，只打印各引擎的通过率
    python3 tools/gen_zh_short.py run   --root <录音根目录> --index merged.json --shard 0/8 --out part.json
merged.json：{原文: 文件相对路径}（网页实际播放的那条）。run 的输出 {原文: [文件, 引擎, 分数]}，新录音写在 <root>/zh2/。
"""
import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asr_check_zh as A   # noqa: E402
import gen_audio_melo as M   # noqa: E402

HAN = re.compile(r'[一-鿿]')
KOKORO = [('zf_xiaoyi', 0.85), ('zf_xiaobei', 0.8), ('zf_xiaoxiao', 0.8)]
KOKORO_ONE = [('zf_xiaoyi', 0.85), ('zf_xiaoyi', 0.7), ('zf_xiaoni', 0.8), ('zf_xiaobei', 0.8)]   # 单字：试跑里 xiaoyi 最清楚
PASS = 0.99


def short_texts(max_han=4):
    texts = M.items_zh('basic') + M.items_zh('levels') + M.items_zh('daily')
    return [t for t in dict.fromkeys(texts) if 1 <= len(HAN.findall(t)) <= max_han and len(HAN.findall(t)) == len(re.sub(r'[\s，。！？、…“”]', '', t))]


PY = {}   # 词典模式：词 → 词典里的拼音


def seq_of(text):
    return M.zh_forced_seq(text) or (M.seq_from_py(text, PY[text]) if text in PY else None)


def candidates(text, cur, root, synth, tmp):
    """[(引擎, 文件路径)]：原来的录音 + Kokoro 几个声音"""
    import gen_audio_zh_voices as V
    out = []
    if cur and (root / cur).exists():
        out.append(('cur', root / cur))
    seq = seq_of(text)
    for voice, sp in (KOKORO_ONE if len(HAN.findall(text)) == 1 else KOKORO):
        f = tmp / f'{voice}-{sp}.mp3'
        try:
            a = synth(text, voice, sp, seq)
            if a is not None and V.write_mp3(a, f, len(HAN.findall(text))):
                out.append((f'{voice}@{sp}', f))
        except Exception as e:   # noqa: BLE001
            print('  kokoro failed', type(e).__name__, flush=True)
    return out


def best_of(text, cands):
    scored = []
    for eng, f in cands:
        try:
            scored.append((A.score(text, A.transcribe(f)), eng, f))
        except Exception as e:   # noqa: BLE001
            print('  asr failed', type(e).__name__, flush=True)
    return scored


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['test', 'run'])
    ap.add_argument('--root', required=True)
    ap.add_argument('--index', required=True)
    ap.add_argument('--shard', default='0/1')
    ap.add_argument('--out', default='short.json')
    ap.add_argument('--n', type=int, default=60)
    ap.add_argument('--human', default='', help='真人录音的索引：有真人录音的词不处理（网页优先播放真人录音）')
    ap.add_argument('--dict', default='', help='词典模式：lang/zh-dict.json 的路径（词和拼音从这里取）')
    ap.add_argument('--stale', default='', help='JSON 列表：这些词原来的录音拼音已经改了，不算候选')
    ap.add_argument('--prefix', default='zh2', help='新录音放在 <root>/<prefix>/')
    a = ap.parse_args()
    import gen_audio_zh_voices as V
    root = Path(a.root)
    idx = json.loads(Path(a.index).read_text(encoding='utf-8'))
    if a.dict:
        for r in json.loads(Path(a.dict).read_text(encoding='utf-8')):
            if 1 <= len(HAN.findall(r[0])) <= 4 and len(HAN.findall(r[0])) == len(r[0]):
                PY[r[0]] = re.sub(r'(\w)r\b', r'\1', r[1]) if r[0][-1:] != '儿' else r[1]
        texts = [t for t in PY if t in idx and seq_of(t)]
    else:
        texts = [t for t in short_texts() if M.zh_forced_seq(t)]
    stale = set(json.loads(Path(a.stale).read_text(encoding='utf-8'))) if a.stale else set()
    for t in stale:
        idx.pop(t, None)
    synth = V.Synth()
    tmp = Path('/tmp/zh-short')
    tmp.mkdir(exist_ok=True)
    if a.mode == 'test':
        random.seed(3)
        cats = {}
        for t in texts:
            cats.setdefault(min(len(HAN.findall(t)), 3), []).append(t)
        for c, v in sorted(cats.items()):
            stat = {}
            best_pass = 0
            sample = random.sample(v, min(a.n, len(v)))
            for t in sample:
                sc = best_of(t, candidates(t, idx.get(t), root, synth, tmp))
                for s, eng, _ in sc:
                    stat.setdefault(eng, [0, 0])
                    stat[eng][0] += s >= PASS
                    stat[eng][1] += 1
                best_pass += bool(sc) and max(s for s, _, _ in sc) >= PASS
            print(f'{c} chars: sample {len(sample)}, best-of pass {best_pass}; ' +
                  ', '.join(f'{e} {p}/{n}' for e, (p, n) in stat.items()), flush=True)
        return
    k, n = map(int, a.shard.split('/'))
    human = json.loads(Path(a.human).read_text(encoding='utf-8')) if a.human else {}
    mine = [t for t in texts if int(hashlib.sha1(t.encode()).hexdigest(), 16) % n == k and t not in human]
    out, passed = {}, 0
    (root / a.prefix).mkdir(exist_ok=True)
    for i, t in enumerate(mine, 1):
        sc = best_of(t, candidates(t, idx.get(t), root, synth, tmp))
        if not sc:
            continue
        s, eng, f = max(sc, key=lambda x: (x[0], x[1] == 'cur'))
        passed += s >= PASS
        if eng == 'cur':
            out[t] = [idx[t], eng, s]
        else:
            rel = f'{a.prefix}/{hashlib.sha1(("short:" + t + "|" + (PY.get(t) or M.rec_val(t)) + "|" + eng).encode()).hexdigest()[:16]}.mp3'
            (root / rel).write_bytes(f.read_bytes())
            out[t] = [rel, eng, s]
        if i % 200 == 0:
            print(f'  {i}/{len(mine)} pass {passed}', flush=True)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')
    print(f'short words: {len(mine)}, clear after choosing {passed}, still unclear {len(mine) - passed}')


if __name__ == '__main__':
    main()
