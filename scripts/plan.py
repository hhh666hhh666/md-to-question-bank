#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 1：扫描 markdown 知识库，切分成出题任务清单。

用法：
  python plan.py --docs <目录或文件> --out .qbgen [--split-level 2]
                 [--max-chars 2000] [--min-chars 80] [--max-per-section 5]
                 [--mode auto|qa|prose] [--with-images] [--category <名>]

产物：
  <out>/tasks.json   每个元素是一个出题单元（章节切片）
  <out>/plan.md      人类可读的切分报告（含被跳过的章节与原因）

切分规则：
  1. 按 H2 切成一节；单节超过 --max-chars 时再按段落二次切分；
  2. 绝不在代码块 / 表格中间切断；
  3. 丢掉 `<details>…</details>` 折叠块（语雀导出的"格式化后的重复答案"）；
  4. 空节（去掉标题/链接/星号后无内容）与低信息密度节跳过；
  5. 出题数量不设配额，只给 --max-per-section 作为告警阈值（Q5/Q14 定）。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qbcommon import (  # noqa: E402
    dump_json, init_console, load_json, slugify, strip_html, strip_numbering,
)

init_console()

FRONT_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", re.S)
HEAD_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
FENCE_RE = re.compile(r"^\s*(?:```|~~~)", re.M)
TABLE_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)
TERM_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff+#._\- ]{1,15}$")
DETAILS_RE = re.compile(r"<details\b.*?</details\s*>", re.S | re.I)
IMG_MD_RE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)")
IMG_HTML_RE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.I)
SEP_RE = re.compile(r"[-=_*~#]{3,}")

# 疑问式标题的标记词（用于判断这一节是不是"问答形态"）
QA_WORDS = ("是什么", "什么是", "有哪些", "哪些", "为什么", "怎么", "怎样", "如何",
            "区别", "什么时候", "是否", "多少", "哪几种", "几种", "能否", "要不要")

# 切分时要整个跳过的目录名。目录约定是 `<学科>/md/` 放知识库、`<学科>/json/` 放产物，
# 而 `json/review.md` 也是 .md —— 不排除的话，重跑切分会把「题库复核报告」当知识库出题
# （2026-10-06 实测：产物入库后重跑，凭空多出 4 个 `review__*` 切片）。
EXCLUDE_DIR_PARTS = {"json", ".qbgen", ".git", "node_modules", "__pycache__", ".venv", "dist", "build"}

# 「是 .md、但不是知识库源」的文件名：受控词表与 `md/` 同级，rglob 会把它一起扫进来，
# 那 77 行标签会被切成一个切片去出题（2026-10-06 实测踩到）。
EXCLUDE_FILE_NAMES = {"tags.md"}


# ------------------------------------------------------------ frontmatter
def parse_frontmatter(text: str):
    m = FRONT_RE.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    cur_key = None
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if cur_key:
                meta.setdefault(cur_key, [])
                if isinstance(meta[cur_key], list):
                    meta[cur_key].append(stripped[2:].strip())
            continue
        if ":" not in stripped:
            continue
        k, v = stripped.split(":", 1)
        k = k.strip().lower()
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
            cur_key = None
        elif v == "":
            meta[k] = []
            cur_key = k
        else:
            meta[k] = v.strip("\"'")
            cur_key = None
    return meta, text[m.end():]


# ------------------------------------------------------------ 清洗
def clean_title(t: str) -> str:
    """清洗标题：先剥 HTML（`# <font ...>存储引擎</font>` 很常见），再去纯分隔符。

    不去分隔符的话，`# -------------------概念-------------------` 会让分类变成一串横线。
    """
    t = strip_html(str(t or ""))
    t = SEP_RE.sub(" ", t)
    t = re.sub(r"^[\s\-=_*~#]+|[\s\-=_*~#]+$", "", t)
    return re.sub(r"\s+", " ", t).strip()


def drop_details(body: str):
    """丢掉 <details> 折叠块（语雀导出里是"格式化后的重复答案"，既撞字数又产生重复题）。"""
    n = len(DETAILS_RE.findall(body))
    return DETAILS_RE.sub("\n", body), n


def extract_images(text: str) -> list[str]:
    """抽出 markdown 图片与 HTML <img> 的地址（--with-images 时需要下载来看）。"""
    urls = [m.group(1) for m in IMG_MD_RE.finditer(text)]
    urls += [m.group(1) for m in IMG_HTML_RE.finditer(text)]
    seen, out = set(), []
    for u in urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def body_lines_of(text: str) -> list[str]:
    """取正文行（剥 HTML、去标题行与注释），用于判空与形态检测。"""
    out = []
    for ln in strip_html(text).splitlines():
        s = ln.strip()
        if not s or HEAD_RE.match(s):
            continue
        out.append(s)
    return out


