"""中文听力对话里「不是第一个女声」的人：用 Kokoro-82M（https://huggingface.co/hexgrad/Kokoro-82M ，Apache 2.0）的普通话声音，
每个人一个真正的声音（以前是把 MeloTTS 女声的录音降调慢放冒充男声，听起来不自然）。

    F（第一个女声）、N（旁白）：还是 MeloTTS 那条录音（audio/index-zh.json，按文字）
    M  男声    zm_yunjian
    M2 第二个男声  zm_yunxi
    F2 第二个女声  zf_xiaoxiao（和 MeloTTS 的女声分得开）

在 GitHub Actions 里运行（.github/workflows/zh-voices.yml）：
    python3 tools/gen_audio_zh_voices.py listen       免费听力（lang/zh-listen.json）→ audio/zh-v/*.mp3、audio/index-zh-voices.json
    python3 tools/gen_audio_zh_voices.py check        不需要模型：只算数量、检查每句都能转成音素（本地试用）
会员听力题的同样的声音由 tools/gen_member_audio.py zh-voices 生成（私有存储 zh/v/…，索引 audio/zh-voices.json）。

索引的 key 是「说话人|原文」（zh.js zhPlayLines：`${spk}|${zh}`），值是文件路径。
文件名由 模型、声音、语速、原文 决定：换了声音或语速会重新生成；已经生成过的跳过；不再用到的文件删掉。
读音：和网页显示的拼音一样（tools/zh/audio_py.json，没有时用这句台词自带的拼音），对不上的字用 pypinyin 默认读法。
音量：和 MeloTTS 中文录音同一套（gen_audio_melo.norm_wav，-21.5 dB），44.1 kHz、48 kbps，同一段对话里男女声一样响。
日志只打印数量（不打印句子）。
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ['ZH_VOICES_DIR']) if os.environ.get('ZH_VOICES_DIR') else ROOT / 'audio'   # 公开的音频仓库里生成时指到那边
sys.path.insert(0, str(ROOT / 'tools'))
import gen_audio_melo as M   # noqa: E402  只用到 norm_wav、wav_speech、zh_forced_seq（不需要 MeloTTS）

# Kokoro v1.0 的普通话声音。换成 hexgrad/Kokoro-82M-v1.1-zh（中文数据多得多，声音 zf_001… / zm_009… 之类）时设 KOKORO_ZH_REPO 和 KOKORO_ZH_VOICES
REPO = os.environ.get('KOKORO_ZH_REPO') or 'hexgrad/Kokoro-82M'
VOICE = {'M': 'zm_yunjian', 'M2': 'zm_yunxi', 'F2': 'zf_xiaoxiao'}
VOICE.update(json.loads(os.environ.get('KOKORO_ZH_VOICES') or '{}'))
SPEED = {'HSK1': 0.88, 'HSK2': 0.9}   # 其他级别 0.95（和 MeloTTS 女声的句子差不多快）
FORCE = os.environ.get('KOKORO_FORCE') == '1'
RATE = 24000
HAN = re.compile(r'[一-鿿]')
SYL = re.compile(r"[a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜńňǹḿ]+")
ZH_VOWEL = re.compile('[aeiouüvāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]')
# 台词里的英文缩写（HSK、AI、G79 次列车）按字母读；misaki 的英语音素写法（A = eɪ，I = aɪ，O = oʊ）
LETTER = {'A': 'ˈA', 'B': 'bˈi', 'C': 'sˈi', 'D': 'dˈi', 'E': 'ˈi', 'F': 'ˈɛf', 'G': 'ʤˈi', 'H': 'ˈAʧ', 'I': 'ˈI', 'J': 'ʤˈA',
          'K': 'kˈA', 'L': 'ˈɛl', 'M': 'ˈɛm', 'N': 'ˈɛn', 'O': 'ˈO', 'P': 'pˈi', 'Q': 'kjˈu', 'R': 'ˈɑɹ', 'S': 'ˈɛs', 'T': 'tˈi',
          'U': 'jˈu', 'V': 'vˈi', 'W': 'dˈʌbᵊlju', 'X': 'ˈɛks', 'Y': 'wˈI', 'Z': 'zˈi'}


def legacy():
    """v1.0 的中文音素是「每个词的拼音 → IPA」（misaki ZHG2P.legacy_call）；v1.1-zh 换了一套前端"""
    return REPO.rstrip('/').endswith('/Kokoro-82M')


def seq_of(text, py=''):
    """每个汉字应该怎么读 [(字, 带调拼音)]：先查 audio_py.json（网页显示的拼音），再用这句台词自带的拼音（音节数要和汉字数一样）"""
    s = M.zh_forced_seq(text)
    if s:
        return s
    chars, sy = HAN.findall(text), SYL.findall(py or '')
    if chars and len(chars) == len(sy):
        return list(zip(chars, sy))
    return None


def three_sandhi(plist):
    """上声连读变调：连在一起的几个第三声，除了最后一个都读第二声（你好 ní hǎo、我也很好 wó yé hén hǎo）。
    网页上的拼音按教材标原调，MeloTTS 自己会变调；Kokoro 的音素按拼音直接转，不变调，所以在这里变。plist：[(词, [拼音tone3…])]，就地修改"""
    flat = [(i, j) for i, (_, pys) in enumerate(plist) for j in range(len(pys))]
    tone = lambda ij: plist[ij[0]][1][ij[1]][-1:]
    k = 0
    while k < len(flat):
        if tone(flat[k]) != '3':
            k += 1
            continue
        e = k
        while e + 1 < len(flat) and tone(flat[e + 1]) == '3':
            e += 1
        for ij in flat[k:e]:
            i, j = ij
            plist[i][1][j] = plist[i][1][j][:-1] + '2'
        k = e + 1


def zh_phonemes(text, seq=None, en=None):
    """和 misaki ZHG2P(v1.0) 一样把中文转成 Kokoro 的音素，只是每个字的拼音用我们给的（seq）；
    数字先转成汉字（cn2an），英文缩写按字母读（en：可选的英语 G2P，用于不是缩写的英文词）。返回音素字符串"""
    import cn2an
    import jieba
    from misaki.zh import ZHG2P
    from pypinyin import Style, lazy_pinyin
    from pypinyin.contrib.tone_convert import to_tone3
    try:
        t = cn2an.transform(M.zh_spoken_numbers(text), 'an2cn')   # 年份、号码逐位读
    except Exception:   # noqa: BLE001
        t = text
    t = ZHG2P.map_punctuation(t)
    chars, pos, out = [c for c, _ in seq] if seq else [], 0, []

    def find(w, start):
        for k in range(start, min(len(chars) - len(w), start + 60) + 1):
            if chars[k:k + len(w)] == w:
                return k
        return -1

    for seg in re.findall(r'[一-鿿]+|[A-Za-z][A-Za-z\'-]*|[^一-鿿A-Za-z]+', t):
        if HAN.match(seg):
            words, plist = [], []
            for w in jieba.lcut(seg, cut_all=False):
                pys = lazy_pinyin(w, style=Style.TONE3, neutral_tone_with_five=True)
                k = find(list(w), pos) if seq and len(pys) == len(w) else -1
                if k >= 0:
                    pys = [to_tone3(seq[k + j][1], neutral_tone_with_five=True) if seq[k + j][1] and ZH_VOWEL.search(seq[k + j][1]) else pys[j] for j in range(len(w))]
                    pos = k + len(w)
                plist.append((w, pys))
            three_sandhi(plist)
            for w, pys in plist:
                ipa = []
                for p in pys:
                    try:
                        ipa.append(ZHG2P.py2ipa(p))
                    except Exception:   # noqa: BLE001  拼音转不了（儿化的 r 之类）：用 misaki 自己的读法
                        ipa.append(ZHG2P.word2ipa(w) if len(w) == 1 else '')
                words.append(''.join(ipa))
            out.append(' '.join(x for x in words if x))
        elif re.match('[A-Za-z]', seg):
            if seg.isupper() and len(seg) <= 6 or not en:
                out.append(' ' + ' '.join(LETTER.get(c.upper(), '') for c in seg if c.isalpha()) + ' ')
            else:
                out.append(f' {en(seg)} ')
        else:
            out.append(seg)
    return re.sub(r'\s+', ' ', ''.join(out).replace(chr(815), '')).strip()


def chunks(ps, limit=400):
    """Kokoro 一次最多 510 个音素：长句在句号、问号、逗号处切开（整句一起合成更自然，所以只在太长时才切）"""
    if len(ps) <= limit:
        return [ps]
    out, cur = [], ''
    for part in re.split(r'(?<=[.!?;:,])\s+', ps):
        if cur and len(cur) + len(part) + 1 > limit:
            out.append(cur)
            cur = part
        else:
            cur = f'{cur} {part}'.strip()
    if cur:
        out.append(cur)
    return [c[:510] for c in out]


class Synth:
    def __init__(self):
        from kokoro import KPipeline
        self.pipe = KPipeline(lang_code='z', repo_id=REPO)
        self._en = None

    def en(self, s):
        """台词里不是缩写的英文词：用英语 G2P 转音素（第一次用到时才加载）；用不了就按字母读"""
        if self._en is None:
            try:
                from kokoro import KPipeline
                g = KPipeline(lang_code='a', repo_id=REPO, model=False).g2p
                self._en = lambda x: g(x)[0]
            except Exception:   # noqa: BLE001
                self._en = lambda x: ' '.join(LETTER.get(c.upper(), '') for c in x if c.isalpha())
        return self._en(s)

    def __call__(self, text, voice, speed, seq=None):
        """返回 24 kHz 的 float 数组；读不出来返回 None"""
        import numpy as np
        if legacy():
            parts = [r.audio for c in chunks(zh_phonemes(text, seq, self.en)) for r in self.pipe.generate_from_tokens(c, voice=voice, speed=speed)]
        else:
            parts = [r.audio for r in self.pipe(text, voice=voice, speed=speed)]
        parts = [p.numpy() if hasattr(p, 'numpy') else p for p in parts if p is not None]
        return np.concatenate(parts) if parts else None


def write_mp3(samples, mp3, n_han, kbps=48):
    """写 wav → 检查真的有声音 → 统一音量（和 MeloTTS 中文一样）→ mp3。没有声音返回 False"""
    import numpy as np
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'x.wav'
        with wave.open(str(wav), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
            w.writeframes((np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes())
        pk, act = M.wav_speech(wav)[:2]
        if pk <= -45 or act < (0.15 if n_han <= 1 else 0.08 * n_han):
            return False
        M.norm_wav(wav)
        mp3.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-i', str(wav), '-ar', '44100', '-ac', '1', '-b:a', f'{kbps}k', str(mp3)], check=True)
    return True


def lines_of(items):
    """听力题 → {key: (说话人, 原文, 语速, seq)}，key = 「说话人|原文」。同一句出现在几个级别时用最慢的语速"""
    jobs = {}
    for x in items:
        sp = SPEED.get(x.get('lv'), 0.95)
        for ln in x.get('lines', []):
            spk, text = ln[0], ln[1]
            if spk not in VOICE or not text or not text.strip():
                continue
            key = f'{spk}|{text}'
            if key in jobs and jobs[key][2] <= sp:
                continue
            jobs[key] = (spk, text, sp, seq_of(text, ln[3] if len(ln) > 3 else ''))
    return jobs


def file_of(spk, text, speed, prefix='zh-v', seq=None):
    """文件名由 模型、声音、语速、原文、读音（拼音）决定：拼音改了就是新文件，会重新生成"""
    tag = f'{REPO}|{VOICE[spk]}|{speed}|{text}|v1'
    if seq:
        tag += '|' + ' '.join(p for _, p in seq) + '|v3' + ('|n2' if re.search(r'\d', text) else '')   # v3：上声连读变调
    return f'{prefix}/{hashlib.sha1(tag.encode()).hexdigest()[:16]}.mp3'


def run_listen(limit=0):
    data = json.loads((ROOT / 'lang' / 'zh-listen.json').read_text(encoding='utf-8'))
    jobs = lines_of(data)
    idx_path = OUT / 'index-zh-voices.json'
    old = {} if FORCE or not idx_path.exists() else json.loads(idx_path.read_text(encoding='utf-8'))
    idx, todo = {}, []
    for key, (spk, text, sp, seq) in jobs.items():
        f = file_of(spk, text, sp, seq=seq)
        if old.get(key) == f and (OUT / f).exists():
            idx[key] = f
        else:
            todo.append((key, f, spk, text, sp, seq))
    if limit:
        todo = todo[:limit]
    print(f'zh voices: {len(jobs)} lines ({", ".join(f"{s} {sum(1 for j in jobs.values() if j[0] == s)}" for s in VOICE)}), '
          f'{len(idx)} done, {len(todo)} to generate, forced pinyin {sum(1 for j in jobs.values() if j[3])}', flush=True)
    synth, ok, bad = (Synth() if todo else None), 0, 0
    for key, f, spk, text, sp, seq in todo:
        try:
            a = synth(text, VOICE[spk], sp, seq)
            if a is not None and write_mp3(a, OUT / f, len(HAN.findall(text))):
                idx[key] = f
                ok += 1
            else:
                bad += 1
        except Exception as e:   # noqa: BLE001  只打印错误类型（不打印句子）
            print('  failed:', type(e).__name__, flush=True)
            bad += 1
        if (ok + bad) % 20 == 0:
            print(f'  {ok + bad}/{len(todo)}', flush=True)
            idx_path.write_text(json.dumps(idx, ensure_ascii=False, separators=(',', ':'), sort_keys=True), encoding='utf-8')
    gone = 0
    if not limit:   # 试跑（--limit）时不删旧文件
        used = set(idx.values())
        for p in (OUT / 'zh-v').glob('*.mp3') if (OUT / 'zh-v').exists() else []:
            if f'zh-v/{p.name}' not in used:
                p.unlink()
                gone += 1
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, separators=(',', ':'), sort_keys=True), encoding='utf-8')
    print(f'zh voices: {ok} new, {bad} failed, index {len(idx)}, removed {gone} unused files')


def run_check():
    """不需要 Kokoro 模型：把每句转成音素（需要 misaki[zh]），只打印数量"""
    data = json.loads((ROOT / 'lang' / 'zh-listen.json').read_text(encoding='utf-8'))
    jobs = lines_of(data)
    lens, long_, err = [], 0, 0
    for spk, text, sp, seq in jobs.values():
        try:
            ps = zh_phonemes(text, seq)
            lens.append(len(ps))
            long_ += len(chunks(ps)) > 1
        except Exception as e:   # noqa: BLE001
            err += 1
            print('  g2p failed:', type(e).__name__)
    print(f'check: {len(jobs)} lines, forced pinyin {sum(1 for j in jobs.values() if j[3])}, g2p errors {err}, '
          f'max phonemes {max(lens or [0])}, split into chunks {long_}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['listen', 'check'])
    ap.add_argument('--limit', type=int, default=0, help='最多生成多少句（试跑用；0 = 全部）')
    a = ap.parse_args()
    run_listen(a.limit) if a.mode == 'listen' else run_check()
