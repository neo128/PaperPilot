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
     --collection-name "Surveys" \
     --limit 0 \
     --max-pages 50 \
     --max-chars 10000 \
     --summary-dir ./summaries \
     --insert-note
   ```
   - `--collection-name`：根据名称解析 Collection（或用 `--collection` 直接给 key）。
   - `--limit`：限制父条目数量（0 表示不限）。
   - `--insert-note`：生成后写入 Zotero Notes（中文 Markdown，本地渲染优先）。

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

---

如需自定义功能，可参考以上脚本结构扩展。运行前确保网络可访问 GitHub、Zotero API 以及豆包 API。

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
