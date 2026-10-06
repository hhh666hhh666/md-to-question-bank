# md-to-question-bank

把一个 markdown 知识库切成章节、逐节出题，产出**可直接导入的题库 JSON**，并附复核报告与可交互预览页。

为一个 AI Agent 技能（skill）包：`SKILL.md` 是技能说明书，`scripts/` 是流水线脚本，`references/` 是出题规范与导入契约。

> **核心设计原则**：题库里一道答案错的题，比没有题更糟——它会被反复刷、把错误知识刻进脑子。
> 所以这个技能的价值不在于「能出题」，而在于**四道防线**：
> **原文溯源 · 脚本硬校验 · 位置均衡 · 人工复核页**。
> 生成环节少一步可以，防线一步都不能省。

---

## 它解决什么问题

让大模型直接「读文档出题」，会稳定地产出三类垃圾：

| 症状 | 具体表现 | 本项目的对策 |
| --- | --- | --- |
| **幻觉题** | 题干和选项看着专业，原文里根本没有这个结论 | 每题必须带一句**逐字照抄**的原文原句 `source_quote`，脚本回原文做字符串比对，找不到整题作废 |
| **位置偏置** | 正确答案永远在 A，导进题库等于「闭眼选 A 就能过」，区分度归零 | `balance_options.py` 事后重排选项，把正确项轮转分配到最少使用的位置 |
| **配额凑数** | 一节只有一个考点，硬凑 5 道题，剩下 4 道是同义反复 | 明确「按知识点出题、不按配额」，一节 0–5 题都合法 |

实测数据：一份 189 题的题库，单选 125 题里 **122 题答案是 A（97.6%）**——不加位置均衡，这份库基本无法使用。

---

## 流水线

**只有第 2 步由模型创作，其余四步都是脚本。** `.qbgen/` 是中间产物（可随时删了重跑），`question-bank/<学科>/json/` 才是交付物。

| # | 步骤 | 命令 | 产物 | 谁做 |
| --- | --- | --- | --- | --- |
| 1 | 切分 | `plan.py --docs question-bank --out .qbgen` | `tasks.json`、`plan.md`、`skipped.json`、`incremental.json` | 脚本 |
| 2 | 出题 | 无脚本，逐片写 `pieces/<id>.json` | 每片一个 JSON（题干 / 选项 / 答案 / 解析 / `source_quote`） | **模型** |
| 3 | 校验 | `validate.py --pieces .qbgen/pieces --tasks .qbgen/tasks.json` | `clean.json`、`disputed.json`、`validation.json` | 脚本 |
| 3b | 位置均衡 | `balance_options.py --pieces .qbgen/pieces` | 原地重排选项顺序与答案 label | 脚本 |
| 4 | 合并 | `merge.py --clean .qbgen/clean.json --bank question-bank/<学科>/json` | `all.json`、`by-category/`、`.full.json`、`.meta.json` | 脚本 |
| 5 | 复核 | `report.py --bank … --tasks … --validation … --skipped …` | `review.md`、`preview.html` | 脚本 |

数据流：

```
md/  ──plan──▶  tasks.json  ──模型出题──▶  pieces/  ──validate──▶  clean.json
                                                                    │
                            ┌───────────────balance_options──────────┘
                            ▼
                        merge  ──▶  json/all.json  ──▶  人工复核  ──▶  自己导入数据库
```

---

## 目录约定

`question-bank/` 是「学科工作区」根，一个学科一个目录：

| 路径 | 作用 |
| --- | --- |
| `question-bank/<学科>/md/` | **知识库源文件**（切分只扫这里）。子目录名 = 分类（`category`），文件名 = 主标签 |
| `question-bank/<学科>/json/` | **产物**：`all.json`（直接导入）、`by-category/`、`.full.json`、`.meta.json`、`preview.html`、`review.md` |
| `question-bank/<学科>/tags.md` | **标签受控词表**（可选但强烈建议）：`plan.py` 只从这张表里挑标签 |
| `.qbgen/` | 中间产物，可随时删了重跑 |

⚠️ `plan.py` 扫 `.md` 时会**排除产物目录 `json/` 与词表文件 `tags.md`**——它们也是 `.md`，不排除的话「题库复核报告」和标签文件会被当成知识库去出题。

---

## 快速开始

依赖：**Python 3.10+，纯标准库**，无需安装任何第三方包。

