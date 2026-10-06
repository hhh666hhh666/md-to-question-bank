#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 3：校验模型产出的题目，隔离不合格项。

用法：
  python validate.py --pieces .qbgen/pieces --tasks .qbgen/tasks.json [--out .qbgen]

产物：
  <out>/clean.json      通过校验（含 warn）的题目，带私有字段 _task_id / _source_*
  <out>/disputed.json   判定失败或被标记为存疑的题目及原因
  <out>/validation.json 逐条校验明细

硬失败（进 disputed）：
  - 字段缺失/类型非法、题型无法识别、选项数不在 2-6、label 重复或越界
  - 单选/判断答案不唯一、多选答案不足 2 个、答案 label 未在选项中声明
  - 选项内容重复/过短/与题干相同、多选答案等于全部选项
  - source_quote 缺失，或**剥掉 HTML 后**仍无法在原文（章节 → 整文件）中找到 ← 防幻觉主防线
  - 考频标签缺失或不在 高频/中频/低频 之内（必标三选一）
  - 标签总数（内容 + 考频）超过 quizzy 的 10 个上限

警告（仍进 clean.json）：
  - 缺解析、内容标签超过 5 个、题干过长、单选只有 2 个选项
  - 选项只有 1 个字符（`0` / `页` 这类是合法内容，只提示确认清晰度）
  - 高频题占比超过 30%（防止"一片高频"）
  - 单节题数超过阈值（阈值取自 tasks.json 的 max_per_section，默认 5；不是配额，只是告警）
  - 单选答案位置偏斜：某一 label 占比超过 50%（题库会被"闭眼选 A"刷穿 → 跑 balance_options.py）
  - 跨节近重复：题干相似度 ≥ 0.85 的题对列出（疑似同一知识点出了两遍，人工定夺）
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qbcommon import (  # noqa: E402
    CONTENT_TAG_LIMIT, FREQ_HIGH, FREQ_HIGH_MAX_RATIO, FREQ_HIGH_MIN_SAMPLE, FREQ_TAGS,
    JUDGE_FALSE_WORDS, JUDGE_TRUE_WORDS, LABELS, SCORE_BY_DIFF, TAG_TOTAL_LIMIT,
    VALID_DIFFS, VALID_TYPES, dump_json, fingerprint, init_console, load_json, norm,
    normalize_answer, normalize_diff, normalize_type, split_tags,
)

init_console()

SINGLE_POS_MAX_RATIO = 0.50  # 单选答案位置最大占比，超过即判定为"闭眼选 A"型偏斜
NEAR_DUP_RATIO = 0.85        # 题干相似度高于此值的题对，列为「疑似同一知识点被出了两遍」


def judge_side(content: str):
    c = norm(content)
    if c in JUDGE_TRUE_WORDS:
        return "T"
    if c in JUDGE_FALSE_WORDS:
        return "F"
    return None


