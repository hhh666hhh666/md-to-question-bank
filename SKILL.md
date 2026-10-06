---
name: md-to-question-bank
description: 出题 / 生成题库 / 题目 JSON / 刷题——把 markdown、笔记、知识库、docs 文档批量转成 quizzy 可导入的题目。用户说「出题、生成题库、根据这份文档出题、把笔记变成题目、更新题库、导入题目、题库 JSON、quiz」时使用。五步流程（只有第 2 步由模型创作，其余都有脚本）：① plan.py 按章节切片，产出 tasks.json + plan.md，并给出「本次新增切片」的增量清单；② 逐片出题写 .qbgen/pieces/<id>.json（单选约 60% / 多选约 25% / 判断约 15%，每题必带原文原句 source_quote 与一个考频标签）；③ validate.py 校验并隔离不合格题（source_quote 须在原文中逐字命中）；④ balance_options.py 重排选项，消除「正确答案总在 A」的位置偏置；⑤ merge.py 增量合并出 all.json / .full.json，report.py 出 review.md 与可交互 preview.html。四道防线：原文溯源、脚本硬校验、位置均衡、人工复核页。另支持增量更新（加章节后只对新片出题）、标签受控词表、大知识库分批派子代理并行出题。
agent_created: true
---

# Markdown → 题库

把 markdown 知识库切成章节，逐节出题，产出**可直接喂给 quizzy** `POST /api/questions/import/json` 的裸数组 JSON，并附复核报告与可交互预览页。

**核心设计原则**：题库里一道答案错的题，比没有题更糟——它会被反复刷、把错误知识刻进脑子。所以这个 skill 的价值不在于"能出题"，而在于**四道防线**：原文溯源、脚本硬校验、位置均衡、人工复核页。生成环节少一步可以，防线一步都不能省。

## 全流程总览

**只有第 2 步是模型创作，其余四步都是脚本**。`.qbgen/` 是中间产物（可随时删了重跑），`question-bank/<学科>/json/` 才是交付物。

| # | 步骤 | 命令（`$SKILL_DIR` = 本 skill 的目录） | 产物 | 谁做 |
| --- | --- | --- | --- | --- |
| 1 | 切分 | `plan.py --docs question-bank --out .qbgen` | `tasks.json`、`plan.md`、`skipped.json`、`incremental.json` | 脚本 |
| 2 | 出题 | 无脚本，逐片写 `pieces/<id>.json` | 每片一个 JSON（题干 / 选项 / 答案 / 解析 / `source_quote`） | **模型** |
| 3 | 校验 | `validate.py --pieces .qbgen/pieces --tasks .qbgen/tasks.json` | `clean.json`、`disputed.json`、`validation.json` | 脚本 |
| 3b | 位置均衡 | `balance_options.py --pieces .qbgen/pieces` | 原地重排选项顺序与答案 label | 脚本 |
| 4 | 合并 | `merge.py --clean .qbgen/clean.json --bank question-bank/<学科>/json` | `all.json`、`by-category/`、`.full.json`、`.meta.json` | 脚本 |
| 5 | 复核 | `report.py --bank … --tasks … --validation … --skipped …` | `review.md`、`preview.html` | 脚本 |

数据流：`md/` →（1）`tasks.json` →（2）`pieces/` →（3）`clean.json` →（3b 重排选项）→（4）`json/all.json` →（5）人工复核 → **使用者自己导入数据库**（本 skill 只落盘，不自动写库）。

## 何时使用

**该用**（出现任一条即加载本 skill）：

- "把这批笔记生成题库"、"根据这份 md 出题"、"知识库更新了，同步一下题库"
- "把这份文档转成题目 JSON"、"批量导入题目"、"生成单选/多选/判断题"
- 已经存在 `question-bank/` 或 `.qbgen/` 目录，要继续出题/增量更新

**不该用**（避免误触发）：

- 只是问某道题怎么做、某道题选哪个 —— 这是聊天，不是出题
- 已经有现成 JSON 题库，只想导进数据库 —— 直接用 `POST /api/questions/import/json`，不需要本 skill
- 出题范围不是 markdown（比如直接让我手写几十道题）—— 直接写就行，本 skill 的价值在"从文档抽题 + 溯源校验"

## 目录约定（quizzy 仓库）

**`question-bank/` 是"学科工作区"根**，一个学科一个目录：

