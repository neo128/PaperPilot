from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import PipelineConfig
from .state import PipelineState, StageRunResult

PYTHON = sys.executable


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _script(repo_root: Path, filename: str) -> str:
    return str((repo_root / "scripts" / filename).resolve())


def _run_command(name: str, command: List[str], cwd: Path) -> StageRunResult:
    process = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True)
    if process.returncode != 0:
        raise RuntimeError(
            f"Stage '{name}' failed with exit code {process.returncode}.\nSTDOUT:\n{process.stdout}\nSTDERR:\n{process.stderr}"
        )
    return StageRunResult(name=name, command=command, stdout=process.stdout, stderr=process.stderr)


def watch_stage(state: PipelineState, config: PipelineConfig) -> PipelineState:
    stage_cfg = config.watch
    if not stage_cfg.enabled:
        return state

    logs_dir = config.logs_dir
    reports_dir = config.reports_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    ts = _timestamp()
    log_path = stage_cfg.log_file or (logs_dir / f"watch_{ts}.log")
    report_path = stage_cfg.report_json or (reports_dir / f"watch_{ts}.json")

    cmd: List[str] = [
        PYTHON,
        _script(config.repo_root, "watch_and_import_papers.py"),
        "--tags",
        str(stage_cfg.tag_file),
        "--since-days",
        str(stage_cfg.since_days),
        "--top-k",
        str(stage_cfg.top_k),
        "--min-score",
        str(stage_cfg.min_score),
        "--log-file",
        str(log_path),
        "--report-json",
        str(report_path),
    ]
    if stage_cfg.create_collections:
        cmd.append("--create-collections")
    if stage_cfg.fill_missing:
        cmd.append("--fill-missing")
    if stage_cfg.dry_run:
        cmd.append("--dry-run")

    result = _run_command("watch-import", cmd, config.repo_root)
    artifacts: Dict[str, object] = {"log": log_path, "report": report_path}
    if report_path.exists():
        try:
            artifacts["report_data"] = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            artifacts["report_data_error"] = "Failed to parse report JSON"
    result.artifacts = artifacts
    state.watch = result
    return state


def dedupe_stage(state: PipelineState, config: PipelineConfig) -> PipelineState:
    stage_cfg = config.dedupe
    if not stage_cfg.enabled:
        return state

    cmd: List[str] = [
        PYTHON,
        _script(config.repo_root, "merge_zotero_duplicates.py"),
        "--group-by",
        stage_cfg.group_by,
    ]
    if stage_cfg.collection:
        cmd += ["--collection", stage_cfg.collection]
    if stage_cfg.collection_name:
        cmd += ["--collection-name", stage_cfg.collection_name]
    if stage_cfg.tag:
        cmd += ["--tag", stage_cfg.tag]
    if stage_cfg.limit:
        cmd += ["--limit", str(stage_cfg.limit)]
    if stage_cfg.dry_run:
        cmd.append("--dry-run")

    result = _run_command("dedupe", cmd, config.repo_root)
    state.dedupe = result
    return state


def summary_stage(state: PipelineState, config: PipelineConfig) -> PipelineState:
    stage_cfg = config.summary
    if not stage_cfg.enabled:
        return state

    stage_cfg.summary_dir.mkdir(parents=True, exist_ok=True)

    cmd: List[str] = [
        PYTHON,
        _script(config.repo_root, "summarize_zotero_with_doubao.py"),
        "--limit",
        str(stage_cfg.limit),
        "--max-pages",
        str(stage_cfg.max_pages),
        "--max-chars",
        str(stage_cfg.max_chars),
        "--note-tag",
        stage_cfg.note_tag,
        "--summary-dir",
        str(stage_cfg.summary_dir),
    ]
    if stage_cfg.collection:
        cmd += ["--collection", stage_cfg.collection]
    if stage_cfg.collection_name:
        cmd += ["--collection-name", stage_cfg.collection_name]
    if stage_cfg.tag:
        cmd += ["--tag", stage_cfg.tag]
    if stage_cfg.recursive:
        cmd.append("--recursive")
    if stage_cfg.insert_note:
        cmd.append("--insert-note")
    if stage_cfg.force:
        cmd.append("--force")
    if stage_cfg.model:
        cmd += ["--model", stage_cfg.model]

    result = _run_command("summaries", cmd, config.repo_root)
    result.artifacts["summary_dir"] = stage_cfg.summary_dir
    state.summary = result
    return state


def abstract_stage(state: PipelineState, config: PipelineConfig) -> PipelineState:
    stage_cfg = config.abstract
    if not stage_cfg.enabled:
        return state

    cmd: List[str] = [
        PYTHON,
        _script(config.repo_root, "enrich_zotero_abstracts.py"),
    ]
    if stage_cfg.collection:
        cmd += ["--collection", stage_cfg.collection]
    if stage_cfg.collection_name:
        cmd += ["--collection-name", stage_cfg.collection_name]
    if stage_cfg.tag:
        cmd += ["--tag", stage_cfg.tag]
    if stage_cfg.limit:
        cmd += ["--limit", str(stage_cfg.limit)]
    if stage_cfg.dry_run:
        cmd.append("--dry-run")

    result = _run_command("abstracts", cmd, config.repo_root)
    state.abstract = result
    return state


def notion_stage(state: PipelineState, config: PipelineConfig) -> PipelineState:
    stage_cfg = config.notion
    if not stage_cfg.enabled:
        return state

    cmd: List[str] = [
        PYTHON,
        _script(config.repo_root, "sync_zotero_to_notion.py"),
        "--limit",
        str(stage_cfg.limit),
        "--tag-file",
        str(stage_cfg.tag_file),
    ]
    if stage_cfg.collection:
        cmd += ["--collection", stage_cfg.collection]
    if stage_cfg.collection_name:
        cmd += ["--collection-name", stage_cfg.collection_name]
    if stage_cfg.tag:
        cmd += ["--tag", stage_cfg.tag]
    if stage_cfg.since_days:
        cmd += ["--since-days", str(stage_cfg.since_days)]
    if stage_cfg.recursive:
        cmd.append("--recursive")
    if stage_cfg.skip_untitled:
        cmd.append("--skip-untitled")
    if stage_cfg.enrich_with_doubao:
        cmd.append("--enrich-with-doubao")

    result = _run_command("notion-sync", cmd, config.repo_root)
    state.notion = result
    return state
