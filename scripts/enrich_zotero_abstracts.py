#!/usr/bin/env python3
"""
Fill missing Zotero abstracts using CrossRef → Semantic Scholar → arXiv.
-----------------------------------------------------------------------------

Scans Zotero items (with optional collection/tag filters), finds entries whose
`abstractNote` is empty, attempts to fetch an abstract from CrossRef (DOI),
then Semantic Scholar (DOI or arXiv ID), then arXiv (by arXiv ID), and writes
the first successful hit back to Zotero. Use --dry-run to preview changes.
"""
from __future__ import annotations

try:  # auto-load .env via sitecustomize if present
    import sitecustomize  # noqa: F401
except Exception:
    pass

import argparse
import datetime as dt
import html
import os
import re
import sys
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import requests
import xml.etree.ElementTree as ET

ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


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


def clean_doi(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    doi = raw.strip()
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    doi = doi.replace("doi:", "").strip()
    return doi or None


ARXIV_ID_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)([A-Za-z0-9.\-]+)")


def extract_arxiv_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = ARXIV_ID_RE.search(url)
    if match:
        return match.group(1)
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


def strip_tags(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<\s*/\s*p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class MetaAbstractParser(HTMLParser):
    TARGETS = {"citation_abstract", "dc.description", "dcterms.abstract", "description", "og:description"}

    def __init__(self) -> None:
        super().__init__()
        self.abstract: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:  # type: ignore[override]
        # Many publisher pages expose short abstracts via <meta name="citation_abstract"> etc.
        if tag.lower() != "meta" or self.abstract:
            return
        attr_map = {k.lower(): v for k, v in attrs if v}
        name = attr_map.get("name") or attr_map.get("property")
        if not name:
            return
        if name.lower() not in self.TARGETS:
            return
        content = attr_map.get("content")
        if content:
            self.abstract = content


def extract_meta_abstract(html_text: str) -> Optional[str]:
    parser = MetaAbstractParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # pragma: no cover - fallback to safe default
        return None
    if parser.abstract:
        return strip_tags(parser.abstract)
    return None


def fetch_crossref_abstract(doi: str) -> Optional[str]:
    url = f"https://api.crossref.org/works/{quote(doi)}"
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Zotero-Abstract-Enricher/0.1"})
        resp.raise_for_status()
    except Exception as exc:  # pragma: no cover
        print(f"[WARN] CrossRef fetch failed for {doi}: {exc}")
        return None
    data = resp.json().get("message", {})
    abstract = data.get("abstract")
    if abstract:
        return strip_tags(abstract)
    return None


def fetch_semantic_scholar_abstract(kind: str, identifier: str) -> Optional[str]:
    paper_id = f"{kind}:{identifier}"
    url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}"
    params = {"fields": "abstract"}
    try:
        resp = requests.get(url, params=params, timeout=20, headers={"User-Agent": "Zotero-Abstract-Enricher/0.1"})
        if resp.status_code == 429:
            print(f"[INFO] Semantic Scholar rate limit hit for {paper_id}, skipping further calls.")
            return "RATE_LIMIT"
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    except Exception as exc:  # pragma: no cover
        print(f"[WARN] Semantic Scholar fetch failed for {paper_id}: {exc}")
        return None
    abstract = (resp.json() or {}).get("abstract")
    if abstract:
        return strip_tags(abstract)
    return None


def fetch_arxiv_abstract(arxiv_id: str) -> Optional[str]:
    url = "http://export.arxiv.org/api/query"
    try:
        resp = requests.get(url, params={"id_list": arxiv_id}, timeout=20, headers={"User-Agent": "Zotero-Abstract-Enricher/0.1"})
        resp.raise_for_status()
    except Exception as exc:  # pragma: no cover
        print(f"[WARN] arXiv fetch failed for {arxiv_id}: {exc}")
        return None
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None
    entry = root.find(f"{ATOM_NS}entry")
    if entry is None:
        return None
    summary = entry.findtext(f"{ATOM_NS}summary")
    if summary:
        return strip_tags(summary)
    return None


def fetch_url_abstract(url: Optional[str], doi: Optional[str], arxiv_id: Optional[str]) -> Optional[Dict[str, str]]:
    if not url:
        return None
    url_clean = url.strip()
    if not url_clean:
        return None
    lower_url = url_clean.lower()

    arxiv_from_url = extract_arxiv_id(url_clean)
    if arxiv_from_url:
        arxiv_id = arxiv_from_url
    if arxiv_id and "arxiv" in lower_url:
        abstract = fetch_arxiv_abstract(arxiv_id)
        if abstract:
            return {"source": "arXiv (URL)", "text": abstract}

    doi_from_url = None
    if "doi.org" in lower_url or lower_url.startswith("doi:"):
        doi_from_url = clean_doi(url_clean)
    if doi_from_url:
        abstract = fetch_crossref_abstract(doi_from_url)
        if abstract:
            return {"source": "CrossRef (URL)", "text": abstract}

    # fall back to simple HTML meta parsing
    try:
        resp = requests.get(
            url_clean,
            timeout=20,
            headers={"User-Agent": "Zotero-Abstract-Enricher/0.1"},
        )
        resp.raise_for_status()
    except Exception as exc:  # pragma: no cover
        print(f"[WARN] Direct URL fetch failed ({url_clean}): {exc}")
        return None

    content_type = resp.headers.get("Content-Type", "").lower()
    if "html" not in content_type and "text" not in content_type:
        return None

    snippet = resp.text[:200000]  # limit memory use
    abstract = extract_meta_abstract(snippet)
    if abstract:
        return {"source": "URL meta", "text": abstract}
    return None


