"""中文录音的「听写检查」：用语音识别（faster-whisper，MIT 许可）把每条录音听一遍，和原文比，找出读漏、读错字、中途断掉、没声音的录音。

比较的是不带声调的拼音（同音字不算错；声调和多音字由 audio_py.json 的拼音保证，这里不管）：
    分数 = 1 − 编辑距离(原文的音节, 识别结果的音节) / 原文音节数
单字、两个字的词识别本来就不稳，只在完全没识别出来时才算可疑。

    python3 tools/asr_check_zh.py --jobs jobs.json --root <录音根目录> --shard 0/8 --out part.json
jobs.json：[[key, 原文, 文件相对路径], …]；输出 {key: [分数, 识别结果]}（只在机器上，公开日志只打印数量）
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
HAN = re.compile(r'[一-鿿]')


def syllables(text):
    from pypinyin import Style, lazy_pinyin
    t = ''.join(HAN.findall(text))
    return [p for p in lazy_pinyin(t, style=Style.NORMAL) if p]


def lev(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def spoken(text):
    """原文 → 实际朗读的文字（数字按 gen_audio_melo.zh_spoken_numbers 写成汉字，其余数字按数值）"""
    try:
        from gen_audio_melo import zh_spoken_numbers
        text = zh_spoken_numbers(text)
    except Exception:   # noqa: BLE001
        pass
    return per_digit(text).replace('幺', '一')


DIG = dict(zip('0123456789', '零一二三四五六七八九'))


def per_digit(text):
    """数字逐位写成汉字（识别结果里的「12306」「1987」和原文的读法都这样比）"""
    return re.sub(r'\d', lambda m: DIG[m.group(0)], text)


def hyp_variants(text):
    """识别结果里的阿拉伯数字可能是逐位读的（12306、1987年），也可能是数值（200块、3点10分）：两种写法都试，取分数高的"""
    out = [per_digit(text)]
    if re.search(r'\d', text):
        try:
            import cn2an
            out.append(cn2an.transform(text, 'an2cn'))
        except Exception:   # noqa: BLE001
            pass
    return [t.replace('幺', '一') for t in out]


def score(src, hyp):
    a = syllables(spoken(src))
    if not a:
        return 1.0
    two = lambda xs: ['er' if x == 'liang' else x for x in xs]   # 两/二 不算错（两百 / 二百）
    best = 0.0
    for h in hyp_variants(hyp):
        b = syllables(h)
        best = max(best, 1 - lev(two(a), two(b)) / len(a))
    return max(0.0, best)


ASR = None


def transcribe(path):
    """语音识别：默认 FunASR 的 Paraformer（普通话专用，阿里达摩院，MIT/Apache 许可）；ASR_ENGINE=whisper 时用 faster-whisper"""
    global ASR
    import os
    eng = os.environ.get('ASR_ENGINE', 'paraformer')
    if ASR is None:
        if eng == 'whisper':
            from faster_whisper import WhisperModel
            ASR = WhisperModel(os.environ.get('ASR_MODEL', 'small'), device='cpu', compute_type='int8')
        else:
            from funasr import AutoModel
            ASR = AutoModel(model='paraformer-zh', disable_update=True, disable_pbar=True, log_level='ERROR')
    if eng == 'whisper':
        segs, _ = ASR.transcribe(str(path), language='zh', beam_size=5, condition_on_previous_text=False, without_timestamps=True)
        return ''.join(s.text for s in segs).strip()
    res = ASR.generate(input=str(path), disable_pbar=True)
    return ''.join(r.get('text', '') for r in res).replace(' ', '').strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--jobs', required=True)
    ap.add_argument('--root', required=True)
    ap.add_argument('--shard', default='0/1')
    ap.add_argument('--out', required=True)
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()
    k, n = map(int, a.shard.split('/'))
    jobs = [j for i, j in enumerate(json.loads(Path(a.jobs).read_text(encoding='utf-8'))) if i % n == k]
    if a.limit:
        jobs = jobs[:a.limit]
    out, missing = {}, 0
    for i, (key, text, rel) in enumerate(jobs, 1):
        f = Path(a.root) / rel
        if not f.exists():
            missing += 1
            out[key] = [-1, '']
            continue
        try:
            hyp = transcribe(f)
        except Exception as e:   # noqa: BLE001
            hyp = ''
            print('  failed', type(e).__name__, flush=True)
        out[key] = [round(score(text, hyp), 3), hyp]
        if i % 200 == 0:
            print(f'  {i}/{len(jobs)}', flush=True)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')
    low = sum(1 for s, _ in out.values() if 0 <= s < 0.8)
    print(f'checked {len(out)}, missing files {missing}, score<0.8: {low}')


if __name__ == '__main__':
    main()
