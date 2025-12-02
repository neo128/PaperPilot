# PaperPilot (English)

PaperPilot is a suite of AI-powered helper scripts that integrate with Zotero for importing, parsing, summarizing, deduplicating, and syncing research papers (focused on Embodied AI / Robotics but generally useful).

## Setup

```bash
# 1) Virtual env (recommended)
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 2) Install deps (markdown is optional but improves local HTML rendering)
pip install requests pypdf openai markdown google-api-python-client

# 3) Environment: copy `.env.example` to `.env` and fill values
cp -n .env.example .env 2>/dev/null || true
# Python scripts auto-load `.env` (no need to source). If you need the vars in shell tools:
# set -a; source .env; set +a
```

Quick checks (optional):

```bash
# Doubao API (needs ARK_API_KEY)
python - <<'PY'
import os
from openai import OpenAI
client = OpenAI(base_url="https://ark.cn-beijing.volces.com/api/v3/bots", api_key=os.environ['ARK_API_KEY'])
resp = client.chat.completions.create(model=os.environ.get('ARK_BOT_MODEL','bot-20251111104927-mf7bx'), messages=[{"role":"user","content":"hello"}])
print(resp.choices[0].message.content)
PY

# Zotero API (needs ZOTERO_* env)
python - <<'PY'
import os,requests
base=f"https://api.zotero.org/users/{os.environ['ZOTERO_USER_ID']}";
r=requests.get(f"{base}/items",headers={"Zotero-API-Key":os.environ['ZOTERO_API_KEY']},params={"limit":1});
r.raise_for_status(); print("Zotero OK")
PY
```

## Quick Start (Recommended)

1) Inspect your collection tree (confirm names)
- `python scripts/list_zotero_collections.py --items 0`

2) Generate RIS and import (optional)
- `python scripts/awesome_vla_to_ris.py --out ./awesome_vla_ris`
- `python scripts/import_ris_folder.py --dir ./awesome_vla_ris --dedupe-by-url`

3) Merge duplicates (prefer items with PDF/Notes, then most recently modified)
- Preview: `python scripts/merge_zotero_duplicates.py --dry-run`
- By collection: `python scripts/merge_zotero_duplicates.py --collection-name "Embodied AI" --limit 200`

4) Summarize PDFs and write AI Notes (Doubao)
- Trial: `python scripts/summarize_zotero_with_doubao.py --limit 20 --max-pages 80 --summary-dir ./summaries --insert-note`
- Full library: `python scripts/summarize_zotero_with_doubao.py --limit 0 --max-pages 100 --summary-dir ./summaries --insert-note`

5) Fill missing abstracts (abstractNote)
- Library: `python scripts/enrich_zotero_abstracts.py`
- Collection: `python scripts/enrich_zotero_abstracts.py --collection-name "Embodied AI" --limit 100`

6) Track recent impactful papers and import (based on tag.json)
- Preview: `python scripts/watch_and_import_papers.py --tags ./tag.json --since-days 14 --top-k 10 --min-score 0.3 --create-collections --dry-run`
- With logs/reports: `python scripts/watch_and_import_papers.py --tags ./tag.json --since-days 14 --top-k 10 --min-score 0.3 --create-collections --log-file logs/run.log --report-json reports/run.json`

7) Sync to Notion (optional, with strict Doubao extraction)
- Preview: `python scripts/sync_zotero_to_notion.py --since-days 30 --limit 200 --tag-file ./tag.json --skip-untitled --dry-run`
- Collection + descendants + Doubao: `python scripts/sync_zotero_to_notion.py --collection-name "Embodied AI" --recursive --limit 500 --tag-file ./tag.json --skip-untitled --enrich-with-doubao`

Tip: Commands rely on variables in `.env` (Python auto-loads it). Start with small `--limit` or `--dry-run`.

## Cheat Sheet

- List collections: `python scripts/list_zotero_collections.py --items 0`
- Merge duplicates: `python scripts/merge_zotero_duplicates.py --dry-run`
- Summarize (full library): `python scripts/summarize_zotero_with_doubao.py --limit 0 --insert-note`
- Fill missing abstracts: `python scripts/enrich_zotero_abstracts.py --limit 200`
- Watch & import: `python scripts/watch_and_import_papers.py --tags ./tag.json --since-days 14 --top-k 10 --min-score 0.3 --create-collections`
- Notion sync (recursive): `python scripts/sync_zotero_to_notion.py --collection-name "Embodied AI" --recursive --skip-untitled`

## One-Click Pipeline

`scripts/ai_toolbox_pipeline.sh` chains the most common steps.

```bash
scripts/ai_toolbox_pipeline.sh --help

# Full run over a collection (with descendants), limit 200
scripts/ai_toolbox_pipeline.sh --all --collection-name "Embodied AI" --recursive --limit 200

# Preview only for watch-import + Notion
scripts/ai_toolbox_pipeline.sh --watch-import --notion-sync --dry-run
```

