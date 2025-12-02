# PaperPilot

PaperPilot 提供一整套与 Zotero 集成的 AI 自动化工具链，用于追踪热门论文、批量导入/去重、补全摘要与 PDF、生成 AI 笔记，并同步到 Notion 等下游系统。

English version & updates: see [`README_EN.md`](README_EN.md).

## 代码文件速览

| 路径 | 作用简述 |
| --- | --- |
| `scripts/watch_and_import_papers.py` | 根据 `tag.json` 关键词 + HuggingFace Papers 趋势追踪新论文、打分筛选并写入 Zotero，可选填补重复条目的缺失字段。 |
| `scripts/fetch_missing_pdfs.py` | 扫描最近新增的 Zotero 条目，自动下载/关联 PDF（arXiv/Unpaywall/直链）以补全本地库。 |
| `scripts/merge_zotero_duplicates.py` | 扫描库内重复条目，保留最优版本并迁移附件/笔记后删除冗余。 |
| `scripts/summarize_zotero_with_doubao.py` | 读取 Zotero PDF，通过 AI 模型（豆包/Qwen/任何 OpenAI 兼容端点）生成摘要，支持写回 Notes。 |
| `scripts/enrich_zotero_abstracts.py` | 对缺少 `abstractNote` 的条目调用 CrossRef/Semantic Scholar/arXiv 补充摘要。 |
| `scripts/list_zotero_collections.py` | 枚举 Zotero Collection 及其树结构，可选列出子项。 |
| `scripts/import_ris_folder.py` | 遍历文件夹中所有 RIS 文件并批量导入 Zotero。 |
| `scripts/export_zotero_pdfs_to_gdrive.py` | 按照 Zotero Collection 树，将条目的 PDF 上传到 Google Drive 并复刻层级。 |
| `scripts/import_embodied_ai_to_zotero.py` | 解析 Embodied_AI_Paper_List README，生成 RIS 或直接创建条目。 |
| `scripts/awesome_vla_to_ris.py` | 解析 Awesome-VLA README，按分类生成 RIS/调用 API。 |
| `scripts/delete_collection_notes.py` | 清理指定集合下的 Notes。 |
| `scripts/ai_toolbox_pipeline.sh` | Bash 版一键流水线，串联去重/摘要/补全/监控/同步等阶段。 |
| `scripts/langchain_pipeline.py` | Python & LangChain 版本的自动化入口，可与 Agentflow 集成。 |
| `scripts/sync_zotero_to_notion.py` | 将 Zotero 条目映射到 Notion，支持 AI 严格抽取（豆包/Qwen/OpenAI 兼容）。 |
| `paperflow/config.py` | LangChain 流水线配置数据类，集中管理各阶段参数。 |
| `paperflow/stages.py` | 具体的子流程实现，负责编排并调用 scripts 下的 CLI。 |
| `paperflow/pipeline.py` | 构建/运行 LangChain Runnable 链，输出 `PipelineState`。 |
| `paperflow/state.py` | 定义流水线的阶段执行结果与摘要结构。 |
| `utils_sources.py` | watch/import 与摘要脚本共用的外部数据抓取与工具函数。 |
| `tag.json` | 标签体系定义（label/description/关键词），用于自动打标签和 collection 映射。 |
| `tag_schema.json` | Notion 数据库属性示意，用于同步脚本的字段映射。 |
| `requirements.txt` | 运行所需 Python 依赖列表。 |
| `.env.example` | 环境变量示例文件，复制为 `.env` 后填入密钥。 |

> 其他 Markdown/日志/报告文件用于记录运行结果或导出的数据。

## 环境准备

```bash
# 1) 建议使用虚拟环境
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 2) 安装依赖（markdown 为可选但推荐，用于本地渲染 Markdown → HTML）
pip install requests pypdf openai markdown google-api-python-client

# 3) 环境变量：复制 `.env.example` 为 `.env` 并填入密钥（Python 脚本会自动加载，无需 source）
cp -n .env.example .env 2>/dev/null || true
# 需要在 shell 中直接使用变量时，可选择：
# set -a; source .env; set +a
```

快速自测（可选）：

```bash
# 测试豆包 API（需 ARK_API_KEY）
python - <<'PY'
import os
from openai import OpenAI
client = OpenAI(base_url="https://ark.cn-beijing.volces.com/api/v3/bots", api_key=os.environ['ARK_API_KEY'])
resp = client.chat.completions.create(model=os.environ.get('ARK_BOT_MODEL','bot-20251111104927-mf7bx'), messages=[{"role":"user","content":"你好"}])
print(resp.choices[0].message.content)
PY

# 测试 Zotero API（需 ZOTERO_*）
python - <<'PY'
import os,requests
base=f"https://api.zotero.org/users/{os.environ['ZOTERO_USER_ID']}";
r=requests.get(f"{base}/items",headers={"Zotero-API-Key":os.environ['ZOTERO_API_KEY']},params={"limit":1});
r.raise_for_status(); print("Zotero OK")
PY
```

