#!/usr/bin/env python3
"""
Import all RIS files in a folder into Zotero via Web API.
--------------------------------------------------------

Usage:
  source ./exp
  python scripts/import_ris_folder.py --dir ./zotero_import \
    --collection-name "Imported (RIS)" --create-collection --dedupe-by-url

Notes:
  - Requires environment variables ZOTERO_USER_ID and ZOTERO_API_KEY.
  - This script parses minimal RIS (TI/UR/AU/PY/KW). Items are created as
    'webpage' with title/url/authors/date/tags and optionally placed into a collection.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests


def ensure_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"Missing required environment variable: {name}")
    return val


class ZoteroAPI:
    def __init__(self, user_id: str, api_key: str) -> None:
        self.base = f"https://api.zotero.org/users/{user_id}"
        self.session = requests.Session()
        self.session.headers.update({"Zotero-API-Key": api_key, "User-Agent": "RIS-Folder-Importer/0.1"})

    def list_collections(self) -> Dict[str, Dict[str, Optional[str]]]:
        r = self.session.get(
            f"{self.base}/collections", params={"limit": 200, "format": "json", "include": "data"}, timeout=30
        )
        r.raise_for_status()
        out: Dict[str, Dict[str, Optional[str]]] = {}
        for x in r.json():
            data = x.get("data", {})
            out[data.get("name")] = {"key": x.get("key"), "parent": data.get("parentCollection")}
        return out

    def ensure_collection(self, name: str, parent_key: Optional[str] = None) -> str:
        existing = self.list_collections()
        for nm, info in existing.items():
            if nm == name and (info["parent"] or None) == (parent_key or None):
                return info["key"]
        body = [{"name": name, **({"parentCollection": parent_key} if parent_key else {})}]
        r = self.session.post(f"{self.base}/collections", json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data and "key" in data[0]:
            return data[0]["key"]
        if isinstance(data, dict) and "successful" in data and "0" in data["successful"]:
            return data["successful"]["0"]["key"]
        raise RuntimeError("Failed to create collection")

    def find_item_by_url(self, url: str) -> bool:
        q = {"format": "json", "include": "data", "q": url, "qmode": "exact", "limit": "1"}
        r = self.session.get(f"{self.base}/items", params=q, timeout=30)
        r.raise_for_status()
        try:
            arr = r.json()
        except Exception:
            return False
        return isinstance(arr, list) and len(arr) > 0

    def create_items(self, items: List[Dict[str, Any]]) -> None:
        # Batch create; Zotero accepts array of item objects
        if not items:
            return
        r = self.session.post(f"{self.base}/items", json=items, timeout=60)
        if r.status_code == 429:
            # backoff simple retry
            r = self.session.post(f"{self.base}/items", json=items, timeout=60)
        r.raise_for_status()


# ----------------- RIS parsing -----------------
REC_END = re.compile(r"^ER\s*-\s*$")
FIELD_RE = re.compile(r"^(?P<code>[A-Z0-9]{2})\s*-\s*(?P<val>.*)$")


def parse_ris_records(text: str) -> List[Dict[str, List[str]]]:
    records: List[Dict[str, List[str]]] = []
    cur: Dict[str, List[str]] = {}
    for raw in text.splitlines():
        line = raw.rstrip("\n\r")
        if not line.strip():
            continue
        if REC_END.match(line):
            if cur:
                records.append(cur)
                cur = {}
            continue
        m = FIELD_RE.match(line)
        if not m:
            continue
        code = m.group("code").upper()
        val = m.group("val").strip()
        cur.setdefault(code, []).append(val)
    if cur:
        records.append(cur)
    return records


def ris_to_zotero_item(rec: Dict[str, List[str]], collection_key: Optional[str]) -> Dict[str, Any]:
    title = (rec.get("TI") or rec.get("T1") or [None])[0]
    url = (rec.get("UR") or [None])[0]
    year = (rec.get("PY") or [None])[0]
    authors = rec.get("AU") or []
    tags = rec.get("KW") or []
    item: Dict[str, Any] = {
        "itemType": "webpage",
        "title": title or (url or "Untitled"),
        "url": url or "",
        "creators": [author_to_creator(a) for a in authors if a],
        "date": year or "",
        "tags": [{"tag": t} for t in tags],
    }
    if collection_key:
        item["collections"] = [collection_key]
    return item


def author_to_creator(author: str) -> Dict[str, str]:
    # Try "Last, First" else split last space
    a = author.strip()
    if "," in a:
        last, first = [x.strip() for x in a.split(",", 1)]
        return {"creatorType": "author", "firstName": first, "lastName": last}
    parts = a.split()
    if len(parts) == 1:
        return {"creatorType": "author", "firstName": "", "lastName": parts[0]}
    return {"creatorType": "author", "firstName": " ".join(parts[:-1]), "lastName": parts[-1]}


def collect_ris_files(root: Path) -> List[Path]:
    return sorted([p for p in root.glob("**/*.ris") if p.is_file()])


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Import all RIS files in a folder into Zotero.")
    ap.add_argument("--dir", default="./zotero_import", help="Directory containing .ris files (recursive).")
    ap.add_argument("--collection", help="Place items under this collection key (all RIS merged).")
    ap.add_argument("--collection-name", help="Resolve and/or create one collection by name (all RIS merged).")
    ap.add_argument(
        "--create-collection",
        action="store_true",
        help="Create collection if not exists (with --collection-name)",
    )
    ap.add_argument("--dedupe-by-url", action="store_true", help="Skip items whose URL already exists in library.")
    ap.add_argument("--batch-size", type=int, default=25, help="Batch size when posting items (default 25).")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(args.dir).expanduser()
    if not root.exists():
        raise SystemExit(f"RIS directory not found: {root}")

    user_id = ensure_env("ZOTERO_USER_ID")
    api_key = ensure_env("ZOTERO_API_KEY")
    api = ZoteroAPI(user_id, api_key)

    # Global single collection (merging all RIS) if provided; otherwise default to per-file collection
    global_collection_key: Optional[str] = args.collection
    if args.collection_name:
        if args.create_collection:
            global_collection_key = api.ensure_collection(args.collection_name)
        else:
            cols = api.list_collections()
            info = cols.get(args.collection_name)
            if not info:
                raise SystemExit(
                    f"Collection named '{args.collection_name}' not found. Use --create-collection to create it."
                )
            global_collection_key = info["key"]

    files = collect_ris_files(root)
    if not files:
        print(f"[INFO] No .ris files under {root}")
        return
    print(f"[INFO] Found {len(files)} .ris files under {root}")

    total, skipped, created = 0, 0, 0
    batch: List[Dict[str, Any]] = []

    def flush_batch():
        nonlocal created, batch
        if not batch:
            return
        try:
            api.create_items(batch)
            created += len(batch)
        finally:
            batch = []

    for f in files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        records = parse_ris_records(text)
        # Determine target collection for this file
        if global_collection_key:
            target_collection = global_collection_key
        else:
            # Default: create/ensure a collection named by RIS filename (without extension)
            target_collection = api.ensure_collection(f.stem)
        for rec in records:
            total += 1
            item = ris_to_zotero_item(rec, target_collection)
            if args.dedupe_by_url and item.get("url"):
                try:
                    if api.find_item_by_url(item["url"]):
                        skipped += 1
                        continue
                except Exception:
                    # If query fails, do not block importing
                    pass
            batch.append(item)
            if len(batch) >= max(1, args.batch_size):
                flush_batch()
    flush_batch()
    print(f"[INFO] Done. Records scanned: {total}, created: {created}, skipped: {skipped}.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        print(f"[ERR] HTTP error: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        sys.exit(1)