Key flags:
- Stages: `--dedupe` `--summarize` `--enrich-abstracts` `--watch-import` `--notion-sync` `--all`
- Scope: `--collection-name` `--recursive` `--limit` `--dry-run`
- Summaries: `--summary-max-pages` `--summary-max-chars` `--summary-dir`
- Watch/import: `--watch-since-days` `--watch-top-k` `--watch-min-score`
- Notion: `--notion-skip-untitled` `--notion-doubao`

## Scripts

### list_zotero_collections.py
- Prints collection tree. Flags: `--root/--root-name` `--items` `--max-depth` `--format markdown` `--no-ids` `--output` `--include-deleted`.

### merge_zotero_duplicates.py
- Groups duplicates by DOI → URL → title/year; keeps item with attachments/notes/PDF priority and latest modified; re-parents unique children; merges tags/collections; deletes redundant parents. Use `--dry-run` to preview.

### summarize_zotero_with_doubao.py
- Walks Zotero items, finds local PDFs, extracts text, calls an OpenAI-compatible API (Doubao by default, Qwen/DashScope/OpenAI supported) for a Markdown summary, and optionally inserts a child note. `--limit 0` is now the default (no cap).
- Scope selectors: `--collection-name/--collection`, `--tag`, `--item-keys`, `--pdf-path`, `--storage-key`, plus `--recursive` for nested collections.
- Time window & scale: `--modified-since-hours` (24h default), `--limit`, `--max-pages`, `--max-chars`.
- Output & behavior: `--summary-dir`, `--insert-note`, `--note-tag`, `--force`.
- AI config: `--ai-provider`, `--ai-base-url`, `--ai-api-key`, `--ai-model/--model`; otherwise falls back to `AI_PROVIDER`, `ARK_API_KEY`, `AI_API_KEY`, etc.
- Storage: `--storage-dir` overrides the default `~/Zotero/storage`.

### enrich_zotero_abstracts.py
- For items missing `abstractNote`, tries URL-first (meta/arXiv/DOI), then CrossRef, then Semantic Scholar, then arXiv. Top-level items only; `--dry-run` previews updates.

### watch_and_import_papers.py
- Uses `tag.json` keyword taxonomy. Fetches arXiv by keywords plus HuggingFace Papers trending lists (daily/weekly/monthly URLs such as `https://huggingface.co/papers/date/YYYY-MM-DD`). Scores each candidate (recency + citations + HF weight), dedupes by DOI/arXiv/URL/title+year, creates Zotero items (with tags/collections) and attaches OA PDF links (arXiv/Unpaywall). Emits text logs and JSON reports.
- Key arguments mirror the CLI defaults:
  - `--tags ./tag.json`, `--since-hours 24` (preferred over `--since-days`), `--top-k`, `--min-score`.
  - `--create-collections`, `--fill-missing`, `--dry-run`, `--log-file`, `--report-json`.
  - HuggingFace controls: `--no-hf-papers`, `--hf-daily/weekly/monthly-limit` (5/20/50 by default), `--hf-weight` (0.3) plus `--hf-daily/weekly/monthly-weight` (1.0/1.1/1.2), and `--hf-override-limit` (default 2) to force-include top HF matches per tag (logs show `HF-OVERRIDE`).
  - `--download-pdf` remains a placeholder; real downloading lives in `fetch_missing_pdfs.py`.

### fetch_missing_pdfs.py
- Ensures recently imported items have a local PDF attachment so downstream summaries/Notion sync work reliably.
- Candidate selection: prefers `.data/new_items_watch.json` (produced by the watch script) filtered by `--since-hours`; if empty, walks `/items/top` within the same window. Keys are deduplicated and capped by `--limit` (`<=0` = unlimited).
- PDF detection: fetches children and treats `attachment` items with `linkMode` imported_file / linked_file / imported_url and PDF MIME/suffix as “already has local PDF”. `linked_url` attachments are recorded only for reference.
- Download strategy: `guess_pdf_sources()` tries (in order) direct `.pdf` URLs, arXiv IDs derived from URL/extra (→ `https://arxiv.org/pdf/<id>.pdf`), then Unpaywall (requires `UNPAYWALL_EMAIL`). Successful downloads land in `storage_dir/auto_pdfs/<key>/` and are attached as `linked_file` (tag `auto-pdf`).
- Flags: `--since-hours`, `--limit`, `--new-items-json`, `--storage-dir`, `--dry-run`.
- Requires `ZOTERO_USER_ID`, `ZOTERO_API_KEY`; `UNPAYWALL_EMAIL` improves hit rate.

