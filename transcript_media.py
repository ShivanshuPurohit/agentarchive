"""Source-coordinate attachments, served only through the authenticated viewer.

No asset database or arbitrary file endpoint: resolve an attachment again from
its recorded component, then apply workspace and media-type boundaries.
"""
from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import pathlib
import re
import urllib.parse

from privacy_filter import workspace_is_excluded

MAX_ASSET = 24 * 1024 * 1024
MEDIA_TYPES = {"image", "input_image", "output_image", "image_url", "audio",
               "input_audio", "output_audio", "video", "document", "file",
               "input_file", "resource", "resource_link"}
TEXT_TYPES = {"text/html", "text/plain", "text/markdown", "text/csv", "application/json"}
MARKDOWN_IMAGE = re.compile(r"!\[([^\]\n]*)\]\((?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)")


def component_value(harness, obj, number):
    payload = obj.get("payload", obj) if harness == "codex" else obj.get("message", obj)
    content = payload.get("content")
    if isinstance(content, list) and payload.get("type") not in {"function_call_output", "custom_tool_call_output"}:
        if number < len(content):
            return content[number]
        return obj.get("toolUseResult", {})
    return payload


def assets_in(value):
    """Return deterministic, bounded references; bytes never enter page JSON."""
    found = []

    def add(ref, mime="", name="Attachment", encoding="", text=None):
        if len(found) < 64:
            found.append({"ref": ref, "mime": mime, "name": name,
                          "encoding": encoding, "text": text})

    def visit(item, depth=0):
        if depth > 16 or len(found) >= 64:
            return
        if isinstance(item, str):
            if item[:1] in {"{", "["}:
                try:
                    visit(json.loads(item), depth + 1)
                    return
                except (ValueError, RecursionError):
                    pass
            for match in MARKDOWN_IMAGE.finditer(item):
                add(match[2] or match[3], name=match[1] or "Image")
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, dict):
            kind = item.get("type")
            if isinstance(kind, str) and kind in MEDIA_TYPES:
                source = item.get("source") or item.get("resource") or item.get("input_audio") or item
                if isinstance(source, dict):
                    mime = source.get("media_type") or source.get("mimeType") or source.get("mime_type") or item.get("mimeType") or ""
                    if not mime and source.get("format"):
                        mime = "audio/" + source["format"]
                    ref = (source.get("data") or source.get("blob") or source.get("url") or
                           source.get("uri") or source.get("image_url") or source.get("audio_url") or
                           source.get("file_data") or source.get("file_path") or source.get("path"))
                    if isinstance(ref, dict):
                        ref = ref.get("url")
                    name = item.get("title") or item.get("filename") or item.get("name") or kind.replace("_", " ").title()
                    encoding = "base64" if any(source.get(k) for k in ("data", "blob", "file_data")) else ""
                    if source.get("type") == "text":
                        encoding = "text"
                    add(ref or "", mime, str(name), encoding, source.get("text"))
                    return
            for key, child in item.items():
                if key not in {"signature", "encrypted_content", "data", "blob"}:
                    visit(child, depth + 1)

    visit(value)
    return found


def descriptors(value):
    rows = []
    for number, asset in enumerate(assets_in(value)):
        ref = str(asset["ref"])
        mime = asset["mime"] or (ref[5:].split(";", 1)[0] if ref.startswith("data:") else "")
        mime = mime or mimetypes.guess_type(urllib.parse.urlsplit(ref).path)[0] or "application/octet-stream"
        remote = ref.startswith(("https://", "http://"))
        rows.append({"asset": number, "mime": mime, "name": asset["name"],
                     "origin": "external" if remote else "embedded" if asset["encoding"] or ref.startswith("data:") or asset["text"] is not None else "local reference",
                     "reference": ref if not ref.startswith("data:") and not asset["encoding"] else None})
    return rows


def redact_media_text(text, value):
    for asset in assets_in(value):
        ref = str(asset["ref"])
        if ref and (asset["encoding"] == "base64" or ref.startswith("data:")):
            text = text.replace(ref, "[attachment bytes served separately]")
    return text


def load_asset(config, index, line, number, asset_number):
    record = index.records([line]).get(line)
    if not record or not any(p["index"] == number for p in index.components(line)):
        raise FileNotFoundError("Unknown attachment")
    assets = assets_in(component_value(index.harness, record, number))
    if not 0 <= asset_number < len(assets):
        raise FileNotFoundError("Unknown attachment")
    asset = assets[asset_number]
    ref, mime, encoding = str(asset["ref"]), str(asset["mime"]), asset["encoding"]
    if ref.startswith("data:"):
        header, ref = ref.split(",", 1)
        mime = header[5:].split(";", 1)[0]
        encoding = "base64" if ";base64" in header else "url"
    if asset["text"] is not None:
        raw = str(asset["text"]).encode()
    elif encoding:
        if len(ref) > MAX_ASSET * 2:
            raise ValueError("Attachment exceeds the 24 MiB preview limit")
        try:
            raw = base64.b64decode(ref, validate=True) if encoding == "base64" else urllib.parse.unquote_to_bytes(ref) if encoding == "url" else ref.encode()
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid attachment encoding") from exc
    else:
        url = urllib.parse.urlsplit(ref)
        if url.scheme not in {"", "file"} or url.netloc:
            raise FileNotFoundError("Attachment is external or was not retained")
        candidate = pathlib.Path(urllib.parse.unquote(url.path)).expanduser()
        if not candidate.is_absolute():
            candidate = pathlib.Path(index.cwd) / candidate
        candidate = candidate.resolve()
        roots = [index.path.parent / "attachments"]
        if index.cwd:
            roots.append(pathlib.Path(index.cwd).expanduser().resolve())
        if workspace_is_excluded(config, str(candidate)) or not any(candidate.is_relative_to(root) for root in roots):
            raise FileNotFoundError("Attachment is outside this source's workspace")
        mime = mimetypes.guess_type(candidate.name)[0] or mime
        if not allowed_mime(mime) or not candidate.is_file():
            raise FileNotFoundError("Attachment is unavailable")
        if candidate.stat().st_size > MAX_ASSET:
            raise ValueError("Attachment exceeds the 24 MiB preview limit")
        with candidate.open("rb") as handle:
            raw = handle.read(MAX_ASSET + 1)
    if len(raw) > MAX_ASSET:
        raise ValueError("Attachment exceeds the 24 MiB preview limit")
    if not allowed_mime(mime):
        raise FileNotFoundError("This attachment type has no safe local preview")
    # Active documents are fetched as text and rendered in a scriptless sandbox.
    served_mime = "text/plain; charset=utf-8" if mime in TEXT_TYPES or mime == "image/svg+xml" else mime
    return served_mime, raw


def allowed_mime(mime):
    return mime.startswith(("image/", "audio/", "video/")) or mime in TEXT_TYPES | {"application/pdf"}
