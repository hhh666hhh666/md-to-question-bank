#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 4：增量合并到题库目录（幂等，重复运行只追加变化部分）。

用法：
  python merge.py --clean .qbgen/clean.json --bank question-bank [--prune]

产物（--bank 目录下，建议指向 question-bank/<学科>/json）：
  all.json                    全量合并版，裸数组，可直接喂 POST /api/questions/import/json
  by-category/<分类>.json      按分类拆分的裸数组，便于分批导入与定位
  .full.json                  带溯源信息（_source_file/_source_section/source_quote/_freq）的完整库，供复核与预览
  .meta.json                  指纹索引，供增量比对

合并策略：
  - 指纹 = sha1(题干归一化 + 题型)。命中指纹**且内容指纹也相同** → 视为已存在，跳过
  - 命中指纹、但**内容指纹不同**（选项 / 答案 / 解析被改过）→ 视为同一题的修订，替换旧题（保留首次加入时间）
  - 指纹未命中、但相似度 ≥0.75（题干 50% + 选项内容重合 50%）的同题型旧题 → 同样是修订，替换
  - 旧题在 .full.json 中被标记 "_locked": true → 视为人工修订过，永不覆盖
  - --prune：源文件已删除、或**该章节已不再产出**的旧题一并移除
    （后者需要 `--tasks .qbgen/tasks.json`；只给 `--docs-root` 时仍只看源文件在不在——那样
    把某一节删掉重出，文件还在、旧题就清不掉，只会不停新增）
  - 注意：导入接口**没有幂等**（逐条新建、无查重），所以**不要重复导入 all.json**，否则会建出重复题
