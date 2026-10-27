"""通用工具：时间、路径、文本处理。仅使用标准库。"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

_WIN_LONG_PREFIX = "\\\\?\\"


def utcnow_iso() -> str:
    """当前 UTC 时间，ISO-8601 秒级。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ts_to_iso(ts: float) -> str:
    """文件系统时间戳转 UTC ISO 字符串（毫秒级，保留精度便于比较）。"""
    try:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return ""


def extended_path(path: str) -> str:
    """为绝对路径加 Windows 长路径前缀，避免超过 260 字符失败。其他平台原样返回。"""
    if os.name != "nt":
        return path
    p = str(path)
    if p.startswith(_WIN_LONG_PREFIX):
        return p
    if len(p) >= 2 and p[1] == ":" and not p.startswith("\\\\"):
        return _WIN_LONG_PREFIX + p
    return p


def sanitize_text(text: str) -> str:
    """清除无法编码进 UTF-8/SQLite 的代理字符，避免个别异常文件名导致整批写入失败。"""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


def fmt_size(n) -> str:
    """人类可读的体积。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_time_local(iso: str | None) -> str:
    """UTC ISO 时间转本地时间简短显示。空值返回 '-'。"""
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso)
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


def read_text_head(abs_root: str, rel_path: str, limit: int = 256 * 1024) -> str | None:
    """只读读取项目内文本文件头部。二进制（首 1KB 含 \\0）、超限或读取失败返回 None。"""
    try:
        full = extended_path(os.path.join(abs_root, rel_path.replace("/", os.sep)))
        if os.path.getsize(full) > limit:
            return None
        with open(full, "rb") as f:
            if b"\0" in f.read(1024):
                return None
            f.seek(0)
            return f.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


def default_db_path() -> Path:
    """默认数据库位置：%LOCALAPPDATA%/TimeArchaeologist/time_arch.db。"""
    env = os.environ.get("TIME_ARCH_DB")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".time_archaeologist"
    return root / "TimeArchaeologist" / "time_arch.db"
