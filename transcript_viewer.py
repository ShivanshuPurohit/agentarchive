"""Foreground, localhost-only AgentArchive transcript viewer."""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import http.cookies
import http.server
import json
import mimetypes
import pathlib
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from privacy_filter import transcript_is_excluded, workspace_is_excluded
from transcript_model import TRANSCRIPT_SUFFIXES, IndexCache, flatten, source_variant, transcript_page
from transcript_media import load_asset

REGISTRY_REFRESH_SECONDS = 300


def _iso_mtime(path: pathlib.Path) -> str:
    return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _source_id(harness: str, path: pathlib.Path) -> str:
    digest = hashlib.sha256(f"{harness}\0{path.resolve()}".encode()).hexdigest()[:20]
    return f"{harness[0]}-{digest}"


def _readonly(path: pathlib.Path) -> sqlite3.Connection:
    last: Optional[Exception] = None
    for attempt in range(3):
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=60)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return conn
        except sqlite3.OperationalError as exc:
            last = exc
            if conn is not None:
                conn.close()
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
    if not pathlib.Path(str(path) + "-wal").exists() and not pathlib.Path(str(path) + "-journal").exists():
        conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    raise sqlite3.OperationalError(f"unable to open read-only database: {path}") from last


def _first_json(path: pathlib.Path, ceiling: int = 512 * 1024) -> Dict[str, Any]:
    with path.open("rb") as handle:
        raw = handle.readline(ceiling + 1)
    if len(raw) > ceiling:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _claude_prefix(path: pathlib.Path, ceiling: int = 192 * 1024) -> Dict[str, str]:
    result: Dict[str, str] = {}
    read = 0
    try:
        with path.open("rb") as handle:
            while read < ceiling:
                raw = handle.readline(min(ceiling - read, 512 * 1024) + 1)
                if not raw or len(raw) + read > ceiling:
                    break
                read += len(raw)
                with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
                    obj = json.loads(raw)
                    if obj.get("type") == "custom-title" and obj.get("customTitle"):
                        result["custom"] = str(obj["customTitle"])
                    elif obj.get("type") == "ai-title" and obj.get("aiTitle"):
                        result["ai"] = str(obj["aiTitle"])
                    cwd = str(obj.get("cwd") or "")
                    started = str(obj.get("timestamp") or "")
                    if cwd and "cwd" not in result:
                        result["cwd"] = cwd
                    if started and "started" not in result:
                        result["started"] = started
                    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                    model = str(msg.get("model") or "")
                    if model and "model" not in result:
                        result["model"] = model
                    if obj.get("type") == "user" and not obj.get("isCompactSummary"):
                        prompt = _prompt_title(flatten(msg.get("content")))
                        if prompt:
                            result.setdefault("prompt", prompt)
    except OSError:
        pass
    return result


def _prompt_title(text: str) -> str:
    value = str(text or "").strip()
    lowered = value.casefold()
    if not value or lowered.startswith(("this session is being continued from a previous conversation",
                                         "<recommended_plugins>", "<app-context>",
                                         "<permissions instructions>", "<environment_context>",
                                         "<user_instructions>", "<local-command-",
                                         "# agents.md instructions")):
        return ""
    if "<command-args>" in value:
        value = value.split("<command-args>", 1)[1]
    value = re.sub(r"</?command-[^>]+>", " ", value)
    value = " ".join(value.split())
    if not value or (value.startswith("/") and " " not in value):
        return ""
    return value[:180]


def _codex_prompt(path: pathlib.Path, ceiling: int = 256 * 1024) -> str:
    read = 0
    try:
        with path.open("rb") as handle:
            while read < ceiling:
                raw = handle.readline(min(ceiling - read, 512 * 1024) + 1)
                if not raw or len(raw) + read > ceiling:
                    break
                read += len(raw)
                with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
                    obj = json.loads(raw)
                    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        title = _prompt_title(flatten(payload.get("content")))
                        if title:
                            return title
    except OSError:
        pass
    return ""