def check_question(q: dict, src_text: str, file_text: str):
    """返回 (errors, warnings, fixed_question)。"""
    errors: list[str] = []
    warns: list[str] = []
    out = dict(q)

    # ---- 题型
    qtype = normalize_type(q.get("type"))
    if not qtype:
        errors.append(f"题型无法识别：{q.get('type')!r}（应为 SINGLE/MULTI/JUDGE）")
    out["type"] = qtype

    # ---- 题干
    stem = str(q.get("stem") or "").strip()
    if not stem:
        errors.append("题干为空")
    elif len(stem) > 500:
        warns.append(f"题干过长（{len(stem)} 字），建议精简到 200 字内")
    out["stem"] = stem

    # ---- 选项
    raw_opts = q.get("options")
    if not isinstance(raw_opts, list) or not raw_opts:
        errors.append("选项缺失或不是数组")
        raw_opts = []
    opts = []
    labels = []
    for o in raw_opts:
        if not isinstance(o, dict):
            errors.append(f"选项不是对象：{o!r}")
            continue
        label = str(o.get("label") or "").strip().upper()
        content = str(o.get("content") or "").strip()
        if label not in LABELS:
            errors.append(f"选项 label 非法：{o.get('label')!r}（应为 A-F）")
        if label in labels:
            errors.append(f"选项 label 重复：{label}")
        labels.append(label)
        if not content:
            errors.append(f"选项 {label} 内容为空")
        elif len(content) < 2:
            # 只有 1 个字符的选项是**合法**的：`0`/`1`/`2`（参数取值）、`页`/`行`/`表`
            # （缓冲单位）都真实存在。2026-10-06 实测有 2 道好题被这条挡下过，
            # 所以只降级为警告：请作者确认它够不够清晰。
            warns.append(f"选项 {label} 只有 1 个字符，确认是否够清晰")
        if content and norm(content) == norm(stem):
            errors.append(f"选项 {label} 与题干完全相同")
        if "以上都" in content or "以上全部" in content:
            warns.append(f"选项 {label} 使用“以上都对/都不对”，区分度弱，慎用")
        opts.append({"label": label, "content": content})
    if len(opts) < 2 or len(opts) > 6:
        errors.append(f"选项数量 {len(opts)} 非法（quizzy 要求 2-6 个）")
    contents = [norm(o["content"]) for o in opts if o["content"]]
    if len(set(contents)) != len(contents):
        errors.append("存在内容重复的选项")

    # ---- 判断题规范化（quizzy 约定固定“正确/错误”两选项）
    if qtype == "JUDGE" and len(opts) == 2:
        sides = [judge_side(o["content"]) for o in opts]
        if all(sides) and sides[0] != sides[1]:
            remap = {o["label"]: ("A" if s == "T" else "B") for o, s in zip(opts, sides)}
            opts = [{"label": "A", "content": "正确"}, {"label": "B", "content": "错误"}]
            out["_judge_remap"] = remap
        else:
            warns.append("判断题选项不是“正确/错误”表述，已按原文保留，请人工确认")

    # ---- 答案
    answer = normalize_answer(q.get("answer"))
    if out.get("_judge_remap"):
        answer = [out["_judge_remap"].get(a, a) for a in answer]
    if not answer:
        errors.append("答案缺失或格式非法")
    else:
        unknown = [a for a in answer if a not in [o["label"] for o in opts]]
        if unknown:
            errors.append(f"答案 label 未在选项中声明：{unknown}")
        if qtype in ("SINGLE", "JUDGE") and len(answer) != 1:
            errors.append(f"{qtype} 必须且只能有 1 个正确答案（当前 {len(answer)} 个）")
        if qtype == "MULTI":
            if len(answer) < 2:
                errors.append("多选题至少需要 2 个正确答案，否则应改为单选")
            if len(answer) == len(opts) and opts:
                errors.append("多选题答案等于全部选项，无区分度")
    out["answer"] = answer
    out["options"] = opts

    if qtype == "SINGLE" and 0 < len(opts) < 3:
        warns.append("单选题只有 2 个选项，区分度偏低")

    # ---- 难度 / 分值
    diff = normalize_diff(q.get("difficulty"))
    if q.get("difficulty") is None:
        warns.append("未标注难度，默认 MEDIUM")
    elif str(q.get("difficulty")).strip().upper() not in VALID_DIFFS:
        warns.append(f"难度 {q.get('difficulty')!r} 已归一化为 {diff}")
    out["difficulty"] = diff
    score = q.get("score")
    if score is None:
        score = SCORE_BY_DIFF[diff]
    try:
        score = int(score)
    except (TypeError, ValueError):
        errors.append(f"分值非法：{q.get('score')!r}")
        score = SCORE_BY_DIFF[diff]
    if not (1 <= score <= 100):
        errors.append(f"分值 {score} 超出 1-100")
    out["score"] = score

    # ---- 分类 / 标签
    category = str(q.get("category") or "").strip() or "未分类"
    if not q.get("category"):
        warns.append("未指定分类，归入“未分类”")
    out["category"] = category
    raw_tags = q.get("tags")
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in re.split(r"[,，、]", raw_tags) if t.strip()]
    content_tags, freq_tags = split_tags(raw_tags or [])

    # 考频档位必标三选一（Q13 定）
    if not freq_tags:
        errors.append(f"缺少考频标签（必须三选一：{'/'.join(FREQ_TAGS)}）")
    elif len(freq_tags) > 1:
        errors.append(f"考频标签只能有一个，当前给了 {len(freq_tags)} 个：{'、'.join(freq_tags)}")

    if len(content_tags) > CONTENT_TAG_LIMIT:
        warns.append(f"内容标签 {len(content_tags)} 个偏多，"
                     f"建议收敛到 {CONTENT_TAG_LIMIT} 个以内以免污染标签库")
    total_tags = content_tags + freq_tags
    if len(total_tags) > TAG_TOTAL_LIMIT:
        errors.append(f"标签总数 {len(total_tags)} 个超过 quizzy 上限 {TAG_TOTAL_LIMIT}")
    out["tags"] = total_tags[:TAG_TOTAL_LIMIT]
    out["_freq"] = freq_tags[0] if freq_tags else None

    # ---- 解析
    if not str(q.get("analysis") or "").strip():
        warns.append("缺少解析（analysis）")
    out["analysis"] = str(q.get("analysis") or "").strip()

    # ---- 原文溯源（防幻觉主防线）
    quote = str(q.get("source_quote") or "").strip()
    if not quote:
        errors.append("缺少 source_quote（无法验证答案在原文中有依据）")
    else:
        if len(quote) > 200:
            errors.append(f"source_quote 过长（{len(quote)} 字），应 ≤200 字且为原文原句")
        nq = norm(quote)
        hit = bool(nq) and (nq in norm(src_text) if src_text else False)
        if not hit:
            hit = bool(nq) and nq in norm(file_text)
            if hit:
                warns.append("source_quote 未命中所属章节，但在同文件其他位置找到")
        if not hit:
            errors.append("source_quote 在原文中找不到对应句子（疑似编造答案）")
    out["source_quote"] = quote

    return errors, warns, out