def is_trivial(s: str) -> bool:
    """只有链接 / 只有星号横线 / 只有"错误日志:"这种标签没正文的行。"""
    t = re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", s)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[\s*\-_~>#|]+", "", t)
    if not t:
        return True
    return bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9 ()（）]{1,20}[:：]", t))


def is_empty_body(text: str) -> bool:
    lines = body_lines_of(text)
    return not lines or all(is_trivial(x) for x in lines)


def detect_mode(title: str, forced: str) -> str:
    """判断这一节是"问答形态"（一节一个知识点，标题即题干骨架）还是散文。"""
    if forced and forced != "auto":
        return forced
    t = clean_title(title)
    if not t or len(t) > 40:
        return "prose"
    if t.rstrip().endswith(("?", "？")):
        return "qa"
    if any(w in t for w in QA_WORDS):
        return "qa"
    return "prose"


# ------------------------------------------------------------ 结构解析
def build_blocks(body: str):
    """把正文切成 [(level, title, lines)]，跳过围栏内的伪标题。"""
    blocks = []
    cur_level, cur_title, cur_lines = 0, "", []
    in_fence = False

    def flush():
        if cur_lines or cur_title:
            blocks.append({"level": cur_level, "title": cur_title, "lines": cur_lines})

    for line in body.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
        m = HEAD_RE.match(line) if not in_fence else None
        if m:
            flush()
            cur_level, cur_title, cur_lines = len(m.group(1)), m.group(2).strip(), []
        else:
            cur_lines.append(line)
    flush()
    return blocks


def group_blocks(blocks, split_level: int):
    """按 split_level 聚合成章节组，保留上级标题作为面包屑。"""
    groups, stack = [], []
    cur = None
    for b in blocks:
        lvl = b["level"] or (split_level + 1)
        stack = [x for x in stack if x[0] < lvl]
        if lvl <= split_level:
            cur = {"title": b["title"], "path": stack + [(lvl, b["title"])],
                   "lines": list(b["lines"])}
            groups.append(cur)
            stack = stack + [(lvl, b["title"])]
        else:
            if cur is None:
                cur = {"title": "", "path": [], "lines": []}
                groups.append(cur)
            cur["lines"].extend(b["lines"])
            stack = stack + [(lvl, b["title"])]
    return groups


def split_paragraphs(lines):
    """按空行切段，代码块整体不拆。"""
    paras, cur, in_fence = [], [], False
    for ln in lines:
        if FENCE_RE.match(ln):
            in_fence = not in_fence
            cur.append(ln)
            continue
        if not in_fence and not ln.strip():
            if cur:
                paras.append(cur)
                cur = []
            continue
        cur.append(ln)
    if cur:
        paras.append(cur)
    return paras


def chunk_group(group, max_chars: int):
    """章节过大时按段落二次切分，返回若干 (part_index, total, lines)。"""
    title = clean_title(group["title"])
    head_line = ("#" * (group["path"][-1][0] if group["path"] else 2) + " " + title) if title else ""
    paras = split_paragraphs(group["lines"])
    chunks, cur, cur_len = [], [], 0
    for p in paras:
        plen = len(re.sub(r"\s+", "", strip_html("\n".join(p))))
        if cur and cur_len + plen > max_chars:
            chunks.append(cur)
            cur, cur_len = [], 0
        if cur:
            cur.append("")  # 保留段落间的空行，避免 markdown 结构被破坏
        cur.extend(p)
        cur_len += plen
    if cur:
        chunks.append(cur)
    if not chunks:
        chunks = [[]]
    total = len(chunks)
    out = []
    for i, ch in enumerate(chunks, 1):
        # 第 2 片起也带上标题（标「（续）」）——早先只给第 1 片带标题，模型拿到
        # `__p02` 时看到的是一段没有上下文的正文，容易判错重点（2026-10-06 实测）。
        if not head_line:
            prefix = []
        else:
            prefix = [head_line] if i == 1 else [head_line + "（续）"]
        out.append({"part": i, "total": total, "lines": prefix + ch})
    return out


