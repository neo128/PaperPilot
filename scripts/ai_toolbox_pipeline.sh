#!/usr/bin/env bash
set -euo pipefail

# PaperPilot one-click pipeline
# Stages: dedupe -> summarize -> enrich_abstracts -> watch_import -> notion_sync

usage() {
  cat <<'USAGE'
PaperPilot Pipeline
-------------------
Runs selected stages end-to-end. Make sure `.env` is filled (Python auto-loads it; shell tools can use `set -a; source .env; set +a`).

Usage:
  scripts/ai_toolbox_pipeline.sh [options]

Options:
  --all                      Run all stages
  --dedupe                   Run duplicate merge
  --summarize                Run AI summarization (writes notes unless --dry-run)
  --enrich-abstracts         Fill missing abstracts
  --watch-import             Watch recent papers via tag.json and import
  --notion-sync              Sync to Notion

  --collection-name NAME     Limit to a Zotero collection by name
  --recursive                When collection is set, include all sub-collections (where supported)
  --limit N                  Limit items per stage (0=unlimited; default varies per stage)
  --dry-run                  Dry-run stages that support it (watch-import, notion-sync). Summarize will skip --insert-note in dry-run.

  # Fine-tune knobs (optional)
  --summary-max-pages N      Default 100
  --summary-max-chars N      Default 100000
  --summary-dir DIR          Default ./summaries
  --watch-since-days N       Default 14
  --watch-top-k N            Default 10
  --watch-min-score F        Default 0.3
  --notion-skip-untitled     Skip items that cannot form a title in Notion stage
  --notion-doubao            Enable Doubao strict extraction in Notion stage

Examples:
  # Full run over a collection (with descendants)
  scripts/ai_toolbox_pipeline.sh --all --collection-name "Embodied AI" --recursive --limit 200

  # Preview-only for watch import + Notion
  scripts/ai_toolbox_pipeline.sh --watch-import --notion-sync --dry-run
USAGE
}

# Defaults
RUN_DEDUPE=0
RUN_SUMMARIZE=0
RUN_ENRICH=0
RUN_WATCH=0
RUN_NOTION=0
COLLECTION_NAME=""
RECURSIVE=0
LIMIT=0
DRY_RUN=0
SUMMARY_MAX_PAGES=100
SUMMARY_MAX_CHARS=100000
SUMMARY_DIR="./summaries"
WATCH_SINCE_DAYS=14
WATCH_TOP_K=10
WATCH_MIN_SCORE=0.3
NOTION_SKIP_UNTITLED=0
NOTION_DOUBAO=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all) RUN_DEDUPE=1; RUN_SUMMARIZE=1; RUN_ENRICH=1; RUN_WATCH=1; RUN_NOTION=1; shift;;
    --dedupe) RUN_DEDUPE=1; shift;;
    --summarize) RUN_SUMMARIZE=1; shift;;
    --enrich-abstracts) RUN_ENRICH=1; shift;;
    --watch-import) RUN_WATCH=1; shift;;
    --notion-sync) RUN_NOTION=1; shift;;
    --collection-name) COLLECTION_NAME="${2:-}"; shift 2;;
    --recursive) RECURSIVE=1; shift;;
    --limit) LIMIT="${2:-0}"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    --summary-max-pages) SUMMARY_MAX_PAGES="${2:-100}"; shift 2;;
    --summary-max-chars) SUMMARY_MAX_CHARS="${2:-100000}"; shift 2;;
    --summary-dir) SUMMARY_DIR="${2:-./summaries}"; shift 2;;
    --watch-since-days) WATCH_SINCE_DAYS="${2:-14}"; shift 2;;
    --watch-top-k) WATCH_TOP_K="${2:-10}"; shift 2;;
    --watch-min-score) WATCH_MIN_SCORE="${2:-0.3}"; shift 2;;
    --notion-skip-untitled) NOTION_SKIP_UNTITLED=1; shift;;
    --notion-doubao) NOTION_DOUBAO=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "[ERR] Unknown option: $1"; usage; exit 1;;
  esac
done

stage() { echo; echo "[STAGE] $1"; }

COLL_ARGS=()
if [[ -n "$COLLECTION_NAME" ]]; then
  COLL_ARGS+=(--collection-name "$COLLECTION_NAME")
fi

if [[ $RUN_DEDUPE -eq 1 ]]; then
  stage "Duplicate merge"
  python scripts/merge_zotero_duplicates.py "${COLL_ARGS[@]}" --limit "${LIMIT}" || true
fi

if [[ $RUN_SUMMARIZE -eq 1 ]]; then
  stage "Summarize with Doubao"
  SUM_ARGS=(--limit "$LIMIT" --max-pages "$SUMMARY_MAX_PAGES" --max-chars "$SUMMARY_MAX_CHARS" --summary-dir "$SUMMARY_DIR")
  if [[ $DRY_RUN -eq 0 ]]; then
    SUM_ARGS+=(--insert-note)
  fi
  if [[ -n "$COLLECTION_NAME" ]]; then
    SUM_ARGS+=(--collection-name "$COLLECTION_NAME")
    [[ $RECURSIVE -eq 1 ]] && SUM_ARGS+=(--recursive)
  fi
  python scripts/summarize_zotero_with_doubao.py "${SUM_ARGS[@]}" || true
fi

if [[ $RUN_ENRICH -eq 1 ]]; then
  stage "Enrich missing abstracts"
  python scripts/enrich_zotero_abstracts.py "${COLL_ARGS[@]}" --limit "$LIMIT" || true
fi

if [[ $RUN_WATCH -eq 1 ]]; then
  stage "Watch & import (tag.json)"
  WI_ARGS=(--tags ./tag.json --since-days "$WATCH_SINCE_DAYS" --top-k "$WATCH_TOP_K" --min-score "$WATCH_MIN_SCORE" --create-collections)
  if [[ $DRY_RUN -eq 1 ]]; then WI_ARGS+=(--dry-run); fi
  python scripts/watch_and_import_papers.py "${WI_ARGS[@]}" || true
fi

if [[ $RUN_NOTION -eq 1 ]]; then
  stage "Sync to Notion"
  NS_ARGS=(--limit "$LIMIT" --tag-file ./tag.json)
  if [[ -n "$COLLECTION_NAME" ]]; then
    NS_ARGS+=(--collection-name "$COLLECTION_NAME")
    [[ $RECURSIVE -eq 1 ]] && NS_ARGS+=(--recursive)
  fi
  [[ $DRY_RUN -eq 1 ]] && NS_ARGS+=(--dry-run)
  [[ $NOTION_SKIP_UNTITLED -eq 1 ]] && NS_ARGS+=(--skip-untitled)
  [[ $NOTION_DOUBAO -eq 1 ]] && NS_ARGS+=(--enrich-with-doubao)
  python scripts/sync_zotero_to_notion.py "${NS_ARGS[@]}" || true
fi

echo
echo "[DONE] Pipeline completed."