| 路径 | 作用 |
| --- | --- |
| `question-bank/<学科>/md/` | **知识库源文件**（切分只扫这里）。子目录名 = 分类（`category`），文件名 = 主标签 |
| `question-bank/<学科>/json/` | **产物**：`all.json`（直接导入）、`by-category/`、`.full.json`、`.meta.json`、`preview.html`、`review.md` |
| `question-bank/<学科>/tags.md` | **标签受控词表**（可选但强烈建议）：`plan.py` 只从这张表里挑标签，见下文专节 |
| `.qbgen/` | 中间产物（`tasks.json` / `pieces/` / `batches/` / `clean.json` / `disputed.json`），可随时删了重跑。**已 gitignore** |

出题时 `--docs` 指向**工作区根**（不是 `md/`）：`--docs question-bank` → 各文件的 `category` 取学科目录名。
指向单个文件也行，但那样分类会退化成一级标题，必要时用 `--category` 强制指定。

⚠️ `plan.py` 扫 `.md` 时会**排除产物目录 `json/` 与词表文件 `tags.md`**——它们也是 `.md`，不排除的话「题库复核报告」和 77 行标签会被当成知识库去出题（两处都实测踩过）。

frontmatter 可覆盖推断（两种写法都支持）：

```markdown
---
category: MySQL
tags: [索引, 回表]
---

# 为什么索引会失效
```

## 流程（五步，第 3 步后加一次位置均衡）

本 skill 装在全局（`~/.workbuddy/skills/`），可在任意项目使用；产物落在**当前项目**的工作目录下。
加载本 skill 时宿主会告知它的 Base directory，先设好这两个变量再执行下面的命令：

```bash
SKILL_DIR="<本 skill 的 Base directory>"   # 加载 skill 时宿主会告知
PY="python3"                                # 纯标准库，任何 python 3.10+ 均可（Windows 下写 py -3 或 python）
```

### 1. 切分（脚本）

```bash
$PY $SKILL_DIR/scripts/plan.py --docs question-bank --out .qbgen --with-images
```

切分规则：按 H2 切片（无 H2 时 H1 也会切），单节超 2000 字再按段落二次切（不切开代码块/表格）；**丢掉 `<details>` 折叠块**（语雀导出的"格式化后的重复答案"，既撞字数又会产生重复题）；空节（去掉标题/链接/星号后没有正文）与低信息密度节跳过并记因。

常用参数：`--split-level 3`、`--max-chars 3000`、`--min-chars 120`、`--mode qa|prose`（默认 auto）、`--with-images`、`--category <名>`、`--max-per-section 5`。

跑完先看 `.qbgen/plan.md`：切片数、**扫描到的源文件**、形态分布（qa/prose）、丢弃的 details 块、图片数、**本次增量**、被跳过的章节与原因。

几条容易踩的：

- **增量**：每次运行都会跟上次的 `tasks.json` 比 id，把「新增 / 消失 / 不变」写进 `plan.md` 与 `incremental.json`。给知识库加了一节后**只对新增片出题**即可（见「大知识库」一节）；别全量重出——重出老章节会改写题干，merge 既可能替换、也可能新增，把已入库的题搅浑。
- **切片 id 的稳定性**：id = `文件名__章节标题`（同一节被二次切片时再加 `__p01` / `__p02`）。**改章节标题 = 换 id**，那一节会被当成全新片，而老题还留在库里 → 重复题。
- **二次切片也带标题**：`__p02` 及之后的片会带上「（续）」标题，避免模型拿到半截正文判错重点。
- **`--min-chars` 对 qa 形态放宽**：问答式的节常常「一句话就是完整答案」，所以 `mode=qa` 时阈值自动降到 60%（不低于 30 字），跳过原因里会写明「问答式节已放宽」。
- **受控词表**：若存在 `question-bank/<学科>/tags.md`，各片的 `tags` 就只从该表挑（见下文专节）；没有则退回自动推断，`plan.md` 会提示。

### 2. 出题（这一步由模型做，是全流程唯一的创作环节）

逐个读 `.qbgen/tasks.json` 里的 task，**每个 task 写一个文件** `.qbgen/pieces/<task_id>.json`：

```json
{
  "task_id": "MySQL__ACID是怎么保证的",
  "questions": [
    {
      "type": "SINGLE",
      "stem": "MySQL 的隔离级别为「可重复读」时，是靠什么来避免幻读的？",
      "options": [
        { "label": "A", "content": "..." }, { "label": "B", "content": "..." },
        { "label": "C", "content": "..." }, { "label": "D", "content": "..." }
      ],
      "answer": ["B"],
      "analysis": "……为什么其他选项错",
      "difficulty": "MEDIUM",
      "score": 2,
      "category": "mysql",
      "tags": ["MVCC", "幻读", "高频"],
      "source_quote": "在可重复读隔离级别下，MySQL 通过 MVCC + 间隙锁来避免幻读"
    }
  ]
}
```

