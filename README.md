# nihonggo-dojo-audio2

猫先生（https://koshokai.github.io/nihonggo-dojo/ ）**词典词条**的录音：日语、中文、英语词典里每一个还没有录音的词都在这里生成一条，
网页上不再需要用手机自带的朗读（TTS）。用 GitHub Pages 提供：`https://koshokai.github.io/nihonggo-dojo-audio2/<路径>`。

| 文件 | 内容 |
| --- | --- |
| `ja-d/*.mp3`、`index-dict-ja.json` | 日语词典（`dict.json`，JMdict 常用词）里没有录音的词，VOICEVOX:春日部つむぎ |
| `zh-d/*.mp3`、`index-dict-zh.json` | 中文词典（`lang/zh-dict.json`）里没有录音的词，MeloTTS 普通话 |
| `en-d/*.mp3`、`index-dict-en.json` | 英语词典（`lang/en-dict.json`）里没有录音的词，Kokoro af_heart |

索引是 `key → 相对路径`，例如 `"とくべつかかく": "ja-d/0a1b2c3d4e5f6789.mp3"`。文件名是 key 的 sha1 前 16 位。

## 索引的 key（和网页朗读时传的文本完全一样）

**日语 `index-dict-ja.json`** 有两种 key：

- `写法|读音`：`dict.js` 的 `jmCard(e)` 里 `w + '|' + e.r[0]`（`w = e.k[0] || e.r[0]`，`e.r[0]` 是词典里第一个读音，原样，片假名不转平假名）。
  例：`将来的|しょうらいてき`、`ＣＤプレーヤー|シーディープレーヤー`、只有假名的词写法和读音一样，例如 `うとうと|うとうと`。
  同音词（箸 / 橋、端）、同形词（一つの写法有几个读音）各自一条录音，用自己的写法合成，重音是对的。
- `读音`：网页很多地方只传读音（`speakBtn(e.r[0])`、`read.js` 生词卡的 `hit.r`、查词下拉），所以每个读音也有一个 key，
  指到其中一个词条的录音。同一个读音有几个词条时，优先有中文释义的词条（`dict.json` 第 5 列），再按词典顺序。

排除规则：读音已经在现有日语录音目录里的词条不生成（`speak()` 会先找到那些：
`HUMAN_JA`=`ja/index-ja-human.json`、`KANA_IDX`=`ja/index-kana.json`、`AUDIO_IDX`=`ja/index-words.json`+`index-daily.json`+`index-examples.json`）。

网页上要加的查找（`app.js` 的 `speak()`）：

```js
let DICT_JA = {};
fetch(`${AUD2}index-dict-ja.json`).then(r => r.ok ? r.json() : {}).then(d => { DICT_JA = d; }).catch(() => {});
// speak(text)：现有目录之后再查词典录音
const file = S.voiceSrc !== 'tts' && (HUMAN_JA[text] || KANA_IDX[text] || AUDIO_IDX[text] || DICT_JA[text]);
// 词典卡片想要同音词各自的录音：jmCard 里用 DICT_JA[w + '|' + e.r[0]]（比如 speakBtn 多带一个 data 属性，speak 先查它）
```

**中文 `index-dict-zh.json`**：key 是词本身（`zh.js` 的词典 `sayBtn(x.w, 'zh-CN')` → `speakLang(text)` → `LANG_IDX_READY.zh[text]`）。
排除主站 `audio/index-zh.json`、`audio/index-zh-human.json` 里已有的词。

**英语 `index-dict-en.json`**：key 是词本身（`en.js` 的词典 `sayBtn(x.w, 'en-US')` → `LANG_IDX_READY.en[text]`）。
排除 nihonggo-dojo-audio 的 `index-en.json`、`index-en-human.json` 里已有的词。

中文、英语只要在 `lang.js` 的 `langAudioIdx(code)` 里把这个目录也并进去（放在最前面，现有目录优先）：

```js
Promise.all([jsonOr(`${audIdxBase(code)}index-${code}.json`), jsonOr(`${audIdxBase(code)}index-${code}-human.json`),
             code === 'zh' || code === 'en' ? jsonOr(`${AUD2}index-dict-${code}.json`) : {}])
  .then(([tts, human, dict]) => (LANG_IDX_READY[code] = Object.assign({}, dict, tts, human)))
```