"""
from __future__ import annotations

import argparse
import difflib
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qbcommon import (  # noqa: E402
    content_fingerprint, dump_json, init_console, load_json, norm, slugify, to_contract,
)

init_console()

SIMILARITY = 0.75


def similarity(a: dict, b: dict) -> float:
    """判断两道题是否为“同一题的不同版本”。

    光比题干不可靠（人工改写后字面相似度会掉到 0.8 以下），
    所以按 题干相似度 50% + 选项内容重合度 50% 综合判断：
    同题改写时选项通常不动，不同题即使题干像，选项也几乎不重叠。
    """
    stem_ratio = difflib.SequenceMatcher(None, norm(a.get("stem", "")),
                                         norm(b.get("stem", ""))).ratio()
    oa = {norm(o.get("content", "")) for o in a.get("options", [])}
    ob = {norm(o.get("content", "")) for o in b.get("options", [])}
    opt_jaccard = len(oa & ob) / len(oa | ob) if (oa | ob) else 0.0
    return 0.5 * stem_ratio + 0.5 * opt_jaccard


def load_prev(bank: Path):
    p = bank / ".full.json"
    if p.exists():
        data = load_json(p)
        return data.get("questions", [])
    return []


def main():
    ap = argparse.ArgumentParser(description="增量合并题目到题库目录")
    ap.add_argument("--clean", required=True, help="validate.py 产出的 clean.json")
    ap.add_argument("--bank", default="question-bank",
                    help="题库输出目录，建议 question-bank/<学科>/json")
    ap.add_argument("--docs-root", default=None, help="知识库根目录，用于 --prune 判断源文件是否还在")
    ap.add_argument("--tasks", default=None,
                    help="plan.py 的 tasks.json；给了就能按「这一节还在不在」清理（配合 --prune）")
    ap.add_argument("--prune", action="store_true", help="移除源文件已不存在、或该章节已不再产出的旧题")
    args = ap.parse_args()

    bank = Path(args.bank).resolve()
    bank.mkdir(parents=True, exist_ok=True)
    docs_root = Path(args.docs_root).resolve() if args.docs_root else bank.parent
    task_ids = None
    if args.tasks:
        tp = Path(args.tasks)
        if tp.exists():
            try:
                task_ids = {t["id"] for t in (load_json(tp).get("tasks") or [])}
            except (OSError, ValueError, TypeError, KeyError):
                task_ids = None

    incoming = load_json(args.clean)["questions"]
    prev = load_prev(bank)

    by_fp = {q.get("_fingerprint"): q for q in prev if q.get("_fingerprint")}
    stats = {"new": 0, "updated": 0, "existing": 0, "locked": 0, "pruned": 0}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    merged: list[dict] = []
    handled_prev = set()  # 已在 fuzzy 分支中被处理的旧题对象 id

    for q in incoming:
        fp = q.get("_fingerprint")
        old = by_fp.get(fp)
        if old is not None:
            # 指纹只认「题干 + 题型」。题干没变、但选项 / 答案 / 解析被改过时，
            # 光看指纹会把这笔修订**静默丢掉**（2026-10-06 实测踩到）。
            # 所以再比一次内容指纹：两样都一样，才算「已存在」。
            if content_fingerprint(q) == content_fingerprint(old):
                stats["existing"] += 1
                continue
            handled_prev.add(id(old))  # 旧题被本次修订取代，别再被后面的循环追加一遍
            if old.get("_locked"):
                stats["locked"] += 1
                merged.append(old)
                continue
            q["_added_at"] = old.get("_added_at", now)
            q["_updated_at"] = now
            stats["updated"] += 1
            merged.append(q)
            continue
        # 模糊匹配：同一题被轻微改写 / 人工修订过
        best, best_score = None, 0.0
        for cand in prev:
            if id(cand) in handled_prev or cand.get("type") != q.get("type"):
                continue
            s = similarity(q, cand)
            if s > best_score:
                best, best_score = cand, s
        if best is not None and best_score >= SIMILARITY:
            handled_prev.add(id(best))
            by_fp.pop(best.get("_fingerprint"), None)
            if best.get("_locked"):
                stats["locked"] += 1
                merged.append(best)
                continue
            q["_added_at"] = best.get("_added_at", now)
            q["_updated_at"] = now
            stats["updated"] += 1
            merged.append(q)
            continue
        q.setdefault("_added_at", now)
        stats["new"] += 1
        merged.append(q)

    # ---- 上一轮遗留、且本轮未触及的旧题
    for old in prev:
        if id(old) in handled_prev:
            continue
        if args.prune:
            src = old.get("_source_file")
            gone_file = bool(src) and not (docs_root / src).exists()
            # 原判据只看「源文件还在不在」——把某一节删掉重出时，文件还在、旧题就永远清不掉，
            # 只会不停地新增。给了 --tasks 就再按 _task_id 判一次「这一节现在还产不产出」。
            gone_section = (task_ids is not None and old.get("_task_id")
                            and old["_task_id"] not in task_ids)
            if gone_file or gone_section:
                stats["pruned"] += 1
                continue
        merged.append(old)

    merged.sort(key=lambda x: (x.get("category", ""), x.get("type", ""), x.get("_fingerprint", "")))

    # ---- 落盘
    dump_json(bank / ".full.json", {"updated_at": now, "count": len(merged), "questions": merged})

    by_cat: dict[str, list] = {}
    for q in merged:
        by_cat.setdefault(q.get("category", "未分类"), []).append(to_contract(q))
    for cat, items in by_cat.items():
        dump_json(bank / "by-category" / f"{slugify(cat)}.json", items)
    dump_json(bank / "all.json", [to_contract(q) for q in merged])

    dump_json(bank / ".meta.json", {
        "updated_at": now,
        "similarity_threshold": SIMILARITY,
        "questions": {
            q.get("_fingerprint"): {
                "stem": q.get("stem", "")[:60],
                "category": q.get("category"),
                "source_file": q.get("_source_file"),
                "source_section": q.get("_source_section"),
                "added_at": q.get("_added_at"),
                "locked": bool(q.get("_locked")),
            } for q in merged
        },
    })

    print(f"合并完成 → {bank}")
    print(f"  新增 {stats['new']} / 更新 {stats['updated']} / 已存在跳过 {stats['existing']} "
          f"/ 锁定保留 {stats['locked']} / 清理 {stats['pruned']}")
    print(f"  题库总数 {len(merged)}，by-category/ 下 {len(by_cat)} 个分类文件 + all.json")
    print("  提醒：导入接口不幂等，all.json 只需导一次")


if __name__ == "__main__":
    main()
