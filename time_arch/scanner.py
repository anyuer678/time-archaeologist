"""只读扫描器。

铁律：只调用 stat / scandir / open(rb)，绝不写入、移动或删除被扫描目录中的任何文件，
不执行其中任何脚本。哈希分块读取且每块检查取消标志。文件记录分批提交事务，
取消时整体回滚该次快照（安全重试语义：下次扫描从头开始即可）。
"""
from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .classify import classify_file
from .db import Database
from .util import extended_path, sanitize_text, ts_to_iso, utcnow_iso

HASH_CHUNK = 1 << 20  # 1 MiB
RECORD_BATCH = 200


class ScanCancelled(Exception):
    pass


class ScanError(Exception):
    pass


@dataclass
class ScanProgress:
    files: int = 0
    bytes_hashed: int = 0
    bytes_cached: int = 0
    files_cached: int = 0
    errors: int = 0
    current: str = ""
    phase: str = "walk"


@dataclass
class ScanResult:
    run_id: int
    project_id: int
    status: str                 # completed / cancelled
    snapshot_id: int | None
    files: int = 0
    bytes_hashed: int = 0
    bytes_cached: int = 0
    files_cached: int = 0
    errors: int = 0
    events_created: int = 0
    findings_created: int = 0
    prev_snapshot_id: int | None = None
    error_messages: list = field(default_factory=list)


def _is_junction(entry) -> bool:
    is_j = getattr(entry, "is_junction", None)
    try:
        return bool(is_j()) if is_j else False
    except OSError:
        return False


