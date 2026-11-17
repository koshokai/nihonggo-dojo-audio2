"""中文词典（lang/zh-dict.json，HSK 以外的常用词）里还没有录音的词：MeloTTS（https://github.com/myshell-ai/MeloTTS ，MIT）普通话 ZH。

合成部分（zh_patch / wav_speech / cut_first_half / synth_zh / norm_wav）照搬主站 tools/gen_audio_melo.py：
按词典里的拼音读（多音字不读错，和网页上显示的拼音一样），读出来没声音就换几种读法再试，统一响度，48 kbps。

    python3 scripts/gen_zh.py --shard 3 --shards 8 --out out     生成这一片（→ out/zh-d/*.mp3、out/index-dict-zh.part.json）
    python3 scripts/gen_zh.py --dry-run                           只算数量（不需要 MeloTTS）
"""
import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import common

KBPS = 48
ZH_VOWEL = re.compile('[aeiouüvāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]')
HAN = re.compile(r'[一-鿿]')


def zh_forced_seq(text, py):
    """这个词每个汉字应该怎么读：[(字, 带调拼音)]，来自词典的拼音（空格分开，每个字一个音节）。
    儿化（「哪儿 nǎr」「一点儿 yì diǎnr」）：最后一个音节去掉 r，「儿」按原来的读法（zh_patch 里 py == 'r' 用原来的结果）"""
    chars = HAN.findall(text)
    sy = (py or '').split()
    if not chars or not sy:
        return None
    if len(sy) == len(chars) - 1 and chars[-1] == '儿' and sy[-1].endswith('r') and len(sy[-1]) > 1:
        sy = sy[:-1] + [sy[-1][:-1], 'r']
    if len(sy) != len(chars):
        return None
    return [(c, '' if p == '-' else p) for c, p in zip(chars, sy)]


def zh_patch():
    """让 MeloTTS 按我们给的拼音读：替换 melo 中文模块里的 lazy_pinyin。
    MeloTTS 先用 jieba 分词，再对每个词调用 lazy_pinyin(词, style=INITIALS) 和 (词, style=FINALS_TONE3)，
    我们按顺序在这段文字的拼音里找到这个词，返回对应的声母、韵母；对不上就用原来的结果。"""
    import importlib
    from pypinyin import Style
    from pypinyin import lazy_pinyin as orig
    from pypinyin.contrib.tone_convert import to_finals_tone3, to_initials
    state = {'seq': None, 'pos': 0}

    def find(chars, w, start):
        for k in range(start, min(len(chars) - len(w), start + 60) + 1):
            if chars[k:k + len(w)] == w:
                return k
        return -1

    def patched(word, *args, **kw):
        res = orig(word, *args, **kw)
        style = kw.get('style', args[0] if args else None)
        seq = state['seq']
        if not seq or style not in (Style.INITIALS, Style.FINALS_TONE3) or len(res) != len(word):
            return res
        k = find([c for c, _ in seq], list(word), state['pos'])
        if k < 0:
            return res
        out = []
        for j in range(len(word)):
            py = seq[k + j][1]
            if not py or py == 'r' or not ZH_VOWEL.search(py):   # 儿化的「儿」、嗯 ńg 之类：用原来的
                out.append(res[j])
            elif style == Style.INITIALS:
                out.append(to_initials(py, strict=True))
            else:
                out.append(to_finals_tone3(py, strict=True, neutral_tone_with_five=True))
        if style == Style.FINALS_TONE3:
            state['pos'] = k + len(word)
            state['last'] = (k, word)
        return out

    mods = []
    for name in ('melo.text.chinese', 'melo.text.chinese_mix'):
        try:
            m = importlib.import_module(name)
        except Exception as e:
            print('  skip', name, type(e).__name__)
            continue
        if hasattr(m, 'lazy_pinyin'):
            m.lazy_pinyin = patched
            mods.append(name)
    print('  pinyin patch:', mods or 'NOT APPLIED', flush=True)
    # MeloTTS 的轻声规则把词尾的「的、地、得」一律读成轻声（绿地 lǜdi、目的 mùdi）：我们给了非轻声读音的，改回去（和主站 gen_audio_melo.py 一样）
    try:
        ch = importlib.import_module('melo.text.chinese')
        tm = ch.tone_modifier
        orig_mt = tm.modified_tone

        def modified_tone(word, pos, finals):
            finals = orig_mt(word, pos, finals)
            seq, last = state['seq'], state.get('last')
            if seq and last and last[1] == word:
                for j, c in enumerate(word):
                    py = seq[last[0] + j][1] if last[0] + j < len(seq) else ''
                    if c in '的地得' and py and ZH_VOWEL.search(py) and j < len(finals):
                        t = to_finals_tone3(py, strict=True, neutral_tone_with_five=True)[-1:]
                        if t in '1234' and finals[j][-1:] == '5':
                            finals[j] = finals[j][:-1] + t
            return finals
        tm.modified_tone = modified_tone
    except Exception as e:   # noqa: BLE001
        print('  tone patch skipped', type(e).__name__)
    return state


