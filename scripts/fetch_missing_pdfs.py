#!/usr/bin/env python3
"""Download PDFs for recently added Zotero items and attach them locally."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests

from utils_sources import fetch_unpaywall_pdf


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


def parse_iso(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return dt.datetime.fromisoformat(value)
    except Exception:
        return None


def extract_arxiv_id(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = re.search(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)([A-Za-z0-9.\-]+)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def has_pdf_attachment(children: Iterable[Dict[str, Any]]) -> bool:
    for child in children:
        data = child.get("data") or child
        if data.get("itemType") != "attachment":
            continue
        filename = (data.get("filename") or "").lower()
        if data.get("contentType") == "application/pdf" or filename.endswith(".pdf"):
            return True
    return False


def sanitize_filename(title: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", title.strip())
    return cleaned[:80] or "document"


class ZoteroAPI:
    def __init__(self, user_id: str, api_key: str) -> None:
        self.base = f"https://api.zotero.org/users/{user_id}"
        self.session = requests.Session()
        self.session.headers.update({"Zotero-API-Key": api_key, "User-Agent": "Zotero-PDF-Fetcher/0.1"})

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

    def fetch_item(self, key: str) -> Dict[str, Any]:
        resp = self.session.get(f"{self.base}/items/{key}", params={"format": "json", "include": "data"})
        resp.raise_for_status()
        return resp.json()["data"]

    def fetch_children(self, parent_key: str) -> List[Dict[str, Any]]:
        resp = self.session.get(
            f"{self.base}/items/{parent_key}/children",
            params={"format": "json", "include": "data", "limit": 50},
        )
        resp.raise_for_status()
        return resp.json()

    def create_linked_file(self, parent_key: str, title: str, path: Path) -> None:
        payload = [
            {
                "itemType": "attachment",
                "parentItem": parent_key,
                "title": title,
                "linkMode": "linked_file",
                "contentType": "application/pdf",
                "path": str(path),
                "tags": [{"tag": "auto-pdf"}],
            }
        ]
        resp = self.session.post(f"{self.base}/items", json=payload)
        resp.raise_for_status()


def guess_pdf_sources(data: Dict[str, Any], unpaywall_email: Optional[str]) -> List[Tuple[str, str]]:
    sources: List[Tuple[str, str]] = []
    url = (data.get("url") or "").strip()
    if url.lower().endswith(".pdf"):
        sources.append((url, "direct URL"))
    arxiv_id = extract_arxiv_id(url) or extract_arxiv_id(data.get("extra"))
    if arxiv_id:
        sources.append((f"https://arxiv.org/pdf/{arxiv_id}.pdf", "arXiv"))
    doi = (data.get("DOI") or data.get("doi") or "").strip()
    if doi and unpaywall_email:
        pdf_url = fetch_unpaywall_pdf(doi, unpaywall_email)
        if pdf_url:
            sources.append((pdf_url, "Unpaywall"))
    return sources


def download_pdf(url: str, dest: Path) -> None:
    resp = requests.get(url, timeout=45)
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)


def load_new_keys(path: Path, cutoff: Optional[dt.datetime]) -> List[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    keys: List[str] = []
    for item in payload.get("items", []):
        key = item.get("key")
        created_at = parse_iso(item.get("created_at"))
        if not key:
            continue
        if cutoff and created_at and created_at < cutoff:
            continue
        keys.append(key)
    return keys


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Auto-fetch PDFs for latest Zotero imports.")
    ap.add_argument("--since-hours", type=float, default=24.0, help="Window in hours to consider items (default 24).")
    ap.add_argument("--limit", type=int, default=0, help="Max number of parents to process (<=0 means unlimited).")
    ap.add_argument(
        "--new-items-json",
        default=".data/new_items_watch.json",
        help="Path to watcher output JSON; if present, use it to seed target keys.",
    )
    ap.add_argument("--storage-dir", help="Override Zotero storage directory.")
    ap.add_argument("--dry-run", action="store_true", help="Preview downloads without touching disk or Zotero.")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    user_id = ensure_env("ZOTERO_USER_ID")
    api_key = ensure_env("ZOTERO_API_KEY")
    zot = ZoteroAPI(user_id, api_key)

    storage_dir = Path(args.storage_dir or os.environ.get("ZOTERO_STORAGE_DIR", Path.home() / "Zotero" / "storage"))
    storage_dir.mkdir(parents=True, exist_ok=True)

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max(args.since_hours, 0.1))
    candidate_keys: List[str] = []
    new_items_path = Path(args.new_items_json)
    candidate_keys.extend(load_new_keys(new_items_path, cutoff))

    if not candidate_keys:
        for entry in zot.iter_top_items():
            data = entry.get("data", {})
            dm = parse_iso(data.get("dateAdded") or data.get("dateModified"))
            if cutoff and dm and dm < cutoff:
                continue
            candidate_keys.append(entry.get("key"))
            if args.limit and len(candidate_keys) >= args.limit:
                break
    else:
        # de-duplicate while preserving order
        seen: Set[str] = set()
        deduped: List[str] = []
        for key in candidate_keys:
            if key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        candidate_keys = deduped
        if args.limit:
            candidate_keys = candidate_keys[: args.limit]

    if not candidate_keys:
        print("[INFO] No items to process for PDF completion.")
        return

    unpaywall_email = os.environ.get("UNPAYWALL_EMAIL")
    fetched = 0
    skipped = 0

    for key in candidate_keys:
        try:
            parent = zot.fetch_item(key)
        except requests.HTTPError as exc:
            print(f"[WARN] Failed to fetch item {key}: {exc}")
            skipped += 1
            continue
        children = zot.fetch_children(key)
        if has_pdf_attachment(children):
            continue
        sources = guess_pdf_sources(parent, unpaywall_email)
        if not sources:
            skipped += 1
            print(f"[INFO] No PDF sources found for {parent.get('title') or key}")
            continue
        title = parent.get("title") or parent.get("shortTitle") or key
        filename = sanitize_filename(title) + ".pdf"
        dest_dir = storage_dir / "auto_pdfs" / key
        dest_path = dest_dir / filename
        success = False
        for url, label in sources:
            print(f"[TRY] {key} ← {label}: {url}")
            if args.dry_run:
                success = True
                break
            try:
                download_pdf(url, dest_path)
                zot.create_linked_file(key, filename, dest_path)
                success = True
                print(f"[OK] Linked local PDF for {key}")
                break
            except Exception as exc:
                print(f"[WARN] Failed via {label}: {exc}")
        if success:
            fetched += 1
        else:
            skipped += 1
        if args.limit and (fetched + skipped) >= args.limit:
            break

    print(f"[INFO] Completed. PDFs added: {fetched}, remaining without PDF: {skipped}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        sys.exit(1)
