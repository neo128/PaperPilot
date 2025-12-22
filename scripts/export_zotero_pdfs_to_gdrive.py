#!/usr/bin/env python3
"""Mirror Zotero collections to Google Drive folders and upload PDF attachments."""
from __future__ import annotations

try:  # auto-load .env via sitecustomize if present
    import sitecustomize  # noqa: F401
except Exception:
    pass

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
except ImportError as exc:  # pragma: no cover - hint for missing deps
    raise SystemExit(
        "Missing google-api-python-client dependency. Install with 'pip install google-api-python-client'."
    ) from exc


FOLDER_MIME = "application/vnd.google-apps.folder"
PDF_MIME = "application/pdf"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
ZOTERO_COLLECTION_PROP = "zotero_collection_key"
ZOTERO_ROOT_PROP = "zotero_root_id"
ZOTERO_PARENT_PROP = "zotero_parent_key"


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


def sanitize_drive_name(name: str, default: str = "Untitled") -> str:
    cleaned = name.strip() or default
    return re.sub(r"[\\/:*?\"<>|]", "_", cleaned)


def sanitize_filename(name: str) -> str:
    cleaned = name.strip() or "document"
    return re.sub(r"[\\/:*?\"<>|]", "_", cleaned)


def escape_drive_query(value: str) -> str:
    return value.replace("'", "\\'")


class ZoteroAPI:
    def __init__(
        self, user_id: str, api_key: str, timeout: int = 45, use_env_proxy: bool = True, retries: int = 3
    ) -> None:
        self.base = f"https://api.zotero.org/users/{user_id}"
        self.timeout = max(5, timeout)
        self.session = requests.Session()
        self.session.trust_env = use_env_proxy
        if not use_env_proxy:
            # Empty proxy dict disables proxies even if set globally.
            self.session.proxies = {}
        retry_cfg = Retry(
            total=max(0, retries),
            connect=max(0, retries),
            read=max(0, retries),
            status=max(0, retries),
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry_cfg)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"Zotero-API-Key": api_key, "User-Agent": "Zotero-GDrive-Export/0.1"})

    def fetch_collections(self) -> List[Dict[str, Optional[str]]]:
        url = f"{self.base}/collections"
        params = {"format": "json", "include": "data", "limit": 200}
        out: List[Dict[str, Optional[str]]] = []
        while url:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            for entry in resp.json():
                data = entry.get("data", {})
                out.append(
                    {
                        "key": entry.get("key"),
                        "name": data.get("name") or "(untitled)",
                        "parent": data.get("parentCollection") or None,
                    }
                )
            url = parse_next_link(resp.headers.get("Link"))
            params = None
        return out

    def iter_items(self, collection: Optional[str], limit: Optional[int]) -> Iterable[Dict[str, Any]]:
        url = f"{self.base}/items/top"
        if collection:
            url = f"{self.base}/collections/{collection}/items/top"
        params = {"format": "json", "include": "data", "limit": 100}
        remaining = limit if (limit and limit > 0) else None
        while url:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
            for entry in payload:
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
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            out.extend([entry.get("data", {}) for entry in resp.json()])
            url = parse_next_link(resp.headers.get("Link"))
            params = None
        return out


@dataclass
class DriveConfig:
    credentials_file: Optional[Path]
    root_folder: str
    dry_run: bool = False
    overwrite: bool = False
    use_oauth: bool = False
    oauth_client_file: Optional[Path] = None
    oauth_token_file: Optional[Path] = None
    http_timeout: int = 180
    upload_chunk_size: int = 5 * 1024 * 1024  # 5 MB
    sync_folders: bool = True
    prune_missing: bool = False


