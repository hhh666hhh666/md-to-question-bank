#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""md-to-question-bank 脚本共享工具（仅依赖标准库）。

提供：
- quizzy 题目导入契约的字段定义与归一化（题型 / 难度 / 选项 / 答案）
- 文本归一化与指纹计算（用于原文溯源比对与增量合并）
- JSON 读写（UTF-8，不转义中文）
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# ---------------------------------------------------------------- 契约常量
# 与 quizzy-server 的 QuestionImportDTO 一致（POST /api/questions/import/json）
CONTRACT_FIELDS = ("type", "stem", "options", "answer", "analysis",
                   "difficulty", "score", "category", "tags")

VALID_TYPES = ("SINGLE", "MULTI", "JUDGE")
VALID_DIFFS = ("EASY", "MEDIUM", "HARD")
LABELS = ("A", "B", "C", "D", "E", "F")
SCORE_BY_DIFF = {"EASY": 1, "MEDIUM": 2, "HARD": 3}

# 考频标签：三档元标签，**不占用内容标签配额**（Q12/Q15 定）
FREQ_TAGS = ("高频", "中频", "低频")
FREQ_HIGH = "高频"
FREQ_HIGH_MAX_RATIO = 0.30   # 高频题占比上限，超过即告警（防止"一片高频"）
FREQ_HIGH_MIN_SAMPLE = 20    # 样本少于此数时不做占比告警（小样本占比纯噪声）
CONTENT_TAG_LIMIT = 5        # 内容标签建议上限，超出给警告
TAG_TOTAL_LIMIT = 10         # quizzy 服务端硬上限（内容 + 考频）

TYPE_ALIASES = {
    "single": "SINGLE", "singlechoice": "SINGLE", "单选": "SINGLE", "单选题": "SINGLE",
    "multi": "MULTI", "multichoice": "MULTI", "多选": "MULTI", "多选题": "MULTI",
    "judge": "JUDGE", "truefalse": "JUDGE", "判断": "JUDGE", "判断题": "JUDGE", "对错": "JUDGE",
}
DIFF_ALIASES = {
    "easy": "EASY", "简单": "EASY", "容易": "EASY",
    "medium": "MEDIUM", "中等": "MEDIUM", "中": "MEDIUM",
    "hard": "HARD", "困难": "HARD", "难": "HARD",
}

# 判断题选项的规范文案（quizzy 约定判断题固定两选项）
JUDGE_TRUE_WORDS = ("正确", "对", "是", "true", "t", "yes", "y")
JUDGE_FALSE_WORDS = ("错误", "错", "不对", "否", "false", "f", "no", "n")

_WS = re.compile(r"\s+")
_MARK_CHARS = "`*_>"
# ⚠️ 真标签必须以字母或 `/` 开头。早先写成 `<[^<>]{1,200}>`，会把裸写的比较符
# 当成标签吃掉（`<` 到下一个 `>` 之间的整段消失）——实测知识库里写
# 「范围查询（>、< 停止匹配）」这类句子时，那段原文会被剥掉、无法被 source_quote 引用。
_TAG_RE = re.compile(r"</?[A-Za-z][^<>]{0,300}>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_ENTITIES = (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
             ("&#39;", "'"), ("&amp;", "&"))  # &amp; 必须最后替换


# ---------------------------------------------------------------- 文本工具
def strip_html(s: str) -> str:
    """剥离 HTML 注释、标签与实体。

    知识库常是从语雀/飞书富文本粘出来的，结论句被
    `<font style="color:rgb(...)">…</font>` 包着。不剥离的话，模型按"读到的
    干净文字"写出的 source_quote 在原文里根本匹配不上，会被防幻觉校验大面积误杀。
    """
    if not s:
        return ""
    s = _COMMENT_RE.sub("", str(s))
    s = _TAG_RE.sub("", s)
    for a, b in _ENTITIES:
        s = s.replace(a, b)
    return s


def norm(s: str) -> str:
    """归一化文本用于比对：剥 HTML → 去全部空白 → 去常见 markdown 标记 → 转小写。"""
    if not s:
        return ""
    s = strip_html(s)
    s = _WS.sub("", s)
    for ch in _MARK_CHARS:
        s = s.replace(ch, "")
    return s.lower()


