"""Preservation of macOS filesystem metadata and extended attributes."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import pathlib
import stat
import sys
from typing import Any, Dict, List, Optional, Tuple

from . import common as utils


_MACOS_XATTR_API: Optional[Tuple[Any, Any]] = None


def native_macos_xattrs(path: pathlib.Path) -> Dict[str, bytes]:
    global _MACOS_XATTR_API
    if sys.platform != "darwin":
        raise NotImplementedError("native xattr fallback is macOS-only")
    if _MACOS_XATTR_API is None:
        library = ctypes.CDLL(None, use_errno=True)
        list_xattr = library.listxattr
        list_xattr.argtypes = [
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
        ]
        list_xattr.restype = ctypes.c_ssize_t
        get_xattr = library.getxattr
        get_xattr.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        get_xattr.restype = ctypes.c_ssize_t
        _MACOS_XATTR_API = (list_xattr, get_xattr)
    list_xattr, get_xattr = _MACOS_XATTR_API
    encoded_path = os.fsencode(path)
    no_follow = 0x0001

    def checked(value: int, operation: str) -> int:
        if value >= 0:
            return value
        error = ctypes.get_errno()
        raise OSError(error, f"{operation}: {os.strerror(error)}", str(path))

    name_bytes = checked(
        list_xattr(encoded_path, None, 0, no_follow),
        "listxattr",
    )
    if name_bytes == 0:
        return {}
    name_buffer = ctypes.create_string_buffer(name_bytes)
    written = checked(
        list_xattr(encoded_path, name_buffer, name_bytes, no_follow),
        "listxattr",
    )
    result: Dict[str, bytes] = {}
    for encoded_name in name_buffer.raw[:written].split(b"\0"):
        if not encoded_name:
            continue
        value_bytes = checked(
            get_xattr(encoded_path, encoded_name, None, 0, 0, no_follow),
            "getxattr",
        )
        if value_bytes:
            value_buffer = ctypes.create_string_buffer(value_bytes)
            value_written = checked(
                get_xattr(
                    encoded_path,
                    encoded_name,
                    value_buffer,
                    value_bytes,
                    0,
                    no_follow,
                ),
                "getxattr",
            )
            value = value_buffer.raw[:value_written]
        else:
            value = b""
        result[encoded_name.decode("utf-8", errors="surrogateescape")] = value
    return result


def path_xattrs(path: pathlib.Path) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    try:
        if hasattr(os, "listxattr") and hasattr(os, "getxattr"):
            values = {
                name: os.getxattr(path, name, follow_symlinks=False)
                for name in sorted(os.listxattr(path, follow_symlinks=False))
            }
        else:
            values = native_macos_xattrs(path)
    except (AttributeError, NotImplementedError) as exc:
        return [], str(exc)
    except OSError as exc:
        return [], repr(exc)
    result: List[Dict[str, Any]] = []
    for name, value in sorted(values.items()):
        result.append(
            {
                "name": name,
                "size_bytes": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
                "value_base64": base64.b64encode(value).decode("ascii"),
            }
        )
    return result, None


def write_platform_metadata(
    stage: pathlib.Path,
) -> Tuple[pathlib.Path, str, int, List[str]]:
    data = stage / "data"
    if not data.is_dir():
        raise RuntimeError(f"staging tree has no data directory: {stage}")
    rows: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for path in sorted(data.rglob("*")):
        info = path.lstat()
        parts = path.relative_to(data).parts
        if not parts:
            continue
        if stat.S_ISLNK(info.st_mode):
            file_type = "symlink"
        elif stat.S_ISDIR(info.st_mode):
            file_type = "directory"
        elif stat.S_ISREG(info.st_mode):
            file_type = "file"
        else:
            file_type = "other"
        birthtime = getattr(info, "st_birthtime", None)
        xattrs, xattr_error = path_xattrs(path)
        row: Dict[str, Any] = {
            "harness": parts[0],
            "relpath": (
                pathlib.PurePosixPath(*parts[1:]).as_posix()
                if len(parts) > 1
                else "."
            ),
            "file_type": file_type,
            "birthtime_ns": (
                int(birthtime * 1_000_000_000)
                if birthtime is not None
                else None
            ),
            "flags": getattr(info, "st_flags", None),
            "inode": info.st_ino,
            "nlink": info.st_nlink,
            "xattrs": xattrs,
        }
        if xattr_error:
            row["xattr_error"] = xattr_error
            warnings.append(f"xattr inventory failed for {path}: {xattr_error}")
        rows.append(row)
    encoded = "".join(
        json.dumps(
            row,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
        for row in rows
    ).encode("utf-8")
    destination = stage / "meta" / "platform-metadata.jsonl"
    utils.atomic_write(destination, encoded, 0o600)
    return destination, hashlib.sha256(encoded).hexdigest(), len(rows), warnings