@dataclass
class Source:
    source_id: str
    harness: str
    path: pathlib.Path
    relpath: str
    owner: str
    root: str
    parent: str = ""
    kind: str = "main"
    storage: str = "live"
    title: str = ""
    project: str = ""
    model: str = ""
    updated: str = ""

    def public(self) -> Dict[str, Any]:
        return {"source_id": self.source_id, "harness": self.harness, "session_id": self.owner,
                "root_session_id": self.root, "parent_session_id": self.parent or None,
                "kind": self.kind, "storage": self.storage,
                "title": self.title or self.owner, "project": self.project,
                "model": self.model, "updated": self.updated}


class Registry:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.sources: Dict[str, Source] = {}
        self.by_path: Dict[str, str] = {}
        self._refresh_lock = threading.Lock()
        self._refreshing = False
        self.refreshed = 0.0
        self._build()

    def _overlays(self) -> tuple[Dict[str, Dict[str, str]], Dict[str, str], Dict[tuple, Dict[str, Any]]]:
        recent: Dict[tuple, Dict[str, Any]] = {}
        db = pathlib.Path(self.config["state_root"]) / "catalog.sqlite"
        if db.is_file():
            conn = _readonly(db)
            try:
                for row in conn.execute("SELECT * FROM sessions"):
                    if not workspace_is_excluded(self.config, row["cwd"]):
                        recent[(row["harness"], row["session_id"])] = dict(row)
            finally:
                conn.close()
        claude: Dict[str, Dict[str, str]] = {}
        root = pathlib.Path(self.config["sources"]["claude"]["root"]).expanduser()
        history = root / "history.jsonl"
        if history.is_file():
            with history.open("rb") as handle:
                for raw in handle:
                    with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
                        row = json.loads(raw)
                        if workspace_is_excluded(self.config, row.get("project")):
                            continue
                        sid, text = str(row.get("sessionId") or ""), str(row.get("display") or "").strip()
                        if not sid:
                            continue
                        item = claude.setdefault(sid, {})
                        if text and not text.startswith("/") and text not in {"login", "logout"}:
                            item.setdefault("title", " ".join(text.split())[:180])
                        if row.get("project"):
                            item["project"] = str(row["project"])
        codex: Dict[str, str] = {}
        croot = pathlib.Path(self.config["sources"]["codex"]["root"]).expanduser()
        names = croot / "session_index.jsonl"
        if names.is_file():
            with names.open("rb") as handle:
                for raw in handle:
                    with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
                        row = json.loads(raw)
                        if ("codex", row.get("id")) in recent and row.get("thread_name"):
                            codex[str(row["id"])] = str(row["thread_name"])
        return claude, codex, recent

    def _add(self, source: Source) -> None:
        if not source.path.is_file():
            return
        canonical = str(source.path.resolve())
        if canonical in self.by_path:
            return
        source.path = source.path.resolve()
        source.source_id = _source_id(source.harness, source.path)
        self.sources[source.source_id] = source
        self.by_path[canonical] = source.source_id

    def _add_live(self, claude: Dict[str, Dict[str, str]], codex: Dict[str, str], recent: Dict[tuple, Dict[str, Any]]) -> None:
        database = pathlib.Path(self.config["state_root"]) / "catalog.sqlite"
        if not database.is_file():
            return
        conn = _readonly(database)
        try:
            coordinates = list(conn.execute("SELECT harness,relpath FROM indexed_files"))
        finally:
            conn.close()
        for coordinate in coordinates:
            harness, rel = coordinate["harness"], coordinate["relpath"]
            if harness not in ("claude", "codex") or not rel.endswith(TRANSCRIPT_SUFFIXES):
                continue
            captured = harness == "claude" and rel.startswith("claude/")
            root = (pathlib.Path(self.config["state_root"]) / "inbox" if captured else
                    pathlib.Path(self.config["sources"][harness]["root"]).expanduser()).resolve()
            path = (root / rel).resolve()
            if root not in path.parents:
                continue
            if not path.is_file() or transcript_is_excluded(self.config, path):
                continue
            first = _first_json(path)
            if harness == "claude":
                child = "/subagents/" in f"/{rel}" or "/workflows/" in f"/{rel}" or bool(first.get("agentId"))
                workflow = "/workflows/" in f"/{rel}"
                owner = str(first.get("agentId") or path.stem) if child else str(first.get("sessionId") or path.stem)
                root_id = str(first.get("sessionId") or path.stem)
                prefix = _claude_prefix(path) if not child else {}
                overlay = claude.get(root_id, {})
                first_message = first.get("message") if isinstance(first.get("message"), dict) else {}
                child_title = _prompt_title(flatten(first_message.get("content"))) if child else ""
                title = (prefix.get("custom") or prefix.get("ai") or overlay.get("title")
                         or prefix.get("prompt") or child_title or str(first.get("slug") or ""))
                project = overlay.get("project") or prefix.get("cwd") or ""
                kind = "workflow" if workflow else ("subagent" if child else "main")
                model, updated = prefix.get("model", ""), prefix.get("started", "") or _iso_mtime(path)
                parent = ""
            else:
                payload = first.get("payload") if isinstance(first.get("payload"), dict) else first
                owner = str(payload.get("id") or path.stem)
                root_id = str(payload.get("session_id") or owner)
                parent = str(payload.get("parent_thread_id") or payload.get("forked_from_id") or "")
                source_kind = str(payload.get("source") or payload.get("thread_source") or "")
                kind = "subagent" if source_kind == "subagent" else "main"
                row = recent.get((harness, owner), {})
                title = _prompt_title(codex.get(owner) or str(row.get("first_prompt") or ""))
                project = str(row.get("cwd") or payload.get("cwd") or "")
                model = str(row.get("model") or "")
                updated = str(row.get("ended_utc") or row.get("started_utc") or _iso_mtime(path))
                source_kind = payload.get("source") or payload.get("thread_source")
                if isinstance(source_kind, dict) or source_kind == "subagent" or parent:
                    kind = "subagent"
                if not title and kind == "main":
                    title = _codex_prompt(path)
            # A kept original opens from a search hit; the sidebar lists the canonical file.
            if source_variant(rel) != "canonical":
                kind = "pre-import"
            self._add(Source("", harness, path, rel, owner, root_id, parent, kind,
                             "captured" if captured else "archived" if "archived_sessions" in rel else "live",
                             title, project, model, updated))

    def _legacy_claude_path(self, database: pathlib.Path, relpath: str) -> Optional[pathlib.Path]:
        with contextlib.suppress(IndexError):
            workspace = database.resolve().parents[2]
            for snapshot in sorted((workspace / "source_snapshot").glob("claude_home_*"), reverse=True):
                for candidate in (snapshot / "projects" / relpath, snapshot / relpath):
                    if candidate.is_file():
                        return candidate
        return None

    def _add_legacy(self, claude: Dict[str, Dict[str, str]], codex: Dict[str, str]) -> None:
        for entry in self.config.get("legacy_indexes", []):
            database = pathlib.Path(entry["database"])
            if not database.is_file():
                continue
            conn = _readonly(database)
            try:
                if entry["harness"] == "claude":
                    rows = conn.execute("SELECT relpath,kind FROM files WHERE kind IN ('main_session','subagent')")
                    for row in rows:
                        path = self._legacy_claude_path(database, row["relpath"])
                        if not path or transcript_is_excluded(self.config, path):
                            continue
                        first = _first_json(path)
                        child = row["kind"] == "subagent"
                        owner = str(first.get("agentId") or path.stem) if child else path.stem
                        root = str(first.get("sessionId") or owner)
                        prefix, overlay = _claude_prefix(path), claude.get(owner, {})
                        self._add(Source("", "claude", path, str(row["relpath"]), owner, root,
                                         parent=root if child else "", kind="subagent" if child else "main", storage="legacy",
                                         title=(prefix.get("custom") or prefix.get("ai") or overlay.get("title")
                                                or prefix.get("prompt", "")),
                                         project=overlay.get("project") or prefix.get("cwd", ""),
                                         model=prefix.get("model", ""),
                                         updated=prefix.get("started", "") or _iso_mtime(path)))
                else:
                    rows = conn.execute("SELECT relpath,source_path,thread_id,root_thread_id,end_timestamp FROM files")
                    for row in rows:
                        path = pathlib.Path(row["source_path"])
                        if not path.is_file() or transcript_is_excluded(self.config, path):
                            continue
                        owner = str(row["thread_id"])
                        header = _first_json(path).get("payload", {})
                        parent = str(header.get("parent_thread_id") or header.get("forked_from_id") or "")
                        self._add(Source("", "codex", path, str(row["relpath"]), owner,
                                         str(row["root_thread_id"] or owner), parent=parent,
                                         kind="subagent" if parent else "main", storage="legacy",
                                         title=_prompt_title(codex.get(owner, "")) or _codex_prompt(path),
                                         project=str(header.get("cwd") or ""),
                                         updated=str(row["end_timestamp"] or _iso_mtime(path))))
            finally:
                conn.close()

    def _build(self) -> None:
        self.sources, self.by_path = {}, {}
        claude, codex, recent = self._overlays()
        self._add_live(claude, codex, recent)
        self._add_legacy(claude, codex)
        self.refreshed = time.monotonic()

    def _refresh_worker(self) -> None:
        try:
            fresh = object.__new__(Registry)
            fresh.config, fresh.sources, fresh.by_path = self.config, {}, {}
            fresh._refresh_lock, fresh._refreshing, fresh.refreshed = threading.Lock(), False, 0.0
            fresh._build()
            with self._refresh_lock:
                self.sources, self.by_path = fresh.sources, fresh.by_path
                self.refreshed = time.monotonic()
        finally:
            self._refreshing = False

    def maybe_refresh(self) -> None:
        if time.monotonic() - self.refreshed <= REGISTRY_REFRESH_SECONDS or self._refreshing:
            return
        with self._refresh_lock:
            if self._refreshing:
                return
            self._refreshing = True
        threading.Thread(target=self._refresh_worker, name="agentarchive-viewer-refresh",
                         daemon=True).start()

    def source(self, source_id: str) -> Source:
        self.maybe_refresh()
        with self._refresh_lock:
            source = self.sources.get(source_id)
        if not source:
            raise KeyError("unknown source")
        return source

    def route_path(self, raw_path: str) -> Optional[Source]:
        with contextlib.suppress(OSError):
            with self._refresh_lock:
                sid = self.by_path.get(str(pathlib.Path(raw_path).resolve()))
                return self.sources.get(sid or "")
        return None

    def sessions(self, query: str, harness: str, children: bool, cursor: str, limit: int) -> Dict[str, Any]:
        self.maybe_refresh()
        priorities = {"live": 4, "archived": 3, "captured": 2, "legacy": 1}
        canonical: Dict[tuple, Source] = {}
        with self._refresh_lock:
            sources = tuple(self.sources.values())
        for source in sources:
            if source.kind == "pre-import" or (source.kind in {"subagent", "workflow"} and not children):
                continue
            if harness and source.harness != harness:
                continue
            haystack = f"{source.title} {source.project} {source.owner}".casefold()
            if query and query.casefold() not in haystack:
                continue
            key = (source.harness, source.owner)
            current = canonical.get(key)
            if current is None or priorities[source.storage] > priorities[current.storage]:
                canonical[key] = source
        rows = sorted(canonical.values(), key=lambda s: (s.updated, s.harness, s.owner), reverse=True)
        start = next((i + 1 for i, row in enumerate(rows) if row.source_id == cursor), 0) if cursor else 0
        page = rows[start:start + limit]
        return {"sessions": [row.public() for row in page],
                "next_cursor": page[-1].source_id if start + limit < len(rows) and page else None,
                "total": len(rows)}