# ------------------------------------------------------------ 元数据推断
def infer_category(meta, rel: Path, h1: str, override: str | None) -> str:
    if override:
        return clean_title(override) or override.strip()
    for key in ("category", "分类", "categories"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    parts = rel.parts
    if len(parts) > 1:
        c = clean_title(strip_numbering(parts[0]))
        if c:
            return c
    c = clean_title(strip_numbering(h1))
    return c or "未分类"


def extract_terms(text: str, exclude, limit: int = 2):
    """从代码块语言名和行内反引号术语中抽候选标签（过滤纯符号/括号类噪声）。"""
    langs = [l.strip() for l in re.findall(r"^\s*(?:```|~~~)\s*([A-Za-z0-9+#\-]+)", text, re.M)]
    ticks = re.findall(r"`([^`\n]{2,20})`", text)
    counter = Counter()
    for t in ticks:
        t = t.strip()
        if len(t) < 2 or len(t) > 16:
            continue
        if not TERM_RE.match(t):
            continue
        counter[t] += 1
    out, seen = [], set(exclude)
    for cand in langs + [w for w, _ in counter.most_common(10)]:
        key = cand.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(cand)
        if len(out) >= limit:
            break
    return out


def find_tag_vocab(md: Path, docs_root: Path):
    """从 md 所在目录往上找 `tags.md`（约定放在 `question-bank/<学科>/tags.md`）。"""
    cur = md.parent
    for _ in range(4):
        cand = cur / "tags.md"
        if cand.is_file():
            return cand
        if cur == docs_root or cur.parent == cur:
            break
        cur = cur.parent
    return None


def load_tag_vocab(path: Path) -> list[str]:
    """读受控词表：每行一个标签（`- 标签` 或裸标签），忽略标题 / 引用 / 表格行。"""
    out, seen = [], set()
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = re.sub(r"^[-*+]\s+", "", ln.strip()).strip()
        if not s or s.startswith(("#", ">", "|", "```")) or len(s) > 24:
            continue
        if s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out


def infer_tags(meta, rel: Path, title: str, text: str, limit: int = 5, vocab=None):
    """内容标签。

    有受控词表（`question-bank/<学科>/tags.md`）时**只从词表里挑**在标题或正文中出现过的，
    标题里出现的优先。旧行为是从整句标题、文件名和代码块语言名里抽，产出的是
    `['MySQL', '为什么要小表驱动大表', 'sql', 'java']` 这类噪声（整句当标签、`java` 其实是
    代码块语言名），既污染标签库、又和模型按 authoring.md 标的标签打架（2026-10-06 实测）。
    没有词表时退回旧行为，并在计划报告里给出提示。
    """
    if vocab:
        hay = strip_html(text) + "\n" + title
        scored = [(hay.count(t) + (50 if t in title else 0), t) for t in vocab if t in hay]
        scored.sort(key=lambda x: (-x[0], len(x[1])))
        return [t for _, t in scored[:limit]]
    tags: list[str] = []
    for key in ("tags", "tag", "标签"):
        v = meta.get(key)
        if isinstance(v, list):
            tags.extend(str(x).strip() for x in v if str(x).strip())
        elif isinstance(v, str) and v.strip():
            tags.extend(x.strip() for x in re.split(r"[,，、]", v) if x.strip())
    if not tags:
        stem = strip_numbering(rel.stem)
        if stem:
            tags.append(stem)
        t = clean_title(strip_numbering(title))
        if t and t.lower() != stem.lower():
            tags.append(t)
        tags.extend(extract_terms(text, set(x.lower() for x in tags)))
    out, seen = [], set()
    for t in tags:
        t = re.sub(r"\s+", " ", str(t)).strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out[:limit]


# ------------------------------------------------------------ 主流程
def process_file(md: Path, docs_root: Path, args, tasks, skipped, seen_ids, stats, vocab=None):
    raw = md.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_frontmatter(raw)
    body, dropped = drop_details(body)
    stats["dropped_details"] += dropped
    blocks = build_blocks(body)
    if dropped:
        skipped.append({"file": str(md.name), "section": "(全文)",
                        "reason": f"丢弃了 {dropped} 个 <details> 折叠块（语雀导出的重复答案）"})
    h1 = next((clean_title(b["title"]) for b in blocks if b["level"] == 1), "")
    rel = md.relative_to(docs_root) if md.is_relative_to(docs_root) else Path(md.name)
    category = infer_category(meta, rel, h1, args.category)

    for group in group_blocks(blocks, args.split_level):
        title = clean_title(group["title"])
        crumbs = " > ".join(clean_title(t) or "(无标题)" for _, t in group["path"]) or "(开头)"
        for chunk in chunk_group(group, args.max_chars):
            text = "\n".join(chunk["lines"]).strip()
            plain_len = len(re.sub(r"\s+", "", strip_html(text)))
            has_code = bool(FENCE_RE.search(text))
            has_table = bool(TABLE_RE.search(text))
            mode = detect_mode(title, args.mode)
            if is_empty_body(text):
                skipped.append({"file": str(rel), "section": crumbs,
                                "reason": "空节（去掉标题/链接/星号后没有正文）"})
                continue
            # 问答式的节常常「一句话就是完整答案」，用同一个阈值会被整节丢掉。
            # 2026-10-06 实测：「MySQL 的 DDL 和 DML 分别是什么含义？」74 字被判低信息密度
            # 跳过，白丢两个考点。所以 qa 形态放宽到 60%（且不低于 30）。
            min_need = max(30, int(args.min_chars * 0.6)) if mode == "qa" else args.min_chars
            if plain_len < min_need and not has_code and not has_table:
                skipped.append({"file": str(rel), "section": crumbs,
                                "reason": f"信息密度不足（非空白字符 {plain_len} < {min_need}"
                                          f"{'，问答式节已放宽' if mode == 'qa' else ''} 且无代码/表格）"})
                continue
            tid = f"{slugify(rel.stem)}__{slugify(title) or 'intro'}"
            if chunk["total"] > 1:
                tid += f"__p{chunk['part']:02d}"
            base, n = tid, 2
            while tid in seen_ids:
                tid = f"{base}-{n}"
                n += 1
            seen_ids.add(tid)
            stats["mode"][mode] += 1
            images = extract_images(text)
            stats["images"] += len(images)
            task = {
                "id": tid,
                "file": str(md.resolve()),
                "rel": str(rel).replace("\\", "/"),
                "section": crumbs,
                "title": strip_numbering(title),
                "h1": h1,
                "category": category,
                "tags": infer_tags(meta, rel, title, text, vocab=vocab),
                "mode": mode,
                "part": chunk["part"],
                "parts": chunk["total"],
                "char_count": plain_len,
                "has_code": has_code,
                "has_table": has_table,
                "max_per_section": args.max_per_section,
                "text": f"[来源] {rel} | {crumbs}\n\n{text}",
            }
            if args.with_images and images:
                task["images"] = images
            tasks.append(task)


def main():
    ap = argparse.ArgumentParser(description="扫描 markdown 知识库，生成出题任务清单")
    ap.add_argument("--docs", required=True, help="知识库目录或单个 .md 文件")
    ap.add_argument("--out", default=".qbgen", help="中间产物目录")
    ap.add_argument("--split-level", type=int, default=2, help="按几级标题切片，默认 2（H2）")
    ap.add_argument("--max-chars", type=int, default=2000, help="单节超过该字数则二次切分")
    ap.add_argument("--min-chars", type=int, default=80, help="低于该非空白字符数视为低信息密度")
    ap.add_argument("--max-per-section", type=int, default=5,
                    help="单节题数告警阈值（不是配额，超出只在报告里标出）")
    ap.add_argument("--mode", default="auto", choices=["auto", "qa", "prose"],
                    help="出题形态：auto 自动识别问答式标题，qa/prose 强制")
    ap.add_argument("--with-images", action="store_true",
                    help="把各节的图片地址抽进 tasks.json（供下载后看图，不影响出题流程）")
    ap.add_argument("--category", default=None, help="强制指定分类（默认从目录名/frontmatter/一级标题推断）")
    args = ap.parse_args()

    docs = Path(args.docs).resolve()
    files = [docs] if docs.is_file() else sorted(docs.rglob("*.md"))
    files = [f for f in files
             if not (set(f.parts) & EXCLUDE_DIR_PARTS)
             and not f.name.startswith(".")
             and f.name.lower() not in EXCLUDE_FILE_NAMES]
    if not files:
        print(f"未找到任何 .md 文件：{docs}")
        sys.exit(1)

    root = docs if docs.is_dir() else docs.parent
    # 受控词表（可选）：question-bank/<学科>/tags.md
    vocab_path = next((p for p in (find_tag_vocab(f, root) for f in files) if p), None)
    vocab = load_tag_vocab(vocab_path) if vocab_path else None

    tasks, skipped, seen_ids = [], [], set()
    stats = {"dropped_details": 0, "images": 0, "mode": Counter()}
    for f in files:
        process_file(f, root, args, tasks, skipped, seen_ids, stats, vocab=vocab)

    out = Path(args.out).resolve()

    # ---- 增量：先跟上次的 tasks.json 比 id，再覆盖写。
    # 用途是「给知识库加了一节之后，只对新片出题」——重出老章节会让题干被改写，
    # 那样 merge 既可能替换、也可能新增，把已经入库的题搅浑。
    prev_path = out / "tasks.json"
    prev_ids: list[str] = []
    if prev_path.exists():
        try:
            prev_ids = [t.get("id") for t in (load_json(prev_path).get("tasks") or [])]
        except (OSError, ValueError, TypeError, AttributeError):
            prev_ids = []
    new_ids = [t["id"] for t in tasks]
    prev_set, new_set = set(prev_ids), set(new_ids)
    added = [i for i in new_ids if i not in prev_set]
    removed = [i for i in prev_ids if i and i not in new_set]

    dump_json(out / "tasks.json", {
        "docs_root": str(root),
        "split_level": args.split_level,
        "max_per_section": args.max_per_section,
        "mode": args.mode,
        "guide": "每个 task 独立出一个文件 .qbgen/pieces/<id>.json；"
                 "按知识点出题（一节有几个可考点就出几题，不设配额）；"
                 f"单节超过 {args.max_per_section} 题会在复核报告里标出。",
        "task_count": len(tasks),
        "tasks": tasks,
    })
    dump_json(out / "skipped.json", {"count": len(skipped), "items": skipped})
    dump_json(out / "incremental.json", {
        "added": added,
        "removed": removed,
        "unchanged": [i for i in new_ids if i in prev_set],
        "note": "added = 本次新增的切片（只对它出题即可，老题会在 merge 时命中指纹自动跳过）；"
                "首次运行（还没有上次的 tasks.json）时 added 就是全部。",
    })

    cats = Counter(t["category"] for t in tasks)
    modes = Counter(t["mode"] for t in tasks)
    vocab_line = (
        f"- 受控词表：`{vocab_path}`（{len(vocab)} 个标签，只从表里挑）" if vocab
        else "- 受控词表：**未找到 `tags.md`**，标签退回自动推断——那会掺进整句标题和"
             "代码块语言名（如 `java`），建议建一份 `question-bank/<学科>/tags.md`"
    )
    def relname(p: Path) -> str:
        try:
            return str(p.relative_to(root)).replace("\\", "/")
        except ValueError:
            return p.name

    lines = [
        "# 出题计划",
        "",
        f"- 知识库：`{root}`",
        f"- 文档数：{len(files)}，切片数：**{len(tasks)}**，跳过：{len(skipped)}",
        "- 扫描到的源文件：" + "、".join(f"`{relname(f)}`" for f in files),
        f"- 切片粒度：H{args.split_level}，单节上限 {args.max_chars} 字",
        f"- 形态：{dict(modes)}（qa = 标题即题干骨架）",
        f"- 丢弃 `<details>` 折叠块：{stats['dropped_details']} 个",
        f"- 图片：{stats['images']} 张" + ("（已写入 tasks.json）" if args.with_images else "（未提取，加 --with-images）"),
        vocab_line,
        "",
        "## 本次增量",
        "",
        f"- 新增 **{len(added)}** / 消失 {len(removed)} / 不变 {len(new_ids) - len(added)}",
    ]
    if added:
        lines += ["", "**新增切片（只对它们出题即可，老题会在 merge 时按指纹跳过）**："]
        for i in added:
            t = next(x for x in tasks if x["id"] == i)
            lines.append(f"- `{i}`（{t['char_count']} 字，{t['mode']}）")
    if removed:
        lines += ["", "**上次有、本次没有的切片**（改了标题，或在源文件里被删 / 被并）："]
        lines += [f"- `{i}`" for i in removed]
    lines += ["", "## 分类分布", ""]
    for c, n in cats.most_common():
        lines.append(f"- {c}：{n} 个切片")
    if skipped:
        lines += ["", "## 被跳过的章节", ""]
        for s in skipped[:50]:
            lines.append(f"- `{s['file']}` → {s['section']}：{s['reason']}")
        if len(skipped) > 50:
            lines.append(f"- …… 另有 {len(skipped) - 50} 项")
    (out / "plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"切片 {len(tasks)} 个（跳过 {len(skipped)}），分类 {len(cats)} 个，"
          f"形态 {dict(modes)}，图 {stats['images']} 张 → {out / 'tasks.json'}")
    print(f"  增量：新增 {len(added)} / 消失 {len(removed)} / 不变 {len(new_ids) - len(added)}"
          + (f"（新增片清单见 {out / 'incremental.json'}）" if added else ""))
    if vocab_path:
        print(f"  受控词表：{vocab_path}（{len(vocab)} 个标签）")
    else:
        print("  ⚠ 未找到 tags.md，标签退回自动推断（建议建一份 question-bank/<学科>/tags.md）")


if __name__ == "__main__":
    main()