def main():
    ap = argparse.ArgumentParser(description="校验生成的题目 JSON")
    ap.add_argument("--pieces", required=True, help="题目分片目录（每片 {\"task_id\":..,\"questions\":[...]}）")
    ap.add_argument("--tasks", required=True, help="plan.py 产出的 tasks.json")
    ap.add_argument("--out", default=".qbgen", help="输出目录")
    args = ap.parse_args()

    tasks_data = load_json(args.tasks)
    task_map = {t["id"]: t for t in tasks_data["tasks"]}
    file_cache: dict[str, str] = {}

    def file_text_of(task):
        p = task.get("file")
        if p not in file_cache:
            try:
                file_cache[p] = Path(p).read_text(encoding="utf-8", errors="replace")
            except OSError:
                file_cache[p] = ""
        return file_cache[p]

    pieces_dir = Path(args.pieces)
    files = sorted(pieces_dir.glob("*.json"))
    if not files:
        print(f"未在 {pieces_dir} 找到题目分片")
        sys.exit(1)

    clean, disputed, report, seen_fp = [], [], [], set()
    for pf in files:
        data = load_json(pf)
        task_id = data.get("task_id") or pf.stem
        task = task_map.get(task_id)
        src_text = task["text"] if task else ""
        file_text = file_text_of(task) if task else ""
        for idx, q in enumerate(data.get("questions", [])):
            errors, warns, fixed = check_question(q, src_text, file_text)
            fp = fingerprint(fixed.get("stem", ""), fixed.get("type", ""))
            if fp in seen_fp and not errors:
                errors.append("与题干重复的题目（本次运行内重复）")
            seen_fp.add(fp)
            fixed["_task_id"] = task_id
            if task:
                fixed["_source_file"] = task.get("rel")
                fixed["_source_section"] = task.get("section")
            fixed["_fingerprint"] = fp
            item = {"piece": pf.name, "task_id": task_id, "index": idx,
                    "stem": fixed.get("stem", "")[:80], "errors": errors, "warnings": warns}
            if errors:
                item["status"] = "fail"
                item["question"] = {k: v for k, v in q.items()}
                disputed.append(item)
            else:
                item["status"] = "warn" if warns else "ok"
                clean.append(fixed)
            report.append(item)

    # ---- 单节题数告警（不是配额，超了只在报告里标出；Q5/Q14 定）
    # 只数最终能进库的题（失败题会被隔离，不该算进"这一节出了几题"）
    max_per = int(tasks_data.get("max_per_section", 5) or 5)
    per_task = Counter(r["task_id"] for r in report if r["status"] != "fail")
    over = {tid: n for tid, n in per_task.items() if n > max_per}
    for r in report:
        if r["status"] != "fail" and r["task_id"] in over:
            r["warnings"].append(f"本节共 {over[r['task_id']]} 题，超过告警阈值 {max_per}")
            if r["status"] == "ok":
                r["status"] = "warn"

    freq_dist = Counter((q.get("_freq") or "未标") for q in clean)
    high_ratio = (freq_dist.get(FREQ_HIGH, 0) / len(clean)) if clean else 0.0

    # ---- 答案位置分布（防止"正确答案总在 A"；模型出题的系统性偏差）
    single_pos = Counter(a for q in clean if q.get("type") == "SINGLE" for a in q.get("answer", []))
    single_n = sum(1 for q in clean if q.get("type") == "SINGLE")
    multi_combo = Counter("".join(q.get("answer", [])) for q in clean if q.get("type") == "MULTI")
    answer_pos: dict = {
        "single_total": single_n,
        "single_counts": dict(single_pos.most_common()),
        "multi_top": multi_combo.most_common(1)[0] if multi_combo else None,
    }
    skew = None
    if single_n >= 10 and single_pos:
        top_label, n_top = single_pos.most_common(1)[0]
        ratio = n_top / single_n
        answer_pos.update(top_label=top_label, top_ratio=round(ratio, 4))
        if ratio > SINGLE_POS_MAX_RATIO:
            skew = (top_label, n_top, single_n, ratio)

    # ---- 跨节近重复（只提示，不拦截）
    # 同一知识点在不同章节被各出一遍时题干不同 → 指纹去重拦不住。这里按题干相似度给提示，
    # 由人工决定是否合并（2026-10-06 实测：189 题里 8 对 >=0.72，其中 >=0.85 的值得逐对看）。
    near_dup: list[dict] = []
    by_type: dict = {}
    for q in clean:
        by_type.setdefault(q.get("type"), []).append(q)
    for qs in by_type.values():
        for i in range(len(qs)):
            for j in range(i + 1, len(qs)):
                a, b = qs[i], qs[j]
                sa, sb = norm(a.get("stem", "")), norm(b.get("stem", ""))
                if not sa or not sb:
                    continue
                if min(len(sa), len(sb)) / max(len(sa), len(sb)) < 0.5:
                    continue  # 长度差太大，不可能是同一道题
                ratio = difflib.SequenceMatcher(None, sa, sb).ratio()
                if ratio >= NEAR_DUP_RATIO:
                    near_dup.append({
                        "ratio": round(ratio, 3),
                        "a_stem": a.get("stem", "")[:60], "a_task": a.get("_task_id"),
                        "b_stem": b.get("stem", "")[:60], "b_task": b.get("_task_id"),
                    })
    near_dup.sort(key=lambda x: -x["ratio"])

    out = Path(args.out)
    dump_json(out / "clean.json", {"count": len(clean), "questions": clean})
    dump_json(out / "disputed.json", {"count": len(disputed), "items": disputed})
    dump_json(out / "validation.json", {
        "total": len(report),
        "ok": sum(1 for r in report if r["status"] == "ok"),
        "warn": sum(1 for r in report if r["status"] == "warn"),
        "fail": sum(1 for r in report if r["status"] == "fail"),
        "freq_dist": dict(freq_dist),
        "high_ratio": round(high_ratio, 4),
        "answer_position": answer_pos,
        "near_duplicates": near_dup[:50],
        "max_per_section": max_per,
        "sections_over_limit": over,
        "items": report,
    })

    print(f"校验完成：通过 {len(clean)}（其中带警告 "
          f"{sum(1 for r in report if r['status'] == 'warn')}），失败 {len(disputed)}")
    print(f"  考频分布：{dict(freq_dist)}；高频占比 {high_ratio:.0%}（上限 {FREQ_HIGH_MAX_RATIO:.0%}）")
    if clean and len(clean) < FREQ_HIGH_MIN_SAMPLE:
        print(f"  （样本 {len(clean)} < {FREQ_HIGH_MIN_SAMPLE}，占比告警不生效——小样本上这个比例没有统计意义）")
    elif clean and high_ratio > FREQ_HIGH_MAX_RATIO:
        print("  ⚠ 高频题占比超限，多半是判据没锚定原文，回 authoring.md 重标")
    if over:
        print(f"  单节超 {max_per} 题的切片：{over}")
    if skew:
        print(f"  ⚠ 单选答案位置偏斜：{skew[0]} 占 {skew[3]:.0%}（{skew[1]}/{skew[2]}）"
              f"→ 跑 scripts/balance_options.py 重排选项")
    if near_dup:
        print(f"  ⚠ 疑似同一知识点出了两遍：{len(near_dup)} 对"
              f"（题干相似度 ≥ {NEAR_DUP_RATIO}，人工确认是否该合并）")
        for d in near_dup[:5]:
            print(f"      {d['ratio']:.2f}  {d['a_stem'][:32]}  ≈  {d['b_stem'][:32]}")
    for d in disputed[:10]:
        print(f"  ✗ [{d['task_id']}#{d['index']}] {d['stem']} :: {'; '.join(d['errors'])}")
    if len(disputed) > 10:
        print(f"  …… 另有 {len(disputed) - 10} 条，见 {out / 'disputed.json'}")


if __name__ == "__main__":
    main()