class ViewerState:
    def __init__(self, config: Dict[str, Any], search: Callable[..., List[Dict[str, Any]]]):
        self.config, self.search = config, search
        self.registry, self.cache = Registry(config), IndexCache(4)
        self.token = secrets.token_urlsafe(32)
        self.authority = ""
        self.lock = threading.RLock()
        from agentarchive.runtime import viewer_root
        root = viewer_root(pathlib.Path(__file__).resolve().parent)
        if not (root / "index.html").is_file():
            raise RuntimeError("Viewer build missing. Run npm --prefix viewer ci && npm --prefix viewer run build.")
        self.assets = {"/" + path.relative_to(root).as_posix():
                       (mimetypes.guess_type(path.name)[0] or "application/octet-stream", path.read_bytes())
                       for path in root.rglob("*") if path.is_file() and not path.is_symlink()}
        self.assets["/"] = self.assets["/index.html"]

    def status(self) -> Dict[str, Any]:
        db = pathlib.Path(self.config["state_root"]) / "catalog.sqlite"
        conn = _readonly(db)
        try:
            capture = conn.execute("SELECT * FROM captures WHERE status IN ('complete','local_complete_offsite_failed') ORDER BY capture_id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        return {"latest_capture": dict(capture) if capture else None,
                "sources": len(self.registry.sources),
                "contract": "Search is capture-bounded; opened transcripts are read live from authoritative JSONL."}

    def source_status(self, source: Source) -> Dict[str, Any]:
        result = {"live_bytes": source.path.stat().st_size, "indexed_bytes": None, "live_delta": None}
        root = pathlib.Path(self.config["sources"][source.harness]["root"]).expanduser().resolve()
        with contextlib.suppress(ValueError):
            rel = source.path.relative_to(root).as_posix()
            db = pathlib.Path(self.config["state_root"]) / "catalog.sqlite"
            conn = _readonly(db)
            try:
                row = conn.execute("SELECT size_bytes,last_capture FROM indexed_files WHERE harness=? AND relpath=?",
                                   (source.harness, rel)).fetchone()
                if row:
                    result.update({"indexed_bytes": row[0], "indexed_capture": row[1],
                                   "live_delta": row[0] != result["live_bytes"]})
            finally:
                conn.close()
        return result