## 快速上手（推荐流程）

按下面顺序跑一遍，可快速体验整套流程（建议先在小范围试跑）：

1) 查看库结构（确认 Collection 名称）
- `python scripts/list_zotero_collections.py --items 0`

2) 从外部清单/README 生成 RIS 并导入（可选）
- 生成 RIS：`python scripts/awesome_vla_to_ris.py --out ./awesome_vla_ris`
- 批量导入（按文件夹）：`python scripts/import_ris_folder.py --dir ./awesome_vla_ris --dedupe-by-url`

3) 去重合并（以带 PDF/Notes、最近修改为优先）
- 预览：`python scripts/merge_zotero_duplicates.py --dry-run`
- 指定集合：`python scripts/merge_zotero_duplicates.py --collection-name "Embodied AI" --limit 200`

4) 生成并写回 AI 摘要（Notes）
- 小范围试跑：
  `python scripts/summarize_zotero_with_doubao.py --limit 20 --max-pages 80 --summary-dir ./summaries --insert-note`
- 全库：
  `python scripts/summarize_zotero_with_doubao.py --limit 0 --max-pages 100 --summary-dir ./summaries --insert-note`
- AI 提供者可通过 `--ai-provider qwen --ai-model qwen3-max` 或上述环境变量来自定义（默认豆包）。

5) 补全缺失摘要（abstractNote）
- 全库：`python scripts/enrich_zotero_abstracts.py`
- 指定集合：`python scripts/enrich_zotero_abstracts.py --collection-name "Embodied AI" --limit 100`

6) 追踪新论文并自动入库（按 tag.json 打分筛选，默认仅关注最近 24 小时）
- 预览：
  `python scripts/watch_and_import_papers.py --tags ./tag.json --since-hours 24 --top-k 10 --min-score 0.3 --create-collections --dry-run`
- 生成日志与报告：
  `python scripts/watch_and_import_papers.py --tags ./tag.json --since-hours 24 --top-k 10 --min-score 0.3 --create-collections --log-file logs/run.log --report-json reports/run.json`
- 补救已有条目：
  `--fill-missing` 会在命中重复时，把缺失的摘要/DOI/URL/年份补写回 Zotero，并把条目加入当前标签对应的 Collection + 标签。
- HuggingFace Papers 趋势：默认会抓取每日/每周/每月的热门论文用于辅助打分，可通过 `--hf-*-limit` / `--hf-weight`（默认 0.3）调整，或用 `--no-hf-papers` 关闭。

7) 自动补全 PDF（默认最近 24 小时）
- 基于 watch 输出执行：`python scripts/fetch_missing_pdfs.py --since-hours 24 --new-items-json .data/new_items_watch.json`
- 或直接按时间窗口扫描：`python scripts/fetch_missing_pdfs.py --since-hours 12 --limit 50`

8) 同步到 Notion（可选，支持 AI 严格抽取补全）
- 预览：
  `python scripts/sync_zotero_to_notion.py --since-days 30 --limit 200 --tag-file ./tag.json --skip-untitled --dry-run`
- 指定集合并递归子集合、启用豆包抽取：
  `python scripts/sync_zotero_to_notion.py --collection-name "Embodied AI" --recursive --limit 500 --tag-file ./tag.json --skip-untitled --enrich-with-doubao`
  - 如需改用 Qwen/其它模型，可配合 `--ai-provider qwen --ai-model qwen3-max` 或相应环境变量。

提示：以上命令依赖 `.env` 中的配置（Python 会自动加载），建议先用较小的 `--limit` 或 `--dry-run` 试跑。

## 命令速查（Cheat Sheet）

- 列出集合树：`python scripts/list_zotero_collections.py --items 0`
- 去重合并：`python scripts/merge_zotero_duplicates.py --dry-run`
- 生成 AI 摘要（全库）：`python scripts/summarize_zotero_with_doubao.py --limit 0 --insert-note`
- 补全缺失摘要：`python scripts/enrich_zotero_abstracts.py --limit 200`
- 监控导入（按标签体系）：`python scripts/watch_and_import_papers.py --tags ./tag.json --since-days 14 --top-k 10 --min-score 0.3 --create-collections`
- 同步到 Notion（递归集合）：`python scripts/sync_zotero_to_notion.py --collection-name "Embodied AI" --recursive --skip-untitled`

## 一键流水线（ai_toolbox_pipeline.sh）