硬性要求（**违反会被校验器打回**）：

1. **`source_quote` 必须是原文里的原句**（≤200 字，逐字照抄，不要改写、不要拼接、不要翻译）。这是防幻觉主防线：校验脚本会剥掉 HTML 后回原文做字符串比对，找不到就判失败。找不到支撑句 = 这道题不该出，宁可少出。
2. **按知识点出题，不按配额**：一节里有几个可考点就出几题（1 到 5 都可能），只有一个点就只出 1 题。**不要为了凑数编题**——单节超过 5 题会在复核报告里被标出来。
3. 题型配比是**参考值**（单选 ~60% / 多选 ~25% / 判断 ~15%），不是配额：优先出该知识点最自然的题型，只在明显偏斜时（比如多选超过一半）才往单选拉。
4. 难度按认知层次自己判：记忆（是什么/定义）=EASY，理解（对比/为什么）=MEDIUM，综合（场景判断/多因素/坑）=HARD。分值跟难度：1 / 2 / 3。前一遍判定的难度就是最终值，不会有人回填。
5. **每题必带一个考频标签**：`高频` / `中频` / `低频`，三选一。判据要锚定原文信号（被单列成标题、被加粗强调、跨节复现），**不许写"据我所知这是面试高频"**；高频题总数不得超过 30%，超了会被警告。
6. 干扰项要"像真的"：用同类概念的常见误解、边界条件、易混术语；不要用明显荒谬、或换个说法的正确答案。
7. 判断题选项固定写 `正确` / `错误` 两条。
8. `category` 和内容标签直接沿用 task 里推断好的值，不要自己发明新分类。

完整出题规范见 `$SKILL_DIR/references/authoring.md`。

**task 里的 `mode`**：`qa` = 这一节是问答形态（标题即题干骨架，优先用它当题干）；`prose` = 散文，题干自己写。

**含图节**：`--with-images` 会把图片地址写进 task 的 `images`。下载后用 Read 看一眼可以帮你判断"配图和配文是否一致"，**但答案仍必须由文字支撑**——图不能顶替 `source_quote`。

### 3. 校验（脚本）

```bash
$PY $SKILL_DIR/scripts/validate.py \
  --pieces .qbgen/pieces --tasks .qbgen/tasks.json --out .qbgen
```

产出 `clean.json`（通过）、`disputed.json`（失败，已隔离）、`validation.json`（明细 + 考频分布 + 单节题数）。
**失败题不要手工改完塞回去**，回到第 2 步针对这些 task 重新出题——手工改等于绕过防线。

常见失败原因：`source_quote` 在原文找不到（编造答案）、缺考频标签或写了两个、多选只有 1 个正确答案、答案 label 不在选项里、选项重复或少于 2 个。

### 3b. 位置均衡（脚本，强烈建议）

```bash
$PY $SKILL_DIR/scripts/balance_options.py --pieces .qbgen/pieces
```

**为什么必须有这一步**：模型出题时会不自觉地把正确答案放在 A。实测一份 189 题的 MySQL 题库，单选 125 题里 **122 题答案是 A**（97.6%），多选 30 题里 21 题答案是 `A,B,C` —— 这种库导进去等于「闭眼选 A 就能过」，区分度归零。`validate.py` 会对「单选答案位置占比 > 50%」给全局告警，本脚本负责修：单选把正确项轮转分配到最少使用的位置，多选挑答案组合最分散的排列。**只改选项顺序与 label，不动任何文字**，所以 `source_quote` 不受影响；判断题（正确/错误是固定语义）与选项带序号前缀的题跳过。

⚠️ 跑完必须**重跑 validate.py**（重建 `clean.json`）再 merge。早先还必须先删掉 `--bank` 下的 `.full.json` / `.meta.json`——那时只看题干指纹，重排选项不换指纹，整库会被判成"已存在"跳过；**现在不必了**，merge 会比对内容指纹，选项变化能被识别为修订（想全量重建时仍然可以删）。

### 4. 合并（脚本，幂等）

```bash
$PY $SKILL_DIR/scripts/merge.py \
  --clean .qbgen/clean.json --bank question-bank/mysql/json \
  --docs-root question-bank --tasks .qbgen/tasks.json
```

