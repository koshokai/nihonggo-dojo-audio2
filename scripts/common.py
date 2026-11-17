"""三种语言共用的部分：下载公开的词典和现有录音目录、算出要生成的词、分片、写索引。

词典（koshokai/nihonggo-dojo 公开仓库）：
    dict.json          日语 JMdict 常用词  [[写法...], [读音...], [[英文释义...]...], 词性, 中文]
    lang/zh-dict.json  中文 HSK 以外的常用词  [词, 拼音, 日语]
    lang/en-dict.json  英语 CEFR 以外的常用词  [词, 音标, 中文, 日语]
现有录音的目录（这些词已经有录音，不再生成）：
    日语  https://koshokai.github.io/nihonggo-dojo-audio/ja/index-{words,daily,examples,ja-human,kana}.json
    中文  https://koshokai.github.io/nihonggo-dojo/audio/index-zh.json、index-zh-human.json
    英语  https://koshokai.github.io/nihonggo-dojo-audio/index-en.json、index-en-human.json
每个文件先试 GitHub Pages，再试 raw.githubusercontent.com（内容一样，都是公开的，不需要 token）。
离线时用 --site-dir（nihonggo-dojo 的本地副本）和 --audio-dir（nihonggo-dojo-audio 的本地副本）。

索引的 key 必须和网页朗读时传的文本完全一样（见 README）：
    日语  「写法|读音」（dict.js jmCard 里的 w + '|' + e.r[0]，w = e.k[0] || e.r[0]），另外每个读音还有一个只用读音的 key
    中文  词本身（zh.js：sayBtn(x.w, 'zh-CN')）
    英语  词本身（en.js：sayBtn(x.w, 'en-US')）
"""
import collections
import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LANGS = ('ja', 'zh', 'en')
FOLDER = {lang: f'{lang}-d' for lang in LANGS}          # 录音文件夹 ja-d/ zh-d/ en-d/
FOLDER['ja'] = 'ja-d2'   # 日语换成冥鳴ひまり：新声音放在新文件夹（文件名只按词算，同名会被当成已经生成过）
INDEX = {lang: f'index-dict-{lang}.json' for lang in LANGS}
PART = 'index-dict-{lang}.part.json'                     # 每个分片新生成的部分（合并时用）

SITE_PAGES = 'https://koshokai.github.io/nihonggo-dojo/'
SITE_RAW = 'https://raw.githubusercontent.com/koshokai/nihonggo-dojo/{ref}/'
AUD_PAGES = 'https://koshokai.github.io/nihonggo-dojo-audio/'
AUD_RAW = 'https://raw.githubusercontent.com/koshokai/nihonggo-dojo-audio/main/'

DICT_SRC = {'ja': 'dict.json', 'zh': 'lang/zh-dict.json', 'en': 'lang/en-dict.json'}
# (仓库, 路径)：site = nihonggo-dojo，audio = nihonggo-dojo-audio。和网页加载的目录一致：
#   日语 app.js speak()：HUMAN_JA（index-ja-human）|| KANA_IDX（index-kana）|| AUDIO_IDX（index-words、daily、examples，打开网页就加载）
#   中文 lang.js langAudioIdx('zh')：audio/index-zh.json + index-zh-human.json（AUD_ZH = 主站 audio/）
#   英语 lang.js langAudioIdx('en')：index-en.json + index-en-human.json（AUD_EN = nihonggo-dojo-audio 根目录）
EXISTING = {
    'ja': [('audio', f'ja/index-{n}.json') for n in ('words', 'daily', 'examples', 'ja-human', 'kana')],
    'zh': [('site', 'audio/index-zh.json'), ('site', 'audio/index-zh-human.json')],
    'en': [('audio', 'index-en.json'), ('audio', 'index-en-human.json')],
}


def fetch_json(repo, path, ref='main', site_dir=None, audio_dir=None):
    """先用本地副本（离线测试），否则依次试 GitHub Pages 和 raw.githubusercontent.com。都失败就停下（不能少排除一部分，否则会重复生成）"""
    local = site_dir if repo == 'site' else audio_dir
    if local:
        return json.loads((Path(local) / path).read_text(encoding='utf-8'))
    if repo == 'site':
        urls = ([SITE_PAGES + path] if ref == 'main' else []) + [SITE_RAW.format(ref=ref) + path]
    else:
        urls = [AUD_PAGES + path, AUD_RAW + path]
    err = None
    for url in urls:
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'dict-audio-bot', 'Cache-Control': 'no-cache'})
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read().decode('utf-8'))
            except Exception as e:   # noqa: BLE001
                err = e
                time.sleep(2 * (attempt + 1))
    raise SystemExit(f'cannot fetch {repo}:{path} ({err})')