class ZoteroAPI:
    def __init__(self, user_id: str, api_key: str) -> None:
        self.base = f"https://api.zotero.org/users/{user_id}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Zotero-API-Key": api_key,
                "User-Agent": "Zotero-Abstract-Enricher/0.1",
            }
        )

    def iter_items(
        self,
        collection: Optional[str] = None,
        tag: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Iterable[Dict[str, Any]]:
        if collection:
            url = f"{self.base}/collections/{collection}/items/top"
        else:
            url = f"{self.base}/items/top"
        params = {
            "format": "json",
            "include": "data",
            "limit": 100,
        }
        if tag:
            params["tag"] = tag
        remaining = limit if limit and limit > 0 else None
        while url:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            for entry in data:
                yield {"key": entry["key"], "version": entry["version"], "data": entry["data"]}
                if remaining is not None:
                    remaining -= 1
                    if remaining == 0:
                        return
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

    def update_item(self, entry: Dict[str, Any], new_data: Dict[str, Any]) -> None:
        headers = {"If-Unmodified-Since-Version": str(entry["version"])}
        resp = self.session.put(f"{self.base}/items/{entry['key']}", json=new_data, headers=headers)
        resp.raise_for_status()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Fill missing Zotero abstracts via CrossRef/SemanticScholar/arXiv.")
    ap.add_argument("--collection", help="Zotero collection key.")
    ap.add_argument("--collection-name", help="Collection name (will be resolved to a key).")
    ap.add_argument("--tag", help="Only process items that contain this tag.")
    ap.add_argument("--limit", type=int, default=0, help="Max number of items to scan (<=0 means no limit).")
    ap.add_argument("--dry-run", action="store_true", help="Preview updates without modifying Zotero.")
    ap.add_argument(
        "--modified-since-hours",
        type=float,
        default=24.0,
        help="Only touch items modified within the last N hours (default 24).",
    )
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


def has_abstract(data: Dict[str, Any]) -> bool:
    abstract = (data.get("abstractNote") or "").strip()
    return bool(abstract)


def enrich_item(entry: Dict[str, Any]) -> Optional[Dict[str, str]]:
    data = entry["data"]
    doi = clean_doi(data.get("DOI") or data.get("doi"))
    arxiv_id = extract_arxiv_id(data.get("url"))
    url = data.get("url")
    semantic_rate_limited = False

    # Try cheap wins first: embedded DOI/arXiv in the URL itself.
    url_result = fetch_url_abstract(url, doi, arxiv_id)
    if url_result:
        return url_result

    # Next prefer CrossRef because it often has structured abstracts.
    if doi:
        abstract = fetch_crossref_abstract(doi)
        if abstract:
            return {"source": "CrossRef", "text": abstract}

    # Semantic Scholar offers both DOI and arXiv lookups; once they rate limit us we stop calling.
    if doi and not semantic_rate_limited:
        abstract = fetch_semantic_scholar_abstract("DOI", doi)
        if abstract == "RATE_LIMIT":
            semantic_rate_limited = True
        elif abstract:
            return {"source": "Semantic Scholar", "text": abstract}

    if arxiv_id and not semantic_rate_limited:
        abstract = fetch_semantic_scholar_abstract("arXiv", arxiv_id)
        if abstract == "RATE_LIMIT":
            semantic_rate_limited = True
        elif abstract:
            return {"source": "Semantic Scholar", "text": abstract}

    if arxiv_id:
        abstract = fetch_arxiv_abstract(arxiv_id)
        if abstract:
            return {"source": "arXiv", "text": abstract}

    return None


def main() -> None:
    args = parse_args()
    user_id = ensure_env("ZOTERO_USER_ID")
    api_key = ensure_env("ZOTERO_API_KEY")
    api = ZoteroAPI(user_id, api_key)

    collection_key = resolve_collection_key(api, args)
    limit = args.limit if args.limit > 0 else None

    scanned = 0
    updated = 0
    skipped = 0
    cutoff = None
    if args.modified_since_hours and args.modified_since_hours > 0:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.modified_since_hours)

    for entry in api.iter_items(collection_key, args.tag, limit):
        scanned += 1
        data = entry["data"]
        if data.get("itemType") in {"note", "attachment"}:
            continue
        if cutoff:
            dm = parse_iso(data.get("dateModified"))
            if dm and dm < cutoff:
                continue
        if has_abstract(data):
            continue
        result = enrich_item(entry)
        if not result:
            skipped += 1
            print(f"[MISS] No abstract found for {entry['key']} ({data.get('title')})")
            continue
        if args.dry_run:
            print(f"[DRY] Would update {entry['key']} with abstract from {result['source']}")
        else:
            new_data = data.copy()
            new_data["abstractNote"] = result["text"]
            api.update_item(entry, new_data)
            updated += 1
            print(f"[OK] Updated {entry['key']} with abstract from {result['source']}")

    print(f"[INFO] Completed. Items scanned: {scanned}, updated: {updated}, missing abstract after lookup: {skipped}.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        print(f"[ERR] HTTP error: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # pragma: no cover
        print(f"[ERR] {exc}", file=sys.stderr)
        sys.exit(1)
