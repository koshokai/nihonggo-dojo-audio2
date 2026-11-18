"""用 MeloTTS（https://github.com/myshell-ai/MeloTTS ，MIT 许可）生成韩语、英语发音（MP3），网页优先播放，没有时用浏览器朗读。

在 GitHub Actions 里运行（.github/workflows/melotts.yml）：
    python3 tools/gen_audio_melo.py ko basic        入门单词 + 常用句（tools/lang/ko.json）
    python3 tools/gen_audio_melo.py en levels       分级词库（lang/en-*.json）
    python3 tools/gen_audio_melo.py ko listen       听力对话（lang/ko-listen.json → audio/index-ko-listen.json）
    python3 tools/gen_audio_melo.py ko daily        今日一句、语法例句、阅读、商务表达（朗读文本 → audio/index-ko.json）
    python3 tools/gen_audio_melo.py zh fix          中文：按 tools/zh_py.py 的拼音（多音字纠正，和网页上显示的一样）
                                                    重新生成读音不对的录音，并补上拼音表的新例字

输出 audio/<语言>/<哈希>.mp3 和 audio/index-<语言>.json（朗读文本 → 文件）。生成过的跳过，可以断点续跑。
"""
import os
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ['MELO_OUT']) if os.environ.get('MELO_OUT') else ROOT / 'audio'   # 在公开的音频仓库里生成时指到那边
PREFIX = os.environ.get('MELO_PREFIX', '')   # 录音文件夹名（默认和语言代码一样：zh/）；音频仓库里新增的放 zh2/
VOICE = {'ko': ('KR', 'KR'), 'en': ('EN', 'EN-US'), 'zh': ('ZH', 'ZH')}


def items_zh(which):
    """中国語（日本用户）：basic = 拼音表例字 + HSK1–3 单词和例句；levels = HSK4–6；daily = 读物、场景会话"""
    J = lambda name: json.loads((ROOT / 'lang' / name).read_text(encoding='utf-8')) if (ROOT / 'lang' / name).exists() else []
    if which in ('basic', 'levels'):
        out = []
        if which == 'basic':
            out += sorted({v[0] for tones in J('zh-pinyin.json').values() for v in tones.values()})
        for L in ((1, 2, 3) if which == 'basic' else (4, 5, 6)):
            rows = J(f'zh-hsk{L}.json')
            out += [r[0] for r in rows] + [r[3] for r in rows if r[3]]
        return out
    out = [x for d in J('zh-daily.json') for x in (d[0], d[5] if len(d) > 5 else '') if x] + [k[0] for w in J('zh-weekly.json') for k in w['k']] + [e[0] for g in J('zh-grammar.json') for e in g['eg']] + [l[1] for x in J('zh-listen.json') for l in x['lines']]
    for a in J('zh-read.json'):
        out += [s[0] for para in a['p'] for s in para] + [w[0] for w in a['w']]
    for t in J('zh-course.json'):
        d = J(f"zh-course-{t['id']}.json")
        for l in d.get('lessons', []):
            out += [x['zh'] for x in l.get('lines', [])] + [w[0] for w in l.get('words', [])] + [e[0] for pt in l.get('patterns', []) for e in pt.get('eg', [])]
    biz = J('zh-biz.json') or {}   # 商务中文的免费样课（会员部分在网站上只有标题）
    for m in biz.get('mails', []):
        out += [m.get('subject', '')] + [ln[0] for ln in m.get('body', [])] + [p[0] for p in m.get('phrases', [])]
    for c in biz.get('vocab', []):
        out += [w[0] for w in c.get('words', [])] + [w[3] for w in c.get('words', []) if len(w) > 3]
    return [t for t in out if t]


def items_hangul():
    """韩文字母表：字母格（辅音 + ㅏ、ㅇ + 元音）、拼读表和字母测验用到的音节、收音例字（和 lang.js 的 koHangul 一致）"""
    L, V = 'ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ', 'ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ'
    syl = lambda l, v: chr(0xAC00 + (L.index(l) * 21 + V.index(v)) * 28)
    out = [syl(l, 'ㅏ') for l in L] + [syl('ㅇ', v) for v in V]
    out += [syl(l, v) for l in 'ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ' for v in 'ㅏㅑㅓㅕㅗㅛㅜㅠㅡㅣㅐㅔㅘㅝㅟ']
    out += list('각간갇갈감갑강')
    return list(dict.fromkeys(out))


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
    # 只算有声音的帧：比最响的地方低 25 dB 以内（录音整体很小声时也能正确放大；原来用固定的 -40 dBFS，小声的录音一帧都算不上，就没放大）
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