判定有四种，**前两条配合才不会丢修订**：

- 指纹（题干 + 题型）命中、**且内容指纹也相同** → 已存在，跳过。内容指纹 = 选项 + 答案 + 解析 + 难度 / 分值 / 分类 / 标签；
- 指纹命中但**内容指纹不同** → 同一题被改过，替换旧题（保留首次加入时间）。⚠️ 早先只看题干指纹，导致「题干没变、只改了答案」的修订被**静默丢弃**（实测踩过）；
- 指纹未命中但相似度 ≥0.75（题干 50% + 选项内容重合 50%）→ 同样是修订，替换；
- 其余 → 新增。

另外两条：题库里标了 `"_locked": true` 的题**永不覆盖**（人工改过的题这么保护）；`--prune` 清理旧题的判据是「源文件已删除」**或**「该章节已不再产出」——后者要加 `--tasks`，只给 `--docs-root` 时仍只看源文件在不在（那样把某一节删掉重出，文件还在、旧题就永远清不掉，只会不停新增）。

产物：`all.json`、`by-category/<分类>.json`（裸数组，导入用）、`.full.json`（含溯源，复核用）、`.meta.json`（指纹索引）。

### 5. 复核 + 导入

```bash
$PY $SKILL_DIR/scripts/report.py \
  --bank question-bank/mysql/json --tasks .qbgen/tasks.json \
  --validation .qbgen/validation.json --skipped .qbgen/skipped.json
```

打开 `preview.html`：按分类/题型/难度/考频筛，展开看答案与原文出处，勾选后导出。同时看 `review.md` 里的分布、覆盖率、单节题数与存疑清单。

导入（**默认只落盘，不自动写库**）：把 `all.json` 内容贴进前端导入框，或

```bash
curl -X POST http://localhost:8080/api/questions/import/json \
  -H "Content-Type: application/json" -H "Authorization: Bearer <token>" \
  --data-binary @question-bank/mysql/json/all.json
```

⚠️ **导入接口不幂等**（逐条新建、无查重），`all.json` **只导一次**；重复导入会建出重复题、而删题会硬删作答统计，所以**导入由使用者自己执行**，本 skill 只落盘。
导入之后如果觉得某道题的难度/考频不对，直接在题库列表页点"编辑"改——**手改过的题在 `.full.json` 里加 `"_locked": true`，重跑不会被覆盖**。

## 大知识库：分批派子代理出题

第 2 步是唯一耗时的模型环节。脚本刻意不做并行（标签归一化与全局去重要保持简单），但**模型层面可以并行**：把切片切成几批、每批交给一个子代理，各自写自己的 `pieces/`，再统一校验与合并。实测 53 片分 6 批，出题从串行的几十分钟压到几分钟。

```bash
$PY $SKILL_DIR/scripts/split_batches.py --tasks .qbgen/tasks.json --out .qbgen/batches --batches 6

# 增量场景：只切「本次新增的切片」
$PY $SKILL_DIR/scripts/split_batches.py --tasks .qbgen/tasks.json --out .qbgen/batches \
  --only-new .qbgen/incremental.json
```

它按 `tasks.json` 原有顺序、按字数均衡地切（保序，长切片不会全挤一批），并写一份 `batches/index.md` 列出每批的 task id，用来核对有没有漏做。

**派给每个子代理的任务书必须包含下面这些**——漏掉任何一条都会产出不合格的题：

1. **先读** `$SKILL_DIR/references/authoring.md`（出题规范全文）和自己的 `batches/b<i>.json`；
2. **一个 task 一个文件**：`.qbgen/pieces/<task 的 id>.json`；文件名与文件里的 `task_id` 都要与 `tasks.json` 的 `id` **逐字一致**（含中文、括号、`%`）；
3. **`source_quote` 逐字照抄 `task.text` 里的原句**（≤200 字，不许改写 / 拼接 / 压缩 / 补标点）——校验器做字符串包含比对，找不到就整题作废；
4. **按知识点出题、不按配额**：一片 1–5 题都可能，只有 1 个考点就只出 1 题，**绝不凑数**；单节 ≤5 题；
5. **标签从受控词表里挑**（下文专节），每题**恰好一个**考频标签，本批 `高频` 不超过 30%；
6. **正确答案的位置要轮转**，别总放 A（否则要靠第 3b 步事后救）；
7. **只允许**读上面两个文件、写 `pieces/*.json`——不得改仓库其它文件、不得执行 git 命令；
8. 回一条**简短**报告：写了哪些文件、各几题、题型 / 难度 / 考频分布、哪些切片只出了 1 题或 0 题及原因。**不要贴题目全文**——那会把主会话的上下文撑爆。