将常用步骤串联为“所见即所得”的流水线脚本，支持按需选择阶段、集合与 dry-run：

```bash
# 查看帮助
scripts/ai_toolbox_pipeline.sh --help

# 全流程（集合及其子集合），限 200 条
scripts/ai_toolbox_pipeline.sh \
  --all \
  --collection-name "Embodied AI" \
  --recursive \
  --limit 200

# 只预览“监控导入 + Notion 同步”，不写入
scripts/ai_toolbox_pipeline.sh --watch-import --notion-sync --dry-run

# 常用参数：
# --dedupe / --summarize / --enrich-abstracts / --watch-import / --notion-sync / --all
# --collection-name NAME  --recursive  --limit N  --dry-run
# --summary-max-pages N   --summary-max-chars N   --summary-dir DIR
# --watch-since-days N    --watch-top-k N        --watch-min-score F
# --notion-skip-untitled  --notion-doubao
```

## LangChain 自动化流程（Python 版）

如果希望在 Python 内部以 LangChain 可组合的方式运行整条流水线，可使用新的 `scripts/langchain_pipeline.py`：

```bash
python scripts/langchain_pipeline.py \
  --collection-name "Embodied AI" \
  --watch-since-days 14 \
  --watch-top-k 15 \
  --summary-limit 150 \
  --abstract-limit 400 \
  --notion-limit 500 \
  --state-json logs/langchain_pipeline_state.json
```

- 流程阶段：追踪导入 → 去重 → AI 摘要 → 补全摘要 → Notion 同步；使用 `--skip-*` 可任意跳过某个阶段。
- 默认仅处理最近 24 小时新增/更新的数据，可通过 `--watch-since-hours`、`--pdf-since-hours`、`--summary-modified-since-hours` 等参数覆盖。
- `--tag-file` 共享 `tag.json` 体系用于打分筛选和 Notion 标签映射；`--collection-name/--collection-key` 会自动作用于除 watch 以外的阶段。
- LangChain 会串联每个 stage 的 `RunnableLambda`，执行完成后会把日志/报告路径写入 JSON（通过 `--state-json` 导出），方便与其他 LangChain/Agentflow 继续衔接。
- 该脚本内部直接调用各 Python 子脚本，保持与 CLI 版本完全一致的行为，仅提供自动化编排能力。
- 默认阶段与脚本、关键参数：
  1. **watch-import** → `scripts/watch_and_import_papers.py`（`--tags ./tag.json --since-hours 24 --top-k 10 --min-score 0.3 --create-collections` 等，同时继承 HF 配置）。
  2. **fetch-pdfs** → `scripts/fetch_missing_pdfs.py`（`--since-hours 24 --new-items-json .data/new_items_watch.json`）。
  3. **dedupe** → `scripts/merge_zotero_duplicates.py`（`--group-by auto --modified-since-hours 24`）。
  4. **summaries** → `scripts/summarize_zotero_with_doubao.py`（`--limit 200 --max-pages 80 --max-chars 80000 --recursive --insert-note --modified-since-hours 24`，可通过 `--ai-provider/--ai-api-key` 指定模型）。
  5. **abstracts** → `scripts/enrich_zotero_abstracts.py`（`--modified-since-hours 24`）。
  6. **notion-sync** → `scripts/sync_zotero_to_notion.py`（`--limit 500 --tag-file ./tag.json --recursive --skip-untitled --enrich-with-doubao --since-hours 24`）。
  - 所有阶段都遵循 `--skip-*`、`--*-since-hours`、`--collection-name` 等通用参数，pipeline CLI 的覆盖会直接传递给对应脚本。

## import_embodied_ai_to_zotero.py

从 HCPLab 的 Embodied_AI_Paper_List README 解析条目，输出 RIS 或直接写入 Zotero。

- 生成 RIS：
  ```bash
  python scripts/import_embodied_ai_to_zotero.py --mode ris --out ./zotero_import
  ```
- 直接写入 Zotero（需要 API 权限）：
  ```bash
  python scripts/import_embodied_ai_to_zotero.py --mode api --create-collections
  ```

## awesome_vla_to_ris.py

解析 Awesome-VLA README，按分类生成 RIS 文件或推送 Zotero。支持元数据增强（DBLP / arXiv）。

```bash
python scripts/awesome_vla_to_ris.py --out ./awesome_vla_ris

# 如需远程获取最新 README
python scripts/awesome_vla_to_ris.py --fetch --out ./awesome_vla_ris

# 可选：基于 README 中的 DBLP 注释 / arXiv 链接补全作者/年份/机构
python scripts/awesome_vla_to_ris.py --enrich-dblp --enrich-arxiv --out ./awesome_vla_ris
```

## summarize_zotero_with_doubao.py