class DriveClient:
    def __init__(self, cfg: DriveConfig):
        self.cfg = cfg
        self._folder_cache: Dict[Tuple[str, str], str] = {}
        self._collection_cache: Dict[str, str] = {}
        self._existing_files: Dict[str, Dict[str, str]] = {}
        if cfg.dry_run:
            self.service = None
            return
        if cfg.use_oauth:
            if not cfg.oauth_client_file:
                raise SystemExit("Missing OAuth client file. Provide --oauth-client-file or set GOOGLE_OAUTH_CLIENT_FILE.")
            client_path = cfg.oauth_client_file.expanduser()
            if not client_path.exists():
                raise SystemExit(f"OAuth client file not found: {client_path}")
            token_path = (cfg.oauth_token_file or Path.home() / ".config" / "zotero-drive-oauth" / "token.json").expanduser()
            try:
                from google_auth_oauthlib.flow import InstalledAppFlow
                from google.auth.transport.requests import Request
            except ImportError as exc:  # pragma: no cover - hint for missing deps
                raise SystemExit("Missing google-auth-oauthlib dependency. Install with 'pip install google-auth-oauthlib'.") from exc
            creds: Optional[Credentials] = None
            if token_path.exists():
                try:
                    creds = Credentials.from_authorized_user_file(str(token_path), scopes=[DRIVE_SCOPE])
                except Exception:
                    creds = None
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    creds = None
            if not creds or not creds.valid:
                flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes=[DRIVE_SCOPE])
                creds = flow.run_local_server(port=0)
                token_path.parent.mkdir(parents=True, exist_ok=True)
                token_path.write_text(creds.to_json())
            self.service = build("drive", "v3", credentials=creds, cache_discovery=False)
            if hasattr(self.service, "_http"):
                try:
                    self.service._http.timeout = cfg.http_timeout
                except Exception:
                    pass
            return
        if not cfg.credentials_file or not cfg.credentials_file.exists():
            raise SystemExit(
                f"Google service account file not found: {cfg.credentials_file}. Set GOOGLE_SERVICE_ACCOUNT_FILE or use --credentials-file."
            )
        creds = service_account.Credentials.from_service_account_file(str(cfg.credentials_file), scopes=[DRIVE_SCOPE])
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)
        if hasattr(self.service, "_http"):
            try:
                self.service._http.timeout = cfg.http_timeout
            except Exception:
                pass

    def _collection_props(self, collection_key: str, parent_key: Optional[str]) -> Dict[str, str]:
        return {
            ZOTERO_COLLECTION_PROP: collection_key,
            ZOTERO_ROOT_PROP: self.cfg.root_folder,
            ZOTERO_PARENT_PROP: parent_key or "",
        }

    def _needs_prop_update(self, current: Optional[Dict[str, str]], expected: Dict[str, str]) -> bool:
        if not current:
            return True
        for key, value in expected.items():
            if current.get(key) != value:
                return True
        return False

    def _find_folder_by_name(self, parent_id: str, name: str) -> Optional[Dict[str, Any]]:
        q_name = escape_drive_query(name)
        query = f"'{parent_id}' in parents and trashed=false and mimeType='{FOLDER_MIME}' and name = '{q_name}'"
        resp = self.service.files().list(q=query, fields="files(id,name,parents,appProperties)", pageSize=1).execute()
        files = resp.get("files", [])
        return files[0] if files else None

    def _find_folder_by_collection_key(self, collection_key: str) -> Optional[Dict[str, Any]]:
        q_key = escape_drive_query(collection_key)
        q_root = escape_drive_query(self.cfg.root_folder)
        query = (
            f"trashed=false and mimeType='{FOLDER_MIME}' and "
            f"appProperties has {{ key='{ZOTERO_COLLECTION_PROP}' and value='{q_key}' }} and "
            f"appProperties has {{ key='{ZOTERO_ROOT_PROP}' and value='{q_root}' }}"
        )
        resp = self.service.files().list(q=query, fields="files(id,name,parents,appProperties)", pageSize=2).execute()
        files = resp.get("files", [])
        if len(files) > 1:
            print(f"[WARN] Multiple Drive folders match collection {collection_key}; using the first.")
        return files[0] if files else None

    def _sync_folder_metadata(
        self,
        folder_id: str,
        info: Dict[str, Any],
        expected_parent_id: str,
        expected_name: str,
        collection_key: str,
        parent_key: Optional[str],
    ) -> None:
        if not self.cfg.sync_folders:
            return
        props = self._collection_props(collection_key, parent_key)
        update_body: Dict[str, Any] = {}
        actions: List[str] = []
        if info.get("name") != expected_name:
            update_body["name"] = expected_name
            actions.append(f"rename to '{expected_name}'")
        if self._needs_prop_update(info.get("appProperties"), props):
            update_body["appProperties"] = props
            actions.append("update zotero metadata")
        parents = info.get("parents") or []
        add_parent: Optional[str] = None
        remove_parents: List[str] = []
        if expected_parent_id not in parents:
            add_parent = expected_parent_id
            remove_parents = parents
            actions.append(f"move under {expected_parent_id}")
        elif len(parents) > 1:
            remove_parents = [p for p in parents if p != expected_parent_id]
            if remove_parents:
                actions.append(f"remove extra parents {remove_parents}")
        if not actions:
            return
        if self.cfg.dry_run:
            print(f"[DRY] Would sync folder '{info.get('name')}' ({folder_id}): " + "; ".join(actions))
            return
        update_kwargs: Dict[str, Any] = {"fileId": folder_id, "fields": "id,name,parents"}
        if update_body:
            update_kwargs["body"] = update_body
        if add_parent:
            update_kwargs["addParents"] = add_parent
        if remove_parents:
            update_kwargs["removeParents"] = ",".join(remove_parents)
        self.service.files().update(**update_kwargs).execute()
        print(f"[DRIVE] Synced folder '{expected_name}' ({folder_id}): " + "; ".join(actions))

    def _list_zotero_folders(self) -> List[Dict[str, Any]]:
        q_root = escape_drive_query(self.cfg.root_folder)
        query = (
            f"trashed=false and mimeType='{FOLDER_MIME}' and "
            f"appProperties has {{ key='{ZOTERO_ROOT_PROP}' and value='{q_root}' }}"
        )
        out: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        while True:
            resp = (
                self.service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id,name,parents,appProperties)",
                    pageToken=page_token,
                    pageSize=1000,
                )
                .execute()
            )
            out.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return out

    def ensure_folder(
        self,
        parent_id: str,
        name: str,
        collection_key: Optional[str] = None,
        parent_key: Optional[str] = None,
    ) -> str:
        safe_name = sanitize_drive_name(name)
        cache_key = (parent_id, safe_name)
        if collection_key and collection_key in self._collection_cache:
            return self._collection_cache[collection_key]
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]
        if self.cfg.dry_run:
            fake_id = f"{parent_id}/{safe_name}"
            self._folder_cache[cache_key] = fake_id
            if collection_key:
                self._collection_cache[collection_key] = fake_id
            return fake_id
        info: Optional[Dict[str, Any]] = None
        if collection_key and self.cfg.sync_folders:
            info = self._find_folder_by_collection_key(collection_key)
        if not info:
            info = self._find_folder_by_name(parent_id, safe_name)
        if info:
            folder_id = info["id"]
            self._folder_cache[cache_key] = folder_id
            if collection_key:
                self._collection_cache[collection_key] = folder_id
            if collection_key and self.cfg.sync_folders:
                self._sync_folder_metadata(folder_id, info, parent_id, safe_name, collection_key, parent_key)
            return folder_id
        metadata: Dict[str, Any] = {"name": safe_name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        if collection_key and self.cfg.sync_folders:
            metadata["appProperties"] = self._collection_props(collection_key, parent_key)
        folder = self.service.files().create(body=metadata, fields="id").execute()
        folder_id = folder["id"]
        print(f"[DRIVE] Created folder '{safe_name}' under {parent_id}")
        self._folder_cache[cache_key] = folder_id
        if collection_key:
            self._collection_cache[collection_key] = folder_id
        return folder_id

    def prune_missing_collections(self, existing_keys: Set[str]) -> None:
        if not self.cfg.prune_missing:
            return
        if self.cfg.dry_run:
            print("[DRY] Skipping prune of missing collections (dry-run enabled).")
            return
        for folder in self._list_zotero_folders():
            props = folder.get("appProperties") or {}
            collection_key = props.get(ZOTERO_COLLECTION_PROP)
            if not collection_key or collection_key in existing_keys:
                continue
            self.service.files().update(fileId=folder["id"], body={"trashed": True}).execute()
            print(
                f"[DRIVE] Trashed folder '{folder.get('name')}' ({folder['id']}) missing in Zotero"
            )

    def _ensure_existing_cache(self, folder_id: str) -> None:
        if self.cfg.dry_run or folder_id in self._existing_files:
            return
        items: Dict[str, str] = {}
        page_token: Optional[str] = None
        while True:
            resp = (
                self.service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields="nextPageToken, files(id,name,mimeType)",
                    pageToken=page_token,
                )
                .execute()
            )
            for f in resp.get("files", []):
                items[f["name"]] = f["id"]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        self._existing_files[folder_id] = items

    def upload_pdf(self, folder_id: str, filename: str, path: Path) -> None:
        safe_name = sanitize_drive_name(filename, default="paper.pdf")
        if not path.exists():
            print(f"[WARN] Local PDF missing: {path}")
            return
        if self.cfg.dry_run:
            print(f"[DRY] Would upload '{safe_name}' from {path} → folder {folder_id}")
            return
        self._ensure_existing_cache(folder_id)
        existing = self._existing_files.get(folder_id, {})
        file_id = existing.get(safe_name)
        media = MediaFileUpload(
            str(path),
            mimetype=PDF_MIME,
            resumable=True,
            chunksize=self.cfg.upload_chunk_size if self.cfg.upload_chunk_size > 0 else None,
        )
        if file_id and self.cfg.overwrite:
            self.service.files().update(fileId=file_id, media_body=media).execute()
            print(f"[DRIVE] Updated existing file '{safe_name}'")
            return
        if file_id and not self.cfg.overwrite:
            print(f"[SKIP] File '{safe_name}' already exists in folder {folder_id}")
            return
        metadata = {"name": safe_name, "parents": [folder_id]}
        created = self.service.files().create(body=metadata, media_body=media, fields="id").execute()
        self._existing_files.setdefault(folder_id, {})[safe_name] = created.get("id")
        print(f"[DRIVE] Uploaded '{safe_name}' ({path})")


