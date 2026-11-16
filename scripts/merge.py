"""把各分片的结果合并进仓库：拷贝新录音到 <语言>-d/，把分片索引并进根目录的 index-dict-<语言>.json。

    python3 scripts/merge.py ja /tmp/art      /tmp/art/<任意名>/ja-d/*.mp3 + /tmp/art/<任意名>/index-dict-ja.part.json

只增加、不删除：有分片失败或超时，其他分片（和失败分片已经完成的部分）照样保存；再运行一次工作流会接着做剩下的。
索引里文件不存在的条目会去掉（不会把网页指到 404）。
"""
import argparse
import json
import shutil
from pathlib import Path

import common


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('lang', choices=common.LANGS)
    ap.add_argument('art', help='下载下来的 artifact 目录')
    ap.add_argument('--repo', default=str(common.REPO))
    a = ap.parse_args()
    repo, folder = Path(a.repo), common.FOLDER[a.lang]
    idx_path = repo / common.INDEX[a.lang]
    idx = common.load_index(idx_path)
    before, copied, parts = len(idx), 0, 0
    for d in sorted(p for p in Path(a.art).iterdir() if p.is_dir()):
        src = d / folder
        if src.is_dir():
            (repo / folder).mkdir(parents=True, exist_ok=True)
            for f in src.glob('*.mp3'):
                shutil.copy2(f, repo / folder / f.name)
                copied += 1
        part = d / common.PART.format(lang=a.lang)
        if part.exists():
            idx.update(json.loads(part.read_text(encoding='utf-8')))
            parts += 1
    missing = [k for k, f in idx.items() if not (repo / f).exists()]
    for k in missing:
        del idx[k]
    common.save_index(idx_path, idx)
    files = len(set(idx.values()))
    print(f'{a.lang}: shards merged {parts}, files copied {copied}, index {before} -> {len(idx)} keys ({files} files), dropped {len(missing)} without file')


if __name__ == '__main__':
    main()