## 标签受控词表（`question-bank/<学科>/tags.md`）

标签在 quizzy 里是**全局共享**的，一次性标签会污染整个标签库。所以约定：

- 词表放 `question-bank/<学科>/tags.md`，一行一个标签（`- 标签`），与本学科的 `md/` 同级；
- `plan.py` 切分时**只从词表里挑**在标题或正文中出现过的标签（标题里的优先），所以 `tasks.json` 里的 `tags` 直接就是可用的干净标签；
- **没有词表时**退回自动推断（从文件名、整句标题、代码块语言名里抽），产出的是 `['MySQL', '为什么要小表驱动大表', 'sql', 'java']` 这类噪声（整句当标签、`java` 其实是代码块语言名）——`plan.md` 会给出提示；
- 新标签**先加进词表再用**；同一概念只留一种写法（`redo日志` 与 `redo log` 不能并存）；
- **考频档位（`高频` / `中频` / `低频`）不进词表**——它由出题环节按原文信号单独标，口径见 `references/authoring.md`。

## 约束速查（quizzy 导入契约）

| 项 | 规则 |
| --- | --- |
| 题型 | `SINGLE` / `MULTI` / `JUDGE`（v1 只有这三种，没有填空问答） |
| 选项 | 2–6 个，label A–F，内容不重复 |
| 答案 | 单选/判断恰好 1 个；多选 ≥2 个且≠全部选项 |
| 难度 | `EASY` / `MEDIUM` / `HARD`；分值 1–100 整数 |
| 考频 | **必标** `高频` / `中频` / `低频` 之一，占用标签位但不占内容标签配额 |
| 标签 | 内容标签 ≤5 个（建议），总数（内容+考频）≤10；分类/标签不存在时自动创建 |
| 解析 | 可为空，但强烈建议写——错题本靠它 |

字段细节与全部校验规则见 `$SKILL_DIR/references/question-contract.md`。

## 已知边界

- 只支持 quizzy 现有的单选/多选/判断，不支持填空、问答、编程题。
- 输出契约是 quizzy 专用的（`SINGLE/MULTI/JUDGE` + 裸数组）。要在别的项目复用，改的是 `scripts/qbcommon.py` 里的契约常量与 `scripts/validate.py` 的校验规则，流程不用动。
- `source_quote` 校验是字符串包含匹配：模型把原文改写得稍微不一样会误判失败，这是**有意为之**（宁可误杀，不可放过）。比对前会剥掉 HTML 标签与实体（`<font>`、`&gt;` 之类），否则语雀导出的富文本会大面积误杀；剥离只认「以字母或 `/` 开头」的真标签，所以正文里裸写的比较符（`>`、`<`、`>=`）**不会**被误吃——早先的宽正则会把「范围查询（>、< 停止）」这类句子整段吃掉，害得可引用的原文变少。
- **考频三档只在同一份素材内部可比，跨学科不可比**——"MySQL 的中频"和"Java 并发的中频"不是一把尺子量的（判据是我的先验，基准率随学科漂移）。`高频` 是这里面唯一有行动含义的档位。
- **难度没有客观兜底**：由模型出题时判断，不做真实正确率回填。组卷 `rule_json` 按难度抽题，所以难度标偏了卷子难度也会偏；觉得不对就在 UI 里手改，或回 `authoring.md` 调判据。
- **题库 JSON 与数据库会不一致**：导入后你在 UI 里改的内容只存在于数据库，JSON 仍是"出厂值"。本 skill 不做双向同步。
- **切片是串行的，出题是并行的**：`plan.py` / `validate.py` 都是单进程（几十个文件没问题）；耗时全在第 2 步，用 `split_batches.py` 分批派子代理即可（见上文专节）。
- 并发会让标签归一化与全局去重变复杂，所以**脚本**刻意不做并行——并行发生在模型层面。
- 这几处「静默出错」都曾被实测踩到，修完都留了正反向用例：`merge` 必须靠内容指纹才不丢修订；`plan.py` 必须排除 `json/` 与 `tags.md`；选项只有 1 个字符（`0` / `页`）是**合法**的（只警告不拦）；`report.py` 的覆盖率必须按 `_task_id` 而不是章节名（分片会算出假百分比）。
