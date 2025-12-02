#!/usr/bin/env python3
"""
List Zotero collection hierarchy (optionally with sample items).
--------------------------------------------------------------

Prints the Zotero collection tree as an indented outline. You can
restrict the output to a specific collection (by key or name) and
optionally show the first N top-level items under each collection.
"""
from __future__ import annotations

try:  # auto-load .env via sitecustomize if present
    import sitecustomize  # noqa: F401
except Exception:
    pass

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

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
                "User-Agent": "Zotero-Collection-Tree/0.1",
            }
        )

    def iter_collections(self) -> Iterable[Dict]:
        url = f"{self.base}/collections"
        params = {"format": "json", "include": "data", "limit": 200}
        while url:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            for entry in resp.json():
                yield entry
            url = parse_next_link(resp.headers.get("Link"))
            params = None

    def iter_collection_items(self, collection_key: str, limit: Optional[int]) -> Iterable[Dict]:
        url = f"{self.base}/collections/{collection_key}/items/top"
        params = {"format": "json", "include": "data", "limit": min(limit or 100, 100)}
        remaining = limit
        while url:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            for entry in resp.json():
                data = entry["data"].copy()
                data["key"] = data.get("key") or entry.get("key")
                yield data
                if remaining is not None:
                    remaining -= 1
                    if remaining == 0:
                        return
            url = parse_next_link(resp.headers.get("Link"))
            params = None

    def iter_trash_collections(self) -> Iterable[Dict]:
        url = f"{self.base}/collections/trash"
        params = {"format": "json", "include": "data", "limit": 200}
        while url:
            resp = self.session.get(url, params=params)
            if resp.status_code == 404:
                return
            resp.raise_for_status()
            for entry in resp.json():
                yield entry
            url = parse_next_link(resp.headers.get("Link"))
            params = None

    def trash_collection_keys(self) -> Set[str]:
        return {entry.get("key") for entry in self.iter_trash_collections() if entry.get("key")}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Output Zotero collection hierarchy.")
    ap.add_argument("--root", help="Collection key to use as the tree root.")
    ap.add_argument("--root-name", help="Collection name to use as the tree root (case insensitive).")
    ap.add_argument("--items", type=int, default=0, help="Show up to N items per collection (0 = skip items).")
    ap.add_argument("--max-depth", type=int, default=0, help="Limit depth (0 = no limit).")
    ap.add_argument(
        "--format",
        choices=["text", "markdown"],
        default="text",
        help="Output format. Markdown omits [COL] labels and supports links.",
    )
    ap.add_argument("--output", help="Write result to this file instead of stdout.")
    ap.add_argument("--no-ids", action="store_true", help="Hide collection/item keys in the output.")
    ap.add_argument("--include-deleted", action="store_true", help="Include items/collections in trash (default skips them).")
    return ap.parse_args()


def resolve_root(nodes: Dict[str, Dict], args: argparse.Namespace) -> Optional[str]:
    if args.root:
        if args.root not in nodes:
            raise SystemExit(f"Collection key '{args.root}' not found.")
        return args.root
    if not args.root_name:
        return None
    for key, node in nodes.items():
        name = node["name"]
        if name and (name == args.root_name or name.lower() == args.root_name.lower()):
            print(f"[INFO] Resolved collection '{name}' → {key}", file=sys.stderr)
            return key
    raise SystemExit(f"Collection named '{args.root_name}' not found.")


def build_collection_maps(
    entries: Iterable[Dict],
    include_deleted: bool,
    trash_keys: Set[str],
) -> Tuple[Dict[str, Dict], Dict[Optional[str], List[Dict]]]:
    nodes: Dict[str, Dict] = {}
    children_by_parent: Dict[Optional[str], List[Dict]] = {}
    for entry in entries:
        key = entry.get("key")
        data = entry.get("data", {})
        if not include_deleted and data.get("deleted"):
            continue
        if not include_deleted and trash_keys and key in trash_keys:
            continue
        parent = data.get("parentCollection") or None
        node = {
            "key": key,
            "name": data.get("name") or "(untitled)",
            "parent": parent,
        }
        nodes[key] = node
        children_by_parent.setdefault(parent, []).append(node)
    for child_list in children_by_parent.values():
        child_list.sort(key=lambda n: n["name"].lower())
    return nodes, children_by_parent