def split_tags(tags) -> tuple[list[str], list[str]]:
    """把标签拆成 (内容标签, 考频标签)。考频标签单独校验，不占内容配额。"""
    content, freq = [], []
    for t in (tags or []):
        t = str(t).strip()
        if not t:
            continue
        (freq if t in FREQ_TAGS else content).append(t)
    return content, freq


def fingerprint(stem: str, qtype: str) -> str:
    """题干指纹：同题干 + 同题型视为同一道题。"""
    return hashlib.sha1((norm(stem) + "|" + str(qtype).upper()).encode("utf-8")).hexdigest()[:16]


def content_fingerprint(q: dict) -> str:
    """内容指纹：选项 + 答案 + 解析 + 难度/分值/分类/标签。

    用途是回答「题干相同的一题，内容被改过没有」。**只靠题干指纹判断"已存在"会
    把修订静默丢掉**——2026-10-06 实测：改了一道题的答案，重跑 merge 报「已存在跳过」，
    改动一个字都没进去。merge.py 现在拿它做二次判据：指纹相同且内容也相同才算
    「已存在」，内容变了按修订替换。
    """
    parts = [
        "|".join(f"{o.get('label')}:{norm(o.get('content', ''))}" for o in (q.get("options") or [])),
        ",".join(q.get("answer") or []),
        norm(q.get("analysis") or ""),
        str(q.get("difficulty") or ""),
        str(q.get("score") or ""),
        str(q.get("category") or ""),
        ",".join(sorted(str(t) for t in (q.get("tags") or []))),
    ]
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def slugify(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "_", str(s or "")).strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"^-+|-+$", "", s)
    return (s or "untitled")[:maxlen]


def strip_numbering(s: str) -> str:
    """去掉文件名/标题常见的序号前缀：01-foo、1.2 foo、第三章 xxx。"""
    s = str(s or "").strip()
    s = re.sub(r"^\d+([.\-_]\d+)*[\s.\-_、]+", "", s)
    s = re.sub(r"^第[一二三四五六七八九十百]+[章节部分篇][\s.\-_、]*", "", s)
    return s.strip()


# ---------------------------------------------------------------- 字段归一化
def normalize_type(v) -> str | None:
    if v is None:
        return None
    key = norm(v)
    if key in TYPE_ALIASES:
        return TYPE_ALIASES[key]
    up = str(v).strip().upper()
    return up if up in VALID_TYPES else None


def normalize_diff(v) -> str:
    if v is None:
        return "MEDIUM"
    key = norm(v)
    if key in DIFF_ALIASES:
        return DIFF_ALIASES[key]
    up = str(v).strip().upper()
    return up if up in VALID_DIFFS else "MEDIUM"


def normalize_answer(v) -> list[str]:
    """答案归一化为大写 label 列表，支持 'A' / 'A,C' / ['A','C'] / ['a','c']。"""
    if v is None:
        return []
    if isinstance(v, str):
        parts = re.split(r"[,，、;；\s]+", v)
    elif isinstance(v, (list, tuple, set)):
        parts = []
        for item in v:
            parts.extend(re.split(r"[,，、;；\s]+", str(item)))
    else:
        parts = [str(v)]
    out = []
    for p in parts:
        p = str(p).strip().upper()
        if not p:
            continue
        if p not in LABELS:
            # 兼容 "选项A" / "(A)" 这类写法
            m = re.fullmatch(r"[（(]?([A-Fa-f])[)）]?", p)
            if m:
                p = m.group(1).upper()
        if p in LABELS and p not in out:
            out.append(p)
    return out


def to_contract(q: dict) -> dict:
    """裁剪成 quizzy 导入契约字段（剔除 _task_id / source_quote 等私有字段）。"""
    opts = q.get("options") or []
    out = {
        "type": q.get("type"),
        "stem": q.get("stem"),
        "options": [{"label": o.get("label"), "content": o.get("content")} for o in opts],
        "answer": list(q.get("answer") or []),
        "analysis": q.get("analysis") or "",
        "difficulty": q.get("difficulty"),
        "score": q.get("score"),
        "category": q.get("category"),
        "tags": list(q.get("tags") or []),
    }
    return out


# ---------------------------------------------------------------- IO
def load_json(path) -> dict | list:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump_json(path, data) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def init_console() -> None:
    """Windows 控制台可能是 GBK，强制 UTF-8 输出避免中文报错。"""
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