def hangul(model, spk):
    """韩文字母表专用的一套：同一个声音、稍慢、音量统一（audio/ko-hg/，index-ko-hangul.json）"""
    idx = {}
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'x.wav'
        for text in items_hangul():
            file = f'ko-hg/{hashlib.sha1(text.encode()).hexdigest()[:16]}.mp3'
            try:
                model.tts_to_file(text, spk, str(wav), speed=0.8, quiet=True)
                norm_wav(wav)
                (OUT / file).parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', str(wav), '-ac', '1', '-b:a', '48k', str(OUT / file)], check=True)
            except Exception as e:
                print('  failed', type(e).__name__, flush=True)   # 不打印句子
                continue
            idx[text] = file
    (OUT / 'index-ko-hangul.json').write_text(json.dumps(idx, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print('ko hangul', len(idx))


def items(lang, which):
    if lang == 'zh':
        return items_zh(which)
    if which == 'daily':
        L = lambda k: json.loads((ROOT / 'lang' / f'{lang}-{k}.json').read_text(encoding='utf-8')) if (ROOT / 'lang' / f'{lang}-{k}.json').exists() else []
        out = [d[0] for d in L('daily')]
        out += [s for g in L('grammar') for s, _ in g['eg']]
        out += [s for a in L('reading') for para in a['p'] for s, _ in para] + [w[0] for a in L('reading') for w in a['w']]
        biz = L('biz')
        out += [x[0] for sc in (biz['phrases'] if biz else []) for x in sc['items']]
        # 商务场景会话的对话、关键表达和商务词汇
        out += [l[1] for sc in (biz.get('scenes', []) if biz else []) for l in sc['lines']] + [x[0] for sc in (biz.get('scenes', []) if biz else []) for x in sc.get('phr', [])]
        out += [w[0] for c in (biz.get('vocab', []) if biz else []) for w in c['words']]
        # 影视名台词和生活小对话
        out += [l['t'] for w in L('quotes') for l in w['lines']] + [x[1] for w in L('quotes') for l in w['lines'] for x in l.get('talk', [])]
        return out
    if which == 'basic':
        d = json.loads((ROOT / 'tools' / 'lang' / f'{lang}.json').read_text(encoding='utf-8'))
        return [r[0] for r in d['words'] + d['phrases']]
    out = []
    for k, _, _ in json.loads((ROOT / 'lang' / f'{lang}-levels.json').read_text(encoding='utf-8')):
        out += [r[0] for r in json.loads((ROOT / 'lang' / f'{lang}-{k}.json').read_text(encoding='utf-8'))]
    return out


ZH_VOWEL = re.compile('[aeiouüvāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]')


AUDIO_PY = None


def zh_forced_seq(text, hsk=None):
    """这段文字每个汉字应该怎么读：[(字, 带调拼音)]，来自 tools/zh/audio_py.json（build_zh.py 生成，和网页显示的拼音一样）"""
    global AUDIO_PY
    if AUDIO_PY is None:
        p = ROOT / 'tools' / 'zh' / 'audio_py.json'
        AUDIO_PY = json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
    py = AUDIO_PY.get(text)
    if not py:
        return None
    return seq_from_py(text, py)


DIG = dict(zip('0123456789', '零一二三四五六七八九'))
DIG_YAO = dict(DIG, **{'1': '幺'})


def zh_spoken_numbers(text):
    """数字按中国人的习惯读法写成汉字（MeloTTS、Kokoro 自己会把所有数字都读成数值：1987年 → 一千九百八十七年、12306 → 一万二千三百零六）：
    年份逐位读（一九八七年、二零一一年）；电话号码、房间号、航班号逐位读，1 读「幺」（幺二三零六、幺二零八房间）；
    网络用语 520、618、666 逐位读。其他（金额、长度、岁数……）交给引擎按数值读。"""
    for k, v in {'520': '五二零', '618': '六一八', '666': '六六六'}.items():
        text = re.sub(rf'(?<!\d){k}(?!\d)', v, text)
    text = re.sub(r'(?<!\d)(\d{4})(?=年)', lambda m: ''.join(DIG[c] for c in m.group(1)), text)
    text = re.sub(r'(?<!\d)(1\d{10}|12306|12345|12315|110|119|120|114|122)(?!\d)', lambda m: ''.join(DIG_YAO[c] for c in m.group(1)), text)
    text = re.sub(r'(?<!\d)(\d{3,5})(?=号?房间|号房|室)', lambda m: ''.join(DIG_YAO[c] for c in m.group(1)), text)
    text = re.sub(r'(?<=[A-Z]{2})(\d{3,4})(?=次?航班)', lambda m: ''.join(DIG_YAO[c] for c in m.group(1)), text)
    return text


def rec_val(text):
    """index-zh-py.json 里记的值：拼音；有数字的再加上数字读法的版本（数字读法改了也会重录）"""
    return zh_py(text) + (' #n2' if re.search(r'\d', text) else '')


def zh_py(text):
    """这段文字的拼音（tools/zh/audio_py.json，和网页显示的一样）；没有时 ''"""
    zh_forced_seq(text)
    return AUDIO_PY.get(text, '')


def seq_from_py(text, py):
    chars = re.findall(r'[一-鿿]', text)
    sy = py.split()
    if len(sy) != len(chars):
        return None
    return [(c, '' if p == '-' else p) for c, p in zip(chars, sy)]


def zh_patch():
    """让 MeloTTS 按我们给的拼音读：替换 melo 中文模块里的 lazy_pinyin。
    MeloTTS 先用 jieba 分词，再对每个词调用 lazy_pinyin(词, style=INITIALS) 和 (词, style=FINALS_TONE3)，
    我们按顺序在这段文字的拼音里找到这个词，返回对应的声母、韵母；对不上（比如数字被转成了汉字）就用原来的结果。"""
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
            print('  skip', name, e)
            continue
        if hasattr(m, 'lazy_pinyin'):
            m.lazy_pinyin = patched
            mods.append(name)
    # MeloTTS 的轻声规则把词尾的「的、地、得」一律读成轻声（土地 tǔdi、目的 mùdi、我得 dei5）：我们给了非轻声读音的，改回去
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
    except Exception as e:
        print('  tone patch skipped', e)
    return state


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


def synth_zh(model, spk, text, wav, state, hsk):
    """生成一条中文录音，检查里面确实有声音；没有就换几种读法再试（加句号、读两遍取一遍、调参数），最后统一音量。
    返回 True 表示成功写好了 wav。"""
    import shutil
    # 拼音按原文查（文件名、目录也用原文）；数字按习惯读法写成汉字再合成（年份、号码逐位读）
    seq = zh_forced_seq(text, hsk)
    text = zh_spoken_numbers(text)
    n = len(re.findall(r'[一-鿿]', text)) or 1
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
        except Exception as e:
            print('  failed', name, repr(text), e, flush=True)
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
    print('  no good take', repr(text), best, flush=True)
    return False


def asr_score(wav, text):
    """用语音识别听这条录音，和原文比：返回 (分数 0–1, 识别结果)。识别引擎和比法见 tools/asr_check_zh.py（默认 FunASR Paraformer）"""
    import asr_check_zh as A
    hyp = A.transcribe(wav)
    return A.score(text, hyp), hyp


def retake_zh(model, spk, text, wav, state, hsk, tries=6):
    """听写检查没通过的录音：换几种合成参数重新生成，每一条都用语音识别听一遍，留下分数最高的（≥0.95 就停）。
    返回 (分数, 识别结果)；wav 是留下的那一条（已统一音量）"""
    import shutil
    seq = zh_forced_seq(text, hsk)
    t = zh_spoken_numbers(text)
    end = '' if re.search(r'[。！？!?.]$', t) else '。'
    variants = [(t + end, 0.9, {}), (t + end, 1.0, {'sdp_ratio': 0.5, 'noise_scale': 0.667}), (t + end, 0.85, {'noise_scale': 0.4}),
                (t + end, 0.95, {'sdp_ratio': 0.0, 'noise_scale': 0.5, 'noise_scale_w': 0.6}), (t + '！' if not end else t, 0.9, {}),
                (t + end, 0.8, {'sdp_ratio': 0.3})][:tries]
    best, tmp = None, wav.with_name('retake.wav')
    for tt, sp, kw in variants:
        state['seq'], state['pos'], state['last'] = seq, 0, None
        try:
            model.tts_to_file(tt, spk, str(tmp), speed=sp, quiet=True, **kw)
        except Exception as e:   # noqa: BLE001
            print('  retake failed', type(e).__name__, flush=True)
            continue
        finally:
            state['seq'] = None
        if wav_speech(tmp)[0] <= -45:
            continue
        norm_wav(tmp)
        sc, hyp = asr_score(tmp, text)
        if best is None or sc > best[0]:
            best = (sc, hyp)
            shutil.copy(tmp, wav)
        if sc >= 0.95:
            break
    return best


def zh_retake(a, model, spk):
    """听写检查（asr-zh.json，zh-asr.yml 生成）分数低的句子：重新生成，选语音识别分数最高的一条。
    只处理 MeloTTS 的句子和单词（「说话人|原文」是 Kokoro 的听力台词，不在这里）。分数没有比原来高的不替换"""
    rep = json.loads((OUT / 'asr-zh.json').read_text(encoding='utf-8'))
    idx_path = OUT / 'index-zh.json'
    idx = json.loads(idx_path.read_text(encoding='utf-8'))
    import asr_check_zh as A
    todo = [(k, v[0]) for k, v in rep.get('low', {}).items() if '|' not in k and k in idx
            and (len(A.syllables(A.spoken(k))) >= 3 or v[0] <= 0.0)]
    todo += [(k, -1) for k in rep.get('no_recording', []) if '|' not in k]
    if a.limit:
        todo = todo[:a.limit]
    print('retake:', len(todo), flush=True)
    hsk = {}
    for L in range(1, 7):
        p = ROOT / 'lang' / f'zh-hsk{L}.json'
        if p.exists():
            hsk.update({r[0]: r[1] for r in json.loads(p.read_text(encoding='utf-8')) if len(r[0]) == 1})
    state = zh_patch()
    prefix = PREFIX or 'zh'
    (OUT / prefix).mkdir(parents=True, exist_ok=True)
    better = same = 0
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'x.wav'
        for text, old in todo:
            r = retake_zh(model, spk, text, wav, state, hsk)
            if not r or r[0] <= max(old, 0) + 0.05:
                same += 1
                continue
            file = f'{prefix}/{hashlib.sha1(("v4r:" + text + "|" + rec_val(text)).encode()).hexdigest()[:16]}.mp3'
            subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', str(wav), '-ac', '1', '-b:a', '48k', str(OUT / file)], check=True)
            idx[text] = file
            better += 1
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'retake: better {better}, not better {same}')


def zh_fix(a, model, spk):
    """中文录音的读音修正：先算出每条录音原来会怎么读（MeloTTS 默认），再算出应该怎么读（zh_py），
    不一样的重新生成（文件名换新，旧缓存不影响）；拼音表里新换的例字也一起生成。"""
    from melo.text.cleaner import clean_text
    lang = getattr(model, 'language', 'ZH')
    idx_path = OUT / 'index-zh.json'
    idx = json.loads(idx_path.read_text(encoding='utf-8')) if idx_path.exists() else {}
    texts = list(dict.fromkeys(items_zh('basic') + items_zh('levels') + items_zh('daily')))
    g2p = lambda t: tuple(map(tuple, clean_text(t, lang)[1:3]))
    before = {}
    for t in texts:   # ① 原来的读法（还没加载我们的拼音纠正）
        try:
            before[t] = g2p(t)
        except Exception as e:
            before[t] = None
    hsk = {}
    for L in range(1, 7):
        p = ROOT / 'lang' / f'zh-hsk{L}.json'
        if p.exists():
            hsk.update({r[0]: r[1] for r in json.loads(p.read_text(encoding='utf-8')) if len(r[0]) == 1})
    state = zh_patch()
    todo, shown = [], 0
    for t in texts:   # ② 应该的读法
        state['seq'], state['pos'] = zh_forced_seq(t, hsk), 0
        try:
            after = g2p(t)
        except Exception as e:
            print('  g2p failed', repr(t), e)
            continue
        finally:
            state['seq'] = None
        if t not in idx or after != before.get(t):
            todo.append(t)
            if t in idx and shown < 40:
                shown += 1
                print('  diff', t[:30], '|', ' '.join(before[t][0][1:-1]) if before.get(t) else '-', '→', ' '.join(after[0][1:-1]), flush=True)
    print(len(todo), 'to (re)generate of', len(texts), flush=True)
    if a.limit:
        todo = todo[:a.limit]
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'x.wav'
        for n, text in enumerate(todo, 1):
            file = f'zh/{hashlib.sha1(("v3:" + text).encode()).hexdigest()[:16]}.mp3'
            if not synth_zh(model, spk, text, wav, state, hsk):
                continue   # 没有一次读出声音：先不放录音，网页用手机自带的中文朗读
            subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', str(wav), '-ac', '1', '-b:a', '48k', str(OUT / file)], check=True)
            idx[text] = file
            if n % 100 == 0:
                print(f'  {n}/{len(todo)}  {n / (time.time() - t0):.1f}/s', flush=True)
                idx_path.write_text(json.dumps(idx, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
            if a.commit_every and n % a.commit_every == 0:
                commit(f'audio: zh fix +{a.commit_every} (MeloTTS)')
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    used = {f.split('/', 1)[1] for f in idx.values() if f.startswith('zh/')}
    gone = [p for p in (OUT / 'zh').glob('*.mp3') if p.name not in used]
    for p in gone:
        p.unlink()
    print('zh fix: index', len(idx), 'removed', len(gone), 'unused files')
    print('zh takes:', ZH_STATS)


def g2p_default_vs_forced(texts):
    """{原文: (MeloTTS 自己的读法, 按我们的拼音的读法)}（只算音素，不合成）。用来判断以前的录音读得对不对"""
    from melo.text.chinese import g2p as _g2p, text_normalize
    g = lambda t: tuple(map(tuple, _g2p(text_normalize(t))[:2]))
    out = {}
    for t in texts:
        try:
            out[t] = [g(t), None]
        except Exception:
            out[t] = [None, None]
    return out


def zh_add(a, model, spk, texts=None):
    """中文录音：没有录音的、拼音改了的（和录音时记下的拼音不一样）都按网页显示的拼音生成。
    OUT/index-zh.json：原文 → 文件（工作流先放进主站和本仓库已有的）；OUT/index-zh-py.json：原文 → 录音时用的拼音
    （以前的录音没有记录：MeloTTS 自己的读法和我们的拼音读出来一样就算对，记下；空字符串 = 已知读错，要重录）。
    文件名带拼音的哈希，改了读音就是新文件，浏览器和 CDN 的旧缓存不影响。"""
    idx_path, py_path = OUT / 'index-zh.json', OUT / 'index-zh-py.json'
    idx = json.loads(idx_path.read_text(encoding='utf-8')) if idx_path.exists() else {}
    rec = json.loads(py_path.read_text(encoding='utf-8')) if py_path.exists() else {}
    texts = list(dict.fromkeys(texts if texts is not None else items_zh(a.which)))
    hsk = {}
    for L in range(1, 7):
        p = ROOT / 'lang' / f'zh-hsk{L}.json'
        if p.exists():
            hsk.update({r[0]: r[1] for r in json.loads(p.read_text(encoding='utf-8')) if len(r[0]) == 1})
    legacy = [t for t in texts if t in idx and t not in rec and zh_py(t) and not re.search(r'\d', t)]
    before = g2p_default_vs_forced(legacy)   # ① 没打补丁时：MeloTTS 自己的读法
    state = zh_patch()
    ok = 0
    for t in legacy:   # ② 按我们的拼音
        state['seq'], state['pos'], state['last'] = zh_forced_seq(t), 0, None
        try:
            after = g2p_default_vs_forced([t])[t][0]
        finally:
            state['seq'] = None
        if before[t][0] is not None and before[t][0] == after:
            rec[t] = rec_val(t)
            ok += 1
    todo = [t for t in texts if t not in idx or (zh_py(t) and rec.get(t) != rec_val(t))]
    print(f'zh: {len(texts)} texts, legacy ok {ok}/{len(legacy)}, to (re)generate {len(todo)}', flush=True)
    if a.limit:
        todo = todo[:a.limit]
    prefix = PREFIX or 'zh'
    (OUT / prefix).mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'x.wav'
        for n, text in enumerate(todo, 1):
            py = zh_py(text)
            file = f'{prefix}/{hashlib.sha1(("v4:" + text + "|" + rec_val(text)).encode()).hexdigest()[:16]}.mp3'
            if not synth_zh(model, spk, text, wav, state, hsk):
                continue
            subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', str(wav), '-ac', '1', '-b:a', '48k', str(OUT / file)], check=True)
            idx[text] = file
            if py:
                rec[text] = rec_val(text)
            if n % 100 == 0:
                print(f'  {n}/{len(todo)}  {n / (time.time() - t0):.1f}/s', flush=True)
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    py_path.write_text(json.dumps(rec, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print('zh takes:', ZH_STATS)


def commit(msg):
    subprocess.run(['git', 'add', 'audio'], cwd=ROOT, check=True)
    if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=ROOT).returncode == 0:
        return
    subprocess.run(['git', 'commit', '-qm', msg], cwd=ROOT, check=True)
    for _ in range(3):
        branch = os.environ.get('GITHUB_REF_NAME', 'main')   # 推回运行的分支
        if subprocess.run(['git', 'pull', '-q', '--rebase', '-X', 'theirs', 'origin', branch], cwd=ROOT).returncode == 0 and \
           subprocess.run(['git', 'push', '-q', 'origin', f'HEAD:{branch}'], cwd=ROOT).returncode == 0:
            return
        time.sleep(5)
    sys.exit('push failed')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('lang', choices=['ko', 'en', 'zh'])
    ap.add_argument('which', choices=['basic', 'levels', 'daily', 'all', 'retake', 'listen', 'fix', 'hangul'])
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--commit_every', type=int, default=0)
    a = ap.parse_args()
    from melo.api import TTS
    language, speaker = VOICE[a.lang]
    model = TTS(language=language, device='cpu')
    spk = model.hps.data.spk2id[speaker]
    if a.which == 'listen':
        return listen(a, model)
    if a.which == 'fix':
        return zh_fix(a, model, spk)
    if a.which == 'hangul':
        return hangul(model, spk)
    if a.lang == 'zh' and a.which == 'retake':
        return zh_retake(a, model, spk)
    if a.lang == 'zh':   # 中文一律按网页显示的拼音读（多音字纠正），拼音改了的重录
        return zh_add(a, model, spk, items_zh('basic') + items_zh('levels') + items_zh('daily') if a.which == 'all' else None)
    idx_path = OUT / f'index-{a.lang}.json'
    idx = json.loads(idx_path.read_text(encoding='utf-8')) if idx_path.exists() else {}
    todo = [t for t in dict.fromkeys(items(a.lang, a.which)) if t not in idx]
    if a.limit:
        todo = todo[:a.limit]
    print(len(todo), 'to generate', flush=True)
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'x.wav'
        for n, text in enumerate(todo, 1):
            file = f'{PREFIX or a.lang}/{hashlib.sha1(text.encode()).hexdigest()[:16]}.mp3'
            try:
                # 单词读得稍慢一点，句子正常语速
                model.tts_to_file(text, spk, str(wav), speed=0.85 if len(text) < 12 else 0.95, quiet=True)
                if wav_speech(wav)[0] <= -45:   # 读出来没有声音：不放这个文件（网页上就不播，下次再试）
                    print('  silent, skipped', flush=True)
                    continue
                norm_wav(wav)   # 音量统一（以前这里漏了，有两成句子比别的小声 6 dB 以上）
                (OUT / file).parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', str(wav), '-ac', '1', '-b:a', '40k', str(OUT / file)], check=True)
            except Exception as e:
                print('  failed', type(e).__name__, flush=True)   # 不打印句子
                continue
            idx[text] = file
            if n % 100 == 0:
                print(f'  {n}/{len(todo)}  {n / (time.time() - t0):.1f}/s', flush=True)
                idx_path.write_text(json.dumps(idx, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
            if a.commit_every and n % a.commit_every == 0:
                commit(f'audio: {a.lang} {a.which} +{a.commit_every} (MeloTTS)')
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(a.lang, a.which, 'index', len(idx))


# 听力对话：和网页 listen.js 的 lsSegs 顺序相同。MeloTTS 每种口音只有一个声音，男声用降调区分；英语旁白用英式口音
NUM = {'ko': ['일 번', '이 번', '삼 번', '사 번'], 'en': ['One.', 'Two.', 'Three.', 'Four.']}
OTHER = {'M': 'F', 'F': 'M', 'M2': 'F', 'F2': 'M'}
SPOKEN = {'gaiyou', 'hatsuwa', 'sokuji'}


def segs(x, lang):
    s = []
    if x.get('intro'):
        s.append(['N', x['intro'], 1.2])
    for spk, line, _ in x['lines']:
        s.append([spk, line, 0.5])
    if x['lines']:
        s[-1][2] = 1.2
    if x.get('q') and x['t'] not in ('hatsuwa', 'sokuji'):
        s.append(['N', x['q'], 1.0])
    if x['t'] in SPOKEN:
        v = OTHER[x['lines'][0][0]] if x['t'] == 'sokuji' else 'N'
        for i, c in enumerate(x['c']):
            s.append(['N', NUM[lang][i], 0.3])
            s.append([v, c, 0.9])
    return s


def listen(a, model):
    import wave
    spk2id = model.hps.data.spk2id
    voice = {'ko': {'N': 'KR', 'F': 'KR', 'M': 'KR', 'F2': 'KR', 'M2': 'KR'},
             'en': {'N': 'EN-BR', 'F': 'EN-US', 'M': 'EN-AU', 'F2': 'EN-Default', 'M2': 'EN_INDIA'}}[a.lang]
    pitch = {'M': 0.82, 'M2': 0.88, 'F2': 1.06}       # 降调 = 男声
    idx_path = OUT / f'index-{a.lang}-listen.json'
    idx = json.loads(idx_path.read_text(encoding='utf-8')) if idx_path.exists() else {}
    data = json.loads((ROOT / 'lang' / f'{a.lang}-listen.json').read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory() as tmp:
        for n, x in enumerate([x for x in data if x['id'] not in idx], 1):
            pcm, rate = b'', 44100
            try:
                for k, (spk, text, pause) in enumerate(segs(x, a.lang)):
                    w1, w2 = Path(tmp) / f'{k}.wav', Path(tmp) / f'{k}p.wav'
                    model.tts_to_file(text, spk2id[voice.get(spk, voice['F'])], str(w1), speed=1.0, quiet=True)
                    f = pitch.get(spk)
                    if f:
                        subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', str(w1), '-af', f'asetrate=44100*{f},aresample=44100,atempo={1 / f:.4f}', '-ac', '1', '-ar', '44100', str(w2)], check=True)
                    else:
                        subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', str(w1), '-ac', '1', '-ar', '44100', str(w2)], check=True)
                    with wave.open(str(w2)) as w:
                        rate = w.getframerate(); pcm += w.readframes(w.getnframes())
                    pcm += b'\0\0' * int(rate * pause)
            except Exception as e:
                print('  failed', x['id'], e, flush=True)
                continue
            out = Path(tmp) / 'all.wav'
            with wave.open(str(out), 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
            file = f"{a.lang}-listen/{x['id']}.mp3"
            (OUT / file).parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', str(out), '-ac', '1', '-b:a', '48k', str(OUT / file)], check=True)
            idx[x['id']] = file
            idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=0), encoding='utf-8')
            print(n, x['id'], flush=True)
    print(a.lang, 'listen index', len(idx))


if __name__ == '__main__':
    main()
