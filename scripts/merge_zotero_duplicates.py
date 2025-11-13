#!/usr/bin/env python3
"""
Merge duplicate Zotero items while preserving attachments/notes.
---------------------------------------------------------------

The script scans Zotero items (optionally scoped to a collection/tag),
groups likely-duplicate parents (DOI → URL → normalized title/year),
keeps the best candidate (preferring PDF/attachments, then recency),
moves unique child attachments/notes to the survivor, merges collections/tags,
and deletes redundant parents. Use --dry-run to preview actions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests


def ensure_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def parse_iso8601(value: Optional[str]) -> dt.datetime:
    if not value:
        return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)


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


def normalize_title(title: str) -> str:
    cleaned = title.lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[^a-z0-9 ]", "", cleaned)
    return cleaned.strip()


def normalize_url(url: str) -> str:
    cleaned = url.strip().lower()
    cleaned = cleaned.split("#", 1)[0]
    cleaned = cleaned.rstrip("/")
    return cleaned


def canonical_group_key(data: Dict[str, Any], mode: str) -> Optional[Tuple[str, str]]:
    def clean_doi(raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        canon = raw.strip().lower()
        canon = canon.replace("https://doi.org/", "").replace("http://doi.org/", "")
        return canon or None

    def title_key() -> Optional[str]:
        title = data.get("title")
        if not title:
            return None
        normalized = normalize_title(title)
        if len(normalized) < 8:
            return None
        year = data.get("year")
        if not year:
            date = data.get("date") or ""
            match = re.search(r"(\d{4})", date)
            year = match.group(1) if match else ""
        return f"{normalized}|{year}" if year else normalized

    doi = clean_doi(data.get("DOI") or data.get("doi"))
    url = data.get("url")
    url = normalize_url(url) if url else None
    title = title_key()

    if mode == "doi":
        return ("doi", doi) if doi else None
    if mode == "url":
        return ("url", url) if url else None
    if mode == "title":
        return ("title", title) if title else None
    if mode != "auto":
        raise SystemExit(f"Unknown group mode: {mode}")

    if doi:
        return ("doi", doi)
    if url:
        return ("url", url)
    if title:
        return ("title", title)
    return None


def child_signature(child: Dict[str, Any]) -> Tuple[str, str, str]:
    data = child["data"]
    item_type = data.get("itemType") or ""
    if item_type == "note":
        note = data.get("note") or ""
        return (item_type, re.sub(r"\s+", " ", note).strip(), "")
    filename = data.get("filename") or data.get("title") or ""
    filename = filename.strip().lower()
    content_type = data.get("contentType") or ""
    link_mode = data.get("linkMode") or ""
    return (item_type, f"{filename}|{content_type}", link_mode)


def has_pdf_attachment(children: Sequence[Dict[str, Any]]) -> bool:
    for child in children:
        data = child["data"]
        if data.get("itemType") != "attachment":
            continue
        filename = (data.get("filename") or "").lower()
        if data.get("contentType") == "application/pdf" or filename.endswith(".pdf"):
            return True
    return False


@dataclass
class ItemBundle:
    entry: Dict[str, Any]
    children: List[Dict[str, Any]]
    attachments: List[Dict[str, Any]]
    notes: List[Dict[str, Any]]
    has_pdf: bool
    modified: dt.datetime
    added: dt.datetime

    def score(self) -> Tuple[int, int, int, dt.datetime, dt.datetime]:
        attachment_count = len(self.attachments)
        note_count = len(self.notes)
        pdf_score = 1 if self.has_pdf else 0
        return (pdf_score, attachment_count, note_count, self.modified, self.added)

    def label(self) -> str:
        data = self.entry["data"]
        title = data.get("title") or "(untitled)"
        return f"{title} [{self.entry['key']}]"


class ZoteroAPI:
    def __init__(self, user_id: str, api_key: str) -> None:
        self.base = f"https://api.zotero.org/users/{user_id}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Zotero-API-Key": api_key,
                "User-Agent": "Zotero-Merge-Duplicates/0.1",
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

    def iter_top_items(
        self,
        collection: Optional[str],
        tag: Optional[str],
        limit: Optional[int],
    ) -> Iterable[Dict[str, Any]]:
        if collection:
            url = f"{self.base}/collections/{collection}/items/top"
        else:
            url = f"{self.base}/items/top"
        params = {"format": "json", "include": "data", "limit": 100}
        if tag:
            params["tag"] = tag
        remaining = limit if limit and limit > 0 else None
        while url:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            entries = resp.json()
            for entry in entries:
                yield {"key": entry["key"], "version": entry["version"], "data": entry["data"]}
                if remaining is not None:
                    remaining -= 1
                    if remaining == 0:
                        return
            url = parse_next_link(resp.headers.get("Link"))
            params = None

    def fetch_children(self, parent_key: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        url = f"{self.base}/items/{parent_key}/children"
        params = {"format": "json", "include": "data", "limit": 100}
        while url:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            for entry in resp.json():
                results.append({"key": entry["key"], "version": entry["version"], "data": entry["data"]})
            url = parse_next_link(resp.headers.get("Link"))
            params = None
        return results

    def delete_item(self, key: str, version: int) -> None:
        headers = {"If-Unmodified-Since-Version": str(version)}
        resp = self.session.delete(f"{self.base}/items/{key}", headers=headers)
        resp.raise_for_status()

    def update_item(self, entry: Dict[str, Any], new_data: Dict[str, Any]) -> None:
        headers = {"If-Unmodified-Since-Version": str(entry["version"])}
        resp = self.session.put(f"{self.base}/items/{entry['key']}", json=new_data, headers=headers)
        resp.raise_for_status()


def build_bundle(api: ZoteroAPI, entry: Dict[str, Any]) -> ItemBundle:
    children = api.fetch_children(entry["key"])
    attachments = [child for child in children if child["data"].get("itemType") == "attachment"]
    notes = [child for child in children if child["data"].get("itemType") == "note"]
    has_pdf = has_pdf_attachment(children)
    modified = parse_iso8601(entry["data"].get("dateModified"))
    added = parse_iso8601(entry["data"].get("dateAdded"))
    return ItemBundle(
        entry=entry,
        children=children,
        attachments=attachments,
        notes=notes,
        has_pdf=has_pdf,
        modified=modified,
        added=added,
    )


def dedupe_children(existing: Iterable[Dict[str, Any]], incoming: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = {child_signature(child) for child in existing}
    unique: List[Dict[str, Any]] = []
    for child in incoming:
        sig = child_signature(child)
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(child)
    return unique


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Merge duplicate Zotero items while preserving attachments.")
    ap.add_argument("--collection", help="Zotero collection key to limit the scan.")
    ap.add_argument("--collection-name", help="Collection name (will be resolved to key).")
    ap.add_argument("--tag", help="Only consider top-level items containing this tag.")
    ap.add_argument("--limit", type=int, default=0, help="Max number of top-level items to scan (<=0 means no limit).")
    ap.add_argument(
        "--group-by",
        choices=["auto", "doi", "url", "title"],
        default="auto",
        help="Grouping heuristic for duplicates. Default auto = DOI → URL → title/year.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Preview actions without modifying Zotero.")
    return ap.parse_args()


def resolve_collection_key(api: ZoteroAPI, args: argparse.Namespace) -> Optional[str]:
    if args.collection:
        return args.collection
    if not args.collection_name:
        return None
    collections = api.list_collections()
    for name, info in collections.items():
        if not name:
            continue
        if name == args.collection_name or name.lower() == args.collection_name.lower():
            print(f"[INFO] Resolved collection '{name}' → {info['key']}")
            return info["key"]
    raise SystemExit(f"Collection named '{args.collection_name}' not found.")


def merge_group(
    api: ZoteroAPI,
    group_key: Tuple[str, str],
    bundles: List[ItemBundle],
    dry_run: bool,
) -> Tuple[int, int]:
    bundles_sorted = sorted(bundles, key=lambda b: b.score(), reverse=True)
    winner = bundles_sorted[0]
    loser_bundles = bundles_sorted[1:]
    print(
        f"[MERGE] {group_key[0]}='{group_key[1]}' → keeping {winner.entry['key']} (attachments={len(winner.attachments)}, "
        f"notes={len(winner.notes)}, pdf={winner.has_pdf}) removing {len(loser_bundles)} duplicates."
    )

    attachments_moved = 0
    notes_moved = 0

    winner_children = list(winner.children)

    union_collections = set(winner.entry["data"].get("collections") or [])
    existing_tags = {tag.get("tag"): tag for tag in winner.entry["data"].get("tags") or [] if tag.get("tag")}

    for loser in loser_bundles:
        union_collections.update(loser.entry["data"].get("collections") or [])
        for tag in loser.entry["data"].get("tags") or []:
            tag_value = tag.get("tag")
            if tag_value and tag_value not in existing_tags:
                existing_tags[tag_value] = tag

        unique_children = dedupe_children(winner_children, loser.attachments + loser.notes)
        if dry_run:
            for child in unique_children:
                print(f"[DRY] Would re-parent {child['data'].get('itemType')} {child['key']} → {winner.entry['key']}")
        else:
            for child in unique_children:
                new_data = child["data"].copy()
                new_data["parentItem"] = winner.entry["key"]
                api.update_item(child, new_data)
                if child["data"].get("itemType") == "note":
                    notes_moved += 1
                else:
                    attachments_moved += 1
            winner_children.extend(unique_children)

        if dry_run:
            print(f"[DRY] Would delete duplicate parent {loser.entry['key']}")
        else:
            api.delete_item(loser.entry["key"], loser.entry["version"])

    if union_collections != set(winner.entry["data"].get("collections") or []) or len(existing_tags) != len(
        winner.entry["data"].get("tags") or []
    ):
        new_data = winner.entry["data"].copy()
        new_data["collections"] = sorted(union_collections)
        new_data["tags"] = list(existing_tags.values())
        if dry_run:
            print(f"[DRY] Would update winner {winner.entry['key']} collections={len(union_collections)} tags={len(existing_tags)}")
        else:
            api.update_item(winner.entry, new_data)

    return attachments_moved, notes_moved


def main() -> None:
    args = parse_args()
    user_id = ensure_env("ZOTERO_USER_ID")
    api_key = ensure_env("ZOTERO_API_KEY")
    api = ZoteroAPI(user_id, api_key)

    collection_key = resolve_collection_key(api, args)
    limit = args.limit if args.limit > 0 else None

    top_items = list(api.iter_top_items(collection_key, args.tag, limit))
    print(f"[INFO] Scanned {len(top_items)} top-level items.")

    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for entry in top_items:
        data = entry["data"]
        if data.get("itemType") in {"note", "attachment"}:
            continue
        key = canonical_group_key(data, args.group_by)
        if not key:
            continue
        groups.setdefault(key, []).append(entry)

    duplicate_groups = {key: entries for key, entries in groups.items() if len(entries) > 1}
    if not duplicate_groups:
        print("[INFO] No duplicates detected with the current heuristic.")
        return

    total_deleted = 0
    total_attachments = 0
    total_notes = 0

    for key, entries in duplicate_groups.items():
        bundles = [build_bundle(api, entry) for entry in entries]
        attachments_moved, notes_moved = merge_group(api, key, bundles, args.dry_run)
        total_attachments += attachments_moved
        total_notes += notes_moved
        total_deleted += len(entries) - 1

    print(
        f"[INFO] Completed. Groups merged: {len(duplicate_groups)}, items removed: {total_deleted}, "
        f"attachments moved: {total_attachments}, notes moved: {total_notes}."
    )


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        print(f"[ERR] HTTP error: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # pragma: no cover
        print(f"[ERR] {exc}", file=sys.stderr)
        sys.exit(1)
