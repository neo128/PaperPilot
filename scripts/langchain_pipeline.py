#!/usr/bin/env python3
"""LangChain-driven orchestration for the Zotero automation flow."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paperflow.config import PipelineConfig
from paperflow.pipeline import run_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Zotero AI toolbox via a LangChain pipeline.")
    parser.add_argument("--tag-file", default="tag.json", help="Path to tag schema used by the watch + Notion stages.")
    parser.add_argument("--collection-name", help="Target collection name for dedupe/summary/abstract/notion stages.")
    parser.add_argument("--collection-key", help="Collection key override (takes precedence over name).")
    parser.add_argument("--item-tag", help="Limit dedupe/summary/abstract stages to items containing this tag.")
    parser.add_argument("--logs-dir", default="logs", help="Directory for pipeline logs emitted by sub-scripts.")
    parser.add_argument("--reports-dir", default="reports", help="Directory for structured reports (watch stage).")
    parser.add_argument("--state-json", help="Optional path to dump aggregated pipeline metadata as JSON.")

    # Stage toggles
    parser.add_argument("--skip-watch", action="store_true", help="Do not run watch/import stage.")
    parser.add_argument("--skip-pdf", action="store_true", help="Do not run PDF completion stage.")
    parser.add_argument("--skip-dedupe", action="store_true", help="Do not run duplicate merge stage.")
    parser.add_argument("--skip-summary", action="store_true", help="Do not run AI summary stage.")
    parser.add_argument("--skip-abstract", action="store_true", help="Do not run abstract enrichment stage.")
    parser.add_argument("--skip-notion", action="store_true", help="Do not run Notion sync stage.")

    # Watch
    parser.add_argument("--watch-since-days", type=int, default=0)
    parser.add_argument("--watch-since-hours", type=float, default=24.0)
    parser.add_argument("--watch-top-k", type=int, default=10)
    parser.add_argument("--watch-min-score", type=float, default=0.3)
    parser.add_argument("--watch-fill-missing", action="store_true")
    parser.add_argument("--watch-dry-run", action="store_true")
    parser.add_argument("--watch-no-create-collections", action="store_true")

    # Dedupe
    parser.add_argument("--dedupe-limit", type=int, default=0)
    parser.add_argument("--dedupe-group-by", choices=["auto", "doi", "url", "title"], default="auto")
    parser.add_argument("--dedupe-dry-run", action="store_true")
    parser.add_argument("--dedupe-modified-since-hours", type=float, default=24.0)

    # PDF completion
    parser.add_argument("--pdf-since-hours", type=float, default=24.0)
    parser.add_argument("--pdf-limit", type=int, default=0)
    parser.add_argument("--pdf-new-items-json", default=".data/new_items_watch.json")
    parser.add_argument("--pdf-storage-dir")
    parser.add_argument("--pdf-dry-run", action="store_true")

    # Summary
    parser.add_argument("--summary-limit", type=int, default=200)
    parser.add_argument("--summary-max-pages", type=int, default=80)
    parser.add_argument("--summary-max-chars", type=int, default=80000)
    parser.add_argument("--summary-note-tag", default="AI总结")
    parser.add_argument("--summary-dir", default="summaries")
    parser.add_argument("--summary-no-insert-note", action="store_true")
    parser.add_argument("--summary-force", action="store_true")
    parser.add_argument("--summary-model", help="Override Doubao bot id for summarization.")
    parser.add_argument("--summary-non-recursive", action="store_true")
    parser.add_argument("--summary-modified-since-hours", type=float, default=24.0)

    # Abstract enrichment
    parser.add_argument("--abstract-limit", type=int, default=0)
    parser.add_argument("--abstract-dry-run", action="store_true")
    parser.add_argument("--abstract-modified-since-hours", type=float, default=24.0)

    # Notion sync
    parser.add_argument("--notion-limit", type=int, default=500)
    parser.add_argument("--notion-since-days", type=int, default=0)
    parser.add_argument("--notion-since-hours", type=float, default=24.0)
    parser.add_argument("--notion-tag", help="Only sync items containing this Zotero tag.")
    parser.add_argument("--notion-no-doubao", action="store_true")
    parser.add_argument("--notion-no-skip-untitled", action="store_true")
    parser.add_argument("--notion-non-recursive", action="store_true")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = PipelineConfig()
    cfg.logs_dir = Path(args.logs_dir)
    cfg.reports_dir = Path(args.reports_dir)
    cfg.watch.enabled = not args.skip_watch
    cfg.watch.tag_file = Path(args.tag_file)
    cfg.watch.since_days = args.watch_since_days
    cfg.watch.since_hours = args.watch_since_hours
    cfg.watch.top_k = args.watch_top_k
    cfg.watch.min_score = args.watch_min_score
    cfg.watch.fill_missing = args.watch_fill_missing
    cfg.watch.dry_run = args.watch_dry_run
    cfg.watch.create_collections = not args.watch_no_create_collections

    cfg.pdf.enabled = not args.skip_pdf
    cfg.pdf.since_hours = args.pdf_since_hours
    cfg.pdf.limit = args.pdf_limit
    cfg.pdf.new_items_json = Path(args.pdf_new_items_json)
    cfg.pdf.dry_run = args.pdf_dry_run
    if args.pdf_storage_dir:
        cfg.pdf.storage_dir = Path(args.pdf_storage_dir)

    cfg.dedupe.enabled = not args.skip_dedupe
    cfg.dedupe.limit = args.dedupe_limit
    cfg.dedupe.group_by = args.dedupe_group_by
    cfg.dedupe.dry_run = args.dedupe_dry_run
    cfg.dedupe.modified_since_hours = args.dedupe_modified_since_hours

    cfg.summary.enabled = not args.skip_summary
    cfg.summary.limit = args.summary_limit
    cfg.summary.max_pages = args.summary_max_pages
    cfg.summary.max_chars = args.summary_max_chars
    cfg.summary.note_tag = args.summary_note_tag
    cfg.summary.summary_dir = Path(args.summary_dir)
    cfg.summary.insert_note = not args.summary_no_insert_note
    cfg.summary.force = args.summary_force
    cfg.summary.model = args.summary_model
    cfg.summary.recursive = not args.summary_non_recursive
    cfg.summary.modified_since_hours = args.summary_modified_since_hours

    cfg.abstract.enabled = not args.skip_abstract
    cfg.abstract.limit = args.abstract_limit
    cfg.abstract.dry_run = args.abstract_dry_run
    cfg.abstract.modified_since_hours = args.abstract_modified_since_hours

    cfg.notion.enabled = not args.skip_notion
    cfg.notion.limit = args.notion_limit
    cfg.notion.since_days = args.notion_since_days
    cfg.notion.since_hours = args.notion_since_hours
    cfg.notion.tag = args.notion_tag
    cfg.notion.enrich_with_doubao = not args.notion_no_doubao
    cfg.notion.skip_untitled = not args.notion_no_skip_untitled
    cfg.notion.recursive = not args.notion_non_recursive
    cfg.notion.tag_file = Path(args.tag_file)

    collection_name = args.collection_name
    collection_key = args.collection_key
    item_tag = args.item_tag
    for stage in (cfg.dedupe, cfg.summary, cfg.abstract, cfg.notion):
        stage.collection_name = collection_name
        stage.collection = collection_key
        if hasattr(stage, "tag"):
            setattr(stage, "tag", getattr(stage, "tag") or item_tag)

    state = run_pipeline(cfg)
    summary = state.as_dict()
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.state_json:
        path = Path(args.state_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[PIPELINE] State written to {path}")


if __name__ == "__main__":
    main()
