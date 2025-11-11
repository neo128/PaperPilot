#!/usr/bin/env python3
"""
Awesome-VLA README → RIS exporter
---------------------------------
Usage:
  python awesome_vla_to_ris.py --out ./awesome_vla_ris
  python awesome_vla_to_ris.py --fetch --out ./awesome_vla_ris

Reads the Awesome-VLA README (locally by default) and emits RIS files grouped
by section/subsection so they can be imported into Zotero or other reference
managers.
"""
from __future__ import annotations

import argparse
import pathlib
import re
from typing import Dict, List, Optional, Tuple, Any
import xml.etree.ElementTree as ET

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    requests = None

RAW_URL = "https://raw.githubusercontent.com/Panmani/Awesome-VLA/main/README.md"
DEFAULT_README_PATH = pathlib.Path(__file__).resolve().parents[1] / "Awesome-VLA-main" / "README.md"

H2_RE = re.compile(r"^\s*##\s+(?P<name>.+?)\s*$")
H3_RE = re.compile(r"^\s*###\s+(?P<name>.+?)\s*$")
BULLET_RE = re.compile(r"^\s*[\*\-]\s+(?P<text>.+)$")
QUOTE_RE = re.compile(r'"([^"]+)"')
ITALIC_RE = re.compile(r"\*(?P<text>[^*]+)\*")
PAPER_LINK_RE = re.compile(r"\[\s*Paper\s*\]\((https?://[^\s)]+)\)", re.I)
FALLBACK_LINK_RE = re.compile(r"(https?://[^\s)\]]+)")
YEAR_RE = re.compile(r"\b(19|20|21)\d{2}\b")
DBLP_RE = re.compile(r"DBLP:([^\s,>]+)")
ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", re.I)

COLLECT_FROM_SECTION = "components of vla"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def fetch_readme_text(url: str = RAW_URL) -> str:
    if requests is None:
        raise SystemExit("Install 'requests' to fetch the README (pip install requests).")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def load_readme(fetch: bool, path: pathlib.Path) -> str:
    if fetch:
        return fetch_readme_text(RAW_URL)
    if not path.exists():
        # 自动回退为远程获取，减少本地依赖
        return fetch_readme_text(RAW_URL)
    return path.read_text(encoding="utf-8")