def norm_wav(path, target_db=-21.5):
    """音量调到统一水平（按有声音部分的平均 RMS，和日语录音一致），峰值不超过 -1 dBFS"""
    import array
    import math
    import wave
    with wave.open(str(path)) as w:
        prm = w.getparams()
        data = array.array('h', w.readframes(prm.nframes))
    N, hop = int(prm.framerate * .03), int(prm.framerate * .01)
    es = [sum(x * x for x in data[st:st + N]) / N for st in range(0, max(0, len(data) - N), hop)]
    if not es:
        return
    gate = max(max(es) * 10 ** (-25 / 10), (10 ** (-60 / 20) * 32768) ** 2)
    acc = sum(e for e in es if e > gate)
    cnt = sum(1 for e in es if e > gate)
    if not cnt:
        return
    rms, peak = math.sqrt(acc / cnt) / 32768, max(abs(x) for x in data) / 32768
    gain = min(10 ** ((target_db - 20 * math.log10(rms)) / 20), 10 ** (-1 / 20) / max(peak, 1e-6))
    out = array.array('h', (max(-32768, min(32767, round(x * gain))) for x in data))
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(prm.nchannels)
        w.setsampwidth(prm.sampwidth)
        w.setframerate(prm.framerate)
        w.writeframes(out.tobytes())


def wav_speech(path):
    """这段录音里有多少「真正的声音」：(最响的 20ms 帧的音量 dBFS, 有声音的总时长 秒)。
    MeloTTS 读单个汉字时常常只出一点点杂音，靠这个判断要不要换个方法重新生成。"""
    import array
    import math
    import wave
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        data = array.array('h', w.readframes(w.getnframes()))
    win = max(1, int(rate * .02))
    fr = [math.sqrt(sum(x * x for x in data[i:i + win]) / win) / 32768 for i in range(0, len(data) - win, win)]
    pk = max(fr) if fr else 0
    act = sum(1 for r in fr if r > max(pk * .1, 10 ** (-55 / 20))) * .02
    return 20 * math.log10(max(pk, 1e-9)), act, fr, rate, data


def cut_first_half(path):
    """「字，字。」这样读两遍的录音：在中间最安静的地方切开，只留前一遍"""
    import wave
    _, _, fr, rate, data = wav_speech(path)
    n = len(fr)
    if n < 10:
        return
    lo, hi = int(n * .3), int(n * .75)
    k = min(range(lo, hi), key=lambda i: sum(fr[i:i + 4]))
    end = int((k + 2) * .02 * rate)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(data[:end].tobytes())


ZH_STATS = {}


def synth_zh(model, spk, text, wav, state, seq):
    """生成一条中文录音，检查里面确实有声音；没有就换几种读法再试（加句号、读两遍取一遍、调参数），最后统一音量。
    返回 True 表示成功写好了 wav。"""
    for k, v in {'520': '五二零', '618': '六一八', '666': '六六六'}.items():
        text = text.replace(k, v)
    n = len(HAN.findall(text)) or 1
    speed = 0.8 if n == 1 else 0.85 if len(text) < 12 else 0.95
    end = '' if re.search(r'[。！？!?.]$', text) else '。'
    tries = [('plain', text, 1, {}), ('dot', text + end, 1, {}), ('twice', f'{text}，{text}。', 2, {}),
             ('excl', text + '！', 1, {}), ('sdp', text + end, 1, {'sdp_ratio': 0.5, 'noise_scale': 0.667}),
             ('twice-slow', f'{text}，{text}。', 2, {'noise_scale': 0.5})]
    need = 0.15 if n == 1 else 0.11 * n
    best, tmp = None, wav.with_name('try.wav')
    for name, t, rep, kw in tries:
        state['seq'], state['pos'], state['last'] = (seq * rep if seq else None), 0, None
        try:
            model.tts_to_file(t, spk, str(tmp), speed=speed, quiet=True, **kw)
        except Exception:
            continue
        finally:
            state['seq'] = None
        if rep == 2:
            cut_first_half(tmp)
        pk, act = wav_speech(tmp)[:2]
        ok = pk > -45 and act >= need
        if best is None or act > best[0]:
            best = (act, name)
            shutil.copy(tmp, wav)
        if ok:
            ZH_STATS[name] = ZH_STATS.get(name, 0) + 1
            norm_wav(wav)
            return True
    ZH_STATS['none'] = ZH_STATS.get('none', 0) + 1
    return False


def main():
    a = common.common_args(argparse.ArgumentParser()).parse_args()
    r = common.start('zh', a)
    if r is None:
        return
    todo, idx, need, out = r
    from melo.api import TTS
    model = TTS(language='ZH', device='cpu')
    spk = model.hps.data.spk2id['ZH']
    state = zh_patch()
    budget, prog, forced = common.Budget(a.max_minutes), common.Progress(len(todo), every=100), 0
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'x.wav'
        for e in todo:
            if budget.over():
                print('time budget reached', flush=True)
                break
            file = common.file_for('zh', e['key'])
            seq = zh_forced_seq(e['text'], e['py'])
            forced += seq is not None
            try:
                if not synth_zh(model, spk, e['text'], wav, state, seq):
                    prog.bad()      # 几种读法都没有读出声音：先不放录音，下次运行再试
                    continue
                (out / file).parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', str(wav), '-ac', '1', '-b:a', f'{KBPS}k', str(out / file)], check=True)
            except Exception:
                prog.bad()
                continue
            idx[e['key']] = file
            prog.ok()
            if prog.n % 100 == 0:
                common.finish('zh', out, idx)
    part = common.finish('zh', out, idx)
    prog.done('zh')
    print(f'zh: {forced} with dictionary pinyin, takes {ZH_STATS}, part index {len(part)}')


if __name__ == '__main__':
    main()
