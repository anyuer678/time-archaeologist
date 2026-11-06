"""只读 Git 元数据痕迹：HEAD / refs / packed-refs / reflog。

只读取 .git 下的文本文件，不解压 commit 对象、不执行 git 命令、不读取工作区状态。
reflog 时间来自 Git 自身记录（提交、切分支、合并等动作时间），与文件 mtime
含义不同，也不能解释为工作时长。
"""
from __future__ import annotations

import os
import re

from .util import extended_path

_MAX_BYTES = 2 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_RELOG_RE = re.compile(
    r"^([0-9a-fA-F]{7,40}) ([0-9a-fA-F]{7,40}) (.+?) <(.*?)> (\d+) ([+-]\d{4})\t(.*)$")


def _read_text(path: str, limit: int = _MAX_BYTES) -> str | None:
    try:
        with open(extended_path(path), "rb") as f:
            return f.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


def find_git_dir(root: str) -> str | None:
    """返回仓库的 .git 目录；支持 .git 文件（gitdir: 间接指向）。"""
    g = os.path.join(root, ".git")
    if os.path.isdir(g):
        return g
    if os.path.isfile(g):
        text = (_read_text(g, 4096) or "").strip()
        m = re.match(r"gitdir:\s*(.+)", text)
        if m:
            target = m.group(1).strip()
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(root, target))
            return target if os.path.isdir(target) else None
    return None


def _from_ts(ts: str) -> str:
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return ""


def _parse_reflog(path: str, label: str, limit: int) -> list[dict]:
    text = _read_text(path)
    if not text:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        m = _RELOG_RE.match(line)
        if not m:
            continue
        old, new, author, email, ts, tz, message = m.groups()
        out.append({
            "ref": label, "old_sha": old, "new_sha": new, "author": author,
            "email": email, "time_utc": _from_ts(ts), "tz": tz,
            "message": message.strip()[:200],
        })
        if len(out) >= limit * 4:
            break
    return out


def _read_refs(git_dir: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    refs_dir = os.path.join(git_dir, "refs")
    for base, _dirs, files in os.walk(refs_dir):
        for fn in files:
            full = os.path.join(base, fn)
            rel = os.path.relpath(full, git_dir).replace(os.sep, "/")
            sha = (_read_text(full, 200) or "").strip()
            if _SHA_RE.match(sha):
                refs[rel] = sha
    packed = _read_text(os.path.join(git_dir, "packed-refs"))
    if packed:
        for line in packed.splitlines():
            if line.startswith(("#", "^")):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and _SHA_RE.match(parts[0]):
                refs.setdefault(parts[1].strip(), parts[0])
    return refs


def read_git_meta(root: str, limit: int = 100) -> dict | None:
    """读取 Git 元数据；不是 Git 仓库（或无法定位 .git）时返回 None。

    返回 {git_dir, head, branch, head_sha, refs, reflog}，reflog 按时间倒序。
    只读取文本元数据，不解压对象库，也不访问网络或执行 git。
    """
    git_dir = find_git_dir(root)
    if git_dir is None:
        return None
    head = (_read_text(os.path.join(git_dir, "HEAD"), 4096) or "").strip()
    branch = None
    head_sha = None
    if head.startswith("ref: "):
        branch = head[5:].strip()
        ref_file = os.path.join(git_dir, *branch.split("/"))
        sha = (_read_text(ref_file, 200) or "").strip()
        head_sha = sha if _SHA_RE.match(sha) else None
    elif _SHA_RE.match(head):
        head_sha = head
    refs = _read_refs(git_dir)
    if head_sha is None and branch:
        head_sha = refs.get(branch)
    reflog = _parse_reflog(os.path.join(git_dir, "logs", "HEAD"), "HEAD", limit)
    if branch:
        reflog += _parse_reflog(
            os.path.join(git_dir, "logs", *branch.split("/")), branch, limit)
    seen = set()
    unique = []
    for entry in sorted(reflog, key=lambda r: r["time_utc"], reverse=True):
        # HEAD 与分支 reflog 会重复记录同一动作，按动作内容去重
        key = (entry["old_sha"], entry["new_sha"], entry["time_utc"],
               entry["message"])
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return {
        "git_dir": git_dir,
        "head": head or None,
        "branch": branch,
        "head_sha": head_sha,
        "refs": refs,
        "reflog": unique[:limit],
    }