def existing_keys(lang, **kw):
    keys = set()
    for repo, path in EXISTING[lang]:
        keys |= set(fetch_json(repo, path, **kw))
    return keys


def entries(lang, ref='main', site_dir=None, audio_dir=None):
    """词典里的全部词条，按词典顺序：[{key, text, kana/py, read}]。
    read：网页只传读音时用的 key（日语是读音，中文、英语就是 key 本身）"""
    rows = fetch_json('site', DICT_SRC[lang], ref=ref, site_dir=site_dir, audio_dir=audio_dir)
    out, seen = [], set()
    for row in rows:
        if lang == 'ja':
            k, r = row[0], row[1]
            if not r or not r[0]:
                continue
            w = k[0] if k else r[0]                          # = dict.js jmCard 的 w
            e = {'key': f'{w}|{r[0]}', 'text': w, 'kana': r[0], 'read': r[0], 'zh': bool(len(row) > 4 and row[4])}
        elif lang == 'zh':
            if not row or not row[0]:
                continue
            e = {'key': row[0], 'text': row[0], 'py': row[1] if len(row) > 1 else '', 'read': row[0]}
        else:
            if not row or not row[0] or not row[0].strip():
                continue
            e = {'key': row[0], 'text': row[0], 'read': row[0]}
        if e['key'] in seen:
            continue
        seen.add(e['key'])
        out.append(e)
    return out


def shard_of(key, shards):
    """和 gen_audio.py 的 JA_SHARD 一样：按 key 的 sha1 分片"""
    return int(hashlib.sha1(str(key).encode()).hexdigest(), 16) % max(1, shards)


def file_for(lang, key):
    return f'{FOLDER[lang]}/{hashlib.sha1(key.encode()).hexdigest()[:16]}.mp3'


def load_index(path):
    p = Path(path)
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}


def save_index(path, idx):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(idx, ensure_ascii=False, separators=(',', ':'), sort_keys=True), encoding='utf-8')


def repo_files(repo_dir, lang):
    """仓库里已经有的录音文件（相对路径）。工作流里只检出索引（blob:none + sparse），文件列表用 git ls-tree 取，不下载录音本身"""
    folder = FOLDER[lang]
    lst = os.environ.get('HAVE_FILES')
    if lst and Path(lst).exists():
        return {l.strip() for l in Path(lst).read_text(encoding='utf-8').splitlines() if l.strip().startswith(folder + '/')}
    d = Path(repo_dir) / folder
    return {f'{folder}/{p.name}' for p in d.glob('*.mp3')} if d.exists() else set()


def plan(lang, shard=0, shards=1, ref='main', site_dir=None, audio_dir=None, repo_dir=REPO):
    """算出这一片要生成的词条。返回 (todo, stats, keep, need)：
    keep = 这一片里已经生成过的（文件名由 key 决定，仓库里有这个文件就算生成过）→ 直接写进这一片的索引
    need = 全部分片里「没有现有录音」的词条（日语算只用读音的 key 时要用）"""
    kw = dict(ref=ref, site_dir=site_dir, audio_dir=audio_dir)
    ents = entries(lang, **kw)
    have = existing_keys(lang, **kw)
    files = repo_files(repo_dir, lang)
    # 日语按读音排除（网页传的是读音，HUMAN_JA[读音] || KANA_IDX[读音] || AUDIO_IDX[读音] 有的就会先播那个）
    need = [e for e in ents if e['read'] not in have]
    todo, keep = [], {}
    for e in need:
        if shard_of(e['key'], shards) != shard:
            continue
        f = file_for(lang, e['key'])
        if f in files:                       # 生成过了（断点续跑）
            keep[e['key']] = f
            continue
        todo.append(e)
    stats = {'entries': len(ents), 'covered_by_existing': len(ents) - len(need), 'need': len(need),
             'shard_need': sum(1 for e in need if shard_of(e['key'], shards) == shard),
             'already_done': len(keep), 'todo': len(todo)}
    return todo, stats, keep, need