def iter_files(root: str, cancel_event: threading.Event | None,
               on_error: Callable[[str, str], None]) -> Iterator[dict]:
    """迭代目录树中的文件（含符号链接记录，不跟随目录链接避免循环）。

    yields: {rel_path, abs_path, size, mtime, ext, category, is_symlink, link_target}
    """
    sep = os.sep
    stack: list[tuple[str, str]] = [(root, "")]
    while stack:
        cur_abs, cur_rel = stack.pop()
        try:
            with os.scandir(extended_path(cur_abs)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            on_error(cur_rel or ".", f"{type(e).__name__}: {e}")
            continue
        for entry in entries:
            if cancel_event is not None and cancel_event.is_set():
                raise ScanCancelled()
            name = sanitize_text(entry.name)
            abs_path = os.path.join(cur_abs, entry.name)
            rel_path = sanitize_text(f"{cur_rel}{sep}{name}" if cur_rel else name)
            try:
                is_link = entry.is_symlink() or _is_junction(entry)
                if is_link:
                    try:
                        target = sanitize_text(os.readlink(extended_path(abs_path)))
                    except OSError:
                        target = None
                    st = entry.stat(follow_symlinks=False)
                    yield {
                        "rel_path": rel_path, "abs_path": abs_path,
                        "size": st.st_size, "mtime": ts_to_iso(st.st_mtime),
                        "ext": os.path.splitext(name)[1].lower(),
                        "category": classify_file(rel_path),
                        "is_symlink": True, "link_target": target,
                    }
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append((abs_path, rel_path))
                    continue
                if entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    yield {
                        "rel_path": rel_path, "abs_path": abs_path,
                        "size": st.st_size, "mtime": ts_to_iso(st.st_mtime),
                        "ext": os.path.splitext(name)[1].lower(),
                        "category": classify_file(rel_path),
                        "is_symlink": False, "link_target": None,
                    }
                # 其他类型（设备、管道等）跳过
            except OSError as e:
                on_error(rel_path, f"{type(e).__name__}: {e}")


def hash_file(abs_path: str, cancel_event: threading.Event | None) -> str:
    """分块计算 SHA-256，每块之间响应取消。只读打开。"""
    h = hashlib.sha256()
    with open(extended_path(abs_path), "rb") as f:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ScanCancelled()
            block = f.read(HASH_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def build_events_from_diff(db: Database, project_id: int, run_id: int,
                           prev_snap_id: int | None, new_snap_id: int) -> list[dict]:
    """对比前后快照生成事件。

    确定性规则：
    - file_added / file_removed / file_modified：两次扫描之间观察到的事实 → observed。
    - 首次扫描（无前序快照）：现存文件记为基线事件，依据明确说明“登记时已存在，
      出现时间未知”，不得解读为扫描期间新增。
    - log_generated / artifact_appeared：基于类别启发式的推断 → inferred（推测）。
    - fs_mtime 只是文件系统时间戳，绝不解释为“真实工作时间”，证据中显式声明。
    """
    now = utcnow_iso()
    events: list[dict] = []
    if prev_snap_id is None:
        rows = db.query(
            "SELECT rel_path, size, mtime, sha256, category FROM file_records "
            "WHERE snapshot_id=?", (new_snap_id,))
        diff = {"added": [dict(r) for r in rows], "removed": [], "modified": []}
    else:
        diff = db.compare_snapshots(prev_snap_id, new_snap_id, limit=100000)

    for f in diff["added"]:
        if prev_snap_id is None:
            basis = (f"首次扫描基线（快照 #{new_snap_id}）：登记该项目时该文件已存在，"
                     f"其出现的确切时间未知。")
            snapshots_ref = [new_snap_id]
        else:
            basis = (f"快照 #{prev_snap_id} 中不存在，快照 #{new_snap_id} 中出现"
                     f"（两次扫描之间新增）")
            snapshots_ref = [prev_snap_id, new_snap_id]
        events.append({
            "project_id": project_id, "run_id": run_id, "event_type": "file_added",
            "rel_path": f["rel_path"], "category": f["category"], "observed_at": now,
            "fs_mtime": f["mtime"], "certainty": "observed",
            "evidence": {
                "basis": basis,
                "snapshots": snapshots_ref,
                "size": f["size"], "sha256": f["sha256"],
                "note": "事件时间以扫描观察时间为准；fs_mtime 是文件系统时间戳，不代表真实工作时间。",
            },
        })
    for f in diff["removed"]:
        events.append({
            "project_id": project_id, "run_id": run_id, "event_type": "file_removed",
            "rel_path": f["rel_path"], "category": f["category"], "observed_at": now,
            "fs_mtime": f["mtime"], "certainty": "observed",
            "evidence": {
                "basis": f"快照 #{prev_snap_id} 中存在，快照 #{new_snap_id} 中已消失（两次扫描之间删除）",
                "snapshots": [prev_snap_id, new_snap_id],
                "last_seen_size": f["size"], "last_seen_sha256": f["sha256"],
                "note": "删除发生的确切时间未知，仅知介于两次扫描之间。",
            },
        })
    for f in diff["modified"]:
        events.append({
            "project_id": project_id, "run_id": run_id, "event_type": "file_modified",
            "rel_path": f["rel_path"], "category": f["category"], "observed_at": now,
            "fs_mtime": f["new_mtime"], "certainty": "observed",
            "evidence": {
                "basis": f"快照 #{prev_snap_id} 与 #{new_snap_id} 之间内容签名变化（SHA-256 或大小不同）",
                "snapshots": [prev_snap_id, new_snap_id],
                "old": {"size": f["old_size"], "sha256": f["old_sha256"], "mtime": f["old_mtime"]},
                "new": {"size": f["new_size"], "sha256": f["new_sha256"], "mtime": f["new_mtime"]},
                "note": "内容变化发生在两次扫描之间；mtime 是文件系统时间戳，不代表真实工作时间。",
            },
        })

    # 推测事件：新增文件中，日志/构建产物类别 → 推断为运行产物（明确标记 inferred）
    for f in diff["added"]:
        if f["category"] == "log":
            events.append({
                "project_id": project_id, "run_id": run_id, "event_type": "log_generated",
                "rel_path": f["rel_path"], "category": f["category"], "observed_at": now,
                "fs_mtime": f["mtime"], "certainty": "inferred",
                "evidence": {
                    "basis": ("启发式推断：文件扩展名/路径符合日志特征（.log / logs 目录），"
                              "推测由程序运行生成。"),
                    "kind": "heuristic", "snapshots": [new_snap_id],
                    "note": "未直接观察其生成过程。此为推测，非事实。",
                },
            })
        elif f["category"] in ("build", "data") and f["size"] > 0:
            events.append({
                "project_id": project_id, "run_id": run_id, "event_type": "artifact_appeared",
                "rel_path": f["rel_path"], "category": f["category"], "observed_at": now,
                "fs_mtime": f["mtime"], "certainty": "inferred",
                "evidence": {
                    "basis": (f"启发式推断：文件属于{'构建产物' if f['category'] == 'build' else '数据'}"
                              f"类别（目录名/扩展名匹配），推测为工具链或实验流程产物。"),
                    "kind": "heuristic", "snapshots": [new_snap_id],
                    "note": "未直接观察生成过程。此为推测，非事实。",
                },
            })
    return events


def run_scan(db: Database, project_id: int,
             cancel_event: threading.Event | None = None,
             progress_cb: Callable[[ScanProgress], None] | None = None,
             analyze: bool = True) -> ScanResult:
    """同步执行一次完整扫描（测试与后台线程共用）。

    增量哈希：与上一次完成快照相比 size+mtime 均未变的文件，直接复用当时的
    SHA-256，不重新读取内容（rsync/git 同款快速检查折衷）。文件内容被篡改且
    时间戳被刻意还原的极端情况会被漏检——已作为已知限制记录。
    """
    project = db.get_project(project_id)
    if project is None:
        raise ScanError(f"项目不存在: {project_id}")
    root = project["path"]
    if not os.path.isdir(extended_path(root)):
        raise ScanError(f"项目目录不可访问: {root}")

    prev_for_cache = db.latest_snapshot(project_id)
    hash_cache: dict[str, tuple[int, str, str]] = {}
    if prev_for_cache is not None:
        for r in db.query(
                "SELECT rel_path, size, mtime, sha256 FROM file_records "
                "WHERE snapshot_id=? AND sha256 IS NOT NULL", (prev_for_cache["id"],)):
            hash_cache[r["rel_path"]] = (r["size"], r["mtime"], r["sha256"])

    run_id = db.start_run(project_id)
    snapshot_id = db.create_snapshot(project_id, run_id)
    progress = ScanProgress()
    errors: list[str] = []
    pending: list[dict] = []

    def flush() -> None:
        if pending:
            db.insert_file_records(snapshot_id, pending)
            pending.clear()

    def on_error(rel: str, msg: str) -> None:
        progress.errors += 1
        errors.append(f"{rel}: {msg}")
        if len(errors) <= 200:
            pending.append({
                "rel_path": rel, "size": 0, "mtime": "", "ext": "",
                "category": "other", "sha256": None, "is_symlink": False,
                "link_target": None, "read_error": msg,
            })

    try:
        for info in iter_files(root, cancel_event, on_error):
            if cancel_event is not None and cancel_event.is_set():
                raise ScanCancelled()
            sha, read_err = None, None
            if not info["is_symlink"]:
                cached = hash_cache.get(info["rel_path"])
                if cached and cached[0] == info["size"] and cached[1] == info["mtime"]:
                    sha = cached[2]
                    progress.files_cached += 1
                    progress.bytes_cached += info["size"]
                else:
                    try:
                        sha = hash_file(info["abs_path"], cancel_event)
                        progress.bytes_hashed += info["size"]
                    except ScanCancelled:
                        raise
                    except OSError as e:
                        read_err = f"{type(e).__name__}: {e}"
                        progress.errors += 1
                        errors.append(f"{info['rel_path']}: {read_err}")
            pending.append({
                "rel_path": info["rel_path"], "size": info["size"], "mtime": info["mtime"],
                "ext": info["ext"], "category": info["category"], "sha256": sha,
                "is_symlink": info["is_symlink"], "link_target": info["link_target"],
                "read_error": read_err,
            })
            progress.files += 1
            progress.current = info["rel_path"]
            if progress_cb and (progress.files % 25 == 0 or progress.files < 10):
                progress_cb(progress)
            if len(pending) >= RECORD_BATCH:
                flush()
        flush()

        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()

        progress.phase = "diff"
        if progress_cb:
            progress_cb(progress)
        prev = db.previous_completed_snapshot(project_id, snapshot_id)
        events = build_events_from_diff(db, project_id, run_id,
                                        prev["id"] if prev else None, snapshot_id)
        n_events = db.record_events(events)

        findings_created = 0
        if analyze:
            from .analyze import analyze_snapshot  # 延迟导入避免环
            findings = analyze_snapshot(db, project_id, snapshot_id)
            findings_created = db.record_findings(findings)

        db.complete_run(run_id, snapshot_id, progress.files, progress.bytes_hashed, progress.errors)
        return ScanResult(run_id=run_id, project_id=project_id, status="completed",
                          snapshot_id=snapshot_id, files=progress.files,
                          bytes_hashed=progress.bytes_hashed,
                          bytes_cached=progress.bytes_cached,
                          files_cached=progress.files_cached,
                          errors=progress.errors,
                          events_created=n_events, findings_created=findings_created,
                          prev_snapshot_id=prev["id"] if prev else None,
                          error_messages=errors)
    except ScanCancelled:
        db.cancel_run(run_id)
        return ScanResult(run_id=run_id, project_id=project_id, status="cancelled",
                          snapshot_id=None, files=progress.files,
                          bytes_hashed=progress.bytes_hashed, errors=progress.errors,
                          error_messages=errors)
    except Exception as e:  # 未预期异常：回滚快照并记录，保证库一致
        db.fail_run(run_id, f"{type(e).__name__}: {e}")
        raise


class ScanService:
    """后台扫描服务：UI 线程通过 start()/cancel() 控制，回调在 worker 线程执行。"""

    def __init__(self, db: Database):
        self.db = db
        self._thread: threading.Thread | None = None
        self._cancel: threading.Event | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, project_id: int, on_done: Callable[[ScanResult], None],
              on_progress: Callable[[ScanProgress], None] | None = None) -> bool:
        if self.busy:
            return False
        self._cancel = threading.Event()

        def worker():
            try:
                result = run_scan(self.db, project_id, self._cancel, on_progress)
            except Exception as e:  # pragma: no cover - 兜底
                result = ScanResult(run_id=-1, project_id=project_id, status="error",
                                    snapshot_id=None, error_messages=[str(e)])
            on_done(result)

        self._thread = threading.Thread(target=worker, daemon=True,
                                        name=f"scan-{project_id}")
        self._thread.start()
        return True

    def cancel(self) -> None:
        if self._cancel:
            self._cancel.set()

    def join(self, timeout: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
