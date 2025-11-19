from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class WatchStageConfig:
    enabled: bool = True
    tag_file: Path = field(default_factory=lambda: Path("tag.json"))
    since_days: int = 14
    top_k: int = 10
    min_score: float = 0.3
    create_collections: bool = True
    fill_missing: bool = False
    dry_run: bool = False
    log_file: Optional[Path] = None
    report_json: Optional[Path] = None


@dataclass
class DedupeStageConfig:
    enabled: bool = True
    collection: Optional[str] = None
    collection_name: Optional[str] = None
    tag: Optional[str] = None
    limit: int = 0
    group_by: str = "auto"
    dry_run: bool = False


@dataclass
class SummaryStageConfig:
    enabled: bool = True
    collection: Optional[str] = None
    collection_name: Optional[str] = None
    tag: Optional[str] = None
    recursive: bool = True
    limit: int = 200
    max_pages: int = 80
    max_chars: int = 80000
    note_tag: str = "AI总结"
    summary_dir: Path = field(default_factory=lambda: Path("summaries"))
    insert_note: bool = True
    force: bool = False
    model: Optional[str] = None


@dataclass
class AbstractStageConfig:
    enabled: bool = True
    collection: Optional[str] = None
    collection_name: Optional[str] = None
    tag: Optional[str] = None
    limit: int = 0
    dry_run: bool = False


@dataclass
class NotionStageConfig:
    enabled: bool = True
    collection: Optional[str] = None
    collection_name: Optional[str] = None
    recursive: bool = True
    tag: Optional[str] = None
    limit: int = 500
    since_days: int = 0
    skip_untitled: bool = True
    enrich_with_doubao: bool = True
    tag_file: Path = field(default_factory=lambda: Path("tag.json"))


@dataclass
class PipelineConfig:
    repo_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1])
    watch: WatchStageConfig = field(default_factory=WatchStageConfig)
    dedupe: DedupeStageConfig = field(default_factory=DedupeStageConfig)
    summary: SummaryStageConfig = field(default_factory=SummaryStageConfig)
    abstract: AbstractStageConfig = field(default_factory=AbstractStageConfig)
    notion: NotionStageConfig = field(default_factory=NotionStageConfig)

    logs_dir: Path = field(default_factory=lambda: Path("logs"))
    reports_dir: Path = field(default_factory=lambda: Path("reports"))

    def resolve(self) -> "PipelineConfig":
        base = self.repo_root
        self.logs_dir = (self.logs_dir if self.logs_dir.is_absolute() else base / self.logs_dir).resolve()
        self.reports_dir = (self.reports_dir if self.reports_dir.is_absolute() else base / self.reports_dir).resolve()
        self.watch.tag_file = self._resolve_path(self.watch.tag_file)
        self.watch.log_file = self._resolve_optional(self.watch.log_file)
        self.watch.report_json = self._resolve_optional(self.watch.report_json)
        self.summary.summary_dir = self._resolve_path(self.summary.summary_dir)
        self.notion.tag_file = self._resolve_path(self.notion.tag_file)
        return self

    def _resolve_path(self, target: Path) -> Path:
        return target if target.is_absolute() else (self.repo_root / target).resolve()

    def _resolve_optional(self, target: Optional[Path]) -> Optional[Path]:
        if not target:
            return None
        return self._resolve_path(target)