def resolve_collection_key(
    collections: Dict[str, Dict[str, Optional[str]]], name: Optional[str], key: Optional[str]
) -> Optional[str]:
    if key:
        return key
    if not name:
        return None
    for info in collections.values():
        if info.get("name") and info["name"].lower() == name.lower():
            return info["key"]
    raise SystemExit(f"Collection named '{name}' not found.")


def iter_pdf_attachments(zot: ZoteroAPI, item_key: str) -> Iterable[Dict[str, Any]]:
    for child in zot.fetch_children(item_key):
        if child.get("itemType") != "attachment":
            continue
        filename = (child.get("filename") or "").lower()
        if child.get("contentType") == PDF_MIME or filename.endswith(".pdf"):
            yield child


def resolve_local_path(att: Dict[str, Any], storage_dir: Path) -> Optional[Path]:
    mode = (att.get("linkMode") or "").lower()
    if mode == "linked_file":
        raw = att.get("path")
        if not raw:
            return None
        if raw.startswith("storage:"):
            rel = raw.split("storage:", 1)[1].lstrip("/\\")
            return (storage_dir / rel).expanduser()
        return Path(raw).expanduser()
    if mode in {"imported_file", "imported_url"}:
        key = att.get("key")
        filename = att.get("filename")
        if key and filename:
            return storage_dir / key / filename
    return None


