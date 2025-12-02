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

try:  # auto-load .env via sitecustomize if present
    import sitecustomize  # noqa: F401
except Exception:
    pass

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_utils import AIConfig, create_openai_client, resolve_ai_config
from utils_sources import fetch_unpaywall_pdf

DEFAULT_AI_MODEL = "bot-20251111104927-mf7bx"


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
        url = f"{self.base}/items/top"
        if collection:
            url = f"{self.base}/collections/{collection}/items/top"
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

    def fetch_children(self, parent_key: str) -> List[Dict[str, Any]]:
        url = f"{self.base}/items/{parent_key}/children"
        params = {"format": "json", "include": "data", "limit": 100}
        out: List[Dict[str, Any]] = []
        while url:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            out.extend([e.get("data", {}) for e in resp.json()])
            url = parse_next_link(resp.headers.get("Link"))
            params = None
        return out

    def list_child_collections(self, parent_key: str) -> List[Dict[str, Any]]:
        url = f"{self.base}/collections/{parent_key}/collections"
        params = {"format": "json", "include": "data", "limit": 200}
        out: List[Dict[str, Any]] = []
        resp = self.session.get(url, params=params)
        resp.raise_for_status()
        for entry in resp.json():
            data = entry.get("data", {})
            out.append({"key": entry.get("key"), "name": data.get("name"), "parent": data.get("parentCollection")})
        return out


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

    def create_page(self, props: Dict[str, Any], debug: bool = False) -> str:
        url = "https://api.notion.com/v1/pages"
        data = {"parent": {"database_id": self.database_id}, "properties": props}
        resp = self.session.post(url, json=data)
        if resp.status_code == 429:
            time.sleep(1.0)
            resp = self.session.post(url, json=data)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = {"text": resp.text}
            if debug:
                print(f"[DEBUG] Notion create payload: {json.dumps(data)[:2000]}...")
                print(f"[DEBUG] Notion create error: {json.dumps(body)[:2000]}...")
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.json()["id"]

    def update_page(self, page_id: str, props: Dict[str, Any], debug: bool = False) -> None:
        url = f"https://api.notion.com/v1/pages/{page_id}"
        data = {"properties": props}
        resp = self.session.patch(url, json=data)
        if resp.status_code == 429:
            time.sleep(1.0)
            resp = self.session.patch(url, json=data)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = {"text": resp.text}
            if debug:
                print(f"[DEBUG] Notion update payload: {json.dumps(data)[:2000]}...")
                print(f"[DEBUG] Notion update error: {json.dumps(body)[:2000]}...")
            resp.raise_for_status()
        resp.raise_for_status()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Sync Zotero items to Notion database")
    ap.add_argument("--collection", help="Zotero collection key.")
    ap.add_argument("--collection-name", help="Zotero collection name (resolve to key).")
    ap.add_argument("--tag", help="Only sync items with this tag.")
    ap.add_argument("--since-days", type=int, default=0, help="Deprecated. Prefer --since-hours for finer control.")
    ap.add_argument("--since-hours", type=float, default=24.0, help="Only sync items modified within last N hours (default 24).")
    ap.add_argument("--limit", type=int, default=200, help="Max number of items to consider (<=0 means unlimited).")
    ap.add_argument("--tag-file", default="tag.json", help="Tag schema JSON path (for auto Tags).")
    ap.add_argument("--dry-run", action="store_true", help="Preview actions without writing to Notion.")
    ap.add_argument("--skip-untitled", action="store_true", help="Skip items that have no usable title (after fallbacks).")
    ap.add_argument("--debug", action="store_true", help="Print debug info (property mapping, payload) on errors.")
    ap.add_argument(
        "--enrich-with-doubao",
        action="store_true",
        help="Use an AI helper (Doubao/Qwen/OpenAI-compatible) to extract Key Contributions/Limitations strictly from title/abstract/notes.",
    )
    ap.add_argument("--doubao-max-chars", type=int, default=4000, help="Max characters to send to Doubao for extraction.")
    ap.add_argument("--ai-provider", help="AI provider used for enrichment (doubao, qwen, dashscope, openai, etc.).")
    ap.add_argument("--ai-base-url", help="Override AI base URL for enrichment.")
    ap.add_argument("--ai-api-key", help="Override AI API key for enrichment.")
    ap.add_argument("--ai-model", help="Override AI model id for enrichment.")
    ap.add_argument("--recursive", action="store_true", help="When a collection is given, include items from all descendant sub-collections.")
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