```bash
# 1. 把本仓库放到宿主的 skills 目录，或任意位置后设置 SKILL_DIR
SKILL_DIR="/path/to/md-to-question-bank"
PY="python3"

# 2. 在你自己的项目里准备知识库
#    question-bank/mysql/md/*.md

# 3. 切分
$PY $SKILL_DIR/scripts/plan.py --docs question-bank --out .qbgen --with-images

# 4. 出题：读 .qbgen/tasks.json，每个 task 写一个 .qbgen/pieces/<task_id>.json
#    （这一步由具备出题能力的模型完成，或由人手工写）

# 5. 校验 → 位置均衡 → 重新校验 → 合并 → 出复核页
$PY $SKILL_DIR/scripts/validate.py        --pieces .qbgen/pieces --tasks .qbgen/tasks.json --out .qbgen
$PY $SKILL_DIR/scripts/balance_options.py --pieces .qbgen/pieces
$PY $SKILL_DIR/scripts/validate.py        --pieces .qbgen/pieces --tasks .qbgen/tasks.json --out .qbgen
$PY $SKILL_DIR/scripts/merge.py --clean .qbgen/clean.json --bank question-bank/mysql/json \
     --docs-root question-bank --tasks .qbgen/tasks.json
$PY $SKILL_DIR/scripts/report.py --bank question-bank/mysql/json --tasks .qbgen/tasks.json \
     --validation .qbgen/validation.json --skipped .qbgen/skipped.json

# 6. 打开 question-bank/mysql/json/preview.html 人工复核
```

⚠️ 第 3b 步跑完**必须重跑 `validate.py`**（重建 `clean.json`）再 `merge.py`。

### 大知识库：分批派子代理并行出题

第 2 步是唯一耗时的模型环节，脚本刻意不并行，但模型层面可以：

```bash
$PY $SKILL_DIR/scripts/split_batches.py --tasks .qbgen/tasks.json --out .qbgen/batches --batches 6

# 增量场景：只切「本次新增的切片」
$PY $SKILL_DIR/scripts/split_batches.py --tasks .qbgen/tasks.json --out .qbgen/batches \
  --only-new .qbgen/incremental.json
```

实测 53 片分 6 批，出题从串行的几十分钟压到几分钟。派给每个子代理的任务书要求与禁止事项见 `SKILL.md`。

---

## 导入契约

输出契约对齐 `quizzy-server` 的 `POST /api/questions/import/json`：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | `SINGLE` / `MULTI` / `JUDGE` |
| `stem` | string | 是 | 题干 |
| `options` | array | 是 | `[{label, content}]`，2–6 个，label 为 A–F |
| `answer` | array | 是 | 正确选项 label 列表，如 `["A","C"]` |
| `analysis` | string | 否 | 解析，建议必填 |
| `difficulty` | string | 否 | `EASY` / `MEDIUM` / `HARD` |
| `score` | int | 否 | 1–100 |
| `category` | string | 否 | 分类名 |
| `tags` | array | 否 | ≤10 个，**必须包含一个考频标签**（`高频`/`中频`/`低频`） |

请求体是**裸数组** `[ {...}, {...} ]`，不是 `{"questions":[...]}`。

完整字段细节、全部校验规则与已知陷阱见 [`references/question-contract.md`](references/question-contract.md)；
出题规范（什么样的题才算好题、干扰项怎么设计、难度怎么判）见 [`references/authoring.md`](references/authoring.md)。

**本工具只落盘，不自动写库。** 导入接口不幂等（逐条新建、无查重），`all.json` 只导一次，导入由使用者自己执行。

---

## 复用到别的项目

输出契约是 quizzy 专用的。要在别的系统复用，改的是：

- `scripts/qbcommon.py` 里的契约常量（题型 / 难度 / 选项 / 答案的字段定义与归一化）
- `scripts/validate.py` 的校验规则

**流程不用动**——切分、溯源校验、位置均衡、合并、复核这五步是通用的。

---

## 已知边界

- 只支持单选 / 多选 / 判断，不支持填空、问答、编程题。
- `source_quote` 校验是**字符串包含匹配**：模型把原文改写得稍微不一样会误判失败，这是**有意为之**（宁可误杀，不可放过）。比对前会剥掉 HTML 标签与实体，避免富文本导出（语雀 / 飞书）大面积误杀。
- **考频三档只在同一份素材内部可比，跨学科不可比**；`高频` 是唯一有行动含义的档位。
- **难度没有客观兜底**：由出题时判断，不做真实正确率回填。
- 题库 JSON 与数据库会不一致：导入后在 UI 里改的内容只存在于数据库，JSON 仍是「出厂值」，本工具不做双向同步。
- 切片是串行的，出题是并行的：脚本刻意不做并行（并发会让标签归一化与全局去重变复杂），并行发生在模型层面。

---

## 目录结构

```
.
├── SKILL.md                          # 技能说明书：完整流程、参数、踩坑记录
├── README.md
├── references/
│   ├── authoring.md                  # 出题规范
│   └── question-contract.md          # 导入契约与校验规则
└── scripts/
    ├── qbcommon.py                   # 契约常量与归一化（共用）
    ├── plan.py                       # ① 切分
    ├── split_batches.py              # 分批派子代理
    ├── validate.py                   # ③ 校验（防幻觉主防线）
    ├── balance_options.py            # ③b 位置均衡
    ├── merge.py                      # ④ 增量合并
    └── report.py                     # ⑤ 复核报告 + 预览页
```
