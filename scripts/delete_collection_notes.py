#!/usr/bin/env python3
"""
Delete all notes within a Zotero collection.
-------------------------------------------

Usage examples:
  # delete notes in a collection identified by key
  python delete_collection_notes.py --collection 5BWLSF7H

  # or lookup collection by name, preview deletions first
  python delete_collection_notes.py --collection-name "Surveys" --dry-run

Environment variables (required):
  ZOTERO_USER_ID   - numeric user id
  ZOTERO_API_KEY   - API key with write access
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, Iterable, List, Optional, Set

import requests


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
                "User-Agent": "Zotero-Delete-Notes/0.1",
            }
        )

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

    def iter_collection_parents(self, collection_key: str, limit: int) -> Iterable[Dict]:
        remaining = limit
        url = f"{self.base}/collections/{collection_key}/items/top"
        params = {"format": "json", "include": "data", "limit": 100}
        while url and (remaining > 0):
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            for item in data:
                yield item["data"]
                remaining -= 1
                if remaining == 0:
                    break
            if remaining == 0:
                break
            url = parse_next_link(resp.headers.get("Link"))
            params = None  # subsequent URLs already have params

    def fetch_children(self, parent_key: str) -> List[Dict]:
        resp = self.session.get(
            f"{self.base}/items/{parent_key}/children",
            params={"format": "json", "include": "data", "limit": 100},
        )
        resp.raise_for_status()
        return [entry["data"] for entry in resp.json()]

    def list_collection_notes(self, collection_key: str) -> List[Dict]:
        notes: List[Dict] = []
        url = f"{self.base}/collections/{collection_key}/items"
        params = {"format": "json", "include": "data", "limit": 100, "itemType": "note"}
        while url:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            notes.extend(entry["data"] for entry in resp.json())
            url = parse_next_link(resp.headers.get("Link"))
            params = None
        return notes

    def delete_item(self, key: str, version: int) -> None:
        headers = {"If-Unmodified-Since-Version": str(version)}
        resp = self.session.delete(f"{self.base}/items/{key}", headers=headers)
        resp.raise_for_status()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Delete all notes within a Zotero collection.")
    ap.add_argument("--collection", help="Zotero collection key (e.g., ABC12345).")
    ap.add_argument("--collection-name", help="Zotero collection name (will be resolved to a key).")
    ap.add_argument("--limit", type=int, default=0, help="Maximum number of parent items to scan (<=0 means no limit).")
    ap.add_argument("--dry-run", action="store_true", help="Preview deletions without performing them.")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    user_id = ensure_env("ZOTERO_USER_ID")
    api_key = ensure_env("ZOTERO_API_KEY")
    api = ZoteroAPI(user_id, api_key)

    collection_key = args.collection
    if args.collection_name:
        collections = api.list_collections()
        target = None
        for name, info in collections.items():
            if name == args.collection_name or (name and name.lower() == args.collection_name.lower()):
                target = info
                collection_key = info["key"]
                print(f"[INFO] Resolved collection '{name}' → {collection_key}")
                break
        if not target:
            raise SystemExit(f"Collection named '{args.collection_name}' not found.")
    if not collection_key:
        raise SystemExit("Please specify --collection or --collection-name.")

    parent_limit = args.limit if args.limit and args.limit > 0 else 1_000_000
    processed_parents = 0
    deleted_notes = 0
    seen_note_keys: Set[str] = set()

    for parent in api.iter_collection_parents(collection_key, parent_limit):
        processed_parents += 1
        children = api.fetch_children(parent["key"])
        note_children = [c for c in children if c.get("itemType") == "note"]
        if not note_children:
            continue
        for note in note_children:
            seen_note_keys.add(note["key"])
            if args.dry_run:
                print(f"[DRY] Would delete child note {note['key']} under {parent.get('title') or parent['key']}")
                continue
            api.delete_item(note["key"], note["version"])
            deleted_notes += 1
            print(f"[DEL] Child note {note['key']} removed from {parent.get('title') or parent['key']}")

    top_notes = api.list_collection_notes(collection_key)
    for note in top_notes:
        if note["key"] in seen_note_keys:
            continue  # already deleted as child
        if note.get("parentItem"):
            continue  # child note handled above
        if args.dry_run:
            print(f"[DRY] Would delete top-level note {note['key']}")
            continue
        api.delete_item(note["key"], note["version"])
        deleted_notes += 1
        print(f"[DEL] Top-level note {note['key']} removed.")

    print(f"[INFO] Completed. Parents scanned: {processed_parents}, notes deleted: {deleted_notes}.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        print(f"[ERR] HTTP error: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # pragma: no cover
        print(f"[ERR] {exc}", file=sys.stderr)
        sys.exit(1)