### export_zotero_pdfs_to_gdrive.py
- Mirrors the Zotero collection tree into Google Drive folders and uploads each item's PDF attachment(s). Perfect for mirroring curated topics into a shared Drive with the same hierarchy.
- Prereqs: create a Google Cloud service account, generate a JSON key, share the target Drive folder with the service account email (Editor access), and note the folder ID (`https://drive.google.com/drive/folders/<ID>`). Install `google-api-python-client` (already listed in `requirements.txt`).
- Environment: `ZOTERO_USER_ID`, `ZOTERO_API_KEY`, plus either `--credentials-file` or `GOOGLE_SERVICE_ACCOUNT_FILE` / `GOOGLE_APPLICATION_CREDENTIALS`. `--drive-root-folder` (or `GOOGLE_DRIVE_ROOT_FOLDER`) is required unless running `--dry-run`.
- Dry run example (preview folders/files):
  ```bash
  # With .env filled in, Python auto-loads it
  python scripts/export_zotero_pdfs_to_gdrive.py \
    --collection-name "Embodied AI" \
    --drive-root-folder 1AbCdEfGhIjKlmnOp \
    --dry-run
  ```
- Actual upload with overwrite:
  ```bash
  python scripts/export_zotero_pdfs_to_gdrive.py \
    --drive-root-folder 1AbCdEfGhIjKlmnOp \
    --credentials-file ./service-account.json \
    --limit 0 \
    --overwrite
  ```
- Behavior notes:
  - Defaults to all top-level collections; use `--collection` or `--collection-name` to export a subtree, and `--no-recursive` to stay on the current level only.
  - Reads local attachments from `ZOTERO_STORAGE_DIR` (imported_file / linked_file / imported_url). If only a `linked_url` exists, the script downloads it to a temp folder before uploading.
  - Skips files that already exist in the Drive folder unless `--overwrite` is set.
  - `--limit` caps the number of parent items per collection (0 = unlimited). `--dry-run` shows the intended plan without touching Drive.

### sync_zotero_to_notion.py
- Syncs Zotero items to a Notion database with strict column-name mapping and optional Doubao extraction. Key flags:
  - `--collection-name/--collection` (with `--recursive`), `--tag`, `--since-days`, `--limit`, `--tag-file`, `--skip-untitled`, `--dry-run`, `--debug`.
  - `--enrich-with-doubao` uses only title + abstract + AI Notes (strictly no fabrication) to fill: Key Contributions, Limitations, Robot Platform, Model Type, Research Area.
- Columns (write if present):
  - Required: `Paper Title` (title)
  - Text: `Abstract`, `AI Notes`, `Key Contributions`, `Limitations`, `My Notes`
  - Multi-select: `Authors`, `Tags`, `Research Area`, `Model Type`, `Robot Platform`
  - Links: `Project Page`, `Code`, `Video`
  - Others: `Venue` (select/multi_select/rich_text), `Year` (number/select/rich_text), `DOI` (url/rich_text), `Zotero Key` (rich_text)

### import_ris_folder.py
- Imports all `.ris` files in a folder (and subfolders) to Zotero. Default: each RIS file → its own collection; can merge into one collection via flags; `--dedupe-by-url` avoids duplicates.

## Troubleshooting
- Missing env var: fill `.env` (e.g., `ZOTERO_*`, `ARK_API_KEY`, Notion/Unpaywall/Drive). Python auto-loads `.env`; to expose vars in shell, run `set -a; source .env; set +a`.
- Network errors: use local PDFs or local README; retry when network recovers.
- No matching items: relax filters; verify collection names/tags.
- No local PDF: ensure the PDF is a stored or linked attachment.
- Doubao 400: check bot id; pass `--model` where supported or rely on fallback.
- Markdown rendering: install `markdown` package.

## Directory Structure
- `scripts/awesome_vla_to_ris.py` — build RIS from Awesome-VLA
- `scripts/import_embodied_ai_to_zotero.py` — import list to RIS/Zotero via API
- `scripts/summarize_zotero_with_doubao.py` — batch summaries → Notes (Markdown)
- `scripts/fetch_missing_pdfs.py` — auto-download/link PDFs for recent items
- `scripts/export_zotero_pdfs_to_gdrive.py` — mirror collections to Google Drive and upload PDFs
- `scripts/merge_zotero_duplicates.py` — merge duplicates safely
- `scripts/list_zotero_collections.py` — print collection tree (markdown/text)
- `scripts/enrich_zotero_abstracts.py` — fill missing abstracts
- `scripts/watch_and_import_papers.py` — watch/import with scoring/logging
- `scripts/sync_zotero_to_notion.py` — Zotero → Notion sync (strict mapping; Doubao extraction)
- `scripts/ai_toolbox_pipeline.sh` — one-click pipeline
- `.env.example` — env template (copy to `.env`, Python auto-loads)

## Safety
- Destructive actions (e.g., deleting notes) should be run with `--dry-run` first.
- For batch writes, start small (`--limit`) and inspect results before going all-in.