`audUrl()` 要把这三个文件夹指到这个仓库（放在其他判断前面，否则 `ja-d/` 会被当成 `AUD` 下的路径）：

```js
window.AUD2 = 'https://koshokai.github.io/nihonggo-dojo-audio2/';
window.audUrl = f => /^https?:/.test(f) ? f : /^(ja|zh|en)-d\//.test(f) ? AUD2 + f : /* 原来的判断 */ ...;
```

## 生成

在 Actions 页面手动运行（`workflow_dispatch`），每种语言一个工作流：

- `Dictionary audio - Japanese (VOICEVOX)`（`.github/workflows/dict-ja.yml`）
- `Dictionary audio - Chinese (MeloTTS)`（`dict-zh.yml`）
- `Dictionary audio - English (Kokoro)`（`dict-en.yml`）

每个工作流分 8 片并行（按 key 的 sha1 分片），每片在 `max_minutes`（默认 300 分钟）到了以后自己停下、上传已经生成的部分；
最后的 `commit` 任务把所有分片的结果合并进 `<语言>-d/` 和 `index-dict-<语言>.json` 并提交。只增加、不删除：
有分片失败或超时，其他部分照样保存，再运行一次就接着做剩下的（已经有文件的词跳过）。`limit` 填一个小数字可以先试跑。

词表和现有录音目录都在运行时从公开地址下载（GitHub Pages，连不上时用 raw.githubusercontent.com），不需要任何 Secret：

- `https://github.com/koshokai/nihonggo-dojo`：`dict.json`、`lang/zh-dict.json`、`lang/en-dict.json`、`audio/index-zh*.json`
- `https://koshokai.github.io/nihonggo-dojo-audio/`：`ja/index-*.json`、`index-en*.json`

只算数量（不需要合成引擎）：

```sh
python3 scripts/gen_ja.py --dry-run        # zh、en 同样；离线时加 --site-dir <nihonggo-dojo> --audio-dir <nihonggo-dojo-audio>
```

日志只打印数量，不打印词。VOICEVOX 引擎用 `docker run --log-driver none` 启动（作为 service 容器时，任务结束会把访问日志打印出来）。

| 语言 | 引擎和设置（和网站现有录音一样） |
| --- | --- |
| 日语 | VOICEVOX engine（`voicevox/voicevox_engine:cpu-latest`），style 8 春日部つむぎ；先用写法合成，读出的假名和词典读音不一样时改用读音；两拍以内的词元音不清音化；统一响度（有声部分 −21.5 dB）；40 kbps |
| 中文 | MeloTTS ZH；按词典里的拼音读（替换 MeloTTS 的 `lazy_pinyin`，儿化保留）；读不出声音时换几种读法重试；统一响度；48 kbps |
| 英语 | Kokoro-82M `af_heart`，单词语速 0.9；`loudnorm` 统一响度；44.1 kHz、48 kbps |

## 大小

GitHub Pages 一个网站最多 1 GB。按现有录音的平均大小（日语单词约 4–5 KB、中文 2–4 字约 7–8 KB、英语单词约 10 KB）估算：
日语约 1.4 万条 ≈ 62 MB，中文约 0.98 万条 ≈ 71 MB，英语约 1.34 万条 ≈ 151 MB，合计约 **285 MB**（加上索引约 2 MB）。

## 致谢和授权

- 日语录音：**VOICEVOX:春日部つむぎ**（https://voicevox.hiroshiba.jp/ 、https://tsukushinyoki10.wixsite.com/ktsumugiofficial ）。使用时请注明「VOICEVOX:春日部つむぎ」，并遵守角色的使用规定。
- 中文录音：**MeloTTS**（MyShell.ai，MIT License，https://github.com/myshell-ai/MeloTTS ）。
- 英语录音：**Kokoro-82M**（hexgrad，Apache License 2.0，https://huggingface.co/hexgrad/Kokoro-82M ），声音 af_heart。
- 日语词表来自 JMdict（Electronic Dictionary Research and Development Group，CC BY-SA 4.0），经猫先生整理；中文、英语词表由猫先生编写。