def iter_collection_tree_items(zot: ZoteroAPI, root_key: str, tag: Optional[str], limit: Optional[int]) -> Iterable[Dict[str, Any]]:
    """Depth-first traversal collecting top-level items from root and all descendants.
    De-duplicates by item key across collections.
    """
    seen_items: set = set()
    stack: List[str] = [root_key]
    yielded = 0
    cap = limit if (limit and limit > 0) else None
    while stack:
        ck = stack.pop()
        # items at this collection
        for entry in zot.iter_items(ck, tag, 0 if cap is None else max(0, cap - yielded) or 0):
            key = entry.get("key")
            if key in seen_items:
                continue
            seen_items.add(key)
            yield entry
            yielded += 1
            if cap is not None and yielded >= cap:
                return
        # push children
        for child in zot.list_child_collections(ck):
            stack.append(child["key"])


def build_property_mapping(db: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    props = db.get("properties", {})
    mapping: Dict[str, Dict[str, str]] = {}
    # 1) Title: prefer explicit 'Paper Title', otherwise first title prop
    if "Paper Title" in props and props["Paper Title"].get("type") == "title":
        mapping["title"] = {"name": "Paper Title", "type": "title"}
    else:
        for pname, pdef in props.items():
            if pdef.get("type") == "title":
                mapping["title"] = {"name": pname, "type": "title"}
                break
    # 2) Strict name-based mapping to避免类型回退导致错位
    exact_candidates = {
        "authors": ["Authors", "作者"],
        "year": ["Year", "年份"],
        "abstract": ["Abstract", "摘要"],
        "tags": ["Tags", "标签"],
        "venue": ["Venue"],
        "ai_notes": ["AI Notes", "AI总结"],
        "url_main": ["Project Page", "URL", "Link"],
        "code": ["Code"],
        "video": ["Video"],
        "datasets": ["Datasets / Benchmarks"],
        "key_contrib": ["Key Contributions"],
        "limitations": ["Limitations"],
        "research_area": ["Research Area"],
        "model_type": ["Model Type"],
        "robot_platform": ["Robot Platform"],
        "my_notes": ["My Notes"],
        "relevance": ["Relevance"],
        "status": ["Status"],
        "related_project": ["Related Project"],
        "zotero_key": ["Zotero Key"],
        "doi": ["DOI"],
    }
    for key, names in exact_candidates.items():
        for name in names:
            if name in props:
                # Each property may exist with CN/EN label; pick the first match to keep type fidelity.
                mapping[key] = {"name": name, "type": props[name].get("type")}
                break
    return mapping


def _derive_title(data: Dict[str, Any]) -> str:
    title = (data.get("title") or "").strip()
    if title:
        return title
    short = (data.get("shortTitle") or "").strip()
    if short:
        return short
    venue = data.get("publicationTitle") or data.get("proceedingsTitle") or data.get("conferenceName")
    year = (data.get("date") or data.get("year") or "")[:4]
    combo = " ".join([s for s in [venue, year] if s])
    if combo.strip():
        return combo.strip()
    url = (data.get("url") or "").strip()
    if url:
        return url
    doi = (data.get("DOI") or data.get("doi") or "").strip()
    if doi:
        return doi
    return "(untitled)"


def _sanitize_text(s: str) -> str:
    if not s:
        return ""
    # remove surrogate code points and control chars Notion dislikes
    s = re.sub(r"[\ud800-\udfff]", "", s)
    s = s.replace("\x00", "")
    try:
        s = s.encode("utf-8", "ignore").decode("utf-8", "ignore")
    except Exception:
        pass
    return s


def extract_ai_notes_text(zot: ZoteroAPI, item: Dict[str, Any]) -> str:
    data = item.get("data", {})
    text = ""
    try:
        children = zot.fetch_children(data.get("key") or item.get("key"))
        for ch in children:
            if ch.get("itemType") == "note":
                note_html = ch.get("note") or ""
                if ("AI总结" in note_html) or ("豆包自动总结" in note_html) or ("AI Summary" in note_html):
                    txt = re.sub(r"<[^>]+>", " ", note_html)
                    text = _sanitize_text(re.sub(r"\s+", " ", txt).strip())
                    break
    except Exception:
        pass
    return text


def build_ai_client(args: argparse.Namespace) -> Optional[Tuple[object, AIConfig]]:
    try:
        config = resolve_ai_config(args.ai_provider, args.ai_model, args.ai_base_url, args.ai_api_key, DEFAULT_AI_MODEL)
    except SystemExit as exc:
        print(f"[WARN] {exc}")
        return None
    try:
        client = create_openai_client(config)
    except Exception as exc:
        print(f"[WARN] Failed to initialize AI client: {exc}")
        return None
    return client, config


def extract_fields_with_ai(
    client,
    model: str,
    title: str,
    abstract: str,
    ai_notes: str,
    max_chars: int = 4000,
) -> Optional[Dict[str, Any]]:
    source_text = (title + "\n\n" + abstract + "\n\n" + ai_notes)[: max_chars]
    sys_prompt = (
        "你是一个严格的信息抽取助手。仅从提供的文本中提取信息，不要编造，不要从常识推断。"
        "没有明确出现的信息必须留空。返回 JSON，字段：key_contributions(string), limitations(string), robot_platform(string[]), model_type(string[]), research_area(string[])."
    )
    user_prompt = (
        "【输入文本】\n" + source_text + "\n\n"
        "【输出格式】仅输出 JSON：{\n  \"key_contributions\": string,\n  \"limitations\": string,\n  \"robot_platform\": string[],\n  \"model_type\": string[],\n  \"research_area\": string[]\n}"
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
        )
        text = resp.choices[0].message.content if resp.choices else ""
        if not text:
            return None
        m = re.search(r"\{[\s\S]*\}", text)
        obj = json.loads(m.group(0) if m else text)
        if not isinstance(obj, dict):
            return None
        out: Dict[str, Any] = {}
        out["key_contributions"] = _sanitize_text(obj.get("key_contributions") or "")
        out["limitations"] = _sanitize_text(obj.get("limitations") or "")
        def _norm_list(x):
            if isinstance(x, list):
                return [str(_sanitize_text(str(i))) for i in x if str(i).strip()]
            if isinstance(x, str) and x.strip():
                return [str(_sanitize_text(x))]
            return []
        out["robot_platform"] = _norm_list(obj.get("robot_platform"))
        out["model_type"] = _norm_list(obj.get("model_type"))
        out["research_area"] = _norm_list(obj.get("research_area"))
        return out
    except Exception:
        return None


def _set_prop_rich_text(props: Dict[str, Any], meta: Dict[str, str], value: str) -> None:
    if not value:
        return
    name = meta["name"]
    typ = meta["type"]
    if typ == "rich_text":
        props[name] = {"rich_text": [{"text": {"content": _sanitize_text(value)[:1999]}}]}
    elif typ == "title":
        props[name] = {"title": [{"text": {"content": _sanitize_text(value)[:1999]}}]}


def _set_prop_list(props: Dict[str, Any], meta: Dict[str, str], values: List[str]) -> None:
    if not values:
        return
    name = meta["name"]
    typ = meta["type"]
    if typ == "multi_select":
        props[name] = {"multi_select": [{"name": v} for v in values if v]}
    elif typ == "select":
        props[name] = {"select": {"name": values[0]}}
    elif typ == "rich_text":
        props[name] = {"rich_text": [{"text": {"content": ", ".join(values)[:1999]}}]}

def make_properties(item: Dict[str, Any], mapping: Dict[str, Dict[str, str]], labels: List[str], unpaywall_email: Optional[str], zot: ZoteroAPI) -> Dict[str, Any]:
    data = item.get("data", {})
    title = _derive_title(data)
    authors = [c.get("lastName") or c.get("name") for c in data.get("creators") or [] if (c.get("lastName") or c.get("name"))]
    date = data.get("date") or data.get("year") or ""
    year = date[:4] if date else None
    abstract = _sanitize_text(data.get("abstractNote") or "")
    url = data.get("url") or ""
    doi = data.get("DOI") or data.get("doi") or ""
    zot_key = data.get("key") or item.get("key") or ""
    # arXiv/links extraction for Code/Video/Project Page — use a best-effort regex pass.
    github = None
    video = None
    # extract links from url/abstract
    for m in re.finditer(r"https?://\S+", (url + "\n" + abstract)):
        link = m.group(0).rstrip(").,;]")
        if (not github) and ("github.com" in link.lower()):
            github = link
        if (not video) and ("youtube.com" in link.lower() or "youtu.be" in link.lower() or "bilibili.com" in link.lower()):
            video = link

    # Venue inference from Zotero fields
    venue = data.get("publicationTitle") or data.get("proceedingsTitle") or data.get("conferenceName") or data.get("series") or data.get("publisher") or ""

    # Extract AI summary from child notes
    ai_notes_text = ""
    try:
        children = zot.fetch_children(data.get("key") or item.get("key"))
        for ch in children:
            if ch.get("itemType") == "note":
                note_html = ch.get("note") or ""
                # heuristic markers we used elsewhere
                if ("AI总结" in note_html) or ("豆包自动总结" in note_html) or ("AI Summary" in note_html):
                    # strip basic HTML tags
                    txt = re.sub(r"<[^>]+>", " ", note_html)
                    ai_notes_text = _sanitize_text(re.sub(r"\s+", " ", txt).strip())
                    break
    except Exception:
        pass

    # Merge Zotero tag names with auto labels (optional) so Notion stays in sync with both manual and inferred tags.
    zot_tags = [t.get("tag") for t in (data.get("tags") or []) if t.get("tag")]
    all_labels = list({*labels, *zot_tags}) if labels or zot_tags else []

    props: Dict[str, Any] = {}

    def set_title(prop: str, value: str) -> None:
        props[prop] = {"title": [{"text": {"content": value}}]}

    def set_rich_text(prop: str, value: str) -> None:
        if value is None:
            return
        value = _sanitize_text(value)
        props[prop] = {"rich_text": [{"text": {"content": value[:1999]}}]}

    def set_multi_select(prop: str, values: List[str]) -> None:
        props[prop] = {"multi_select": [{"name": v} for v in values if v]}

    def set_number(prop: str, value: Optional[int]) -> None:
        props[prop] = {"number": value if value is not None else None}

    def set_url(prop: str, value: Optional[str]) -> None:
        if value:
            props[prop] = {"url": value}

    # required: title
    set_title(mapping["title"]["name"], title)
    # optional fields
    if mapping.get("authors"):
        prop = mapping["authors"]["name"]
        ptype = mapping["authors"]["type"]
        if ptype == "multi_select":
            set_multi_select(prop, authors)
        elif ptype == "rich_text":
            set_rich_text(prop, ", ".join(authors))
        elif ptype == "people":
            # Cannot create arbitrary Notion users; skip safely
            pass
    if mapping.get("year"):
        prop = mapping["year"]["name"]
        ptype = mapping["year"]["type"]
        if ptype == "number":
            set_number(prop, int(year) if (year and year.isdigit()) else None)
        elif ptype == "rich_text":
            set_rich_text(prop, year or "")
        elif ptype == "select":
            if year:
                props[prop] = {"select": {"name": year}}
    if mapping.get("abstract"):
        set_rich_text(mapping["abstract"]["name"], abstract)
    if mapping.get("tags"):
        prop = mapping["tags"]["name"]
        ptype = mapping["tags"]["type"]
        if ptype == "multi_select":
            set_multi_select(prop, all_labels)
        elif ptype == "rich_text":
            set_rich_text(prop, ", ".join(all_labels))
        elif ptype == "select":
            if all_labels:
                props[prop] = {"select": {"name": all_labels[0]}}
    # Project Page / URL
    if mapping.get("url_main"):
        set_url(mapping["url_main"]["name"], url or None)
    if mapping.get("doi") and doi:
        prop = mapping["doi"]["name"]
        ptype = mapping["doi"]["type"]
        if ptype == "url":
            set_url(prop, doi)
        elif ptype == "rich_text":
            set_rich_text(prop, doi)
        else:
            pass
    if mapping.get("zotero_key") and zot_key:
        zk_prop = mapping["zotero_key"]["name"]
        zk_type = mapping["zotero_key"]["type"]
        if zk_type == "rich_text":
            set_rich_text(zk_prop, zot_key)
        elif zk_type == "url":
            set_url(zk_prop, zot_key)
        elif zk_type == "title":
            set_title(zk_prop, zot_key)
    # Code / Video (best-effort extraction)
    if mapping.get("code") and github:
        set_url(mapping["code"]["name"], github)
    if mapping.get("video") and video:
        set_url(mapping["video"]["name"], video)
    if mapping.get("venue") and venue:
        prop = mapping["venue"]["name"]
        ptype = mapping["venue"]["type"]
        if ptype == "multi_select":
            set_multi_select(prop, [venue])
        elif ptype == "select":
            props[prop] = {"select": {"name": venue}}
        elif ptype == "rich_text":
            set_rich_text(prop, venue)
        else:
            # Venue shouldn't be a URL; avoid mis-mapping
            pass
    if mapping.get("ai_notes") and ai_notes_text:
        set_rich_text(mapping["ai_notes"]["name"], ai_notes_text)

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
    since_hours = args.since_hours if args.since_hours and args.since_hours > 0 else None

    # load tag schema for auto tags
    schema = load_tag_schema(args.tag_file)
    key_to_keywords, key_to_label = build_keyword_maps(schema)

    # Notion DB schema and property mapping
    db = notion.get_database()
    mapping = build_property_mapping(db)
    title_prop = mapping.get("title", {"name": "Paper Title"})["name"]
    zotero_key_prop = mapping.get("zotero_key", {}).get("name")  # may be None

    unpaywall_email = os.environ.get("UNPAYWALL_EMAIL")

    ai_client = None
    ai_config: Optional[AIConfig] = None
    if args.enrich_with_doubao:
        client_bundle = build_ai_client(args)
        if client_bundle:
            ai_client, ai_config = client_bundle

    # fetch and filter items
    scanned = 0
    created = 0
    updated = 0

    cutoff = None
    if since_hours:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=since_hours)
    elif since_days:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)

    # Choose iterator: recursive collection tree or flat
    iterator: Iterable[Dict[str, Any]]
    if collection_key and args.recursive:
        iterator = iter_collection_tree_items(zot, collection_key, args.tag, limit or 1000000)
    else:
        iterator = zot.iter_items(collection_key, args.tag, limit or 1000000)

    for entry in iterator:
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

        display_title = _derive_title(data)
        if args.skip_untitled and display_title == "(untitled)":
            print("[SKIP] Untitled item (no title/url/doi)")
            continue

        title = data.get("title") or ""
        abstract = data.get("abstractNote") or ""
        labels = match_tags(title, abstract, key_to_keywords, key_to_label)

        props = make_properties(entry, mapping, labels, unpaywall_email, zot)

        # Optional structured enrichment via AI, strictly from provided text
        if args.enrich_with_doubao:
            if not ai_client or not ai_config:
                if args.debug:
                    print("[DEBUG] AI enrichment client not available; skip enrichment")
            else:
                ai_text = extract_ai_notes_text(zot, entry)
                ex = extract_fields_with_ai(ai_client, ai_config.model, title, abstract, ai_text, args.doubao_max_chars)
                if ex:
                    if ex.get("key_contributions") and mapping.get("key_contrib"):
                        _set_prop_rich_text(props, mapping["key_contrib"], ex["key_contributions"])
                    if ex.get("limitations") and mapping.get("limitations"):
                        _set_prop_rich_text(props, mapping["limitations"], ex["limitations"])
                    if mapping.get("robot_platform"):
                        _set_prop_list(props, mapping["robot_platform"], ex.get("robot_platform") or [])
                    if mapping.get("model_type"):
                        _set_prop_list(props, mapping["model_type"], ex.get("model_type") or [])
                    if mapping.get("research_area"):
                        _set_prop_list(props, mapping["research_area"], ex.get("research_area") or [])

        # Dedup & upsert
        page_id: Optional[str] = None
        if zotero_key_prop and (data.get("key") or entry.get("key")):
            try:
                page_id = notion.query_by_text(zotero_key_prop, data.get("key") or entry.get("key"))
            except requests.HTTPError:
                page_id = None
        if not page_id:
            page_id = notion.query_by_title(title_prop, display_title)

        if args.dry_run:
            action = "UPDATE" if page_id else "CREATE"
            print(f"[DRY] {action} '{title[:80]}' → Notion")
            continue

        try:
            if page_id:
                notion.update_page(page_id, props, debug=args.debug)
                updated += 1
                print(f"[UPD] {display_title[:80]}")
            else:
                notion.create_page(props, debug=args.debug)
                created += 1
                print(f"[ADD] {display_title[:80]}")
        except requests.HTTPError as exc:
            print(f"[ERR] Notion API error for '{display_title[:80]}': {exc}")
            if args.debug:
                try:
                    print(f"[DEBUG] Mapping used: {json.dumps(mapping)}")
                except Exception:
                    pass

    print(f"[INFO] Completed. Scanned={scanned} created={created} updated={updated}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        sys.exit(1)
