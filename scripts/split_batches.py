#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 2 的辅助脚本：把 tasks.json 切成 N 批，供并行派子代理出题。

为什么需要它：出题是**模型环节**，也是全流程最耗时的一步。脚本层面刻意不做并行（标签归一化
与全局去重会变复杂），但**模型层面可以并行**——把切片分成若干批、每批交给一个子代理，各自写
`.qbgen/pieces/<id>.json`，最后统一校验 / 合并。实测 53 片分 6 批，出题从串行几十分钟压到几分钟。

用法：
  python split_batches.py --tasks .qbgen/tasks.json --out .qbgen/batches --batches 6

  # 增量场景：只把「本次新增的切片」切出来（配合 plan.py 产出的 incremental.json）
  python split_batches.py --tasks .qbgen/tasks.json --out .qbgen/batches \\
         --only-new .qbgen/incremental.json

产物：
  <out>/b1.json … <out>/bN.json  每批 {"batch": i, "total": N, "tasks": [...]}
  <out>/index.md                 每批的任务清单（用来核对有没有漏做）

分配方式：按 tasks.json 原有顺序累加，超过「总字数 ÷ 批数」就换下一批——**保序且按字数均衡**，
比按个数均分更公平（长切片不会全挤在同一批里）。

⚠️ 派子代理时要交代的纪律见 SKILL.md 的「大知识库：分批派子代理出题」一节，
其中最容易漏的是「统一标签词表」与「source_quote 必须逐字照抄」。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qbcommon import dump_json, init_console, load_json  # noqa: E402

init_console()


def main() -> None:
    ap = argparse.ArgumentParser(description="把出题任务切成 N 批，供并行派子代理")
    ap.add_argument("--tasks", default=".qbgen/tasks.json")
    ap.add_argument("--out", default=".qbgen/batches")
    ap.add_argument("--batches", type=int, default=6, help="切几批（建议 4–8）")
    ap.add_argument("--only-new", default=None,
                    help="只切 incremental.json 里 added 的切片（增量场景）")
    args = ap.parse_args()

    data = load_json(args.tasks)
    tasks = list(data.get("tasks") or [])
    total_all = len(tasks)

    if args.only_new:
        inc = load_json(args.only_new)
        keep = set(inc.get("added") or [])
        tasks = [t for t in tasks if t.get("id") in keep]
        if not tasks:
            print("incremental.json 里没有新增切片（added 为空）——本次没有要出的题")
            return

    if not tasks:
        print("tasks.json 里没有切片")
        sys.exit(1)

    n = max(1, min(args.batches, len(tasks)))
    total_chars = sum(len(t.get("text") or "") for t in tasks)
    target = total_chars / n

    groups: list[list[dict]] = [[]]
    acc = 0
    for t in tasks:
        ln = len(t.get("text") or "")
        if groups[-1] and acc + ln > target and len(groups) < n:
            groups.append([])
            acc = 0
        groups[-1].append(t)
        acc += ln

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("b*.json"):  # 清掉上一轮的批次文件，避免子代理拿到过期批次
        old.unlink()

    index = ["# 出题批次", "",
             f"- 源：`{args.tasks}`"
             + (f"（只取新增切片，来自 `{args.only_new}`）" if args.only_new else ""),
             f"- 切片 **{len(tasks)}** / 总数 {total_all}，切成 **{len(groups)}** 批", ""]
    for i, g in enumerate(groups, 1):
        chars = sum(len(t.get("text") or "") for t in g)
        dump_json(out / f"b{i}.json", {"batch": i, "total": len(groups), "tasks": g})
        index.append(f"## b{i}.json — {len(g)} 片 / {chars} 字")
        index += [f"- `{t['id']}`（{t.get('char_count', '?')} 字，{t.get('mode')}）" for t in g]
        index.append("")
    (out / "index.md").write_text("\n".join(index), encoding="utf-8")

    print(f"切成 {len(groups)} 批 → {out}/b1.json … b{len(groups)}.json（共 {len(tasks)} 片）")
    for i, g in enumerate(groups, 1):
        print(f"  b{i}: {len(g)} 片 / {sum(len(t.get('text') or '') for t in g)} 字")
    print(f"  每批清单见 {out / 'index.md'}")


if __name__ == "__main__":
    main()