def ensure_pdf_local(att: Dict[str, Any], storage_dir: Path, temp_dir: Path) -> Optional[Path]:
    path = resolve_local_path(att, storage_dir)
    if path and path.exists():
        return path
    url = att.get("url") or att.get("path")
    if url and url.startswith("http"):
        try:
            resp = requests.get(url, timeout=45)
            resp.raise_for_status()
        except Exception as exc:  # pragma: no cover - network error reporting
            print(f"[WARN] Failed to download {url}: {exc}")
            return None
        filename = sanitize_filename(att.get("filename") or att.get("title") or att.get("key") or "download")
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        dest = temp_dir / filename
        dest.write_bytes(resp.content)
        return dest
    print(f"[WARN] Attachment {att.get('title') or att.get('key')} has no local file or downloadable URL")
    return None


def derive_drive_filename(item: Dict[str, Any], att: Dict[str, Any]) -> str:
    # Prefer the Zotero item title to avoid many attachments sharing the same filename.
    title = item.get("title") or item.get("shortTitle") or att.get("title") or att.get("filename") or att.get("key")
    safe = sanitize_filename(title or "paper")
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    return safe


def build_collection_maps(collections: List[Dict[str, Optional[str]]]) -> Tuple[
    Dict[str, Dict[str, Optional[str]]], Dict[Optional[str], List[Dict[str, Optional[str]]]]
]:
    by_key: Dict[str, Dict[str, Optional[str]]] = {}
    children: Dict[Optional[str], List[Dict[str, Optional[str]]]] = {}
    for col in collections:
        by_key[col["key"]] = col
        parent = col.get("parent")
        children.setdefault(parent, []).append(col)
    return by_key, children


