#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 3b：消除正确答案的位置偏置（幂等、确定性）。

为什么需要这一步：模型出题时会不自觉地把正确答案放在 A。实测一份 189 题
的 MySQL 题库，单选 125 题里 **122 题答案是 A**（97.6%），多选 30 题里 21 题
答案是 A,B,C —— 入库后等于「闭眼选 A 就能过」，题库的区分度直接归零。
validate.py 会对这种偏斜给出全局告警，本脚本负责修。

用法：
  python balance_options.py --pieces .qbgen/pieces [--seed 20261006]

做法（**只改选项顺序和 answer 的 label，不改任何文字**，所以 source_quote 不受影响）：
  - SINGLE：把正确答案轮转分配到"当前使用次数最少"的 label 位置
  - MULTI：在若干随机排列里挑「答案 label 组合已被用过最少」的那个
  - JUDGE：不动（正确/错误是固定语义，没有位置可言）
  - 选项带序号前缀（0/1/2、一/二/三）的题：跳过，重排会破坏可读性

跑完必须重新执行 validate.py（会重建 clean.json），再 merge.py。
注意 merge.py 的指纹只看「题干 + 题型」，选项重排不换指纹，所以对**已存在的题库**
要先把 `--bank` 下的 `.full.json` / `.meta.json` 删掉再合并，否则会被判为"已存在"跳过。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qbcommon import LABELS, init_console  # noqa: E402

init_console()

_ORDINAL = re.compile(r"^\s*(?:[0-9０-９]|[（(]?[一二三四五六七八九][)）])")


def has_order_semantics(opts) -> bool:
    """选项自带序号（0/1/2、一/二/三）时重排会让人读不懂，跳过。"""
    return any(_ORDINAL.match(str(o.get("content") or "")) for o in opts)


def relabel(contents) -> list[dict]:
    return [{"label": L, "content": c} for L, c in zip(LABELS, contents)]


def main() -> None:
    ap = argparse.ArgumentParser(description="重排选项，消除正确答案的位置偏置")
    ap.add_argument("--pieces", default=".qbgen/pieces", help="题目分片目录")
    ap.add_argument("--seed", type=int, default=20261006, help="随机种子（同种子结果一致）")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    pieces = Path(args.pieces)
    files = sorted(pieces.glob("*.json"))
    if not files:
        print(f"未在 {pieces} 找到题目分片")
        sys.exit(1)

    data: dict[Path, dict] = {}
    singles: list[tuple[Path, int]] = []
    multis: list[tuple[Path, int]] = []
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        data[f] = d
        for i, q in enumerate(d.get("questions", [])):
            if q.get("type") not in ("SINGLE", "MULTI") or has_order_semantics(q.get("options") or []):
                continue
            (singles if q["type"] == "SINGLE" else multis).append((f, i))

    # ---- SINGLE：把正确项挪到最少使用的位置
    use: Counter[str] = Counter()
    for f, i in singles:
        q = data[f]["questions"][i]
        labels = list(LABELS[: len(q["options"])])
        target = min(labels, key=lambda L: (use[L], labels.index(L)))
        use[target] += 1
        corr = [o["content"] for o in q["options"] if o["label"] in q["answer"]]
        rest = [o["content"] for o in q["options"] if o["label"] not in q["answer"]]
        seq, ri = [], 0
        for L in labels:
            if L == target and corr:
                seq.append(corr.pop(0))
            else:
                seq.append(rest[ri])
                ri += 1
        q["options"] = relabel(seq)
        q["answer"] = [target]

    # ---- MULTI：挑答案组合最分散的排列
    seen: Counter[str] = Counter()
    for f, i in multis:
        q = data[f]["questions"][i]
        orig = q["options"]
        correct = {o["content"] for o in orig if o["label"] in q["answer"]}
        best = None
        for _ in range(16):
            perm = orig[:]
            rng.shuffle(perm)
            key = "".join(L for L, o in zip(LABELS, perm) if o["content"] in correct)
            if best is None or seen[key] < best[0]:
                best = (seen[key], key, perm)
        _, key, perm = best
        seen[key] += 1
        q["options"] = relabel(o["content"] for o in perm)
        q["answer"] = [L for L, o in zip(LABELS, perm) if o["content"] in correct]

    for f, d in data.items():
        f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    allq = [q for d in data.values() for q in d["questions"]]
    skipped = len(allq) - len(singles) - len(multis)
    print(f"位置均衡完成：重排单选 {len(singles)} 题、多选 {len(multis)} 题，"
          f"跳过 {skipped} 题（判断题 / 选项带序号）")
    for t in ("SINGLE", "MULTI"):
        qs = [q for q in allq if q["type"] == t]
        if qs:
            print(f"  {t}（{len(qs)}）答案分布：{Counter(''.join(q['answer']) for q in qs).most_common(8)}")
    print("  下一步：重跑 validate.py（重建 clean.json），再 merge.py")


if __name__ == "__main__":
    main()