读取 Zotero 中的条目或本地 PDF，调用豆包 API 生成 Markdown 总结，并写入 Zotero Notes（可选）。

常用场景：

1. **批量处理某个 Zotero Collection：**
  ```bash
  # .env 已填好后直接运行
  python scripts/summarize_zotero_with_doubao.py \
    --collection-name "Embodied AI" \
    --recursive \
    --limit 0 \
    --max-pages 100 \
    --max-chars 100000 \
    --summary-dir ./summaries \
    --insert-note
  ```
   - `--collection-name`：根据名称解析 Collection（或用 `--collection` 直接给 key）。
   - `--limit`：限制父条目数量（0 表示不限）。
  - `--insert-note`：生成后写入 Zotero Notes（中文 Markdown，本地渲染优先）。

2. **对整个文献库运行（不指定 Collection/Tag）：**
   ```bash
   # .env 已填好后直接运行；以下为小范围试跑
   python scripts/summarize_zotero_with_doubao.py \
     --limit 50 \
     --max-pages 100 \
     --max-chars 100000 \
     --summary-dir ./summaries \
     --insert-note

   # 全量运行（limit=0 表示不限；大库请谨慎）
   python scripts/summarize_zotero_with_doubao.py \
     --limit 0 \
     --max-pages 100 \
     --max-chars 100000 \
     --summary-dir ./summaries \
     --insert-note
   ```

2. **直接处理本地 PDF：**
   ```bash
   python scripts/summarize_zotero_with_doubao.py \
     --pdf-path ~/Zotero/storage/TFID34RJ/*.pdf \
     --max-pages 50 \
     --max-chars 80000 \
     --summary-dir ./summaries \
     --insert-note
   ```
   - 若 PDF 来自 Zotero storage，脚本会自动找到对应条目并写入 Note。

选项速查：

- 选择范围：`--collection-name` / `--collection` / `--tag` / `--item-keys` / `--pdf-path` / `--storage-key`
- 递归集合：`--recursive`（当集合下只有子集合时很有用）
- 控制规模：`--limit`（0=不设上限）、`--max-pages`（读取 PDF 页数）、`--max-chars`（传给模型的字符上限）
- 时间窗口：`--modified-since-hours`（默认 24，0 或 None 表示不过滤）
- 输出控制：`--summary-dir`（本地保存）、`--insert-note`（写回 Zotero）、`--note-tag`（给 Note 打标签）、`--force`（忽略已有“AI总结/豆包自动总结”笔记，强制重写）
- AI 选择：`--ai-provider`（doubao/qwen/dashscope/openai/...）、`--ai-base-url`、`--ai-api-key`、`--ai-model`/`--model`（若不指定则读取 `AI_PROVIDER`、`ARK_API_KEY`、`AI_API_KEY` 等环境变量）
- 环境/路径：`--storage-dir`（Zotero storage 路径，默认 `~/Zotero/storage`）

显示优化：

- 笔记内容为中文 Markdown，并在本地优先渲染为 HTML，渲染失败再回退为 data-markdown。
- 若需更好的表格/代码块渲染，建议安装 `markdown` 包（已在上方依赖列出）。

断点续跑 / 去重策略：

- 当 `--insert-note` 启用时，脚本会自动跳过已存在“AI总结”或历史“豆包自动总结”的条目（或带有 `--note-tag` 标签的笔记），避免重复生成；如需覆盖更新，请加 `--force`。

## delete_collection_notes.py

删除指定 Collection 中的所有 Notes（包含顶层 Note 与附属 Note）。谨慎使用，建议先 `--dry-run`。

```bash
# .env 已填好后直接运行
# 预览
python scripts/delete_collection_notes.py --collection-name "Surveys" --dry-run
# 真正删除
python scripts/delete_collection_notes.py --collection-name "Surveys"
```

可通过 `--collection` 指定 Collection Key，`--limit` 限制扫描条目数。

## merge_zotero_duplicates.py

扫描 Zotero 中的顶层条目，按“DOI → URL → 标题/年份”自动分组重复项，并基于“是否包含 PDF/附件、是否已有 Notes、最近修改时间”三重优先级选出保留对象。其余重复条目的附件与 Notes 会自动迁移到保留条目，集合（Collections）与标签也会合并，最后删除冗余条目。

常用示例：

```bash
# .env 已填好后直接运行
# 预览整个库中的重复情况（不执行修改）
python scripts/merge_zotero_duplicates.py --dry-run

# 只处理某个集合，限定 200 条以内
python scripts/merge_zotero_duplicates.py \
  --collection-name "Embodied AI" \
  --limit 200
```

