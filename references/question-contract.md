# quizzy 题目导入契约与校验规则

对齐 `quizzy-server` 的 `QuestionImportDTO` + `QuestionImportService`（`POST /api/questions/import/json`）。

## 一、接口事实（易踩坑）

- 请求体是**裸数组** `[ {...}, {...} ]`，不是 `{"questions":[...]}`。
  `DESIGN.md` 里那种包装写法与实际接口不一致，以 `QuestionImportExportController` 为准。
- 响应 `ImportResultVO{total, successCount, failed:[{row, stem, reason}]}`：**逐条保存、跳过错误行**，不会整体回滚。
- ⚠️ **导入不幂等**：`importJson` 只是循环 `questionService.save(...)`，**没有任何按题干查重**，
  而 `question` 表也没有唯一键（`stem` 是 TEXT，只有普通索引）。**同一份 JSON 导两次 = 两套重复题。**
- 导入**不回传题目 id**：`failed` 里用 `stem` 标识问题行，项目自身就把题干当行的身份。
- 分类与标签不存在时**自动创建**，同名复用。
- 服务端 `parseType` 会先 `toUpperCase()`，兼容 `single/multi/judge`；中文别名（"单选"）实际上匹配不上，所以脚本统一输出大写英文枚举。

## 二、字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | `SINGLE` / `MULTI` / `JUDGE` |
| `stem` | string | 是 | 题干，Markdown |
| `options` | array | 是 | `[{label, content}]`，2–6 个，label 为 A–F |
| `answer` | array | 是 | 正确选项 label 列表，如 `["A","C"]` |
| `analysis` | string | 否 | 解析，Markdown，建议必填 |
| `difficulty` | string | 否 | `EASY` / `MEDIUM` / `HARD`，默认 `MEDIUM` |
| `score` | int | 否 | 1–100，默认 1 |
| `category` | string | 否 | 分类名，缺失归入"未分类" |
| `tags` | array | 否 | 标签，≤10 个。其中**必须包含一个考频标签**（`高频`/`中频`/`低频`） |

私有字段（`_task_id` / `_source_file` / `_source_section` / `source_quote` / `_fingerprint` / `_freq` / `_locked`）**只存在于 `.qbgen/` 和 `question-bank/<学科>/json/.full.json`**，写 `all.json` 与 `by-category/` 下的文件时会被剥离，避免污染导入请求。

## 三、校验规则（`validate.py` 实际执行）

### 硬失败（隔离到 `disputed.json`，不入库）

1. 题型无法识别、题干为空
2. 选项数 <2 或 >6；label 不在 A–F；label 重复；选项内容 <2 字
3. 选项内容去重后数量变少；某选项与题干完全相同
4. 答案为空；答案 label 未在选项中声明
5. `SINGLE` / `JUDGE` 答案不是恰好 1 个
6. `MULTI` 答案 <2 个；或答案 = 全部选项
7. `source_quote` 缺失、>200 字，或在原文（本切片 → 整文件）中找不到 ← 防幻觉主防线
8. 分值非整数或不在 1–100
9. **考频标签缺失**，或给了两个以上（必标三选一）
10. 标签总数（内容 + 考频）>10
11. 本次运行内题干+题型重复

### 警告（仍入库，但会在报告里列出）

- 缺 `analysis`；未标难度；未指定分类
- 判断题选项不是"正确/错误"表述（保留原文，需人工确认）
- **内容标签 >5 个**（考频标签不占这 5 个配额）；单选题只有 2 个选项
- 题干 >500 字；选项使用"以上都对/都不对"
- `source_quote` 不在本切片但在同文件其他位置找到
- **高频题占比 >30%**（批次级告警，防"一片高频"）
- **单节题数超过 `tasks.json` 里的 `max_per_section`**（默认 5；不是配额，只是把异常多的挑出来让你自己看）

### 自动归一化

- 题型别名（单选/判断题/single…）→ 大写枚举
- 难度别名（简单/中等/困难…）→ 大写枚举（难度值本身由模型判断，校验器不计算难度）
- 答案支持 `"A,C"` / `["a","c"]` / `"选项A"` → `["A","C"]`
- **判断题**：选项为 正确/错误（或对/错、True/False、是/否）时，统一改写为
  `A=正确 / B=错误`，并按语义重映射答案（原答案 A=错误 → 新答案 B）
- 缺分值 → 按难度补 1/2/3
- **`source_quote` 比对前会剥掉 HTML**（`<font>` 等标签、`<!-- -->` 注释、`&gt;` 等实体），
  否则语雀/飞书导出的富文本会让干净引文大面积误杀

## 四、增量与锁（`merge.py`）

- 指纹 = `sha1(norm(stem) + "|" + type)`，其中 `norm` 剥 HTML、去空白、去 markdown 标记、转小写
- 命中已有指纹 → 已存在，跳过
- 未命中时按 **题干相似度 × 50% + 选项内容 Jaccard × 50%** 匹配旧题，≥0.75 且题型相同 → 视为修订并替换（保留 `_added_at`）
- 旧题带 `"_locked": true` → 永不覆盖（人工改过的题在 `question-bank/.full.json` 里加这个字段）
- `--prune` 时按 `_source_file` 相对 `--docs-root` 检查，源文件已删除的题移除
