#!/usr/bin/env python3
from __future__ import annotations

try:  # auto-load .env via sitecustomize if present
    import sitecustomize  # noqa: F401
except Exception:
    pass

import datetime as dt
import html
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import requests
import xml.etree.ElementTree as ET

ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
HF_PAPERS_BASE = os.environ.get("HF_PAPERS_BASE", "https://huggingface.co/papers")
HF_DATA_PROPS_PATTERN = re.compile(r'data-props="([^"]+)"')


def strip_tags(text: Optional[str]) -> str:
    if not text:
        return ""
    txt = html.unescape(text)
    txt = re.sub(r"<\s*/\s*p\s*>", "\n\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<\s*/\s*br\s*/?\s*>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def parse_authors(entry: ET.Element) -> List[str]:
    authors: List[str] = []
    for a in entry.findall(f"{ATOM_NS}author"):
        name = a.findtext(f"{ATOM_NS}name")
        if name:
            authors.append(name.strip())
    return authors


def parse_arxiv_id(entry: ET.Element) -> Optional[str]:
    id_text = entry.findtext(f"{ATOM_NS}id") or ""
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([A-Za-z0-9.\-]+)", id_text)
    if m:
        return m.group(1)
    for link in entry.findall(f"{ATOM_NS}link"):
        href = link.attrib.get("href")
        if not href:
            continue
        m = re.search(r"arxiv\.org/(?:abs|pdf)/([A-Za-z0-9.\-]+)", href)
        if m:
            return m.group(1)
    return None


def parse_arxiv_pdf(entry: ET.Element) -> Optional[str]:
    for link in entry.findall(f"{ATOM_NS}link"):
        if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
            href = link.attrib.get("href")
            if href:
                return href
    # fallback
    aid = parse_arxiv_id(entry)
    if aid:
        return f"https://arxiv.org/pdf/{aid}.pdf"
    return None


def parse_arxiv_doi(entry: ET.Element) -> Optional[str]:
    for doi in entry.findall(f"{ARXIV_NS}doi"):
        val = (doi.text or "").strip()
        if val:
            return val
    return None


def fetch_arxiv_by_keywords(keywords: List[str], since_days: int, max_results: int = 200) -> List[Dict[str, Any]]:
    # Build queries per keyword to keep the query string reasonable.
    results: Dict[str, Dict[str, Any]] = {}
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
    for kw in keywords:
        q = f"all:{quote(kw)}"
        url = "http://export.arxiv.org/api/query"
        params = {
            "search_query": q,
            "start": 0,
            "max_results": min(max_results, 200),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        try:
            resp = requests.get(url, params=params, timeout=30, headers={"User-Agent": "Zotero-Watch/0.1"})
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
        except Exception:
            continue
        for entry in root.findall(f"{ATOM_NS}entry"):
            title = (entry.findtext(f"{ATOM_NS}title") or "").strip()
            summary = (entry.findtext(f"{ATOM_NS}summary") or "").strip()
            published = entry.findtext(f"{ATOM_NS}published") or entry.findtext(f"{ATOM_NS}updated")
            try:
                pub_dt = dt.datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
            except Exception:
                pub_dt = None
            if pub_dt and pub_dt < cutoff:
                continue
            arxiv_id = parse_arxiv_id(entry)
            if not arxiv_id:
                continue
            key = f"arxiv:{arxiv_id}"
            authors = parse_authors(entry)
            url_abs = f"https://arxiv.org/abs/{arxiv_id}"
            pdf_url = parse_arxiv_pdf(entry)
            doi = parse_arxiv_doi(entry)
            results[key] = {
                "source": "arxiv",
                "title": title,
                "abstract": strip_tags(summary),
                "authors": authors,
                "date": published.split("T")[0] if published else None,
                "year": published[:4] if published else None,
                "url": url_abs,
                "pdf_url": pdf_url,
                "arxiv_id": arxiv_id,
                "doi": doi,
            }
    return list(results.values())


def fetch_s2_metadata(kind: str, identifier: str) -> Dict[str, Any]:
    paper_id = f"{kind}:{identifier}"
    url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}"
    params = {"fields": "title,year,externalIds,citationCount,influentialCitationCount,authors,abstract"}
    try:
        resp = requests.get(url, params=params, timeout=20, headers={"User-Agent": "Zotero-Watch/0.1"})
        if resp.status_code == 429:
            return {"rate_limited": True}
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
    except Exception:
        return {}
    data = resp.json() or {}
    out: Dict[str, Any] = {
        "title": data.get("title"),
        "year": data.get("year"),
        "citationCount": data.get("citationCount"),
        "influentialCitationCount": data.get("influentialCitationCount"),
        "abstract": strip_tags(data.get("abstract")),
    }
    ext = data.get("externalIds") or {}
    out["doi"] = ext.get("DOI") or ext.get("doi")
    return out


def fetch_crossref_metadata(doi: str) -> Dict[str, Any]:
    url = f"https://api.crossref.org/works/{quote(doi)}"
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Zotero-Watch/0.1"})
        resp.raise_for_status()
    except Exception:
        return {}
    msg = (resp.json() or {}).get("message", {})
    title_list = msg.get("title") or []
    authors = []
    for a in msg.get("author", []) or []:
        name = " ".join(x for x in [a.get("given"), a.get("family")] if x)
        if name:
            authors.append(name)
    abstract = strip_tags(msg.get("abstract")) if msg.get("abstract") else None
    year = None
    if msg.get("issued", {}).get("date-parts"):
        try:
            year = msg["issued"]["date-parts"][0][0]
        except Exception:
            pass
    return {
        "title": title_list[0] if title_list else None,
        "authors": authors,
        "abstract": abstract,
        "year": year,
    }


def fetch_unpaywall_pdf(doi: str, email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    url = f"https://api.unpaywall.org/v2/{quote(doi)}"
    try:
        resp = requests.get(url, params={"email": email}, timeout=20, headers={"User-Agent": "Zotero-Watch/0.1"})
        resp.raise_for_status()
    except Exception:
        return None
    data = resp.json() or {}
    best = data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf") or best.get("url")
    return pdf_url


def normalize_authors(authors: Iterable[str]) -> List[Dict[str, str]]:
    creators = []
    for a in authors:
        name = a.strip()
        if not name:
            continue
        parts = name.split()
        if len(parts) >= 2:
            creators.append({"creatorType": "author", "firstName": " ".join(parts[:-1]), "lastName": parts[-1]})
        else:
            creators.append({"creatorType": "author", "name": name})
    return creators


def _extract_hf_payload(html_text: str) -> Optional[Dict[str, Any]]:
    for raw in HF_DATA_PROPS_PATTERN.findall(html_text):
        payload = html.unescape(raw)
        if "papers" not in payload and "Papers" not in payload:
            continue
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            continue
    return None


def _hf_fetch_urls(period: str, identifier: str) -> List[str]:
    urls = [f"{HF_PAPERS_BASE}/{period}/{identifier}"]
    if period == "date":
        urls.append(f"{HF_PAPERS_BASE}?sort=trending&time=daily")
    elif period == "week":
        urls.append(f"{HF_PAPERS_BASE}?sort=trending&time=weekly")
    elif period == "month":
        urls.append(f"{HF_PAPERS_BASE}?sort=trending&time=monthly")
    return urls


def fetch_hf_period(period: str, identifier: str, label: str, limit: int) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    headers = {"User-Agent": "Zotero-Watch/0.1"}
    data = None
    for url in _hf_fetch_urls(period, identifier):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
        except Exception:
            continue
        data = _extract_hf_payload(resp.text)
        if data:
            break
    if not data:
        return []
    key_priority = {
        "date": ["dailyPapers", "papers"],
        "week": ["weeklyPapers", "papers", "dailyPapers"],
        "month": ["monthlyPapers", "papers", "dailyPapers"],
    }
    papers_list = None
    for key in key_priority.get(period, []):
        papers_list = data.get(key)
        if papers_list:
            break
    if not papers_list:
        papers_list = data.get("papers")
    if not papers_list:
        return []
    results: List[Dict[str, Any]] = []
    for idx, item in enumerate(papers_list):
        if len(results) >= limit:
            break
        paper = item.get("paper") or item
        title = paper.get("title") or item.get("title") or ""
        if not title:
            continue
        abstract = paper.get("summary") or item.get("summary") or ""
        url = paper.get("projectPage") or item.get("projectPage") or paper.get("paperUrl") or item.get("paperUrl")
        arxiv_id = paper.get("id") or paper.get("arxivId") or item.get("arxiv_id")
        doi = paper.get("doi") or item.get("doi")
        pdf_url = paper.get("pdfUrl") or item.get("pdf_url")
        if arxiv_id:
            abs_url = f"https://arxiv.org/abs/{arxiv_id}"
            if not url or "huggingface.co" in (url or "").lower():
                url = abs_url
            if not pdf_url:
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        elif not url:
            url = paper.get("paperUrl") or item.get("paperUrl")
        published = paper.get("publishedAt") or item.get("publishedAt")
        year = published[:4] if published else None
        date_str = published[:10] if published else None
        authors_raw = paper.get("authors") or item.get("authors") or []
        authors: List[str] = []
        if isinstance(authors_raw, list):
            for entry in authors_raw:
                if isinstance(entry, dict):
                    name = entry.get("name")
                else:
                    name = str(entry)
                if name:
                    authors.append(name)
        elif isinstance(authors_raw, str):
            authors = [authors_raw]
        rank = idx + 1
        score = max(0.0, 1.0 - (rank / max(1, limit + 1)))
        results.append(
            {
                "title": title,
                "abstract": abstract,
                "url": url,
                "arxiv_id": arxiv_id,
                "doi": doi,
                "pdf_url": pdf_url,
                "authors": authors,
                "timeframe": label,
                "rank": rank,
                "hf_score": score,
                "date": date_str,
                "year": year,
            }
        )
    return results