选项提示：
- `--tag`：只处理含有该标签的顶层条目。
- `--group-by`：可切换分组策略（`auto`/`doi`/`url`/`title`），默认 auto。
- `--dry-run`：仅打印迁移/删除计划，便于确认。

## list_zotero_collections.py

输出 Zotero 文献库的 Collection 层级结构，可选展示每个 Collection 下的部分条目，便于快速了解库的组织方式。

```bash
# .env 已填好后直接运行
# 打印所有 Collection 的树状结构
python scripts/list_zotero_collections.py

# 仅查看某个集合及其子层级，并展示每层前 3 条文献
python scripts/list_zotero_collections.py \
  --root-name "Embodied AI" \
  --items 3

# 将全部层级输出保存到文件
python scripts/list_zotero_collections.py \
  --items 0 | tee collections_tree.txt

# 生成 Markdown，并写入 paper.md（含每层前 5 条文献及其 URL）
python scripts/list_zotero_collections.py \
  --items 5 \
  --format markdown \
  --no-ids \
  --output paper.md
```

常用选项：
- `--root` / `--root-name`：以某个集合为根节点输出（默认输出全部顶层）。
- `--items`：每个集合展示前 N 条顶层条目（0 表示不展示）。
- `--max-depth`：限制树的深度，避免输出过长。
- `--format markdown`：以 Markdown 输出（条目自动附带 URL，集合名称加粗）。
- `--no-ids`：隐藏集合/条目的 key，适合分享或文档。
- `--output FILE`：直接写入文件，便于保存（默认打印到终端）。
- 默认忽略回收站（Trash）中的集合与条目；如需包含，可加 `--include-deleted`。
- 不指定 `--root` 时会遍历整个库；可配合 shell 重定向（`>` 或 `tee`）保存结果。

## enrich_zotero_abstracts.py

批量检查 Zotero 条目是否缺失摘要（abstractNote），并按“条目 URL → CrossRef → Semantic Scholar → arXiv”的优先级自动补全，无法获取则跳过。

```bash
# .env 已填好后直接运行
# 扫描整个库，找出缺失摘要的条目并写回
python scripts/enrich_zotero_abstracts.py

# 仅处理某个 Collection / Tag，并先 dry-run 预览
python scripts/enrich_zotero_abstracts.py \
  --collection-name "Embodied AI" \
  --tag "Awesome-VLA" \
  --limit 100 \
  --dry-run
```

默认策略：
- 若条目已提供 URL，则先尝试：a) 直接解析 URL（支持 arXiv/DOI、常见 meta 标签）提取摘要；b) 从该 URL 推导的 DOI / arXiv ID。
- 若仍无摘要，再根据条目的 DOI 请求 CrossRef；无结果时按 DOI/arXiv ID 查询 Semantic Scholar，最后直接调用 arXiv API。
- 只处理顶层条目（忽略 notes/attachments），已有摘要的条目会跳过。
- `--dry-run`：仅显示计划写入的条目与来源，不修改 Zotero。

---

如需自定义功能，可参考以上脚本结构扩展。运行前确保网络可访问 GitHub、Zotero API 以及豆包 API。

## watch_and_import_papers.py

基于 `tag.json` 的关键词体系，自动检索近期高影响力论文、打分筛选、去重后写入 Zotero，并按标签放入对应 Collection，自动附上 PDF 链接与摘要（若可获取）。

# .env 已填好后直接运行

# 以 tag.json 为标签体系，检索近 14 天，按每个标签保留 Top-10
python scripts/watch_and_import_papers.py \
  --tags ./tag.json \
  --since-days 14 \
  --top-k 10 \
  --min-score 0.3 \
  --create-collections \
  --dry-run

# 真正写入，并输出日志与 JSON 报告
python scripts/watch_and_import_papers.py \
  --tags ./tag.json \
  --since-days 14 \
  --top-k 10 \
  --min-score 0.3 \
  --create-collections \
  --log-file logs/run.log \
  --report-json reports/run.json
```

特性：
- 数据源：arXiv（关键词），可选使用 Semantic Scholar 与 CrossRef 进行引用数与摘要补全（已内置降级策略）。
- 打分：默认 0.5×时效 + 0.35×引用数(归一化) + 0.15×重要引用(归一化)。
- 去重：优先使用 DOI → arXiv ID → 规范化 URL → 标题+年份；库内索引 + 本次运行去重。
- 写入：创建父条目（`journalArticle`），写入标题/作者/日期/DOI/URL/摘要/标签/集合，并附上 PDF 链接（arXiv 或 Unpaywall）。
- 日志：在 `logs/` 生成文本日志；在 `reports/` 生成 JSON 报告，包含候选/新增/跳过统计与错误信息。
- HuggingFace Trending：默认同时抓取每日/每周/每月排行榜（`https://huggingface.co/papers/date/YYYY-MM-DD` / `.../week/YYYY-Wxx` / `.../month/YYYY-MM`），按 `--hf-weight` 与相应 period 权重混入评分；`--hf-override-limit` 可强制保留每个标签前 N 条 HF 结果并在日志中标记 “HF-OVERRIDE”。