def _bounded_int(values: Dict[str, List[str]], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(values.get(name, [str(default)])[0])
    except ValueError:
        raise ValueError(f"invalid {name}")
    return max(low, min(high, value))


def _handler(state: ViewerState) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "AgentArchiveViewer/1"

        def log_message(self, _format: str, *args: Any) -> None:
            return

        def _headers(self, status: int, content_type: str, length: int = 0, cookie: str = "") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; frame-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()

        def _json(self, value: Any, status: int = 200) -> None:
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            self._headers(status, "application/json; charset=utf-8", len(raw))
            if self.command != "HEAD":
                self.wfile.write(raw)

        def _authenticated(self) -> bool:
            if self.headers.get("Host") != state.authority:
                return False
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            value = jar.get("agent_archive_viewer_" + state.authority.rsplit(":", 1)[-1])
            return bool(value and secrets.compare_digest(value.value, state.token))

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if self.headers.get("Host") != state.authority:
                self._json({"error": "invalid host"}, 403)
                return
            if parsed.path == "/" and query.get("token"):
                if secrets.compare_digest(query["token"][0], state.token):
                    self.send_response(303)
                    self.send_header("Location", "/")
                    cookie_name = "agent_archive_viewer_" + state.authority.rsplit(":", 1)[-1]
                    self.send_header("Set-Cookie", f"{cookie_name}={state.token}; HttpOnly; SameSite=Strict; Path=/")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
            if not self._authenticated():
                self._json({"error": "viewer capability required"}, 403)
                return
            try:
                if parsed.path in state.assets:
                    media, raw = state.assets[parsed.path]
                    self._headers(200, media, len(raw))
                    if self.command != "HEAD":
                        self.wfile.write(raw)
                elif parsed.path == "/api/status":
                    self._json(state.status())
                elif parsed.path == "/api/sessions":
                    q = query.get("q", [""])[0][:256]
                    harness = query.get("harness", [""])[0]
                    if harness not in {"", "claude", "codex"}:
                        raise ValueError("invalid harness")
                    self._json(state.registry.sessions(q, harness, query.get("children", ["0"])[0] == "1",
                                                       query.get("cursor", [""])[0],
                                                       _bounded_int(query, "limit", 50, 1, 100)))
                elif parsed.path == "/api/search":
                    q = query.get("q", [""])[0].strip()
                    if not q or len(q) > 512:
                        raise ValueError("search query must be 1-512 characters")
                    hits = state.search(state.config, q, _bounded_int(query, "limit", 20, 1, 50))
                    routed = []
                    for hit in hits:
                        item = dict(hit)
                        source = state.registry.route_path(str(item.get("raw_path") or ""))
                        if source:
                            item["source_id"] = source.source_id
                            item["transcript_url"] = f"#source={source.source_id}&line={item.get('line_no') or 1}"
                        routed.append(item)
                    self._json({"hits": routed})
                elif parsed.path.startswith("/api/transcripts/"):
                    tail = parsed.path.removeprefix("/api/transcripts/").split("/")
                    source = state.registry.source(tail[0])
                    with state.lock:
                        index = state.cache.get(source.harness, source.path)
                        if len(tail) == 2 and tail[1] == "usage":
                            result = index.usage.public(_bounded_int(query, "line", 1, 1, max(1, index.line_count)),
                                                        _bounded_int(query, "after", 0, 0, max(1, index.line_count)))
                            result["parse_errors"] = index.parse_errors
                            self._json(result)
                        elif len(tail) == 2 and tail[1] == "media":
                            media, raw = load_asset(state.config, index,
                                                   _bounded_int(query, "line", 1, 1, max(1, index.line_count)),
                                                   _bounded_int(query, "index", 0, 0, 10000),
                                                   _bounded_int(query, "asset", 0, 0, 63))
                            self._headers(200, media, len(raw))
                            if self.command != "HEAD":
                                self.wfile.write(raw)
                        elif len(tail) == 2 and tail[1] == "item":
                            line = _bounded_int(query, "line", 1, 1, max(1, index.line_count))
                            number = _bounded_int(query, "index", 0, 0, 10000)
                            offset = _bounded_int(query, "offset", 0, 0, 2 ** 31 - 1)
                            size = _bounded_int(query, "limit", 65536, 1, 65536)
                            item = next((x for x in index.components(line) if int(x["index"]) == number), None)
                            if item is None:
                                raise KeyError("unknown transcript item")
                            text = str(item.get("text") or "")
                            self._json({"ref": {"line": line, "index": number}, "offset": offset,
                                        "text": text[offset:offset + size], "char_count": len(text),
                                        "next_offset": offset + size if offset + size < len(text) else None})
                        elif len(tail) == 1:
                            page = transcript_page(index,
                                                   _bounded_int(query, "from_line", 1, 1, max(1, index.line_count)),
                                                   _bounded_int(query, "limit", 120, 20, 200),
                                                   _bounded_int(query, "focus_line", 0, 0, max(1, index.line_count)))
                            page.update({"source": source.public(), "freshness": state.source_status(source)})
                            with state.registry._refresh_lock:
                                family = [s.public() for s in state.registry.sources.values()
                                          if s.harness == source.harness and s.source_id != source.source_id
                                          and (s.root == source.root or s.parent == source.owner or s.owner == source.parent)]
                            page["related"] = list({s["session_id"]: s for s in reversed(family)}.values())[:100]
                            self._json(page)
                        else:
                            raise KeyError("unknown endpoint")
                else:
                    self._json({"error": "not found"}, 404)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except (KeyError, FileNotFoundError):
                self._json({"error": "not found"}, 404)
            except Exception:
                self._json({"error": "viewer request failed"}, 500)

        def do_POST(self) -> None:
            self._json({"error": "read-only viewer"}, 405)

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

    return Handler


def serve(config: Dict[str, Any], search: Callable[..., List[Dict[str, Any]]], port: int = 0,
          open_browser: bool = True) -> int:
    state = ViewerState(config, search)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _handler(state))
    server.daemon_threads = True
    state.authority = f"127.0.0.1:{server.server_address[1]}"
    url = f"http://{state.authority}/?token={urllib.parse.quote(state.token)}"
    print(f"AgentArchive viewer: {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
