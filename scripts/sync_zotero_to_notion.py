#!/usr/bin/env python3
"""
Sync Zotero items to a Notion database.
--------------------------------------

Features
- Reads env: ZOTERO_USER_ID, ZOTERO_API_KEY, NOTION_API_KEY, NOTION_DATABASE_ID
- Optional filters: --collection-name/--collection, --tag, --since-days, --limit
- Dedupes by Notion "Zotero Key" (if present) else Title equality
- Maps Zotero fields to Notion properties dynamically by inspecting DB schema:
  - title: first property with type 'title' (fallback to 'Paper Title')
  - Authors: multi_select (names only)
  - Year: number (fallback to Date if available)
  - Abstract: rich_text
  - Tags: multi_select (auto from tag.json sample_keywords)
  - URL: url
  - DOI: rich_text or url if property exists
  - Zotero Key: rich_text (recommended for dedupe)
  - PDF: url (arXiv PDF or Unpaywall if available via DOI)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from utils_sources import fetch_unpaywall_pdf


def ensure_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"Missing required environment variable: {name}")
    return val


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
        self.session.headers.update({"Zotero-API-Key": api_key, "User-Agent": "Zotero-Notion-Sync/0.1"})

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

    def iter_items(self, collection: Optional[str], tag: Optional[str], limit: int) -> Iterable[Dict[str, Any]]:
        url = f"{self.base}/items"
        if collection:
            url = f"{self.base}/collections/{collection}/items"
        params = {"format": "json", "include": "data", "limit": 100}
        if tag:
            params["tag"] = tag
        remaining = limit if (limit and limit > 0) else None
        while url:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            for entry in resp.json():
                yield entry
                if remaining is not None:
                    remaining -= 1
                    if remaining == 0:
                        return
            url = parse_next_link(resp.headers.get("Link"))
            params = None


class NotionAPI:
    def __init__(self, api_key: str, database_id: str) -> None:
        self.api_key = api_key
        self.database_id = database_id
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28",
                "User-Agent": "Zotero-Notion-Sync/0.1",
            }
        )

    def get_database(self) -> Dict[str, Any]:
        url = f"https://api.notion.com/v1/databases/{self.database_id}"
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json()

    def query_by_title(self, title_prop: str, title: str) -> Optional[str]:
        url = f"https://api.notion.com/v1/databases/{self.database_id}/query"
        data = {"filter": {"property": title_prop, "title": {"equals": title}}}
        resp = self.session.post(url, json=data)
        resp.raise_for_status()
        js = resp.json()
        if js.get("results"):
            return js["results"][0]["id"]
        return None

    def query_by_text(self, prop_name: str, text: str) -> Optional[str]:
        url = f"https://api.notion.com/v1/databases/{self.database_id}/query"
        data = {"filter": {"property": prop_name, "rich_text": {"equals": text}}}
        resp = self.session.post(url, json=data)
        resp.raise_for_status()
        js = resp.json()
        if js.get("results"):
            return js["results"][0]["id"]
        return None

    def create_page(self, props: Dict[str, Any]) -> str:
        url = "https://api.notion.com/v1/pages"
        data = {"parent": {"database_id": self.database_id}, "properties": props}
        resp = self.session.post(url, json=data)
        if resp.status_code == 429:
            time.sleep(1.0)
            resp = self.session.post(url, json=data)
        resp.raise_for_status()
        return resp.json()["id"]

    def update_page(self, page_id: str, props: Dict[str, Any]) -> None:
        url = f"https://api.notion.com/v1/pages/{page_id}"
        data = {"properties": props}
        resp = self.session.patch(url, json=data)
        if resp.status_code == 429:
            time.sleep(1.0)
            resp = self.session.patch(url, json=data)
        resp.raise_for_status()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Sync Zotero items to Notion database")
    ap.add_argument("--collection", help="Zotero collection key.")
    ap.add_argument("--collection-name", help="Zotero collection name (resolve to key).")
    ap.add_argument("--tag", help="Only sync items with this tag.")
    ap.add_argument("--since-days", type=int, default=0, help="Only sync items modified within last N days.")
    ap.add_argument("--limit", type=int, default=200, help="Max number of items to consider (<=0 means unlimited).")
    ap.add_argument("--tag-file", default="tag.json", help="Tag schema JSON path (for auto Tags).")
    ap.add_argument("--dry-run", action="store_true", help="Preview actions without writing to Notion.")
    return ap.parse_args()


def load_tag_schema(path: str) -> Dict[str, Any]:
    if not path:
        return {}
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def build_keyword_maps(schema: Dict[str, Any]) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    key_to_keywords = {k: (v.get("sample_keywords") or []) for k, v in schema.items()}
    key_to_label = {k: (v.get("label") or k) for k, v in schema.items()}
    return key_to_keywords, key_to_label


def match_tags(title: str, abstract: str, key_to_keywords: Dict[str, List[str]], key_to_label: Dict[str, str]) -> List[str]:
    text = f"{(title or '').lower()} {(abstract or '').lower()}"
    tags: List[str] = []
    for key, keywords in key_to_keywords.items():
        for kw in keywords:
            if kw and kw.lower() in text:
                label = key_to_label.get(key) or key
                tags.append(label)
                break
    return tags


def resolve_collection_key(zot: ZoteroAPI, name: Optional[str], key: Optional[str]) -> Optional[str]:
    if key:
        return key
    if not name:
        return None
    collections = zot.list_collections()
    for cname, info in collections.items():
        if cname and (cname == name or cname.lower() == name.lower()):
            print(f"[INFO] Resolved collection '{cname}' → {info['key']}")
            return info["key"]
    raise SystemExit(f"Collection named '{name}' not found.")


def build_property_mapping(db: Dict[str, Any]) -> Dict[str, str]:
    props = db.get("properties", {})
    mapping: Dict[str, str] = {}
    # title
    for pname, pdef in props.items():
        if pdef.get("type") == "title":
            mapping["title"] = pname
            break
    mapping.setdefault("title", "Paper Title")
    # common fields
    def find_prop(target_type: str, candidates: List[str]) -> Optional[str]:
        for cname in candidates:
            if cname in props and props[cname].get("type") == target_type:
                return cname
        # fallback: first prop with target_type
        for pname, pdef in props.items():
            if pdef.get("type") == target_type:
                return pname
        return None

    mapping_optional = {
        "authors": find_prop("multi_select", ["Authors", "Author", "作者"]),
        "year": find_prop("number", ["Year", "年份"]),
        "abstract": find_prop("rich_text", ["Abstract", "摘要"]),
        "tags": find_prop("multi_select", ["Tags", "标签"]),
        "url": find_prop("url", ["URL", "Link"]),
        "doi": find_prop("rich_text", ["DOI"]),
        "zotero_key": find_prop("rich_text", ["Zotero Key"]),
        "pdf": find_prop("url", ["PDF", "PDF URL"]),
    }
    for k, v in mapping_optional.items():
        if v:
            mapping[k] = v
    return mapping


def make_properties(item: Dict[str, Any], mapping: Dict[str, str], labels: List[str], unpaywall_email: Optional[str]) -> Dict[str, Any]:
    data = item.get("data", {})
    title = data.get("title") or ""
    authors = [c.get("lastName") or c.get("name") for c in data.get("creators") or [] if (c.get("lastName") or c.get("name"))]
    date = data.get("date") or data.get("year") or ""
    year = date[:4] if date else None
    abstract = data.get("abstractNote") or ""
    url = data.get("url") or ""
    doi = data.get("DOI") or data.get("doi") or ""
    zot_key = data.get("key") or item.get("key") or ""
    pdf_url = None
    # arXiv PDF shortcut
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([A-Za-z0-9.\-]+)", url)
    if m:
        pdf_url = f"https://arxiv.org/pdf/{m.group(1)}.pdf"
    if not pdf_url and doi:
        pdf_url = fetch_unpaywall_pdf(doi, unpaywall_email)

    props: Dict[str, Any] = {}

    def set_title(prop: str, value: str) -> None:
        props[prop] = {"title": [{"text": {"content": value}}]}

    def set_rich_text(prop: str, value: str) -> None:
        if value is None:
            return
        props[prop] = {"rich_text": [{"text": {"content": value}}]}

    def set_multi_select(prop: str, values: List[str]) -> None:
        props[prop] = {"multi_select": [{"name": v} for v in values if v]}

    def set_number(prop: str, value: Optional[int]) -> None:
        props[prop] = {"number": value if value is not None else None}

    def set_url(prop: str, value: Optional[str]) -> None:
        if value:
            props[prop] = {"url": value}

    # required: title
    set_title(mapping["title"], title)
    # optional fields
    if mapping.get("authors"):
        set_multi_select(mapping["authors"], authors)
    if mapping.get("year"):
        set_number(mapping["year"], int(year) if (year and year.isdigit()) else None)
    if mapping.get("abstract"):
        set_rich_text(mapping["abstract"], abstract)
    if mapping.get("tags"):
        set_multi_select(mapping["tags"], labels)
    if mapping.get("url"):
        set_url(mapping["url"], url or None)
    if mapping.get("doi") and doi:
        set_rich_text(mapping["doi"], doi)
    if mapping.get("zotero_key") and zot_key:
        set_rich_text(mapping["zotero_key"], zot_key)
    if mapping.get("pdf") and pdf_url:
        set_url(mapping["pdf"], pdf_url)

    return props


def main() -> None:
    args = parse_args()
    user_id = ensure_env("ZOTERO_USER_ID")
    api_key = ensure_env("ZOTERO_API_KEY")
    notion_api_key = ensure_env("NOTION_API_KEY")
    notion_db = ensure_env("NOTION_DATABASE_ID")

    zot = ZoteroAPI(user_id, api_key)
    notion = NotionAPI(notion_api_key, notion_db)

    collection_key = resolve_collection_key(zot, args.collection_name, args.collection)
    limit = args.limit if args.limit and args.limit > 0 else None
    since_days = args.since_days if args.since_days and args.since_days > 0 else None

    # load tag schema for auto tags
    schema = load_tag_schema(args.tag_file)
    key_to_keywords, key_to_label = build_keyword_maps(schema)

    # Notion DB schema and property mapping
    db = notion.get_database()
    mapping = build_property_mapping(db)
    title_prop = mapping.get("title", "Paper Title")
    zotero_key_prop = mapping.get("zotero_key")  # may be None

    unpaywall_email = os.environ.get("UNPAYWALL_EMAIL")

    # fetch and filter items
    scanned = 0
    created = 0
    updated = 0

    cutoff = None
    if since_days:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)

    for entry in zot.iter_items(collection_key, args.tag, limit or 1000000):
        data = entry.get("data", {})
        if data.get("itemType") in {"note", "attachment"}:
            continue
        if cutoff:
            dm = data.get("dateModified")
            if dm:
                try:
                    ts = dt.datetime.fromisoformat(dm.replace("Z", "+00:00"))
                    if ts < cutoff:
                        continue
                except Exception:
                    pass
        scanned += 1

        title = data.get("title") or ""
        abstract = data.get("abstractNote") or ""
        labels = match_tags(title, abstract, key_to_keywords, key_to_label)

        props = make_properties(entry, mapping, labels, unpaywall_email)

        # Dedup & upsert
        page_id: Optional[str] = None
        if zotero_key_prop and (data.get("key") or entry.get("key")):
            try:
                page_id = notion.query_by_text(zotero_key_prop, data.get("key") or entry.get("key"))
            except requests.HTTPError:
                page_id = None
        if not page_id:
            page_id = notion.query_by_title(title_prop, title)

        if args.dry_run:
            action = "UPDATE" if page_id else "CREATE"
            print(f"[DRY] {action} '{title[:80]}' → Notion")
            continue

        try:
            if page_id:
                notion.update_page(page_id, props)
                updated += 1
                print(f"[UPD] {title[:80]}")
            else:
                notion.create_page(props)
                created += 1
                print(f"[ADD] {title[:80]}")
        except requests.HTTPError as exc:
            print(f"[ERR] Notion API error for '{title[:80]}': {exc}")

    print(f"[INFO] Completed. Scanned={scanned} created={created} updated={updated}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        sys.exit(1)