常用参数（与脚本保持一致）：
- `--tags ./tag.json`：标签定义文件，默认即 `tag.json`。
- `--since-hours 24`：时间窗口（小时），优先于 `--since-days`（后者仅为了兼容旧流程）。
- `--top-k` / `--min-score`：控制每个标签保留的候选数量与得分阈值。
- `--create-collections` / `--fill-missing` / `--dry-run`：分别对应自动建合集、回填已有条目缺失字段，以及仅预览。
- `--log-file` / `--report-json`：重定向运行日志与 JSON 报告；留空则以时间戳命名。
- `--no-hf-papers`：完全禁用 HuggingFace Trending 来源。
- `--hf-daily-limit` / `--hf-weekly-limit` / `--hf-monthly-limit`：每个周期抓取数量（默认 5 / 20 / 50）。
- `--hf-weight`：HF 影响力分值占比（默认 0.3），并可通过 `--hf-daily-weight` / `--hf-weekly-weight` / `--hf-monthly-weight` 对不同周期再加权（默认 1.0 / 1.1 / 1.2）。
- `--hf-override-limit`：每个标签保底纳入的 HF 条目数量（默认 2），即便打分低于 `--min-score` 也会被选中并在日志中注明 “HF override”。
- `--download-pdf`：为未来留的参数，目前仍以“链接”形式附加 PDF（下载逻辑集中在 `fetch_missing_pdfs.py`）。

提示：
- 建议设置 `UNPAYWALL_EMAIL` 以便通过 Unpaywall 获取开放获取 PDF 链接。
- 大库环境可多次运行；若只想预览变更，可加 `--dry-run`。

## fetch_missing_pdfs.py

主要目标是：在 watch / 近期新增的条目里找到缺失 PDF 的 Zotero 项，然后自动下载并挂载本地附件，确保后续摘要/Notion 流水线有可用的 PDF。

运行示例：

# .env 已填好后直接运行
# 优先使用 watch 结果（.data/new_items_watch.json），扫描过去 24 小时新增：
python scripts/fetch_missing_pdfs.py --since-hours 24 --new-items-json .data/new_items_watch.json

# 或直接按时间窗口遍历整个库：
python scripts/fetch_missing_pdfs.py --since-hours 12 --limit 50
```

策略精要：
- **候选来源**：优先读取 `.data/new_items_watch.json` 并按 `--since-hours` 过滤，若为空再回退遍历 `/items/top`（同样按时间过滤）；所有 key 合并去重后按 `--limit` 截断（`<=0` 表示不限）。
- **判断是否缺 PDF**：通过 `fetch_children` 拉取附件，只有 `itemType=="attachment"` 且 `linkMode` 属于 imported_file/linked_file/imported_url 并带 PDF MIME/后缀才视为“已有本地 PDF”；存在 `linked_url` 时会记录远程链接供日志参考。
- **下载策略**：`guess_pdf_sources()` 会优先选择 arXiv 直链（`https://arxiv.org/pdf/<id>.pdf`）、原始 URL 直链（以 `.pdf` 结尾）、以及配合 `UNPAYWALL_EMAIL` 调用 Unpaywall 获取开放获取 PDF。下载完成后落地到 `storage_dir/auto_pdfs/<key>/` 并通过 `create_linked_file()` 新建 `linked_file` 附件（标记 tag=`auto-pdf`）。
- **Dry run**：加 `--dry-run` 时仅打印尝试顺序 ` [TRY] key ← label: url`，不会触碰磁盘或 Zotero。
- **日志**：每个条目的处理结果都会输出 `[INFO] Item ... already has local PDF attachments` / `[OK] Linked local PDF for ...` 等提示，最终统计补全数量与剩余缺失数。

常用参数：
- `--since-hours 24`：候选时间窗口（小时）。
- `--limit`：最多处理的父条目数量（0 表示不设上限）。
- `--new-items-json .data/new_items_watch.json`：watch 阶段输出（可指定自定义路径或直接跳过该文件）。
- `--storage-dir ~/Zotero/storage`：自定义 Zotero storage 根目录。
- `--dry-run`：只打印，不下载/写入。

必备环境变量：`ZOTERO_USER_ID`, `ZOTERO_API_KEY`，推荐设置 `UNPAYWALL_EMAIL` 以提高命中率。

## export_zotero_pdfs_to_gdrive.py