def export_collection(
    zot: ZoteroAPI,
    drive: DriveClient,
    collection: Dict[str, Optional[str]],
    children_map: Dict[Optional[str], List[Dict[str, Optional[str]]]],
    parent_drive_id: str,
    storage_dir: Path,
    args: argparse.Namespace,
    temp_dir: Path,
) -> None:
    folder_id = drive.ensure_folder(
        parent_drive_id,
        collection.get("name") or collection["key"],
        collection_key=collection.get("key"),
        parent_key=collection.get("parent"),
    )
    limit = args.limit if (args.limit and args.limit > 0) else None
    count = 0
    for entry in zot.iter_items(collection["key"], limit):
        data = entry.get("data", entry)
        item_key = data.get("key")
        title = data.get("title") or data.get("shortTitle") or "(untitled)"
        attachments = list(iter_pdf_attachments(zot, item_key)) if item_key else []
        if not attachments:
            continue
        for att in attachments:
            local_path = ensure_pdf_local(att, storage_dir, temp_dir)
            if not local_path:
                continue
            drive_name = derive_drive_filename(data, att)
            drive.upload_pdf(folder_id, drive_name, local_path)
            count += 1
    print(f"[COL] {collection.get('name')} uploaded PDFs: {count}")
    if args.recursive:
        for child in children_map.get(collection["key"], []):
            export_collection(zot, drive, child, children_map, folder_id, storage_dir, args, temp_dir)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Export Zotero PDFs to Google Drive with collection hierarchy.")
    ap.add_argument("--collection", help="Zotero collection key to export (defaults to all top-level collections).")
    ap.add_argument("--collection-name", help="Zotero collection name to export.")
    ap.add_argument("--drive-root-folder", help="Destination Google Drive folder ID.")
    ap.add_argument(
        "--credentials-file",
        help="Path to Google service account JSON (defaults to GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_APPLICATION_CREDENTIALS).",
    )
    ap.add_argument(
        "--oauth-client-file",
        help="Path to OAuth client credentials JSON (installed app). Overrides service account when provided.",
    )
    ap.add_argument(
        "--oauth-token-file",
        help="Path to store OAuth user token (defaults to ~/.config/zotero-drive-oauth/token.json).",
    )
    ap.add_argument("--limit", type=int, default=0, help="Max items per collection (<=0 means no limit).")
    ap.add_argument("--no-recursive", dest="recursive", action="store_false", help="Do not descend into child collections.")
    ap.add_argument(
        "--no-sync-folders",
        dest="sync_folders",
        action="store_false",
        help="Do not rename/move Drive folders to match Zotero collection changes.",
    )
    ap.add_argument(
        "--prune-missing-collections",
        action="store_true",
        help="Trash Drive folders for collections removed from Zotero (only folders tagged by this tool).",
    )
    ap.set_defaults(recursive=True, sync_folders=True)
    ap.add_argument("--overwrite", action="store_true", help="Overwrite files with the same name (default skips existing).")
    ap.add_argument("--dry-run", action="store_true", help="Preview folders/uploads without touching Google Drive.")
    ap.add_argument("--http-timeout", type=int, default=180, help="HTTP timeout (seconds) for Drive API uploads.")
    ap.add_argument("--upload-chunk-mb", type=int, default=5, help="Upload chunk size in MB (resumable uploads).")
    ap.add_argument("--zotero-timeout", type=int, default=45, help="HTTP timeout (seconds) for Zotero API.")
    ap.add_argument(
        "--zotero-retries",
        type=int,
        default=3,
        help="Retry count for Zotero API GET requests (handles transient SSL/connection errors).",
    )
    ap.add_argument(
        "--no-proxy",
        action="store_true",
        help="Ignore HTTP(S)_PROXY environment variables for Zotero API calls (useful if a local proxy breaks SSL).",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    user_id = ensure_env("ZOTERO_USER_ID")
    api_key = ensure_env("ZOTERO_API_KEY")
    drive_folder = args.drive_root_folder or os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER")
    if not drive_folder:
        raise SystemExit("Missing --drive-root-folder argument or GOOGLE_DRIVE_ROOT_FOLDER env variable.")
    oauth_client_file = args.oauth_client_file or os.environ.get("GOOGLE_OAUTH_CLIENT_FILE")
    oauth_token_file = args.oauth_token_file or os.environ.get("GOOGLE_OAUTH_TOKEN_FILE")
    use_oauth = bool(oauth_client_file)

    creds_file = args.credentials_file or os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )
    creds_path = Path(creds_file).expanduser() if creds_file else None
    if not args.dry_run and not use_oauth:
        if not creds_path:
            raise SystemExit("Missing Google service account credentials. Provide --credentials-file or set env var.")
        if not creds_path.exists():
            raise SystemExit(f"Google service account file not found: {creds_path}")

    storage_dir = Path(os.environ.get("ZOTERO_STORAGE_DIR", Path.home() / "Zotero" / "storage"))
    if not storage_dir.exists():
        print(f"[WARN] Zotero storage directory {storage_dir} does not exist; some attachments may fail.")

    zot = ZoteroAPI(
        user_id,
        api_key,
        timeout=args.zotero_timeout,
        use_env_proxy=not args.no_proxy,
        retries=max(0, args.zotero_retries),
    )
    collections = zot.fetch_collections()
    by_key, children_map = build_collection_maps(collections)

    root_key = resolve_collection_key(by_key, args.collection_name, args.collection)
    targets: List[Dict[str, Optional[str]]]
    if root_key:
        targets = [by_key[root_key]]
    else:
        targets = children_map.get(None, [])
        if not targets:
            raise SystemExit("No top-level collections found in Zotero library.")

    cfg = DriveConfig(
        credentials_file=creds_path,
        root_folder=drive_folder,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
        use_oauth=use_oauth,
        oauth_client_file=Path(oauth_client_file).expanduser() if oauth_client_file else None,
        oauth_token_file=Path(oauth_token_file).expanduser() if oauth_token_file else None,
        http_timeout=max(10, args.http_timeout),
        upload_chunk_size=max(256 * 1024, args.upload_chunk_mb * 1024 * 1024),
        sync_folders=args.sync_folders,
        prune_missing=args.prune_missing_collections,
    )
    drive = DriveClient(cfg)

    with tempfile.TemporaryDirectory(prefix="zotero_drive_") as tmp_dir:
        temp_path = Path(tmp_dir)
        for col in targets:
            export_collection(zot, drive, col, children_map, cfg.root_folder, storage_dir, args, temp_path)
    drive.prune_missing_collections(set(by_key.keys()))


if __name__ == "__main__":
    try:
        main()
    except HttpError as exc:  # pragma: no cover - surface Drive API errors
        print(f"[ERR] Google Drive API error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        sys.exit(130)
