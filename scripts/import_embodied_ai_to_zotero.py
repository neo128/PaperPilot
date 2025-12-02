#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Embodied AI Paper List → Zotero (one-off importer)
--------------------------------------------------
Usage examples:
  # 1) Generate per-category RIS files (recommended for one-off; no API key needed)
  python import_embodied_ai_to_zotero.py --mode ris --out ./zotero_import

  # 2) Directly push to Zotero via Web API (needs env vars)
  export ZOTERO_USER_ID=1234567
  export ZOTERO_API_KEY=your_zotero_api_key
  python import_embodied_ai_to_zotero.py --mode api --create-collections

Notes:
- RIS files can be imported in Zotero via: File → Import → RIS → choose file.
  Tip: select the target Collection first, then import; or tick "Place into new collection" during import.
- API mode will deduplicate by URL before creating items.
"""
try:  # auto-load .env via sitecustomize if present
    import sitecustomize  # noqa: F401
except Exception:
    pass

import argparse
import os
import re
import sys
import time
import json
import pathlib
from typing import List, Dict, Tuple, Optional

try:
    import requests
except ImportError:
    print("This script requires 'requests'. Install via: pip install requests")
    sys.exit(1)

RAW_URL = "https://raw.githubusercontent.com/HCPLab-SYSU/Embodied_AI_Paper_List/main/README.md"
# RAW_URL = "https://github.com/HCPLab-SYSU/Embodied_AI_Paper_List/blob/main/README.md"
# RAW_URL="https://github.com/yueen-ma/Awesome-VLA/blob/main/README.md"

# Regex helpers
H2_RE = re.compile(r'^\s*##\s+(?P<name>.+?)\s*$', re.M)
BULLET_RE = re.compile(r'^\s*[\*\-]\s+(?P<text>.+)$')
PAPER_LINK_RE = re.compile(r'\[\s*Paper\s*\]\((https?://[^\s)]+)\)', re.I)
FALLBACK_LINK_RE = re.compile(r'\((https?://(?:arxiv\.org|openaccess\.thecvf\.com|cvf\.com|ieeexplore\.ieee\.org|dl\.acm\.org|openreview\.net|aclanthology\.org|proceedings|github\.io)[^\s)]+)\)', re.I)
HTML_TAG_RE = re.compile(r'<[^>]+>')
TITLE_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')
YEAR_RE = re.compile(r'\b(19|20|21)\d{2}\b')

DEFAULT_CATEGORIES = [
    "Books & Surveys",
    "Embodied Simulators",
    "Embodied Perception",
    "Embodied Interaction",
    "Embodied Agent",
    "Sim-to-Real Adaptation",
    "Datasets",
]

def fetch_readme_text(url: str = RAW_URL) -> str:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text

def normalize_heading_text(raw: Optional[str]) -> str:
    if not raw:
        return ""
    no_tags = HTML_TAG_RE.sub(" ", raw)
    # Drop emoji / unicode symbols not needed for matching
    ascii_only = no_tags.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_only).strip()

def match_category(raw_heading: Optional[str]) -> Optional[str]:
    normalized = normalize_heading_text(raw_heading).lower()
    for cat in DEFAULT_CATEGORIES:
        if cat.lower() in normalized:
            return cat
    return None

def parse_markdown(md: str) -> List[Dict[str, str]]:
    """
    Return list of dicts: {title, category, url}
    """
    lines = md.splitlines()
    current_cat: Optional[str] = None
    pending: Optional[Dict] = None  # {"title": str, "buffer": List[str]}
    items: List[Dict[str, str]] = []

    def flush_pending():
        nonlocal pending, items
        if not pending or not current_cat:
            pending = None
            return
        buf = "\n".join(pending["buffer"])
        # prefer [Paper] link
        url = None
        m = PAPER_LINK_RE.search(buf)
        if m:
            url = m.group(1).strip()
        if not url:
            m2 = FALLBACK_LINK_RE.search(buf)
            if m2:
                url = m2.group(1).strip()
        title = sanitize_title(pending["title"])
        year = extract_year(pending["title"], pending["buffer"])
        authors = extract_authors(pending["buffer"])
        if url and title:
            items.append({
                "title": title,
                "category": current_cat,
                "url": url,
                "authors": authors,
                "year": year,
            })
        pending = None

    for i, line in enumerate(lines):
        # Category heading
        m_h2 = H2_RE.match(line)
        if m_h2:
            name = m_h2.group("name").strip()
            cat_name = match_category(name)
            if cat_name:
                flush_pending()
                current_cat = cat_name
            continue
        # Bulleted paper entry starts
        m_b = BULLET_RE.match(line)
        if m_b and current_cat:
            # Start a new pending entry
            flush_pending()
            pending = {"title": m_b.group("text").strip(), "buffer": []}
            continue
        # Accumulate lines under the current bullet
        if pending is not None:
            pending["buffer"].append(line)
    flush_pending()

    # Deduplicate by (category, url)
    seen = set()
    uniq = []
    for it in items:
        k = (it["category"], it["url"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    return uniq

def sanitize_title(s: str) -> str:
    if not s:
        return None
    m = TITLE_BOLD_RE.search(s)
    if m:
        core = m.group(1)
    else:
        core = s.split(",")[0]
    # Remove inline links / markdown artifacts
    core = re.sub(r'\[[^\]]+\]\([^)]+\)', '', core)
    core = core.replace("**", "")
    core = re.sub(r'\s+', ' ', core).strip(' -•\t')
    return core if core else None

def extract_year(title_line: Optional[str], buffer_lines: List[str]) -> Optional[str]:
    candidates = []
    if title_line:
        candidates.append(title_line)
    candidates.extend(buffer_lines[:3])  # usually year nearby
    for text in candidates:
        if not text:
            continue
        m = YEAR_RE.search(text)
        if m:
            return m.group(0)
    return None

def extract_authors(buffer_lines: List[str]) -> List[str]:
    block: List[str] = []
    for line in buffer_lines:
        stripped = line.strip()
        if not stripped:
            if block:
                break
            continue
        if stripped.startswith('[[') or stripped.startswith('![') or stripped.startswith('<'):
            if block:
                break
            continue
        if 'http' in stripped:
            if block:
                break
            continue
        block.append(stripped)
    if not block:
        return []
    text = " ".join(block)
    if not re.search(r'(,|\band\b|&|;)', text, re.I):
        return []
    normalized = re.sub(r'\band\b', ',', text, flags=re.I)
    normalized = normalized.replace('&', ',').replace(';', ',')
    parts = [p.strip(' .') for p in normalized.split(',')]
    authors = [p for p in parts if p]
    return authors

# ---------- RIS export ----------
def ris_escape(val: str) -> str:
    return val.replace('\n', ' ')

def make_ris_record(title: str, url: str, tags: List[str], authors: Optional[List[str]]=None, year: Optional[str]=None) -> str:
    parts = []
    parts.append("TY  - ELEC")
    if title:
        parts.append(f"TI  - {ris_escape(title)}")
    if authors:
        for name in authors:
            parts.append(f"AU  - {ris_escape(name)}")
    if year:
        parts.append(f"PY  - {year}")
    if url:
        parts.append(f"UR  - {url}")
    for t in tags:
        parts.append(f"KW  - {ris_escape(t)}")
    parts.append("ER  - ")
    return "\n".join(parts)

def export_ris_per_category(items: List[Dict[str, str]], out_dir: str) -> List[str]:
    out_paths = []
    by_cat: Dict[str, List[Dict[str, str]]] = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)
    pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
    for cat, lst in by_cat.items():
        safe_cat = re.sub(r'[^A-Za-z0-9_]+', '_', cat).strip('_')
        fn = f"Embodied_AI_{safe_cat}.ris"
        path = os.path.join(out_dir, fn)
        with open(path, "w", encoding="utf-8") as f:
            for it in lst:
                ris = make_ris_record(
                    title=it["title"],
                    url=it["url"],
                    tags=["Embodied_AI_Paper_List", cat],
                    authors=it.get("authors"),
                    year=it.get("year"),
                )
                f.write(ris + "\n\n")
        out_paths.append(path)
    return out_paths

# ---------- Zotero API push (optional) ----------
class ZoteroClient:
    def __init__(self, user_id: str, api_key: str):
        self.base = f"https://api.zotero.org/users/{user_id}"
        self.session = requests.Session()
        self.session.headers.update({"Zotero-API-Key": api_key})

    def find_item_by_url(self, url: str) -> bool:
        q = {
            "format": "json",
            "include": "data",
            "q": url,
            "qmode": "exact",
            "limit": "1",
        }
        r = self.session.get(f"{self.base}/items", params=q, timeout=30)
        r.raise_for_status()
        try:
            arr = r.json()
        except Exception:
            return False
        return isinstance(arr, list) and len(arr) > 0

    def list_collections(self) -> Dict[str, Dict]:
        r = self.session.get(f"{self.base}/collections", params={"limit": 200, "format": "json", "include": "data"}, timeout=30)
        r.raise_for_status()
        arr = r.json()
        out = {}
        for x in arr:
            data = x.get("data", {})
            out[data.get("name")] = {"key": x.get("key"), "parent": data.get("parentCollection")}
        return out

    def ensure_collection(self, name: str, parent_key: Optional[str]=None) -> str:
        existing = self.list_collections()
        for nm, info in existing.items():
            if nm == name and (info["parent"] or None) == (parent_key or None):
                return info["key"]
        body = [ {"name": name, **({"parentCollection": parent_key} if parent_key else {})} ]
        r = self.session.post(f"{self.base}/collections", json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        # normalize
        key = None
        if isinstance(data, list) and data and "key" in data[0]:
            key = data[0]["key"]
        elif isinstance(data, dict) and "successful" in data and "0" in data["successful"]:
            key = data["successful"]["0"]["key"]
        if not key:
            raise RuntimeError("Failed to create collection")
        return key

    def create_webpage_item(self, title: str, url: str, tags: List[str], collections: Optional[List[str]]=None):
        body = [{
            "itemType": "webpage",
            "title": title or url,
            "url": url,
            "tags": [{"tag": t} for t in tags],
            **({"collections": collections} if collections else {}),
            "extra": f"Imported from HCPLab-SYSU/Embodied_AI_Paper_List on {time.strftime('%Y-%m-%d %H:%M:%S')}"
        }]
        r = self.session.post(f"{self.base}/items", json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(2); r = self.session.post(f"{self.base}/items", json=body, timeout=30)
        r.raise_for_status()
        return r.json()

def push_via_api(items: List[Dict[str, str]], create_collections: bool=False, parent_name: str="Embodied_AI_Paper_List"):
    user_id = os.environ.get("ZOTERO_USER_ID")
    api_key = os.environ.get("ZOTERO_API_KEY")
    if not user_id or not api_key:
        raise SystemExit("Please set ZOTERO_USER_ID and ZOTERO_API_KEY environment variables for API mode.")
    z = ZoteroClient(user_id, api_key)

    parent_key = None
    if create_collections:
        parent_key = z.ensure_collection(parent_name, None)

    by_cat: Dict[str, List[Dict[str, str]]] = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)

    for cat, lst in by_cat.items():
        sub_key = None
        if create_collections:
            sub_key = z.ensure_collection(cat, parent_key)
        for it in lst:
            if z.find_item_by_url(it["url"]):
                print(f"[SKIP] Exists: {it['url']}")
                continue
            tags = ["Embodied_AI_Paper_List", cat]
            cols = [sub_key] if sub_key else None
            try:
                z.create_webpage_item(it["title"], it["url"], tags, cols)
                print(f"[OK] {it['title']}")
            except Exception as e:
                print(f"[ERR] {it['title']}: {e}")
            time.sleep(0.2)  # be nice to API

def main():
    ap = argparse.ArgumentParser(description="Import Embodied AI Paper List into Zotero (one-off).")
    ap.add_argument("--mode", choices=["ris","api"], default="ris", help="ris: export per-category RIS files; api: push directly via Zotero API")
    ap.add_argument("--out", default="./zotero_import", help="Output directory for RIS files")
    ap.add_argument("--create-collections", action="store_true", help="(API mode) create parent & category collections and place items inside")
    args = ap.parse_args()

    md = fetch_readme_text(RAW_URL)
    items = parse_markdown(md)
    if not items:
        print("No items parsed. README format may have changed.")
        sys.exit(1)

    print(f"Parsed {len(items)} items across categories.")

    if args.mode == "ris":
        paths = export_ris_per_category(items, args.out)
        print("RIS files written:")
        for p in paths:
            print(" -", p)
        print("\nImport tips: In Zotero, select a collection and import the corresponding RIS file; or choose 'Place into new collection' during import.")
    else:
        push_via_api(items, create_collections=args.create_collections)
        print("Done.")

if __name__ == "__main__":
    main()