按照 Zotero Collection 的树状结构，将条目的 PDF 附件同步到 Google Drive 指定目录下，自动创建对应的文件夹。适用于把分学科/专题的 PDF 备份到共享云盘，保持与 Zotero 一致的层级。

使用前准备：
- 在 Google Cloud Console 创建服务账号，生成 JSON Key（`service-account.json`）。
- 在目标 Google Drive 文件夹上点击“共享”，把服务账号的邮箱加入为编辑者，获取该文件夹的 `folderId`（链接 `https://drive.google.com/drive/folders/<ID>` 中的 `<ID>`）。
- `pip install google-api-python-client`（已写入 `requirements.txt`）。
- 配置 `GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account.json`（或在命令行用 `--credentials-file` 指定）。

示例：同步名为 “Embodied AI” 的集合（含全部子集合）到某个 Drive 文件夹，并仅预览：

```bash
# .env 已填好后直接运行
python scripts/export_zotero_pdfs_to_gdrive.py \
  --collection-name "Embodied AI" \
  --drive-root-folder 1AbCdEfGhIjKlmnOp \
  --dry-run
```

真实上传：

```bash
python scripts/export_zotero_pdfs_to_gdrive.py \
  --drive-root-folder 1AbCdEfGhIjKlmnOp \
  --credentials-file ./service-account.json \
  --limit 0 \
  --overwrite
```

脚本要点：
- 默认遍历所有顶层集合；可用 `--collection`（key）或 `--collection-name` 只导出某个子树，`--no-recursive` 可仅同步当前层。
- 会使用 `ZOTERO_STORAGE_DIR` 下的本地附件（`imported_file` / `linked_file` / `imported_url`）。若仅有 `linked_url`，会尝试下载到临时目录后再上传。
- Google Drive 端默认跳过同名文件，可通过 `--overwrite` 覆盖已有文件。
- `--limit` 控制每个集合下最多处理多少条目（0 表示不限）。
- 支持 `--dry-run` 观察将创建哪些文件夹/文件。

必备环境变量：`ZOTERO_USER_ID`, `ZOTERO_API_KEY`，另可通过 `GOOGLE_DRIVE_ROOT_FOLDER` / `GOOGLE_SERVICE_ACCOUNT_FILE` / `GOOGLE_APPLICATION_CREDENTIALS` 预设目标与凭据。

## sync_zotero_to_notion.py

将 Zotero 条目同步至 Notion 数据库（去重、自动标签、字段严格映射、可选豆包抽取补全）。

环境变量：
- `ZOTERO_USER_ID`, `ZOTERO_API_KEY`
- `NOTION_API_KEY`, `NOTION_DATABASE_ID`
- 可选 `UNPAYWALL_EMAIL`（通过 DOI 尝试补充开放 PDF 链接）
- 可选 `ARK_API_KEY` / `ARK_BOT_MODEL`（启用 `--enrich-with-doubao` 时使用）

常用示例：
# .env 已填好后直接运行
# 预览（最近 30 天，跳过无标题条目，不写入）
# 预览（最近 30 天，跳过无标题条目，不写入）
python scripts/sync_zotero_to_notion.py \
  --since-days 30 \
  --limit 200 \
  --tag-file ./tag.json \
  --skip-untitled \
  --dry-run

# 按集合及其子集合递归同步，并使用豆包严格抽取补全字段
python scripts/sync_zotero_to_notion.py \
  --collection-name "Embodied AI" \
  --recursive \
  --limit 500 \
  --tag-file ./tag.json \
  --skip-untitled \
  --enrich-with-doubao
