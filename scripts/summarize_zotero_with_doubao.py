#!/usr/bin/env python3
"""
Zotero PDF → Doubao summary → Zotero note
----------------------------------------

This script walks through Zotero items, finds local PDF attachments, extracts a
text snippet, sends it to the Doubao (ByteDance Ark) Chat Completions API, and
stores the returned summary back into Zotero as a child note.

Prerequisites:
  * `pip install requests pypdf openai`
  * Environment variables:
        ZOTERO_USER_ID       # required, numeric Zotero user id
        ZOTERO_API_KEY       # required, API key with write access
        ARK_API_KEY          # required, Doubao API key
        ZOTERO_STORAGE_DIR   # optional, defaults to ~/Zotero/storage
        ARK_BOT_MODEL        # optional, defaults to bot-20251111104927-mf7bx

Example:
  python summarize_zotero_with_doubao.py --tag Awesome-VLA --limit 5 --max-pages 8
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import os
import sys
import textwrap
from textwrap import dedent
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

import requests
from openai import OpenAI

DEFAULT_BOT_MODEL = "bot-20251111104927-mf7bx"

try:
    from pypdf import PdfReader
except ImportError as exc:  # pragma: no cover - dependency hint
    raise SystemExit("Missing dependency 'pypdf'. Install via: pip install pypdf") from exc


class ZoteroAPI:
    def __init__(self, user_id: str, api_key: str) -> None:
        self.base = f"https://api.zotero.org/users/{user_id}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Zotero-API-Key": api_key,
                "User-Agent": "Zotero-Doubao-Summary/0.1",
            }
        )

    def iter_items(
        self,
        collection: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 25,
    ) -> Generator[Dict[str, Any], None, None]:
        url = f"{self.base}/items"
        if collection:
            url = f"{self.base}/collections/{collection}/items"
        params = {
            "format": "json",
            "include": "data",
            "limit": limit,
        }
        if tag:
            params["tag"] = tag

        yielded = 0
        while url and yielded < limit:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            for item in data:
                yield item["data"]
                yielded += 1
                if yielded >= limit:
                    break
            url = parse_next_link(resp.headers.get("Link"))
            params = None  # already encoded in Link

    def fetch_item(self, item_key: str) -> Dict[str, Any]:
        resp = self.session.get(f"{self.base}/items/{item_key}", params={"format": "json", "include": "data"})
        resp.raise_for_status()
        return resp.json()["data"]

    def fetch_children(self, parent_key: str) -> List[Dict[str, Any]]:
        resp = self.session.get(
            f"{self.base}/items/{parent_key}/children",
            params={"format": "json", "include": "data", "limit": 50},
        )
        resp.raise_for_status()
        return [entry["data"] for entry in resp.json()]

    def create_note(self, parent_key: str, note_html: str, tags: Optional[List[str]] = None) -> None:
        payload = [
            {
                "itemType": "note",
                "parentItem": parent_key,
                "note": note_html,
                "tags": [{"tag": t} for t in (tags or [])],
            }
        ]
        resp = self.session.post(f"{self.base}/items", json=payload)
        resp.raise_for_status()

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


def find_pdf_attachments(children: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    pdfs: List[Dict[str, Any]] = []
    for child in children:
        if child.get("itemType") != "attachment":
            continue
        filename = (child.get("filename") or "").lower()
        is_pdf = child.get("contentType") == "application/pdf" or filename.endswith(".pdf")
        if not is_pdf:
            continue
        if child.get("linkMode") not in {"imported_file", "linked_file", "imported_url"}:
            continue
        pdfs.append(child)
    return pdfs


def resolve_pdf_path(storage_root: Path, attachment: Dict[str, Any]) -> Path:
    path_hint = attachment.get("path")
    if path_hint:
        if path_hint.startswith("storage:"):
            rel = path_hint.split("storage:", 1)[1].lstrip("/")
            return storage_root / rel
        return Path(path_hint).expanduser()
    key = attachment["key"]
    filename = attachment.get("filename") or "document.pdf"
    return storage_root / key / filename


def derive_attachment_key(pdf_path: Path, storage_root: Path) -> Optional[str]:
    try:
        rel = pdf_path.resolve().relative_to(storage_root.resolve())
    except ValueError:
        return None
    parts = rel.parts
    return parts[0] if parts else None


def get_parent_for_attachment(zotero: "ZoteroAPI", attachment_key: str) -> Optional[str]:
    try:
        data = zotero.fetch_item(attachment_key)
    except requests.HTTPError as exc:
        print(f"[WARN] Failed to fetch attachment {attachment_key}: {exc}")
        return None
    parent = data.get("parentItem")
    return parent or data.get("key")


def has_existing_ai_summary(
    zotero: "ZoteroAPI",
    parent_key: str,
    note_tag: Optional[str] = None,
) -> bool:
    """Return True if the parent item already has an AI summary note.

    Heuristics:
      - note HTML contains "AI总结" or legacy "豆包自动总结"
      - or has a tag exactly equals to note_tag (when provided)
    """
    try:
        children = zotero.fetch_children(parent_key)
    except Exception:
        return False
    for c in children:
        if c.get("itemType") != "note":
            continue
        note_html = c.get("note") or ""
        if ("AI总结" in note_html) or ("豆包自动总结" in note_html):
            return True
        if note_tag:
            for t in c.get("tags") or []:
                if (t.get("tag") or "") == note_tag:
                    return True
    return False


def extract_pdf_text(path: Path, max_pages: int) -> str:
    reader = PdfReader(str(path))
    pages = reader.pages[: max_pages or len(reader.pages)]
    texts: List[str] = []
    for idx, page in enumerate(pages):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # pragma: no cover - PDF quirks
            print(f"[WARN] Failed to read page {idx+1} of {path.name}: {exc}")
            text = ""
        texts.append(text.strip())
    return "\n\n".join(filter(None, texts))

from textwrap import dedent
from openai import OpenAI
import re
import time

class DoubaoClient:
    """专为 AI / AGI / 具身智能 / 机器人 论文精读设计的解读专家"""

    def __init__(self, api_key: str, model: str, max_retries: int = 2) -> None:
        self.client = OpenAI(
            base_url="https://ark.cn-beijing.volces.com/api/v3/bots",
            api_key=api_key,
        )
        self.model = model
        self.max_retries = max_retries

    # —— 文本截断与段落保持 ——
    @staticmethod
    def _truncate_cjk_preserve_paragraphs(text: str, max_chars: int) -> str:
        if not text or len(text) <= max_chars:
            return text or ""
        cut = text[:max_chars]
        breakpoints = ["\n", "。", "！", "？", "；", ".", "?", "!"]
        last = max((cut.rfind(bp) for bp in breakpoints), default=-1)
        if last >= int(max_chars * 0.6):
            cut = cut[: last + 1]
        return cut.strip() + "\n\n…（片段已截断，仅基于此生成）"

    # —— 领域提示构建（中文/英文模板） ——
    def _build_prompt(self, title: str, excerpt: str, locale: str, out_limit: int) -> str:
        domain_note = (
            "本任务聚焦于人工智能（AI）、通用人工智能（AGI）、具身智能与机器人领域。"
            "要求严格依托正文内容，不得凭空编造，不可引入外部知识。"
        )
        if (locale or "").lower() == "en":
            return dedent(f"""
            You are a professional reviewer and AI/AGI/Embodied Intelligence expert. 
            {domain_note}
            Answer *strictly based on the excerpt below* — if information is missing, say “Not mentioned in excerpt”.
            Respond in **English Markdown**, ≤{out_limit} words.

            ## Abstract (1–2 sentences)
            - Main claim and quantitative gains if any.

            ## Problem & Motivation
            - Core research question and context.

            ## Method & Key Techniques
            - 3–5 bullet points, concise and factual.

            ## Experiments & Findings
            - Dataset/setup
            - Metrics/results (include numbers)
            - Core conclusions with evidence markers [E#1–n]

            ## Limitations & Future Work
            - Each 2–3 bullets; say “Not mentioned” if missing.

            ## Evidence Snippets
            - Direct quotes from the excerpt backing above claims.

            Title: {title}
            EXCERPT:
            {excerpt}
            """).strip()

        return dedent(f"""
        你是资深的 AI / AGI / 具身智能 / 机器人领域论文审稿专家。
        {domain_note}
        仅可基于《正文片段》生成内容，不得主观推断。
        请用 **Markdown 中文** 输出，整体不超过 {out_limit} 字。

        ## 摘要（1–2句）
        - 简明概括论文主要贡献或性能提升（如出现数值请保留）。

        ## 研究背景与问题
        - 背景与动机
        - 目标与挑战

        ## 方法与关键技术
        - 3–5 条技术要点（涉及模型架构、感知融合、控制算法等）

        ## 实验与结论
        - 数据集与实验设置（若有提及）
        - 结果指标（含数值）
        - 核心结论（每条以【证据#n】标注）

        ## 局限性与未来工作
        - 局限 2–3 条
        - 未来工作 2–3 条（若未出现请写“未在片段出现”）

        ## 证据摘录
        - 从片段中引用原句，编号为【证据#1,#2,…】

        论文标题：{title}
        《正文片段》：
        {excerpt}
        """).strip()

    # —— 核心接口：生成结构化解读卡片 ——
    def summarize(self, title: str, text: str, locale: str = "zh", max_chars: int = 4000) -> str:
        excerpt = self._truncate_cjk_preserve_paragraphs(text or "", max_chars)
        out_limit = max(800, min(2000, max_chars // 2))

        system_msg = (
            "你是豆包，由字节跳动开发的科研解读助手。"
            if (locale or "").lower() != "en"
            else "You are Doubao, an AI research assistant specialized in AI/AGI/robotics paper analysis."
        )
        prompt = self._build_prompt(title, excerpt, locale, out_limit)

        for attempt in range(self.max_retries):
            try:
                completion = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": [{"type": "text", "text": system_msg}]},
                        {"role": "user", "content": [{"type": "text", "text": prompt}]},
                    ],
                    temperature=0.15,
                    top_p=0.9,
                )
                content = (completion.choices[0].message.content or "").strip()
                return self._clean_output(content)
            except Exception as e:
                if attempt + 1 < self.max_retries:
                    time.sleep(1.5)
                    continue
                return self._fallback(title, e, locale)

    # —— 结果清洗：去除模型冗余前缀或错层结构 ——
    @staticmethod
    def _clean_output(text: str) -> str:
        text = re.sub(r"^```(?:markdown|md)?", "", text, flags=re.MULTILINE)
        text = re.sub(r"```$", "", text, flags=re.MULTILINE)
        return text.strip()

    # —— 异常回退模板 ——
    @staticmethod
    def _fallback(title: str, error: Exception, locale: str) -> str:
        if (locale or "").lower() == "en":
            return dedent(f"""
            # {title}
            > Generation failed ({error}). Placeholder only.

            ## Abstract
            - Not generated.

            ## Problem / Method / Experiments / Limitations
            - Not present.

            ## Evidence
            - (none)
            """).strip()

        return dedent(f"""
        # {title}
        > 生成失败（{error}）。以下为占位模板。

        ## 研究框架梳理
        - 背景：未在片段出现
        - 方法：未在片段出现
        - 结果：未在片段出现

        ## 证据摘录
        - （无）
        """).strip()


 





def make_note_html(summary: str) -> str:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    def _normalize_md(text: str) -> str:
        # 去除模型可能添加的反斜杠转义（如 \#, \*, \- 等）
        text = re.sub(r"\\([#*_\-`\[\]()])", r"\1", text)
        return text

    md_text = _normalize_md(summary or "")

    # 优先本地渲染为 HTML，避免 Zotero 端无法识别 data-markdown 时显示错乱
    html_fragment = None
    try:
        import markdown as _md  # type: ignore
        html_fragment = _md.markdown(
            md_text,
            extensions=["extra", "tables", "fenced_code", "sane_lists"],
            output_format="html5",
        )
    except Exception:
        html_fragment = None

    if html_fragment:
        return f"<p><strong>AI总结</strong>（{timestamp}）</p>" + html_fragment

    # 兜底：将 Markdown 作为纯文本交给 Zotero；保持换行与空白
    safe_text = html.escape(md_text)
    return (
        f"<p><strong>AI总结</strong>（{timestamp}）</p>"
        f"<div data-markdown=\"true\" data-mime-type=\"text/markdown\" style=\"white-space:pre-wrap\">{safe_text}</div>"
    )


def ensure_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize Zotero PDFs via Doubao and attach notes or save locally.")
    parser.add_argument("--tag", help="Only process items tagged with this string.")
    parser.add_argument("--collection", help="Only process items inside the specified collection key.")
    parser.add_argument("--collection-name", help="Lookup a Zotero collection by name and process its items.")
    parser.add_argument("--item-keys", help="Comma-separated list of specific Zotero item keys to process.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of parent items to process (<=0 means no cap).")
    parser.add_argument("--max-pages", type=int, default=12, help="Max PDF pages to read per attachment (default: 12).")
    parser.add_argument("--max-chars", type=int, default=12000, help="Max characters to send to Doubao (after extraction).")
    parser.add_argument("--note-tag", default="AI总结", help="Tag to add to the generated note.")
    parser.add_argument("--storage-dir", help="Override Zotero storage directory (defaults to ~/Zotero/storage).")
    parser.add_argument("--pdf-path", action="append", help="Process a standalone PDF path (repeat flag to add more).")
    parser.add_argument("--storage-key", action="append", help="Process PDFs inside the specified Zotero storage key folder.")
    parser.add_argument("--summary-dir", help="When using --pdf-path/--storage-key, save summaries into this directory (defaults to stdout only).")
    parser.add_argument("--insert-note", action="store_true", help="Insert generated summaries back into Zotero notes when PDFs come from storage.")
    parser.add_argument("--model", help="Override Doubao bot model id (defaults to env ARK_BOT_MODEL or built-in).")
    parser.add_argument("--force", action="store_true", help="Ignore existing AI总结/豆包总结笔记并重新生成。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    ark_key = ensure_env("ARK_API_KEY")
    bot_model = args.model or os.environ.get("ARK_BOT_MODEL") or DEFAULT_BOT_MODEL
    if not bot_model.startswith("bot-"):
        print(f"[WARN] Model '{bot_model}' does not look like a Doubao bot id; falling back to {DEFAULT_BOT_MODEL}.")
        bot_model = DEFAULT_BOT_MODEL
    storage_dir = Path(args.storage_dir or os.environ.get("ZOTERO_STORAGE_DIR", Path.home() / "Zotero" / "storage"))
    if not storage_dir.exists():
        raise SystemExit(f"Zotero storage directory not found: {storage_dir}")

    doubao = DoubaoClient(ark_key, bot_model)

    local_pdfs: List[Tuple[str, Path]] = []
    if args.pdf_path:
        for raw in args.pdf_path:
            path = Path(raw).expanduser()
            if not path.exists():
                print(f"[WARN] PDF path not found: {path}")
                continue
            local_pdfs.append((path.stem, path))
    if args.storage_key:
        for key in args.storage_key:
            folder = storage_dir / key
            if not folder.exists():
                print(f"[WARN] Storage key '{key}' not found at {folder}")
                continue
            for pdf in folder.glob("*.pdf"):
                local_pdfs.append((pdf.stem, pdf))

    require_remote_lookup = bool(args.collection or args.collection_name or args.item_keys or not local_pdfs)
    need_zotero = args.insert_note or require_remote_lookup
    zotero_client: Optional[ZoteroAPI] = None
    resolved_collection_key: Optional[str] = None
    if need_zotero:
        user_id = ensure_env("ZOTERO_USER_ID")
        api_key = ensure_env("ZOTERO_API_KEY")
        zotero_client = ZoteroAPI(user_id, api_key)
        resolved_collection_key = args.collection
        if args.collection_name:
            collections = zotero_client.list_collections()
            match_name = None
            match_info = None
            for name, info in collections.items():
                if name == args.collection_name or (name and name.lower() == args.collection_name.lower()):
                    match_name = name
                    match_info = info
                    break
            if not match_info:
                print(f"[ERR] Collection named '{args.collection_name}' not found in Zotero.")
                return
            resolved_collection_key = match_info["key"]
            print(f"[INFO] Resolved collection '{match_name}' → {resolved_collection_key}")

    if local_pdfs:
        summary_dir = Path(args.summary_dir).expanduser() if args.summary_dir else None
        if summary_dir:
            summary_dir.mkdir(parents=True, exist_ok=True)
        for title_hint, pdf_path in local_pdfs:
            print(f"[INFO] Summarizing local PDF: {pdf_path}")
            # 如果准备写回 Zotero，则先检查是否已有 AI 总结
            if args.insert_note and zotero_client and not args.force:
                attachment_key = derive_attachment_key(pdf_path, storage_dir)
                if attachment_key:
                    parent_key = get_parent_for_attachment(zotero_client, attachment_key)
                    if parent_key and has_existing_ai_summary(zotero_client, parent_key, args.note_tag):
                        print(f"    [SKIP] Existing AI总结 note found for item {parent_key}; skipping.")
                        continue
            text = extract_pdf_text(pdf_path, args.max_pages)
            if not text:
                print("    [WARN] Failed to extract text; skipping.")
                continue
            summary = doubao.summarize(title_hint, text, locale="zh", max_chars=args.max_chars)
            if summary_dir:
                out_file = summary_dir / f"{pdf_path.stem}.summary.txt"
                out_file.write_text(summary, encoding="utf-8")
                print(f"    [OK] Summary saved to {out_file}")
            else:
                print("---- Summary Start ----")
                print(summary)
                print("---- Summary End ----")
            if args.insert_note:
                if not zotero_client:
                    print("    [WARN] Cannot insert note because Zotero credentials are missing.")
                    continue
                attachment_key = derive_attachment_key(pdf_path, storage_dir)
                if not attachment_key:
                    print("    [WARN] PDF path is outside Zotero storage; skipping note insertion.")
                    continue
                parent_key = get_parent_for_attachment(zotero_client, attachment_key)
                if not parent_key:
                    print(f"    [WARN] Unable to find Zotero parent for attachment {attachment_key}.")
                    continue
                if not args.force and has_existing_ai_summary(zotero_client, parent_key, args.note_tag):
                    print(f"    [SKIP] Existing AI总结 note found for item {parent_key}; skipping insert.")
                    continue
                note_html = make_note_html(summary)
                zotero_client.create_note(parent_key, note_html, tags=[args.note_tag])
                print(f"    [OK] Note inserted into Zotero item {parent_key}.")
        return

    fetch_limit = args.limit if args.limit and args.limit > 0 else 1_000_000

    if args.item_keys:
        parent_items = [zotero_client.fetch_item(key.strip()) for key in args.item_keys.split(",") if key.strip()]
    else:
        parent_items = list(
            zotero_client.iter_items(collection=resolved_collection_key, tag=args.tag, limit=fetch_limit)
        )

    if not parent_items:
        scope = []
        if args.tag:
            scope.append(f"tag='{args.tag}'")
        if args.collection:
            scope.append(f"collection='{args.collection}'")
        scope_desc = ", ".join(scope) if scope else "entire library (limited by permissions)"
        print(f"[INFO] No Zotero items matched {scope_desc}; nothing to process.")
        return

    processed_items = 0
    notes_created = 0

    for parent in parent_items:
        title = parent.get("title") or parent.get("shortTitle") or parent.get("key")
        parent_key = parent["key"]
        note_parent_key = parent_key
        print(f"[INFO] Processing {title} ({parent_key})")
        processed_items += 1

        if parent.get("itemType") == "attachment":
            pdfs = find_pdf_attachments([parent])
            if parent.get("parentItem"):
                note_parent_key = parent["parentItem"]
        else:
            children = zotero_client.fetch_children(parent_key)
            pdfs = find_pdf_attachments(children)
            if not pdfs:
                attachment_types = [child.get("itemType") for child in children if child.get("itemType")]
                print(
                    f"[WARN] No local PDF attachments for {title}; "
                    f"children types: {attachment_types or 'none'}"
                )
                continue
        # 若已存在 AI总结/豆包自动总结 的笔记，则整条跳过（可用 --force 覆盖）
        if not args.force and has_existing_ai_summary(zotero_client, note_parent_key, args.note_tag):
            print("    [SKIP] Existing AI总结 note found; skipping this item.")
            continue
        if not pdfs:
            print(f"[WARN] Item {title} is tagged but not a PDF attachment; skipping.")
            continue

        created_for_item = False
        for attachment in pdfs:
            pdf_path = resolve_pdf_path(storage_dir, attachment)
            if not pdf_path.exists():
                print(f"[WARN] PDF not found on disk: {pdf_path}")
                continue
            print(f"  - Reading {pdf_path.name}")
            text = extract_pdf_text(pdf_path, args.max_pages)
            if not text:
                print("    [WARN] Empty text extracted; skipping.")
                continue
            summary = doubao.summarize(title, text, locale="zh", max_chars=args.max_chars)
            note_html = make_note_html(summary)
            zotero_client.create_note(note_parent_key, note_html, tags=[args.note_tag])
            print("    [OK] Note created.")
            notes_created += 1
            created_for_item = True
        if not created_for_item:
            print("    [INFO] No summaries created for this item (missing/empty PDFs).")

    print(f"[INFO] Completed. Items scanned: {processed_items}, notes created: {notes_created}.")

if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        print(f"[ERR] HTTP error: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # pragma: no cover
        print(f"[ERR] {exc}", file=sys.stderr)
        sys.exit(1)
