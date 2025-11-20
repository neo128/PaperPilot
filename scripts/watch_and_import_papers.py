#!/usr/bin/env python3
"""
Watch and import recent impactful papers into Zotero based on tag.json
----------------------------------------------------------------------

Pipeline:
  - Load tag.json taxonomy with sample keywords per tag
  - Fetch recent candidates from arXiv (keywords, since-days)
  - Optionally enrich with Semantic Scholar citation stats and CrossRef metadata
  - Score by recency and citations; keep top-k per tag
  - Deduplicate against Zotero by DOI/arXiv/URL/title+year
  - Create items in Zotero under target collections, apply tags, attach PDF URL
  - Write text logs and JSON report
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import textwrap
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests

from utils_sources import (
    fetch_arxiv_by_keywords,
    fetch_crossref_metadata,
    fetch_s2_metadata,
    fetch_unpaywall_pdf,
    normalize_authors,
)


def ensure_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def parse_next_link(link_header: Optional[str]) -> Optional[str]:
    if not link_header:
        return None
    for chunk in link_header.split(","):
        parts = chunk.split(";")
        if len(parts) < 2:
            continue
        url_part = parts[0].strip()
        rel_part = parts[1].strip()
        if rel_part == 'rel="next"':
            return url_part.strip("<>")
    return None


class ZoteroAPI:
    def __init__(self, user_id: str, api_key: str) -> None:
        self.base = f"https://api.zotero.org/users/{user_id}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Zotero-API-Key": api_key,
                "User-Agent": "Zotero-Watch-Importer/0.1",
            }
        )

    def iter_top_items(self) -> Iterable[Dict[str, Any]]:
        url = f"{self.base}/items/top"
        params = {"format": "json", "include": "data", "limit": 100}
        while url:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            for entry in resp.json():
                yield entry
            url = parse_next_link(resp.headers.get("Link"))
            params = None

    def list_collections(self) -> Dict[str, Dict[str, Optional[str]]]:
        resp = self.session.get(
            f"{self.base}/collections",
            params={"limit": 200, "format": "json", "include": "data"},
        )
        resp.raise_for_status()
        out: Dict[str, Dict[str, Optional[str]]] = {}
        for entry in resp.json():
            data = entry.get("data", {})
            out[data.get("name")] = {"key": entry.get("key"), "parent": data.get("parentCollection")}
        return out

    def create_collection_if_missing(self, name: str) -> str:
        collections = self.list_collections()
        for cname, info in collections.items():
            if cname == name or (cname and cname.lower() == name.lower()):
                return info["key"]
        payload = [{"name": name}]
        resp = self.session.post(f"{self.base}/collections", json=payload)
        resp.raise_for_status()
        # Location header contains keys; but simpler: re-list
        collections = self.list_collections()
        return collections[name]["key"]

    def create_items(self, items: List[Dict[str, Any]]) -> List[str]:
        resp = self.session.post(f"{self.base}/items", json=items)
        resp.raise_for_status()
        # Parse Zotero batch response. Typical shape:
        # {
        #   "successful": {"0": {"key": "ABCD1234", "version": 1}},
        #   "failed": {},
        #   "unchanged": {}
        # }
        keys: List[str] = []
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            succ = data.get("successful") or {}
            if isinstance(succ, dict):
                for _, info in succ.items():
                    if isinstance(info, dict) and info.get("key"):
                        keys.append(info["key"])
        elif isinstance(data, list):
            # Very defensive: some proxies may wrap differently
            for entry in data:
                if isinstance(entry, dict):
                    succ = entry.get("successful") or {}
                    if isinstance(succ, dict):
                        for _, info in succ.items():
                            if isinstance(info, dict) and info.get("key"):
                                keys.append(info["key"])
        return keys

    def create_attachment_url(self, parent_key: str, title: str, url: str) -> None:
        payload = [
            {
                "itemType": "attachment",
                "parentItem": parent_key,
                "title": title,
                "linkMode": "linked_url",
                "contentType": "application/pdf",
                "url": url,
            }
        ]
        resp = self.session.post(f"{self.base}/items", json=payload)
        resp.raise_for_status()

    def update_item(self, entry: Dict[str, Any], new_data: Dict[str, Any]) -> None:
        headers = {"If-Unmodified-Since-Version": str(entry.get("version"))}
        resp = self.session.put(f"{self.base}/items/{entry['key']}", json=new_data, headers=headers)
        resp.raise_for_status()


def normalize_title(s: Optional[str]) -> str:
    if not s:
        return ""
    import re as _re

    return _re.sub(r"[^a-z0-9 ]", "", _re.sub(r"\s+", " ", s.lower())).strip()


@dataclass
class Candidate:
    """Lightweight container representing one fetched paper before it becomes a Zotero item."""
    title: str
    authors: List[str]
    date: Optional[str]
    year: Optional[str]
    url: Optional[str]
    pdf_url: Optional[str]
    doi: Optional[str]
    arxiv_id: Optional[str]
    abstract: Optional[str]
    source: str
    score: float = 0.0
    tags: Set[str] = None  # type: ignore
    collections: Set[str] = None  # type: ignore

    def identity(self) -> str:
        if self.doi:
            return f"doi:{self.doi.lower()}"
        if self.arxiv_id:
            return f"arxiv:{self.arxiv_id}"
        if self.url:
            from urllib.parse import urlsplit

            parts = urlsplit(self.url)
            norm_url = f"{parts.scheme}://{parts.netloc}{parts.path}".lower().rstrip("/")
            return f"url:{norm_url}"
        if self.title and self.year:
            return f"ty:{normalize_title(self.title)}|{self.year}"
        return f"t:{normalize_title(self.title)}"


def normalized_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    from urllib.parse import urlsplit

    stripped = url.strip()
    if not stripped:
        return None
    parts = urlsplit(stripped)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path}".lower().rstrip("/")


def candidate_ty_key(cand: Candidate) -> Optional[str]:
    if not cand.title or not cand.year:
        return None
    title_norm = normalize_title(cand.title)
    if not title_norm:
        return None
    return f"{title_norm}|{cand.year}"


def build_library_index(zot: ZoteroAPI) -> Dict[str, Any]:
    # The index keeps both quick-membership sets and entry lookups so we can
    # dedupe incoming candidates and optionally patch the existing entry.
    doi_set: Set[str] = set()
    arxiv_set: Set[str] = set()
    url_set: Set[str] = set()
    ty_set: Set[str] = set()
    by_doi: Dict[str, Dict[str, Any]] = {}
    by_arxiv: Dict[str, Dict[str, Any]] = {}
    by_url: Dict[str, Dict[str, Any]] = {}
    by_ty: Dict[str, Dict[str, Any]] = {}
    for entry in zot.iter_top_items():
        data = entry.get("data", {})
        if data.get("itemType") in {"note", "attachment"}:
            continue
        doi = (data.get("DOI") or data.get("doi") or "").strip().lower()
        if doi:
            doi_set.add(doi)
            by_doi.setdefault(doi, entry)
        url = (data.get("url") or "").strip()
        if url:
            from urllib.parse import urlsplit

            parts = urlsplit(url)
            norm_url = f"{parts.scheme}://{parts.netloc}{parts.path}".lower().rstrip("/")
            url_set.add(norm_url)
            by_url.setdefault(norm_url, entry)
        title = normalize_title(data.get("title"))
        year = data.get("year") or (data.get("date") or "")[:4]
        if title and year:
            ty_key = f"{title}|{year}"
            ty_set.add(ty_key)
            by_ty.setdefault(ty_key, entry)
        # try to detect arxiv id from url
        import re as _re

        m = _re.search(r"arxiv\.org/(?:abs|pdf)/([A-Za-z0-9.\-]+)", url or "")
        if m:
            arc = m.group(1)
            arxiv_set.add(arc)
            by_arxiv.setdefault(arc, entry)
    return {
        "doi": doi_set,
        "arxiv": arxiv_set,
        "url": url_set,
        "ty": ty_set,
        "by_doi": by_doi,
        "by_arxiv": by_arxiv,
        "by_url": by_url,
        "by_ty": by_ty,
    }


def find_existing_entry(idx: Dict[str, Any], cand: Candidate) -> Optional[Dict[str, Any]]:
    # Check identifiers in order of reliability to find a concrete Zotero entry.
    if cand.doi and cand.doi in idx["by_doi"]:
        return idx["by_doi"].get(cand.doi)
    if cand.arxiv_id and cand.arxiv_id in idx["by_arxiv"]:
        return idx["by_arxiv"].get(cand.arxiv_id)
    url_key = normalized_url(cand.url)
    if url_key and url_key in idx["by_url"]:
        return idx["by_url"].get(url_key)
    ty_key = candidate_ty_key(cand)
    if ty_key and ty_key in idx["by_ty"]:
        return idx["by_ty"].get(ty_key)
    return None


def enrich_existing_entry(
    zot: ZoteroAPI,
    entry: Dict[str, Any],
    cand: Candidate,
    label: str,
    collection_key: Optional[str],
    log_fn,
) -> bool:
    # Fill in missing metadata on an existing Zotero entry using the richer
    # candidate data we just fetched from external sources.
    data = entry.get("data", {})
    new_data = data.copy()
    changed_fields: List[str] = []

    def mark(field: str) -> None:
        # Track which fields changed for logging/debugging.
        if field not in changed_fields:
            changed_fields.append(field)

    abstract_existing = (new_data.get("abstractNote") or "").strip()
    if cand.abstract and not abstract_existing:
        new_data["abstractNote"] = cand.abstract
        mark("abstract")
    doi_existing = (new_data.get("DOI") or new_data.get("doi") or "").strip()
    if cand.doi and not doi_existing:
        new_data["DOI"] = cand.doi
        mark("doi")
    url_existing = (new_data.get("url") or "").strip()
    if cand.url and not url_existing:
        new_data["url"] = cand.url
        mark("url")
    year_existing = new_data.get("year")
    if cand.year and not year_existing:
        new_data["year"] = cand.year
        mark("year")

    collections = list(new_data.get("collections") or [])
    if collection_key and collection_key not in collections:
        collections.append(collection_key)
        new_data["collections"] = collections
        mark("collection")

    tags = list(new_data.get("tags") or [])
    if label and not any((t or {}).get("tag") == label for t in tags):
        tags.append({"tag": label})
        new_data["tags"] = tags
        mark("tag")

    if not changed_fields:
        return False
    zot.update_item(entry, new_data)
    entry["data"] = new_data
    log_fn(
        f"[FILL] Updated existing item {entry['key']} with {', '.join(changed_fields)} "
        f"derived from '{cand.title[:80]}'"
    )
    return True


def compute_score(now: dt.datetime, cand: Candidate, max_days: int, cit: Optional[int], inf_cit: Optional[int]) -> float:
    # Recency score: 1.0 when today, decays to 0 at max_days
    recency = 0.0
    max_days = max(max_days or 0, 1)
    ref_date: Optional[dt.datetime] = None
    if cand.date:
        try:
            ref_date = dt.datetime.fromisoformat(cand.date + "T00:00:00+00:00")
        except Exception:
            ref_date = None
    if not ref_date and cand.year:
        # Fall back to publishing year so older records still get a recency weight.
        try:
            ref_date = dt.datetime(int(cand.year), 1, 1, tzinfo=dt.timezone.utc)
        except Exception:
            ref_date = None
    if ref_date:
        days = max(0, (now - ref_date).days)
        recency = max(0.0, 1.0 - min(days, max_days) / max_days)
    # Citation normalization with soft cap
    def norm(x: Optional[int], cap: int) -> float:
        if x is None:
            return 0.0
        return min(x, cap) / cap

    c1 = norm(cit, 200)
    c2 = norm(inf_cit, 50)
    # Weighted sum
    return 0.5 * recency + 0.35 * c1 + 0.15 * c2


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Watch and import recent papers to Zotero based on tag.json")
    ap.add_argument("--tags", default="tag.json", help="Path to tag schema JSON.")
    ap.add_argument("--since-days", type=int, default=0, help="Deprecated. Prefer --since-hours for finer control.")
    ap.add_argument(
        "--since-hours",
        type=float,
        default=24.0,
        help="Time window in hours for fetching/processing new papers (default: 24).",
    )
    ap.add_argument("--top-k", type=int, default=10, help="Max items per tag to consider after scoring.")
    ap.add_argument("--min-score", type=float, default=0.3, help="Minimum score threshold to import.")
    ap.add_argument("--create-collections", action="store_true", help="Auto create collections for tags if missing.")
    ap.add_argument("--download-pdf", action="store_true", help="Download PDFs instead of linking (not implemented; links only).")
    ap.add_argument("--fill-missing", action="store_true", help="Update existing items when missing abstract/DOI/URL.")
    ap.add_argument("--dry-run", action="store_true", help="Preview actions only.")
    ap.add_argument("--log-file", help="Write text log to this path.")
    ap.add_argument("--report-json", help="Write JSON report to this path.")
    return ap.parse_args()


def open_log(report_dir: Path, log_file: Optional[str]) -> Tuple[Path, Any]:
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = Path(log_file) if log_file else report_dir / f"watch_{ts}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = log_path.open("w", encoding="utf-8")
    return log_path, fh


def main() -> None:
    args = parse_args()
    user_id = ensure_env("ZOTERO_USER_ID")
    api_key = ensure_env("ZOTERO_API_KEY")
    zot = ZoteroAPI(user_id, api_key)

    tags_path = Path(args.tags)
    if not tags_path.exists():
        raise SystemExit(f"tag file not found: {tags_path}")
    tag_schema = json.loads(tags_path.read_text(encoding="utf-8"))

    base_dir = Path.cwd()
    logs_dir = base_dir / "logs"
    reports_dir = base_dir / "reports"
    state_dir = base_dir / ".data"
    reports_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)

    log_path, log_fh = open_log(logs_dir, args.log_file)
    report_path = Path(args.report_json) if args.report_json else reports_dir / (log_path.stem + ".json")
    report: Dict[str, Any] = {
        "started_at": dt.datetime.now().isoformat(),
        "params": {
            "since_days": args.since_days,
            "since_hours": args.since_hours,
            "top_k": args.top_k,
            "min_score": args.min_score,
            "create_collections": args.create_collections,
            "fill_missing": args.fill_missing,
            "dry_run": args.dry_run,
        },
        "tags": {},
        "summary": {"candidates": 0, "added": 0, "skipped": 0, "updated": 0},
        "errors": [],
    }
    def log(line: str) -> None:
        print(line)
        print(line, file=log_fh)

    effective_days = args.since_days if args.since_days and args.since_days > 0 else max(args.since_hours / 24.0, 0.01)
    log(
        f"[INFO] Started watch. since_hours={args.since_hours} (→ days={effective_days:.2f}) top_k={args.top_k} min_score={args.min_score}"
    )

    log("[INFO] Building library index for dedupe...")
    idx = build_library_index(zot)
    log(f"[INFO] Library index sizes: DOI={len(idx['doi'])} arXiv={len(idx['arxiv'])} URL={len(idx['url'])} TY={len(idx['ty'])}")

    now = dt.datetime.now(dt.timezone.utc)
    created_identities: Set[str] = set()
    new_items: List[Dict[str, Any]] = []
    unpaywall_email = os.environ.get("UNPAYWALL_EMAIL")

    for tag_key, cfg in tag_schema.items():
        label = cfg.get("label") or tag_key
        keywords = cfg.get("sample_keywords") or []
        if not keywords:
            continue
        report["tags"][tag_key] = {"label": label, "candidates": 0, "added": 0, "skipped": 0, "updated": 0}
        log(f"[TAG] {tag_key} '{label}' keywords={len(keywords)}")

        # Fetch candidates from arXiv
        cands_raw = fetch_arxiv_by_keywords(keywords, since_days=effective_days, max_results=args.top_k * 5)
        candidates: List[Candidate] = []
        for it in cands_raw:
            candidates.append(
                Candidate(
                    title=it.get("title") or "",
                    authors=it.get("authors") or [],
                    date=it.get("date"),
                    year=it.get("year"),
                    url=it.get("url"),
                    pdf_url=it.get("pdf_url"),
                    doi=(it.get("doi") or "").lower() or None,
                    arxiv_id=it.get("arxiv_id"),
                    abstract=it.get("abstract"),
                    source="arxiv",
                    tags={label},
                    collections={label},
                )
            )

        # Enrich a limited slice with S2 / CrossRef to get citations / better abstracts.
        # This keeps the API cost bounded while still letting the scorer reason on richer metadata.
        enriched: List[Tuple[Candidate, Optional[int], Optional[int]]] = []
        for cand in candidates[: min(len(candidates), args.top_k * 5)]:
            cit = inf = None
            rate_limited = False
            # Prefer DOI; else arXiv id
            if cand.doi:
                meta = fetch_s2_metadata("DOI", cand.doi)
                if meta.get("rate_limited"):
                    rate_limited = True
                else:
                    cit = meta.get("citationCount")
                    inf = meta.get("influentialCitationCount")
                    # backfill title/year/abstract if missing
                    cand.year = cand.year or (str(meta.get("year")) if meta.get("year") else None)
                    if not cand.abstract and meta.get("abstract"):
                        cand.abstract = meta.get("abstract")
            if not cand.doi or rate_limited:
                if cand.arxiv_id:
                    meta = fetch_s2_metadata("arXiv", cand.arxiv_id)
                    if not meta.get("rate_limited"):
                        cit = cit or meta.get("citationCount")
                        inf = inf or meta.get("influentialCitationCount")
                        if not cand.doi and meta.get("doi"):
                            cand.doi = (meta.get("doi") or "").lower()
                        if not cand.abstract and meta.get("abstract"):
                            cand.abstract = meta.get("abstract")
                        cand.year = cand.year or (str(meta.get("year")) if meta.get("year") else None)
            if cand.doi:
                cr = fetch_crossref_metadata(cand.doi)
                if cr.get("abstract") and not cand.abstract:
                    cand.abstract = cr.get("abstract")
            enriched.append((cand, cit, inf))

        # Score and select top-k
        for cand, cit, inf in enriched:
            cand.score = compute_score(now, cand, args.since_days, cit, inf)
        candidates_sorted = sorted([c for c, _, _ in enriched], key=lambda c: c.score, reverse=True)
        selected = [c for c in candidates_sorted if c.score >= args.min_score][: args.top_k]
        report["tags"][tag_key]["candidates"] = len(candidates)
        log(f"[SCORE] tag={tag_key} total={len(candidates)} selected={len(selected)}")

        # Ensure collection exists if requested
        collection_key: Optional[str] = None
        if args.create_collections and selected:
            try:
                collection_key = zot.create_collection_if_missing(label)
                log(f"[COL] ensured collection '{label}' → {collection_key}")
            except Exception as exc:
                log(f"[ERR] create collection '{label}': {exc}")
                report["errors"].append({"collection": label, "error": str(exc)})

        # Import selected
        for cand in selected:
            ident = cand.identity()
            existing_entry = find_existing_entry(idx, cand)
            duplicate_in_library = existing_entry is not None
            duplicate_in_run = ident in created_identities
            if duplicate_in_library or duplicate_in_run:
                log(f"[SKIP] duplicate {cand.title[:80]} ({ident})")
                if args.fill_missing and existing_entry:
                    try:
                        if enrich_existing_entry(zot, existing_entry, cand, label, collection_key, log):
                            report["tags"][tag_key]["updated"] += 1
                            report["summary"]["updated"] += 1
                    except Exception as exc:
                        log(f"[WARN] Failed to enrich existing item {existing_entry['key']}: {exc}")
                report["tags"][tag_key]["skipped"] += 1
                report["summary"]["skipped"] += 1
                continue

            item_type = "journalArticle"
            creators = normalize_authors(cand.authors)
            new_item = {
                "itemType": item_type,
                "title": cand.title,
                "creators": creators,
                "abstractNote": cand.abstract or "",
                "url": cand.url or "",
                "DOI": cand.doi or "",
                "date": cand.date or (cand.year or ""),
                "tags": [{"tag": label}],
                "collections": [collection_key] if collection_key else [],
            }

            if args.dry_run:
                log(f"[ADD-DRY] {cand.title[:80]} | DOI={cand.doi or '-'} | arXiv={cand.arxiv_id or '-'} → {label}")
                report["summary"]["candidates"] += 1
                continue

            try:
                keys = zot.create_items([new_item])
                parent_key = keys[0] if keys else None
                report["summary"]["candidates"] += 1
                if not parent_key:
                    # Treat as successful create if HTTP returned 2xx; increment counters but note missing key
                    report["tags"][tag_key]["added"] += 1
                    report["summary"]["added"] += 1
                    log(f"[ADD] {cand.title[:80]} → {label} [key: unknown]")
                else:
                    created_identities.add(ident)
                    report["tags"][tag_key]["added"] += 1
                    report["summary"]["added"] += 1
                    log(f"[ADD] {cand.title[:80]} → {label} [{parent_key}]")
                    new_items.append(
                        {
                            "key": parent_key,
                            "title": cand.title,
                            "tag": label,
                            "collection_key": collection_key,
                            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        }
                    )
                    # attach PDF url
                    pdf_url = cand.pdf_url or (fetch_unpaywall_pdf(cand.doi, unpaywall_email) if cand.doi else None)
                    if pdf_url:
                        try:
                            zot.create_attachment_url(parent_key, "PDF", pdf_url)
                            log(f"[ATTACH] PDF linked for {parent_key}")
                        except Exception as exc:
                            log(f"[WARN] Attach PDF failed for {parent_key}: {exc}")
            except requests.HTTPError as exc:
                log(f"[ERR] Create item failed: {exc}")
                report["errors"].append({"title": cand.title, "error": str(exc)})

    new_items_path = state_dir / "new_items_watch.json"
    new_payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "since_hours": args.since_hours,
        "items": new_items,
    }
    try:
        new_items_path.write_text(json.dumps(new_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"[INFO] Recorded {len(new_items)} new items → {new_items_path}")
    except Exception as exc:
        log(f"[WARN] Failed to write new items file: {exc}")

    report["finished_at"] = dt.datetime.now().isoformat()
    log(f"[INFO] Done. Summary: {json.dumps(report['summary'])}")
    log_fh.flush()
    log_fh.close()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] Report → {report_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        sys.exit(1)