def clean_heading(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("#", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_markdown(md: str) -> List[Dict[str, Optional[str]]]:
    lines = md.splitlines()
    collecting = False
    section: Optional[str] = None
    subsection: Optional[str] = None
    items: List[Dict[str, Optional[str]]] = []
    pending_comment: Optional[str] = None

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line:
            continue
        stripped = line.strip()
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            pending_comment = stripped
            continue

        h2 = H2_RE.match(line)
        if h2:
            section = clean_heading(h2.group("name"))
            subsection = None
            if section.lower() == COLLECT_FROM_SECTION:
                collecting = True
            elif collecting:
                collecting = True  # stay collecting for subsequent sections
            continue

        if not collecting:
            continue

        h3 = H3_RE.match(line)
        if h3:
            subsection = clean_heading(h3.group("name"))
            continue

        bullet = BULLET_RE.match(line)
        if bullet and section:
            entry = parse_bullet(
                original_text=bullet.group("text"),
                section=section,
                subsection=subsection,
                meta_hint=pending_comment,
            )
            if entry:
                items.append(entry)
            pending_comment = None
    return dedupe_items(items)


def parse_bullet(
    original_text: str,
    section: str,
    subsection: Optional[str],
    meta_hint: Optional[str],
) -> Optional[Dict[str, Optional[str]]]:
    alias, remainder = extract_alias_and_text(original_text)
    title = extract_title(remainder)
    venue, year, institution, date_str = extract_venue_and_year(remainder)
    url = extract_url(original_text)
    dblp_id = extract_dblp_id(meta_hint)
    arxiv_id = extract_arxiv_id(url) if url else None

    if not title or not url:
        return None

    category = build_category(section, subsection)
    tags = ["Awesome-VLA", section]
    if subsection:
        tags.append(subsection)
    if alias:
        tags.append(alias)

    return {
        "title": title,
        "alias": alias,
        "venue": venue,
        "year": year,
        "date": date_str,
        "institution": institution,
        "url": url,
        "category": category,
        "section": section,
        "subsection": subsection,
        "tags": tags,
        "authors": [],
        "dblp_id": dblp_id,
        "arxiv_id": arxiv_id,
    }


def extract_alias_and_text(text: str) -> Tuple[Optional[str], str]:
    stripped = text.strip()
    if stripped.startswith("**"):
        end = stripped.find("**", 2)
        if end != -1:
            alias = stripped[2:end].strip()
            remainder = stripped[end + 2 :].lstrip(":").strip()
            return alias or None, remainder
    return None, stripped


def extract_title(text: str) -> Optional[str]:
    match = QUOTE_RE.search(text)
    if match:
        return match.group(1).strip()
    fragment = text.split(",")[0].strip()
    return fragment or None


def extract_venue_and_year(text: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    match = ITALIC_RE.search(text)
    if not match:
        return None, None, None, None
    raw = match.group("text").strip()
    parts = [p.strip() for p in raw.split(",")]
    institution = parts[0] if parts else None
    date_str = ", ".join(parts[1:]).strip() if len(parts) > 1 else None
    year_match = YEAR_RE.search(raw)
    year = year_match.group(0) if year_match else None
    if not date_str and year:
        date_str = year
    return raw or None, year, institution, date_str


def extract_url(text: str) -> Optional[str]:
    primary = PAPER_LINK_RE.search(text)
    if primary:
        return primary.group(1).strip()
    fallback = FALLBACK_LINK_RE.search(text)
    if fallback:
        return fallback.group(1).strip()
    return None


def extract_dblp_id(comment: Optional[str]) -> Optional[str]:
    if not comment:
        return None
    match = DBLP_RE.search(comment)
    if match:
        return match.group(1)
    return None


def extract_arxiv_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = ARXIV_ID_RE.search(url)
    if match:
        return match.group(1)
    return None


def build_category(section: str, subsection: Optional[str]) -> str:
    if subsection:
        return f"{section} / {subsection}"
    return section


def dedupe_items(items: List[Dict[str, Optional[str]]]) -> List[Dict[str, Optional[str]]]:
    seen = set()
    unique: List[Dict[str, Optional[str]]] = []
    for item in items:
        key = (item["title"], item["url"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def ris_escape(val: str) -> str:
    return val.replace("\n", " ")


def make_ris_record(item: Dict[str, Optional[str]]) -> str:
    parts: List[str] = ["TY  - ELEC"]
    title = item.get("title")
    venue = item.get("venue")
    year = item.get("year")
    date = item.get("date")
    url = item.get("url")
    tags = item.get("tags") or []
    authors = item.get("authors") or []
    institution = item.get("institution")

    if title:
        parts.append(f"TI  - {ris_escape(title)}")
    for author in authors:
        parts.append(f"AU  - {ris_escape(author)}")
    if venue:
        parts.append(f"T2  - {ris_escape(venue)}")
    if year:
        parts.append(f"PY  - {year}")
    if date:
        parts.append(f"DA  - {ris_escape(date)}")
    if institution:
        parts.append(f"PB  - {ris_escape(institution)}")
    if url:
        parts.append(f"UR  - {url}")
    for tag in tags:
        parts.append(f"KW  - {ris_escape(tag)}")
    parts.append("ER  - ")
    return "\n".join(parts)


def export_ris(items: List[Dict[str, Optional[str]]], out_dir: str) -> List[str]:
    by_category: Dict[str, List[Dict[str, Optional[str]]]] = {}
    for item in items:
        by_category.setdefault(item["category"], []).append(item)

    out_paths: List[str] = []
    out_path = pathlib.Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    for category, entries in by_category.items():
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", category).strip("_")
        filename = f"Awesome_VLA_{safe or 'General'}.ris"
        path = out_path / filename
        with path.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(make_ris_record(entry) + "\n\n")
        out_paths.append(str(path))
    return out_paths


def ensure_requests():
    if requests is None:
        raise SystemExit("Install 'requests' to use network features (pip install requests).")
    return requests


def fetch_dblp_metadata(dblp_id: str) -> Optional[Dict[str, Any]]:
    ensure_requests()
    url = f"https://dblp.org/rec/{dblp_id}.bib?param=1"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:  # pragma: no cover
        print(f"[WARN] Failed to fetch DBLP record {dblp_id}: {exc}")
        return None
    fields = parse_bibtex(resp.text)
    if not fields:
        return None
    authors_raw = fields.get("author")
    authors = [a.strip() for a in authors_raw.split(" and ")] if authors_raw else []
    venue = fields.get("booktitle") or fields.get("journal") or fields.get("series") or fields.get("title")
    institution = fields.get("institution") or fields.get("organization") or fields.get("publisher") or fields.get("school")
    year = fields.get("year")
    date = fields.get("date") or year
    return {
        "authors": authors,
        "venue": venue,
        "institution": institution,
        "year": year,
        "date": date,
    }


def parse_bibtex(text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    current_key: Optional[str] = None
    buffer: List[str] = []
    brace_depth = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("@"):
            continue
        if "=" in line and brace_depth == 0:
            if current_key and buffer:
                fields[current_key] = clean_bib_value(" ".join(buffer))
                buffer = []
                current_key = None
            key, remainder = line.split("=", 1)
            current_key = key.strip().lower()
            value = remainder.strip().rstrip(",")
            brace_depth = value.count("{") - value.count("}")
            buffer = [value]
            if brace_depth <= 0:
                fields[current_key] = clean_bib_value(" ".join(buffer))
                buffer = []
                current_key = None
        else:
            if current_key is None:
                continue
            brace_depth += line.count("{") - line.count("}")
            buffer.append(line.rstrip(","))
            if brace_depth <= 0:
                fields[current_key] = clean_bib_value(" ".join(buffer))
                buffer = []
                current_key = None
    if current_key and buffer:
        fields[current_key] = clean_bib_value(" ".join(buffer))
    return fields


def clean_bib_value(value: str) -> str:
    cleaned = value.strip().strip(",")
    if cleaned.startswith("{") and cleaned.endswith("}"):
        cleaned = cleaned[1:-1]
    if cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1]
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def fetch_arxiv_metadata(arxiv_id: str) -> Optional[Dict[str, Any]]:
    ensure_requests()
    url = "http://export.arxiv.org/api/query"
    try:
        resp = requests.get(url, params={"id_list": arxiv_id}, timeout=30, headers={"User-Agent": "AwesomeVLA-RIS/1.0"})
        resp.raise_for_status()
    except Exception as exc:  # pragma: no cover
        print(f"[WARN] Failed to fetch arXiv record {arxiv_id}: {exc}")
        return None
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:  # pragma: no cover
        return None
    entry = root.find(f"{ATOM_NS}entry")
    if entry is None:
        return None
    authors = []
    institutions: List[str] = []
    for author in entry.findall(f"{ATOM_NS}author"):
        name = author.findtext(f"{ATOM_NS}name")
        if name:
            authors.append(name.strip())
        affiliation = author.findtext(f"{ARXIV_NS}affiliation")
        if affiliation:
            institutions.append(affiliation.strip())
    published = entry.findtext(f"{ATOM_NS}published")
    date = published.split("T")[0] if published else None
    year = published[:4] if published else None
    institution = ", ".join(dict.fromkeys(filter(None, institutions))) or None
    venue = "arXiv"
    return {
        "authors": authors,
        "year": year,
        "date": date,
        "institution": institution,
        "venue": venue,
    }


def enrich_items(
    items: List[Dict[str, Optional[str]]],
    use_dblp: bool = False,
    use_arxiv: bool = False,
) -> None:
    dblp_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    arxiv_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    for item in items:
        metadata: Optional[Dict[str, Any]] = None
        if use_dblp and item.get("dblp_id"):
            dblp_id = item["dblp_id"]
            if dblp_id not in dblp_cache:
                dblp_cache[dblp_id] = fetch_dblp_metadata(dblp_id)
            metadata = dblp_cache[dblp_id]
        if metadata is None and use_arxiv and item.get("arxiv_id"):
            arxiv_id = item["arxiv_id"]
            if arxiv_id not in arxiv_cache:
                arxiv_cache[arxiv_id] = fetch_arxiv_metadata(arxiv_id)
            metadata = arxiv_cache[arxiv_id]
        if not metadata:
            continue
        if metadata.get("authors"):
            item["authors"] = metadata["authors"]
        if metadata.get("venue") and not item.get("venue"):
            item["venue"] = metadata["venue"]
        if metadata.get("year"):
            item["year"] = metadata["year"]
        if metadata.get("date"):
            item["date"] = metadata["date"]
        if metadata.get("institution") and not item.get("institution"):
            item["institution"] = metadata["institution"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Awesome-VLA README entries to RIS files.")
    parser.add_argument("--out", default="./awesome_vla_ris", help="Directory for the generated RIS files.")
    parser.add_argument("--readme-path", default=str(DEFAULT_README_PATH), help="Local README to parse.")
    parser.add_argument("--fetch", action="store_true", help="Fetch README from the official GitHub repo.")
    parser.add_argument("--enrich-dblp", action="store_true", help="Use DBLP hints to fetch authors/date metadata.")
    parser.add_argument("--enrich-arxiv", action="store_true", help="Use arXiv API for entries without DBLP metadata.")
    args = parser.parse_args()

    md = load_readme(fetch=args.fetch, path=pathlib.Path(args.readme_path))
    items = parse_markdown(md)
    if not items:
        raise SystemExit("No entries parsed; README structure may have changed.")

    if args.enrich_dblp or args.enrich_arxiv:
        enrich_items(items, use_dblp=args.enrich_dblp, use_arxiv=args.enrich_arxiv)

    written = export_ris(items, args.out)
    print(f"Parsed {len(items)} entries across {len(written)} categories.")
    for path in written:
        print(f"- {path}")


if __name__ == "__main__":
    main()