def ja_aliases(need):
    """日语：网页有的地方只传读音（dict.js speakBtn(e.r[0])、read.js 的生词卡、查词下拉）。
    每个读音选一个词条，把「读音」也作为 key 指到它的录音。同一个读音有几个词条（箸 / 橋）时：
    优先有中文释义的（本站编写过释义的多半是更常用的词），再按词典顺序。返回 {读音: 「写法|读音」}"""
    pick = {}
    for e in need:
        cur = pick.get(e['read'])
        if cur is None or (e['zh'] and not cur['zh']):
            pick[e['read']] = e
    return {r: e['key'] for r, e in pick.items()}


class Budget:
    """时间预算：到点就停下、写好索引，让后面的上传和提交来得及（GitHub 托管的 runner 一个任务最多 6 小时）"""

    def __init__(self, minutes):
        self.end = time.time() + minutes * 60 if minutes else None

    def over(self):
        return self.end is not None and time.time() > self.end


def common_args(ap):
    ap.add_argument('--shard', type=int, default=int(os.environ.get('SHARD', 0)))
    ap.add_argument('--shards', type=int, default=int(os.environ.get('SHARDS', 1)))
    ap.add_argument('--ref', default=os.environ.get('SRC_REF', 'main'), help='nihonggo-dojo 的分支')
    ap.add_argument('--out', default=os.environ.get('OUT_DIR', str(REPO / 'out')), help='这一片新生成的录音和索引放这里')
    ap.add_argument('--repo', default=str(REPO), help='本仓库（读已有的 index-dict-*.json 和录音文件列表）')
    ap.add_argument('--site-dir', default=None, help='离线：nihonggo-dojo 的本地副本')
    ap.add_argument('--audio-dir', default=None, help='离线：nihonggo-dojo-audio 的本地副本')
    ap.add_argument('--limit', type=int, default=0, help='最多生成多少个（0 = 全部，试跑用）')
    ap.add_argument('--max-minutes', type=float, default=float(os.environ.get('MAX_MINUTES', 0) or 0),
                    help='到时间就停下（0 = 不限）')
    ap.add_argument('--dry-run', action='store_true', help='只算要生成多少个，不需要合成引擎')
    return ap


def start(lang, a):
    """解析好参数以后统一调用：算出待办，dry-run 就打印数量退出。返回 (todo, keep, need, out_dir)"""
    todo, stats, keep, need = plan(lang, a.shard, a.shards, a.ref, a.site_dir, a.audio_dir, a.repo)
    print(lang, f'shard {a.shard}/{a.shards}', json.dumps(stats), flush=True)
    if lang == 'ja':
        al = ja_aliases(need)
        n = collections.Counter(e['read'] for e in need)
        print('ja reading-only keys:', len(al), 'readings shared by several entries:', sum(1 for r in al if n[r] > 1), flush=True)
    if a.dry_run:
        if a.shards > 1:
            return None
        per = [0] * 8
        for e in need:
            per[shard_of(e['key'], 8)] += 1
        print('per shard (of 8):', per)
        return None
    if a.limit:
        todo = todo[:a.limit]
    return todo, keep, need, Path(a.out)


def finish(lang, out, idx, need=None, shard=0, shards=1):
    """写这一片的索引（只含这一片的 key；日语再加上选中的词条在这一片里的「读音」key）"""
    part = dict(idx)
    if lang == 'ja' and need is not None:
        for r, key in ja_aliases(need).items():
            if key in idx and shard_of(key, shards) == shard:
                part[r] = idx[key]
    save_index(Path(out) / PART.format(lang=lang), part)
    return part


class Progress:
    def __init__(self, total, every=200):
        self.total, self.every, self.n, self.fail, self.t0 = total, every, 0, 0, time.time()

    def ok(self):
        self.n += 1
        if self.n % self.every == 0:
            print(f'  {self.n}/{self.total}  {self.n / (time.time() - self.t0):.2f}/s  failed {self.fail}', flush=True)

    def bad(self):
        self.fail += 1

    def done(self, lang):
        print(f'{lang}: {self.n} new files, {self.fail} failed, {self.total - self.n - self.fail} left', flush=True)