def format_collection_label(name: str, key: str, args: argparse.Namespace) -> str:
    label = name
    if not args.no_ids:
        label = f"{label} ({key})"
    if args.format == "markdown":
        return f"**{label}**"
    return f"[COL] {label}"


def format_item_label(item: Dict, args: argparse.Namespace) -> str:
    title = item.get("title") or item.get("shortTitle") or "(untitled)"
    url = item.get("url")
    label = title
    if args.format == "markdown" and url:
        label = f"[{title}]({url})"
    elif url:
        label = f"{title} <{url}>"
    if not args.no_ids:
        label = f"{label} [{item['key']}]"
    return label


def append_items(
    api: ZoteroAPI,
    collection_key: str,
    items_limit: int,
    depth: int,
    lines: List[str],
    args: argparse.Namespace,
) -> None:
    indent = "  " * (depth + 1)
    shown = 0
    for item in api.iter_collection_items(collection_key, items_limit):
        if not args.include_deleted and item.get("deleted"):
            continue
        label = format_item_label(item, args)
        lines.append(f"{indent}- {label}")
        shown += 1
    if shown == 0:
        lines.append(f"{indent}- (no items)")


def walk_tree(
    api: ZoteroAPI,
    children_by_parent: Dict[Optional[str], List[Dict]],
    current_key: Optional[str],
    depth: int,
    lines: List[str],
    args: argparse.Namespace,
) -> None:
    max_depth = args.max_depth if args.max_depth and args.max_depth > 0 else None
    if max_depth is not None and depth >= max_depth:
        return

    for node in children_by_parent.get(current_key, []):
        indent = "  " * depth
        label = format_collection_label(node["name"], node["key"], args)
        lines.append(f"{indent}- {label}")
        if args.items > 0:
            append_items(api, node["key"], args.items, depth, lines, args)
        walk_tree(api, children_by_parent, node["key"], depth + 1, lines, args)


def main() -> None:
    args = parse_args()
    user_id = ensure_env("ZOTERO_USER_ID")
    api_key = ensure_env("ZOTERO_API_KEY")
    api = ZoteroAPI(user_id, api_key)

    entries = list(api.iter_collections())
    if not entries:
        print("[INFO] No collections found.")
        return

    trash_collections: Set[str] = set()
    if not args.include_deleted:
        trash_collections = api.trash_collection_keys()

    nodes, children_by_parent = build_collection_maps(entries, args.include_deleted, trash_collections)

    root_key = resolve_root(nodes, args)
    if root_key:
        root = nodes[root_key]
        label = format_collection_label(root["name"], root["key"], args)
        lines = [f"- {label}"]
        if args.items > 0:
            append_items(api, root["key"], args.items, 0, lines, args)
        walk_tree(api, children_by_parent, root_key, 1, lines, args)
    else:
        roots = children_by_parent.get(None, [])
        if not roots:
            print("[WARN] No top-level collections found.", file=sys.stderr)
            return
        lines = []
        walk_tree(api, children_by_parent, None, 0, lines, args)

    output_text = "\n".join(lines)
    if args.output:
        Path(args.output).write_text(output_text + "\n", encoding="utf-8")
        print(f"[INFO] Wrote {len(lines)} lines to {args.output}", file=sys.stderr)
    else:
        print(output_text)


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        print(f"[ERR] HTTP error: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # pragma: no cover
        print(f"[ERR] {exc}", file=sys.stderr)
        sys.exit(1)
