"""Lossless-enough, bounded transcript projection over authoritative JSONL.

The archive catalog discovers evidence; this module renders a selected raw
source.  It deliberately stores no transcript text and never exposes opaque
reasoning ciphertext or signatures.
"""

from __future__ import annotations

import collections
import bisect
import hashlib
import json
import os
import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from transcript_media import MEDIA_TYPES, component_value, descriptors, redact_media_text
from transcript_usage import UsageIndex

MAX_RECORD = 32 * 1024 * 1024
PREVIEW_CHARS = 6000
OPAQUE_KEYS = {"encrypted_content", "signature"}


def _opaque(value: Any, label: str) -> str:
    size = len(value) if isinstance(value, (str, bytes, list, dict)) else 0
    return f"<{label}: {size:,} characters>"


def scrub(value: Any, key: str = "") -> Any:
    """Remove private/binary envelopes while preserving useful structure."""
    if key in OPAQUE_KEYS:
        return _opaque(value, key.replace("_", " "))
    if key == "data" and isinstance(value, str) and len(value) > 256:
        return _opaque(value, "binary data")
    if isinstance(value, dict):
        return {str(k): scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def json_text(value: Any) -> str:
    return json.dumps(scrub(value), ensure_ascii=False, indent=2, sort_keys=True)


def flatten(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := flatten(item)))
    if isinstance(value, dict):
        kind = value.get("type")
        if kind in {"text", "input_text", "output_text", "summary_text"}:
            return str(value.get("text") or "")
        if kind in {"image", "input_image", "output_image", "image_url", "document", "audio", "input_audio", "output_audio", "video", "input_file"}:
            source = value.get("source") or {}
            media = source.get("media_type") if isinstance(source, dict) else None
            return f"[{kind}{': ' + media if media else ''}]"
        if "content" in value:
            return flatten(value["content"])
        return json_text(value)
    return "" if value is None else str(value)


def _item(line: int, index: int, kind: str, text: str = "", **extra: Any) -> Dict[str, Any]:
    return {"line": line, "index": index, "kind": kind, "text": text, **extra}


def _codex_message_visibility(role: str, text: str, metadata: Any) -> str:
    if role not in {"user", "assistant"}:
        return "verbose"
    lowered = text.lstrip().casefold()
    internal = isinstance(metadata, dict) and bool(metadata.get("kind") or metadata.get("hidden"))
    if internal or lowered.startswith(("<app-context>", "<permissions instructions>",
                                       "<recommended_plugins>", "<environment_context>",
                                       "<user_instructions>", "# agents.md instructions")):
        return "verbose"
    return "summary"


def _call_title(name: Optional[str], raw: Any) -> str:
    name = str(name or "Tool")
    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = raw
    if isinstance(value, dict):
        hint = (value.get("command") or value.get("query") or value.get("pattern")
                or value.get("file_path") or value.get("path") or value.get("description"))
    else:
        hint = value
    hint = " ".join(str(hint or "").split())
    return f"{name} · {hint[:110]}" if hint else name


def _status_from_output(value: Any) -> Optional[str]:
    text = flatten(value).casefold()
    if "script failed" in text or "iserror\": true" in text or "is_error\": true" in text:
        return "error"
    if "script completed" in text:
        return "success"
    return None


