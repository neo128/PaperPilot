# Zotero-AI-Toolbox

本仓库包含若干与 Zotero 集成的辅助脚本，主要用于批量导入、解析以及总结 Embodied AI 相关文献（RIS 导入、AI 摘要写回 Notes、批量清理等）。

## 环境准备

```bash
# 1) 建议使用虚拟环境
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 2) 安装依赖（markdown 为可选但推荐，用于本地渲染 Markdown → HTML）
pip install requests pypdf openai markdown

# 3) 环境变量（推荐写入 exp 并 source）
export ZOTERO_USER_ID=你的用户ID          # 必需
export ZOTERO_API_KEY=你的APIKey          # 必需，具备写权限
export ARK_API_KEY=豆包APIKey             # 必需
export ZOTERO_STORAGE_DIR=~/Zotero/storage # 可选（默认路径如上）
export ARK_BOT_MODEL=bot-xxxxxxxxxxxxxxx  # 可选，未设置会自动回退

# 4) 每次运行前加载（首次将 exp.example 复制为 exp 并填好变量）
cp -n exp.example exp 2>/dev/null || true
source ./exp
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
  source ./exp
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
   source ./exp
   # 小范围试跑
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
- 输出控制：`--summary-dir`（本地保存）、`--insert-note`（写回 Zotero）、`--note-tag`（给 Note 打标签）、`--force`（忽略已有“AI总结/豆包自动总结”笔记，强制重写）
- 环境/路径：`--storage-dir`（Zotero storage 路径）、`--model`（豆包 bot id，未给会自动回退）

显示优化：

- 笔记内容为中文 Markdown，并在本地优先渲染为 HTML，渲染失败再回退为 data-markdown。
- 若需更好的表格/代码块渲染，建议安装 `markdown` 包（已在上方依赖列出）。

断点续跑 / 去重策略：

- 当 `--insert-note` 启用时，脚本会自动跳过已存在“AI总结”或历史“豆包自动总结”的条目（或带有 `--note-tag` 标签的笔记），避免重复生成；如需覆盖更新，请加 `--force`。

## delete_collection_notes.py

删除指定 Collection 中的所有 Notes（包含顶层 Note 与附属 Note）。谨慎使用，建议先 `--dry-run`。

```bash
source ./exp
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
source ./exp
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
source ./exp
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
source ./exp
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

```bash
source ./exp

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

提示：
- 建议设置 `UNPAYWALL_EMAIL` 以便通过 Unpaywall 获取开放获取 PDF 链接。
- 大库环境可多次运行；若只想预览变更，可加 `--dry-run`。

## sync_zotero_to_notion.py

将 Zotero 条目同步至 Notion 数据库（去重、自动标签、字段映射按数据库属性自动适配）。

环境变量：
- `ZOTERO_USER_ID`, `ZOTERO_API_KEY`
- `NOTION_API_KEY`, `NOTION_DATABASE_ID`
- 可选 `UNPAYWALL_EMAIL`（通过 DOI 尝试补充 PDF 链接）

示例：
```bash
source ./exp

# 同步最近 30 天修改的条目到 Notion（不写入，先预览）
python scripts/sync_zotero_to_notion.py \
  --since-days 30 \
  --limit 200 \
  --tag-file ./tag.json \
  --dry-run

# 按集合同步，并写入 Notion
python scripts/sync_zotero_to_notion.py \
  --collection-name "Embodied AI" \
  --limit 500 \
  --tag-file ./tag.json
```

说明：
- 自动识别 Notion 数据库中的属性（title/Authors/Year/Abstract/Tags/URL/DOI/Zotero Key/PDF），仅对存在的属性写入；建议在 Notion 数据库中创建这些属性。
- 去重优先使用 "Zotero Key"（rich_text），若不存在则按标题完全匹配；若需要更稳妥的去重，请在数据库添加 "Zotero Key" 属性。
- 标签来源于 `tag.json` 的 `sample_keywords`；支持多标签匹配并写入 Notion 的 multi_select。

## import_ris_folder.py

一键导入某文件夹（含子目录）下所有 `.ris` 文件至 Zotero（通过 Web API 创建条目）。默认每个 RIS 文件单独归入以“该文件名”命名的 Collection（不合并）；也支持将全部合并到同一个 Collection。

```bash
source ./exp
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
  - 未加载 `exp` 或缺少必需变量。执行 `source ./exp`，并检查 `ZOTERO_*` 与 `ARK_API_KEY`。

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
- `exp`：环境变量示例文件（执行前 `source ./exp`）
- `awesome_vla_ris/`、`zotero_import/`、`summaries/`：示例输出目录

## 安全提示

- 删除脚本默认“真删”，强烈建议先加 `--dry-run` 预览。
- 批量写入 Notes 前可先设置 `--limit` 小范围试跑，确认格式与内容无误后再扩展到全量。