```

可用参数：
- `--collection` / `--collection-name`：只同步该集合；可配合 `--recursive` 遍历其全部子集合。
- `--tag`：只同步包含该标签的条目。
- `--since-days`：仅处理最近 N 天修改的条目。
- `--limit`：最大条目数（<=0 表示不限）。
- `--tag-file`：自动标签的关键词来源（默认 `./tag.json`，合并到 Notion 的 Tags）。
- `--skip-untitled`：跳过无法生成标题（无 title/shortTitle/venue+year/url/doi）的条目。
- `--enrich-with-doubao`：启用豆包信息抽取（仅基于条目标题/摘要/AI 笔记，严格不编造）。
- `--doubao-max-chars`：传给豆包的最大字符数（默认 4000）。
- `--dry-run`：只打印与预览，不写入 Notion。
- `--debug`：打印 Notion payload 与错误响应，便于定位 400。

字段映射（按列名严格匹配，存在即写入）：
- 必填：`Paper Title`（title）
- 文本：`Abstract`、`AI Notes`、`Key Contributions`、`Limitations`、`My Notes`（rich_text）
- 枚举/多选：`Authors`（multi_select）、`Tags`（multi_select）、`Research Area`（multi_select）、`Model Type`（multi_select）、`Robot Platform`（multi_select）、`Status`（select/multi_select 皆可）
- 链接：`Project Page`、`Code`、`Video`（url）
- 其他：`Venue`（select/multi_select/rich_text 皆可）、`Year`（number/select/rich_text）、`DOI`（url/rich_text）、`Zotero Key`（rich_text）

豆包抽取（开启 `--enrich-with-doubao` 时）：
- 仅从“标题 + 摘要 + AI Notes”中严格抽取以下字段；文本中没有就留空：
  - `Key Contributions`（总结式文本）
  - `Limitations`（总结式文本）
  - `Robot Platform`（列表）
  - `Model Type`（列表）
  - `Research Area`（列表）
- 不读取 PDF；不会编造；结果会经过字符清洗（移除 surrogates/控制字符）避免 Notion 400。

去重策略：
- 优先用 `Zotero Key`（rich_text）去重；不存在则回退为同名 Title。

小贴士：
- 建议在 Notion 数据库中按以上列名创建对应属性；不存在的属性会被跳过，不会报错。
- `--recursive` 适合父集合只包含子集合、不含条目的场景。

## import_ris_folder.py

一键导入某文件夹（含子目录）下所有 `.ris` 文件至 Zotero（通过 Web API 创建条目）。默认每个 RIS 文件单独归入以“该文件名”命名的 Collection（不合并）；也支持将全部合并到同一个 Collection。

```bash
# .env 已填好后直接运行
python scripts/import_ris_folder.py \
  --dir ./awesome_vla_ris \
  --dedupe-by-url

# 合并到同一个集合：
python scripts/import_ris_folder.py \
  --dir ./zotero_import \
  --collection-name "Imported (RIS)" \
  --create-collection \
  --dedupe-by-url
```

说明：
- 该脚本解析基础 RIS 字段（TI/UR/AU/PY/KW），以 `webpage` 类型创建条目，并写入标题、URL、作者、日期与标签。
- 默认：每个 RIS 文件的记录会放到“同名 Collection”下（例如 `Embodied_AI_Embodied_Agent.ris` → `Embodied_AI_Embodied_Agent`）。
- 合并模式：使用 `--collection-name`（可配合 `--create-collection` 自动创建），或直接用 `--collection <key>`。
- `--dedupe-by-url` 可避免重复导入相同 URL 的条目。

## 常见问题（Troubleshooting）

- 报错 “Missing required environment variable …”
  - `.env` 未填或缺少必需变量。补齐 `ZOTERO_*`、`ARK_API_KEY`（及 Notion/Google Drive 等可选项），Python 会自动加载。

- 报错 “Failed to resolve api.zotero.org” 或 GitHub RAW 超时
  - 当前网络无法访问外网。摘要脚本可先用 `--pdf-path/--storage-key` 处理本地 PDF；导入脚本建议使用已下载的 README（或待网络恢复再运行）。

- 提示 “No Zotero items matched … nothing to process.”
  - 过滤条件没有匹配（标签/集合名拼写、大小写）。可去掉 `--tag` 测试，或用 `--collection-name` 指定集合。

- 提示 “No local PDF attachments for … children types: …”
  - 条目下没有本地 PDF。对 `imported_url` 已支持自动解析本地文件；若仍无，请在 Zotero 中将 PDF 保存为本地附件（或用 `--storage-key` 直接指向存储目录）。

- 豆包 400 InvalidParameter / request
  - 通常是请求格式或模型 ID；脚本已使用 Ark 的 `messages` 结构，若环境变量中模型不是 `bot-...` 将自动回退；也可用 `--model` 明确指定。

- Markdown 显示成一行或被转义
  - 已在脚本中做了反转义与本地渲染；若仍不理想，请安装 `markdown` 包，并重新生成笔记。

## 目录结构

- `import_embodied_ai_to_zotero.py`：Embodied_AI_Paper_List → RIS / Zotero API
- `awesome_vla_to_ris.py`：Awesome-VLA → RIS（可 DBLP / arXiv 增强）
- `summarize_zotero_with_doubao.py`：批量摘要（本地 PDF / Collection），写入 Notes（Markdown）
- `delete_collection_notes.py`：删除集合下的所有 Notes（支持 dry-run）
- `.env.example`：环境变量示例文件（复制为 `.env` 后填入），Python 会自动加载
- `awesome_vla_ris/`、`zotero_import/`、`summaries/`：示例输出目录

## 安全提示

- 删除脚本默认“真删”，强烈建议先加 `--dry-run` 预览。
- 批量写入 Notes 前可先设置 `--limit` 小范围试跑，确认格式与内容无误后再扩展到全量。