def _claude(obj: Dict[str, Any], line: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    top, ts = obj.get("type"), obj.get("timestamp")
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = msg.get("content")
    blocks = content if isinstance(content, list) else []
    base = {"timestamp": ts, "model": msg.get("model"), "origin": "durable",
            "event_id": obj.get("uuid"), "model_call_id": msg.get("id") if top == "assistant" else None}
    if top == "assistant":
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                out.append(_item(line, i, "message", str(block.get("text") or ""),
                                 role="assistant", visibility="summary", **base))
            elif kind == "thinking":
                text = str(block.get("thinking") or "")
                if not text:
                    text = _opaque(block.get("signature") or "", "encrypted reasoning")
                out.append(_item(line, i, "reasoning", text, role="reasoning",
                                 visibility="thinking", opaque=not bool(block.get("thinking")), **base))
            elif kind == "redacted_thinking":
                out.append(_item(line, i, "reasoning", _opaque(block.get("data") or "", "redacted reasoning"),
                                 role="reasoning", visibility="thinking", opaque=True, **base))
            elif kind in {"tool_use", "server_tool_use"}:
                raw = block.get("input") or {}
                out.append(_item(line, i, "tool_component", json_text(raw), role="tool",
                                 visibility="normal", phase="request", call_id=block.get("id"),
                                 tool_name=block.get("name"), title=_call_title(block.get("name"), raw), **base))
            elif kind == "fallback":
                out.append(_item(line, i, "boundary", flatten(block), role="system",
                                 visibility="summary", title="Model fallback", **base))
            elif kind in MEDIA_TYPES:
                out.append(_item(line, i, "attachment", flatten(block), role="assistant",
                                 visibility="normal", title=kind.title(), **base))
            else:
                out.append(_item(line, i, "metadata", json_text(block), role="system",
                                 visibility="verbose", title=str(kind or "Unknown block"), **base))
    elif top == "user":
        compact = bool(obj.get("isCompactSummary"))
        visibility = "verbose" if obj.get("isMeta") else "summary"
        if isinstance(content, str):
            out.append(_item(line, 0, "boundary" if compact else "message", content,
                             role="system" if compact else "user",
                             visibility="summary" if compact else visibility, **base))
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_result":
                out.append(_item(line, i, "tool_component", flatten(block.get("content")), role="tool",
                                 visibility="normal", phase="result", call_id=block.get("tool_use_id"),
                                 status="error" if block.get("is_error") else "success", **base))
            elif kind == "text":
                out.append(_item(line, i, "boundary" if compact else "message",
                                 str(block.get("text") or ""), role="system" if compact else "user",
                                 visibility="summary" if compact else visibility, **base))
            elif kind in MEDIA_TYPES:
                out.append(_item(line, i, "attachment", flatten(block), role="user",
                                 visibility="normal", title=kind.title(), **base))
            else:
                out.append(_item(line, i, "metadata", json_text(block), role="system",
                                 visibility="verbose", title=str(kind or "User block"), **base))
        structured = obj.get("toolUseResult")
        call_id = next((b.get("tool_use_id") for b in blocks if isinstance(b, dict)
                        and b.get("type") == "tool_result"), None)
        if structured is not None and call_id:
            out.append(_item(line, len(blocks), "tool_component", json_text(structured), role="tool",
                             visibility="verbose", phase="structured result", call_id=call_id, **base))
    elif top == "system":
        subtype = str(obj.get("subtype") or "System")
        boundary = subtype in {"compact_boundary", "model_refusal_fallback"}
        text = flatten(obj.get("content")) or ("Conversation compacted" if boundary else json_text(obj))
        out.append(_item(line, 0, "boundary" if boundary else "metadata", text, role="system",
                         visibility="summary" if boundary else "verbose", title=subtype, **base))
    elif top == "attachment":
        out.append(_item(line, 0, "attachment", json_text(obj.get("attachment") or {}),
                         role="system", visibility="verbose", title="Attachment", **base))
    elif top not in {"custom-title", "ai-title", "last-prompt"}:
        out.append(_item(line, 0, "metadata", json_text(obj), role="system",
                         visibility="verbose", title=str(top or "Unknown event"), **base))
    return out


def _codex(obj: Dict[str, Any], line: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    top, ts = obj.get("type"), obj.get("timestamp")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
    inner = payload.get("type") if isinstance(payload, dict) else None
    base = {"timestamp": ts, "origin": "durable",
            "event_id": payload.get("id") if isinstance(payload, dict) else None}
    if top in {"session_meta", "turn_context", "world_state", "inter_agent_communication_metadata"}:
        return [_item(line, 0, "metadata", json_text(payload), role="system",
                      visibility="verbose", title=str(top), **base)]
    if top == "compacted":
        history = payload.get("replacement_history") or []
        opaque = sum(len(x.get("encrypted_content") or "") for x in history if isinstance(x, dict))
        text = str(payload.get("message") or "Conversation compacted")
        text += f" · {len(history)} replacement items" + (f" · {opaque:,} opaque characters" if opaque else "")
        return [_item(line, 0, "boundary", text, role="system", visibility="summary", **base)]
    if top == "event_msg":
        event = str(inner or "event")
        base["origin"] = "stream"
        if event in {"agent_message", "user_message", "agent_reasoning"}:
            text = str(payload.get("message") or payload.get("text") or "")
            role = "assistant" if event == "agent_message" else ("user" if event == "user_message" else "reasoning")
            kind = "reasoning" if event == "agent_reasoning" else "message"
            return [_item(line, 0, kind, text, role=role,
                          visibility="thinking" if kind == "reasoning" else "summary", **base)]
        call_id = payload.get("call_id") or payload.get("event_id")
        if call_id and event in {"exec_command_begin", "patch_apply_begin", "mcp_tool_call_begin",
                                 "exec_command_end", "patch_apply_end", "mcp_tool_call_end", "sub_agent_activity"}:
            status: Optional[str] = None
            if event == "exec_command_end" and isinstance(payload.get("exit_code"), int):
                status = "success" if payload["exit_code"] == 0 else "error"
            elif event == "patch_apply_end":
                status = "success" if payload.get("success") else "error"
            elif event == "mcp_tool_call_end":
                ok = (payload.get("result") or {}).get("Ok") if isinstance(payload.get("result"), dict) else None
                status = "error" if isinstance(ok, dict) and ok.get("isError") else "success"
            text = json_text(payload)
            return [_item(line, 0, "tool_component", text, role="tool", visibility="normal",
                          phase="start" if event.endswith("_begin") else "lifecycle",
                          call_id=call_id, tool_name=event, status=status,
                          title=_call_title(event, payload), **base)]
        important = event in {"turn_aborted", "thread_rolled_back"}
        return [_item(line, 0, "boundary" if important else "metadata", json_text(payload),
                      role="system", visibility="normal" if important else "verbose", title=event, **base)]
    flat = top not in {"response_item", "event_msg"} and isinstance(obj.get("type"), str)
    data = payload if top == "response_item" else obj
    inner = data.get("type") if isinstance(data, dict) else inner
    if top != "response_item" and not flat:
        return []
    if inner == "message":
        role = str(data.get("role") or "assistant")
        raw_content = data.get("content")
        blocks = raw_content if isinstance(raw_content, list) else [raw_content]
        metadata = data.get("internal_chat_message_metadata_passthrough")
        return [_item(line, i, "message", flatten(block), role=role,
                      visibility=_codex_message_visibility(role, flatten(block), metadata), **base)
                for i, block in enumerate(blocks) if block is not None] or [
                    _item(line, 0, "message", "", role=role,
                          visibility=_codex_message_visibility(role, "", metadata), **base)]
    if inner == "reasoning":
        summaries = data.get("summary") or []
        content = data.get("content") or []
        result = [_item(line, i, "reasoning", flatten(block), role="reasoning",
                        visibility="thinking", **base) for i, block in enumerate(summaries) if flatten(block)]
        result.extend(_item(line, len(summaries) + i, "reasoning", flatten(block), role="reasoning",
                            visibility="thinking", **base) for i, block in enumerate(content) if flatten(block))
        if data.get("encrypted_content"):
            result.append(_item(line, len(summaries) + len(content), "reasoning",
                                _opaque(data["encrypted_content"], "encrypted reasoning"),
                                role="reasoning", visibility="thinking", opaque=True, **base))
        return result
    if inner in {"function_call", "custom_tool_call"}:
        raw = data.get("arguments") if inner == "function_call" else data.get("input")
        text = raw if isinstance(raw, str) else json_text(raw or {})
        return [_item(line, 0, "tool_component", text, role="tool", visibility="normal",
                      phase="request", call_id=data.get("call_id"), tool_name=data.get("name"),
                      title=_call_title(data.get("name"), raw), **base)]
    if inner in {"function_call_output", "custom_tool_call_output"}:
        value = data.get("output")
        status = _status_from_output(value)
        return [_item(line, 0, "tool_component", flatten(value), role="tool", visibility="normal",
                      phase="result", call_id=data.get("call_id"), status=status, **base)]
    if inner == "web_search_call":
        return [_item(line, 0, "tool_component", json_text(data.get("action") or data), role="tool",
                      visibility="normal", phase="request", call_id=data.get("call_id") or data.get("id"),
                      tool_name="web_search", title="Web search", **base)]
    if inner == "agent_message":
        return [_item(line, 0, "message", flatten(data.get("content")), role="assistant",
                      visibility="summary", **base)]
    return [_item(line, 0, "metadata", json_text(data), role="system", visibility="verbose",
                  title=str(inner or top or "Unknown event"), **base)]


def normalize_record(harness: str, obj: Dict[str, Any], line: int) -> List[Dict[str, Any]]:
    items = _claude(obj, line) if harness == "claude" else _codex(obj, line)
    for item in items:
        media = descriptors(component_value(harness, obj, item["index"]))
        if media:
            item["media"] = media
            item["text"] = redact_media_text(item["text"], component_value(harness, obj, item["index"]))
    return items


@dataclass
class SourceIndex:
    harness: str
    path: pathlib.Path
    offsets: List[int] = field(default_factory=lambda: [0])
    lengths: List[int] = field(default_factory=lambda: [0])
    calls: Dict[str, List[Tuple[int, int, str]]] = field(default_factory=lambda: collections.defaultdict(list))
    component_calls: Dict[Tuple[int, int], str] = field(default_factory=dict)
    call_ids: Dict[str, str] = field(default_factory=dict)
    active_calls: Dict[str, str] = field(default_factory=dict)
    durable: Dict[Tuple[str, str], List[int]] = field(default_factory=lambda: collections.defaultdict(list))
    streams: List[Tuple[int, int, str, str]] = field(default_factory=list)
    mirrors: set = field(default_factory=set)
    duplicates: set = field(default_factory=set)
    seen_events: Dict[str, int] = field(default_factory=dict)
    canonical_lines: List[int] = field(default_factory=list)
    canonical_line_set: set = field(default_factory=set)
    title_custom: str = ""
    title_ai: str = ""
    owner: str = ""
    parent: str = ""
    root: str = ""
    model: str = ""
    cwd: str = ""
    safe_size: int = 0
    inode: int = 0
    prefix: str = ""
    mtime_ns: int = 0
    parse_errors: int = 0

    def __post_init__(self) -> None:
        self.path = self.path.resolve()
        self._rebuild()

    @property
    def line_count(self) -> int:
        return len(self.offsets) - 1

    @property
    def revision(self) -> str:
        return f"{self.inode:x}-{self.safe_size:x}-{self.prefix[:12]}"

    def _prefix(self) -> str:
        with self.path.open("rb") as handle:
            return hashlib.sha256(handle.read(65536)).hexdigest()

    def _metadata(self, obj: Dict[str, Any], first: bool) -> None:
        if self.harness == "claude":
            self.owner = self.owner or str(obj.get("agentId") or obj.get("sessionId") or self.path.stem)
            self.root = self.root or str(obj.get("sessionId") or self.path.stem)
            self.cwd = self.cwd or str(obj.get("cwd") or "")
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            self.model = self.model or str(msg.get("model") or "")
            if obj.get("type") == "custom-title" and obj.get("customTitle"):
                self.title_custom = str(obj["customTitle"])
            if obj.get("type") == "ai-title" and obj.get("aiTitle"):
                self.title_ai = str(obj["aiTitle"])
        elif first:
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
            self.owner = str(payload.get("id") or self.path.stem)
            self.root = str(payload.get("session_id") or self.owner)
            self.parent = str(payload.get("parent_thread_id") or payload.get("forked_from_id") or "")
            self.cwd = str(payload.get("cwd") or "")
        elif obj.get("type") == "turn_context" and isinstance(obj.get("payload"), dict):
            self.model = str(obj["payload"].get("model") or self.model)
            self.cwd = self.cwd or str(obj["payload"].get("cwd") or "")

    def _consume(self, raw: bytes, offset: int, line: int) -> None:
        self.offsets.append(offset)
        self.lengths.append(len(raw))
        if len(raw) > MAX_RECORD:
            self.parse_errors += 1
            return
        try:
            obj = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.parse_errors += 1
            return
        self._metadata(obj, line == 1)
        canonical = False
        components = normalize_record(self.harness, obj, line)
        self.usage.consume(self.harness, obj, line, components, self.model)
        for component in components:
            key = (line, int(component["index"]))
            event_id = str(component.get("event_id") or "")
            if event_id:
                if event_id in self.seen_events and self.seen_events[event_id] != line:
                    self.duplicates.add(key)
                else:
                    self.seen_events[event_id] = line
            if key not in self.duplicates:
                canonical = True
            call_id = component.get("call_id")
            if call_id:
                call_id = str(call_id)
                phase = str(component.get("phase") or "event")
                group = self.active_calls.get(call_id)
                if phase == "request" and group and any(ref[2] == "request" for ref in self.calls[group]):
                    group = f"{call_id}@{line}:{key[1]}"
                group = group or call_id
                self.active_calls[call_id] = group
                self.call_ids[group] = call_id
                self.calls[group].append((line, key[1], phase))
                self.component_calls[key] = group
            text = str(component.get("text") or "")
            if component.get("kind") in {"message", "reasoning"} and text:
                digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
                role = str(component.get("role") or "")
                if component.get("origin") == "stream":
                    self.streams.append((line, key[1], role, digest))
                else:
                    self.durable[(role, digest)].append(line)
        if canonical:
            self.canonical_line_set.add(line)

    def _finish(self, stat: os.stat_result) -> None:
        self.mirrors.clear()
        for line, index, role, digest in self.streams:
            if any(abs(other - line) <= 6 for other in self.durable.get((role, digest), ())):
                self.mirrors.add((line, index))
        self.inode, self.mtime_ns = stat.st_ino, stat.st_mtime_ns
        self.canonical_lines = sorted(self.canonical_line_set)

    def _rebuild(self) -> None:
        self.usage = UsageIndex()
        self.offsets, self.lengths = [0], [0]
        self.calls, self.component_calls, self.call_ids, self.active_calls = collections.defaultdict(list), {}, {}, {}
        self.durable, self.streams, self.mirrors = collections.defaultdict(list), [], set()
        self.duplicates, self.seen_events = set(), {}
        self.canonical_lines, self.canonical_line_set = [], set()
        self.parse_errors = 0
        stat = self.path.stat()
        self.prefix = self._prefix()
        with self.path.open("rb") as handle:
            line = 0
            while raw := handle.readline():
                if not raw.endswith((b"\n", b"\r")):
                    break
                line += 1
                self._consume(raw, handle.tell() - len(raw), line)
            self.safe_size = handle.tell() if not raw else handle.tell() - len(raw)
        self._finish(stat)

    def refresh(self) -> None:
        stat = self.path.stat()
        prefix = self._prefix()
        if stat.st_ino != self.inode or stat.st_size < self.safe_size or prefix != self.prefix:
            self._rebuild()
            return
        if stat.st_size == self.safe_size:
            self.mtime_ns = stat.st_mtime_ns
            return
        with self.path.open("rb") as handle:
            handle.seek(self.safe_size)
            line = self.line_count
            while raw := handle.readline():
                if not raw.endswith((b"\n", b"\r")):
                    break
                line += 1
                self._consume(raw, handle.tell() - len(raw), line)
                self.safe_size = handle.tell()
        self._finish(stat)

    def records(self, lines: Iterable[int]) -> Dict[int, Dict[str, Any]]:
        result: Dict[int, Dict[str, Any]] = {}
        with self.path.open("rb") as handle:
            for line in sorted(set(n for n in lines if 1 <= n <= self.line_count)):
                handle.seek(self.offsets[line])
                raw = handle.read(self.lengths[line])
                if len(raw) <= MAX_RECORD:
                    try:
                        result[line] = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        pass
        return result

    def components(self, line: int) -> List[Dict[str, Any]]:
        obj = self.records([line]).get(line)
        if not obj:
            return [_item(line, 0, "metadata", "<unreadable or oversized record>", role="system",
                          visibility="verbose", title="Parse error")]
        items = normalize_record(self.harness, obj, line)
        for item in items:
            if (line, int(item["index"])) in self.mirrors:
                item["visibility"] = "verbose"
                item["mirror"] = True
            if (line, int(item["index"])) in self.duplicates:
                item["visibility"] = "verbose"
                item["duplicate"] = True
        return items

    def title(self) -> str:
        return self.title_custom or self.title_ai

    def canonical_line(self, line: int) -> int:
        obj = self.records([line]).get(line) or {}
        if self.harness == "claude":
            event_id = str(obj.get("uuid") or "")
        else:
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
            event_id = str(payload.get("id") or "")
        return self.seen_events.get(event_id, line) if event_id else line


class IndexCache:
    def __init__(self, capacity: int = 4):
        self.capacity = capacity
        self._items: "collections.OrderedDict[str, SourceIndex]" = collections.OrderedDict()

    def get(self, harness: str, path: pathlib.Path) -> SourceIndex:
        key = f"{harness}:{path.resolve()}"
        index = self._items.pop(key, None)
        if index is None:
            index = SourceIndex(harness, path)
        else:
            index.refresh()
        self._items[key] = index
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)
        return index


def public_component(item: Dict[str, Any]) -> Dict[str, Any]:
    result = {key: value for key, value in item.items() if key != "text" and value not in (None, "")}
    text = str(item.get("text") or "")
    result.update({"text_preview": text[:PREVIEW_CHARS], "char_count": len(text),
                   "truncated": len(text) > PREVIEW_CHARS,
                   "ref": {"line": item["line"], "index": item["index"]}})
    return result


def transcript_page(index: SourceIndex, start: int, limit: int, focus: int = 0) -> Dict[str, Any]:
    index.refresh()
    if focus:
        focus = index.canonical_line(focus)
        group_id = next((gid for (line, _), gid in index.component_calls.items() if line == focus), None)
        anchor = min((ref[0] for ref in index.calls.get(group_id, ())), default=focus)
        focus = anchor
        start = max(1, anchor - max(12, limit // 4))
    start = max(1, min(start, max(1, index.line_count)))
    available = index.canonical_lines or list(range(1, index.line_count + 1))
    position = bisect.bisect_left(available, start)
    base_lines = available[position:position + limit]
    if not base_lines:
        base_lines = available[-limit:]
        position = max(0, len(available) - len(base_lines))
    end = base_lines[-1] if base_lines else 0
    records = index.records(base_lines)
    components: List[Dict[str, Any]] = []
    emitted_calls = set()
    for line in base_lines:
        obj = records.get(line)
        if not obj:
            components.extend(index.components(line))
            continue
        for item in normalize_record(index.harness, obj, line):
            key = (line, int(item["index"]))
            group_id = index.component_calls.get(key)
            if not group_id:
                if key in index.mirrors:
                    item["visibility"], item["mirror"] = "verbose", True
                if key in index.duplicates:
                    item["visibility"], item["duplicate"] = "verbose", True
                components.append(public_component(item))
                continue
            refs = index.calls[group_id]
            anchor = min(refs)
            if key != anchor[:2] or group_id in emitted_calls:
                continue
            emitted_calls.add(group_id)
            group_parts: List[Dict[str, Any]] = []
            for ref_line, ref_index, _ in refs:
                part = next((p for p in index.components(ref_line) if int(p["index"]) == ref_index), None)
                if part:
                    group_parts.append(public_component(part))
            request = next((p for p in group_parts if p.get("phase") == "request"), None)
            lifecycle = [p for p in group_parts if p.get("phase") == "lifecycle" and p.get("status")]
            status = (lifecycle[-1].get("status") if lifecycle else
                      next((p.get("status") for p in reversed(group_parts) if p.get("status")), "pending"))
            duplicate = bool(request and request.get("duplicate"))
            components.append({"line": anchor[0], "index": anchor[1], "kind": "tool",
                               "role": "tool", "visibility": "verbose" if duplicate else "normal",
                               "duplicate": duplicate, "call_id": index.call_ids.get(group_id, group_id),
                               "title": (request or group_parts[0] if group_parts else {}).get("title")
                                        or (request or {}).get("tool_name") or "Tool call",
                               "tool_name": (request or {}).get("tool_name"), "status": status,
                               "components": group_parts})
    components.sort(key=lambda item: (int(item["line"]), int(item["index"])))
    index.usage.decorate(components, index.harness)
    next_line = available[position + limit] if position + limit < len(available) else None
    previous_line = available[max(0, position - limit)] if position > 0 else None
    return {"from_line": base_lines[0] if base_lines else start, "to_line": end,
            "focus_line": focus or None, "next_line": next_line,
            "previous_line": previous_line, "line_count": index.line_count,
            "display_line_count": len(available), "eof": next_line is None,
            "source_revision": index.revision, "parse_errors": index.parse_errors,
            "title": index.title(), "owner_session_id": index.owner,
            "parent_session_id": index.parent or None, "root_session_id": index.root,
            "model": index.model or None, "cwd": index.cwd or None, "items": components}
