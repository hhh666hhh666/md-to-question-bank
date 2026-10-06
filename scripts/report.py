#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 5：生成人工复核报告 + 可交互预览页。

用法：
  python report.py --bank question-bank [--tasks .qbgen/tasks.json]
                   [--validation .qbgen/validation.json] [--skipped .qbgen/skipped.json]

产物：
  <bank>/review.md    题量分布、章节覆盖、存疑清单
  <bank>/preview.html 单文件预览页：筛选 / 展开答案与来源原文 / 勾选导出
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

sys_path = Path(__file__).resolve().parent
import sys  # noqa: E402

sys.path.insert(0, str(sys_path))
from qbcommon import (  # noqa: E402
    FREQ_HIGH_MAX_RATIO, FREQ_HIGH_MIN_SAMPLE, dump_json, init_console, load_json, to_contract,
)

init_console()

TYPE_CN = {"SINGLE": "单选", "MULTI": "多选", "JUDGE": "判断"}
DIFF_CN = {"EASY": "简单", "MEDIUM": "中等", "HARD": "困难"}


def opt(path, default=None):
    p = Path(path)
    return load_json(p) if p.exists() else default


HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>题库预览 · __TITLE__</title>
<style>
  :root { --bg:#f7f8fa; --card:#fff; --line:#e5e7eb; --text:#1f2328; --muted:#6b7280;
          --brand:#2563eb; --ok:#059669; --warn:#d97706; --bad:#dc2626; --chip:#eef2ff; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  header { position:sticky; top:0; z-index:9; background:var(--card); border-bottom:1px solid var(--line);
           padding:14px 20px; }
  h1 { font-size:17px; margin:0 0 10px; }
  .bar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  select, button { font:inherit; padding:6px 10px; border:1px solid var(--line); border-radius:8px;
                   background:#fff; color:var(--text); cursor:pointer; }
  button.primary { background:var(--brand); border-color:var(--brand); color:#fff; }
  .count { color:var(--muted); margin-left:auto; }
  main { padding:20px; display:grid; gap:14px; max-width:1000px; margin:0 auto; }
  .q { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .q.sel { border-color:var(--brand); box-shadow:0 0 0 2px rgba(37,99,235,.12); }
  .meta { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:8px; }
  .chip { background:var(--chip); color:#3730a3; border-radius:999px; padding:1px 9px; font-size:12px; }
  .chip.t-SINGLE { background:#e0f2fe; color:#075985; }
  .chip.t-MULTI { background:#fce7f3; color:#9d174d; }
  .chip.t-JUDGE { background:#fef3c7; color:#92400e; }
  .chip.d-HARD { background:#fee2e2; color:#991b1b; }
  .chip.d-EASY { background:#dcfce7; color:#166534; }
  .stem { font-weight:600; margin:2px 0 10px; }
  ol.opts { margin:0 0 10px; padding-left:22px; }
  ol.opts li.correct { color:var(--ok); font-weight:600; }
  details { border-top:1px dashed var(--line); padding-top:8px; margin-top:6px; }
  summary { cursor:pointer; color:var(--muted); font-size:13px; }
  .analysis { background:#f9fafb; border-radius:8px; padding:10px 12px; margin:8px 0; }
  .quote { border-left:3px solid var(--brand); background:#f9fafb; padding:8px 12px; margin:8px 0;
           color:#374151; font-size:13px; white-space:pre-wrap; }
  label.pick { display:flex; gap:6px; align-items:flex-start; }
  .src { color:var(--muted); font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>题库预览 · __TITLE__（共 <span id="total">0</span> 题）</h1>
  <div class="bar">
    <select id="f-cat"><option value="">全部分类</option></select>
    <select id="f-type"><option value="">全部题型</option></select>
    <select id="f-diff"><option value="">全部难度</option></select>
    <select id="f-freq"><option value="">全部考频</option></select>
    <label class="pick"><input type="checkbox" id="f-sel"> 只看已勾选</label>
    <button id="btn-all">全选当前</button>
    <button id="btn-none">清空选择</button>
    <button class="primary" id="btn-export">导出选中 JSON</button>
    <button id="btn-copy">复制导入 JSON</button>
    <span class="count">已选 <b id="selcount">0</b> 题</span>
  </div>
</header>
<main id="list"></main>
<script>
const DATA = __DATA__;
const TYPE_CN = {SINGLE:"单选", MULTI:"多选", JUDGE:"判断"};
const DIFF_CN = {EASY:"简单", MEDIUM:"中等", HARD:"困难"};
const list = document.getElementById('list');
const sel = new Set();
let view = [];

function fill(id, values, cn) {
  const el = document.getElementById(id);
  values.forEach(v => {
    const o = document.createElement('option');
    o.value = v; o.textContent = cn ? (cn[v] || v) : v;
    el.appendChild(o);
  });
}
fill('f-cat', [...new Set(DATA.map(q => q.category))].sort());
fill('f-type', [...new Set(DATA.map(q => q.type))], TYPE_CN);
fill('f-diff', [...new Set(DATA.map(q => q.difficulty))], DIFF_CN);
fill('f-freq', ['高频', '中频', '低频']);

function render() {
  const c = document.getElementById('f-cat').value;
  const t = document.getElementById('f-type').value;
  const d = document.getElementById('f-diff').value;
  const f = document.getElementById('f-freq').value;
  const onlySel = document.getElementById('f-sel').checked;
  view = DATA.filter((q, i) =>
    (!c || q.category === c) && (!t || q.type === t) && (!d || q.difficulty === d)
    && (!f || (q.tags || []).includes(f))
    && (!onlySel || sel.has(i)));
  document.getElementById('total').textContent = view.length;
  list.innerHTML = '';
  view.forEach(q => {
    const i = q.__i;
    const el = document.createElement('div');
    el.className = 'q' + (sel.has(i) ? ' sel' : '');
    const ans = new Set(q.answer || []);
    el.innerHTML = `
      <div class="meta">
        <label class="pick"><input type="checkbox" data-i="${i}" ${sel.has(i)?'checked':''}> 选择</label>
        <span class="chip t-${q.type}">${TYPE_CN[q.type] || q.type}</span>
        <span class="chip d-${q.difficulty}">${DIFF_CN[q.difficulty] || q.difficulty}</span>
        <span class="chip">${q.score} 分</span>
        <span class="chip">${q.category}</span>
        ${(q.tags||[]).map(t => `<span class="chip">#${t}</span>`).join('')}
      </div>
      <div class="stem">${escapeHtml(q.stem)}</div>
      <ol class="opts">
        ${(q.options||[]).map(o => `<li class="${ans.has(o.label)?'correct':''}">${o.label}. ${escapeHtml(o.content)}</li>`).join('')}
      </ol>
      <details>
        <summary>答案 / 解析 / 原文出处</summary>
        <div class="analysis"><b>答案：</b>${(q.answer||[]).join(', ')}</div>
        <div class="analysis">${q.analysis ? escapeHtml(q.analysis) : '<i>（无解析）</i>'}</div>
        <div class="quote">${escapeHtml(q.source_quote || '（无原文出处）')}</div>
        <div class="src">来源：${escapeHtml(q._source_file || '')} → ${escapeHtml(q._source_section || '')}</div>
      </details>`;
    list.appendChild(el);
  });
  list.querySelectorAll('input[data-i]').forEach(cb => cb.onchange = e => {
    const i = +e.target.dataset.i;
    e.target.checked ? sel.add(i) : sel.delete(i);
    document.getElementById('selcount').textContent = sel.size;
    render();
  });
  document.getElementById('selcount').textContent = sel.size;
}
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
}
function picked() { return DATA.filter((q, i) => sel.has(i)).map(q => q.__contract); }
document.getElementById('btn-all').onclick = () => { view.forEach(q => sel.add(q.__i)); render(); };
document.getElementById('btn-none').onclick = () => { sel.clear(); render(); };
document.getElementById('f-sel').onchange = render;
['f-cat','f-type','f-diff','f-freq'].forEach(id => document.getElementById(id).onchange = render);
document.getElementById('btn-export').onclick = () => {
  const blob = new Blob([JSON.stringify(picked(), null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'questions-selected.json';
  a.click();
};
document.getElementById('btn-copy').onclick = async () => {
  try { await navigator.clipboard.writeText(JSON.stringify(picked(), null, 2)); alert('已复制导入 JSON'); }
  catch (e) { alert('复制失败，请手动导出'); }
};
render();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="生成复核报告与预览页")
    ap.add_argument("--bank", default="question-bank")
    ap.add_argument("--tasks", default=".qbgen/tasks.json")
    ap.add_argument("--validation", default=".qbgen/validation.json")
    ap.add_argument("--skipped", default=".qbgen/skipped.json")
    args = ap.parse_args()

    bank = Path(args.bank).resolve()
    full = opt(bank / ".full.json")
    if not full:
        print(f"未找到 {bank / '.full.json'}，请先运行 merge.py")
        sys.exit(1)
    questions = full["questions"]
    tasks = opt(args.tasks)
    validation = opt(args.validation)
    skipped = opt(args.skipped)

    # ---------------- review.md
    n = len(questions)
    c_cat = Counter(q.get("category", "未分类") for q in questions)
    c_type = Counter(q.get("type") for q in questions)
    c_diff = Counter(q.get("difficulty") for q in questions)
    c_freq = Counter(q.get("_freq") or "未标" for q in questions)
    max_per = int((tasks or {}).get("max_per_section", 5) or 5) if tasks else 5
    per_task = Counter(q.get("_task_id") for q in questions if q.get("_task_id"))
    over = {k: v for k, v in per_task.items() if v > max_per}

    lines = [
        "# 题库复核报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 题库总数：**{n}**",
        f"- 源文件：{bank / '.full.json'}（含溯源信息）；导入用：`all.json`（或 `by-category/<分类>.json`）",
        "",
        "## 题型 / 难度 / 考频分布",
        "",
        "| 维度 | 分布 |",
        "| --- | --- |",
        "| 题型 | " + "、".join(f"{TYPE_CN.get(k, k)} {v}" for k, v in c_type.most_common()) + " |",
        "| 难度 | " + "、".join(f"{DIFF_CN.get(k, k)} {v}" for k, v in c_diff.most_common()) + " |",
        "| 考频 | " + "、".join(f"{k} {v}" for k, v in c_freq.most_common()) + " |",
        "| 分类 | " + "、".join(f"{k} {v}" for k, v in c_cat.most_common()) + " |",
        "",
    ]

    # 单节题数：不设配额，只把"异常多"的挑出来（Q5/Q14 定）
    lines += ["## 单节题数", "",
              f"- 有题目的切片 {len(per_task)} 个，平均 {n / len(per_task):.1f} 题/节" if per_task
              else "-（无数据）"]
    if over:
        lines.append(f"- **超过告警阈值 {max_per} 题的切片**（自己判断是真密度还是凑数）：")
        for tid, cnt in sorted(over.items(), key=lambda x: -x[1]):
            lines.append(f"  - `{tid}`：{cnt} 题")
    else:
        lines.append(f"- 没有单节超过 {max_per} 题的切片")

    lines += ["", "## 章节覆盖", ""]

    if tasks:
        # ⚠️ 按 _task_id 统计，**不要按 section 名**——同一节的二次切分片（`__p01` / `__p02`）
        # 章节名完全相同，按名字去重会把两片算成一片，报出「53 个切片只覆盖 51、96%」
        # 的假象（2026-10-06 实测），而实际是 100%。
        covered_ids = {q.get("_task_id") for q in questions if q.get("_task_id")}
        all_ids = [t["id"] for t in tasks["tasks"]]
        hit = sum(1 for i in all_ids if i in covered_ids)
        ratio = f"{hit / len(all_ids):.0%}" if all_ids else "n/a"
        lines.append(f"- 切片总数 {len(all_ids)}，产出题目的切片 {hit}，覆盖率 {ratio}")
        empty = [t for t in tasks["tasks"] if t["id"] not in covered_ids]
        if empty:
            lines.append("")
            lines.append("未产出题目的切片（可补充或确认是否确无考点）：")
            for t in empty[:30]:
                lines.append(f"- `{t['rel']}` → {t['section']}")
            if len(empty) > 30:
                lines.append(f"- …… 另有 {len(empty) - 30} 个")
    else:
        lines.append("-（未提供 tasks.json，跳过覆盖率统计）")

    if skipped and skipped.get("count"):
        lines += ["", f"## 被跳过的章节（{skipped['count']}）", ""]
        for s in skipped["items"][:20]:
            lines.append(f"- `{s['file']}` → {s['section']}：{s.get('reason', '')}")
        if skipped["count"] > 20:
            lines.append(f"- …… 另有 {skipped['count'] - 20} 项")

    if validation:
        v = validation
        lines += ["", "## 校验结果", "",
                  f"- 总题数 {v['total']}：通过 {v['ok']}、带警告 {v['warn']}、失败 {v['fail']}"]
        if v.get("freq_dist"):
            ratio = v.get("high_ratio", 0)
            flag = ""
            if v["total"] >= FREQ_HIGH_MIN_SAMPLE and ratio > FREQ_HIGH_MAX_RATIO:
                flag = "（**超限**，判据可能没锚定原文）"
            elif v["total"] < FREQ_HIGH_MIN_SAMPLE:
                flag = "（样本太小，占比仅供参考）"
            lines.append(f"- 考频：{'、'.join(f'{k} {x}' for k, x in v['freq_dist'].items())}"
                         f"；高频占比 {ratio:.0%}{flag}")
        warns = [i for i in v["items"] if i["status"] == "warn"]
        if warns:
            lines += ["", "带警告的题目（建议人工扫一眼）："]
            for i in warns[:20]:
                lines.append(f"- {i['stem']} → {'; '.join(i['warnings'])}")
        fails = [i for i in v["items"] if i["status"] == "fail"]
        if fails:
            lines += ["", "**校验失败、已隔离的题目（未进题库）：**"]
            for i in fails[:30]:
                lines.append(f"- {i['stem']} → {'; '.join(i['errors'])}")

    lines += ["", "## 下一步", "",
              "1. 打开 `preview.html` 抽样核对答案与原文出处；",
              "2. 需要保留的手改题，在 `.full.json` 里给它加 `\"_locked\": true`；",
              "3. 确认后把 `all.json` 内容贴进前端导入框，或用：",
              "   `curl -X POST http://localhost:8080/api/questions/import/json -H \"Content-Type: application/json\" -H \"Authorization: Bearer <token>\" --data-binary @all.json`"]
    (bank / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---------------- preview.html
    payload = []
    for i, q in enumerate(questions):
        item = {k: v for k, v in q.items() if not k.startswith("_")}
        item["_source_file"] = q.get("_source_file", "")
        item["_source_section"] = q.get("_source_section", "")
        item["__i"] = i
        item["__contract"] = to_contract(q)
        payload.append(item)
    data_json = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    html = HTML_TMPL.replace("__DATA__", data_json).replace("__TITLE__", f"{n} 题")
    (bank / "preview.html").write_text(html, encoding="utf-8")

    print(f"报告 → {bank / 'review.md'}")
    print(f"预览 → {bank / 'preview.html'}（{n} 题）")


if __name__ == "__main__":
    main()
