"""SQLite 持久层。

设计要点：
- WAL + synchronous=FULL：进程中断后数据库可自动恢复，不损坏。
- 每次操作独立短事务并显式关闭连接（Windows 下连接不关闭会锁住 db 文件）。
- 跨线程使用经 Database 实例内部锁串行化（UI 线程 + 扫描线程）。
- 打开数据库时自动把上次遗留的 running 扫描标记为 interrupted（异常恢复）。
- events 对 (run_id, event_type, rel_path) 唯一，重复扫描不会重复建事件。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

from .util import utcnow_iso

SCHEMA_VERSION = 3

_BASE_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_scan_at TEXT
)""",
    """CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    files_scanned INTEGER NOT NULL DEFAULT 0,
    bytes_hashed INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    error_text TEXT,
    snapshot_id INTEGER
)""",
    """CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS file_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mtime TEXT NOT NULL DEFAULT '',
    ext TEXT,
    category TEXT NOT NULL DEFAULT 'other',
    sha256 TEXT,
    is_symlink INTEGER NOT NULL DEFAULT 0,
    link_target TEXT,
    read_error TEXT,
    UNIQUE(snapshot_id, rel_path)
)""",
    """CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    observed_at TEXT NOT NULL,
    fs_mtime TEXT,
    certainty TEXT NOT NULL DEFAULT 'observed',
    evidence TEXT NOT NULL DEFAULT '{}',
    UNIQUE(run_id, event_type, rel_path)
)""",
    """CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    rel_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'auto',
    basis TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}',
    detected_at TEXT NOT NULL,
    UNIQUE(snapshot_id, rel_path, kind)
)""",
    "CREATE INDEX IF NOT EXISTS idx_file_records_snap ON file_records(snapshot_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_findings_project ON findings(project_id, id)",
]

# v2（2.0 第二步）：成果档案 + 证据关联。全部幂等，旧数据不动。
_MIGRATIONS: dict[int, list[str]] = {
    2: [
        """CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    primary_path TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'auto',
    first_snapshot_id INTEGER,
    last_snapshot_id INTEGER,
    first_seen_at TEXT,
    last_seen_at TEXT,
    basis TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, kind, primary_path)
)""",
        """CREATE TABLE IF NOT EXISTS artifact_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    snapshot_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    role TEXT NOT NULL,
    basis TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'medium',
    UNIQUE(artifact_id, snapshot_id, rel_path, role)
)""",
        "CREATE INDEX IF NOT EXISTS idx_artifact_files_art ON artifact_files(artifact_id)",
        """CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    snapshot_id INTEGER NOT NULL,
    src_path TEXT NOT NULL,
    dst_path TEXT NOT NULL,
    relation TEXT NOT NULL,
    source TEXT NOT NULL,
    basis TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, snapshot_id, src_path, dst_path, relation, source)
)""",
        "CREATE INDEX IF NOT EXISTS idx_relations_snap ON relations(snapshot_id)",
        "CREATE INDEX IF NOT EXISTS idx_relations_dst ON relations(snapshot_id, dst_path)",
    ],
    # v3（2.0 第三步）：实验复现检查结果。按 (project, snapshot, target) 幂等替换。
    3: [
        """CREATE TABLE IF NOT EXISTS repro_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    snapshot_id INTEGER NOT NULL,
    artifact_id INTEGER REFERENCES artifacts(id) ON DELETE SET NULL,
    target_path TEXT NOT NULL,
    level TEXT NOT NULL,
    score INTEGER NOT NULL,
    checks TEXT NOT NULL DEFAULT '[]',
    generated_at TEXT NOT NULL,
    UNIQUE(project_id, snapshot_id, target_path))""",
        "CREATE INDEX IF NOT EXISTS idx_repro_project ON repro_checks(project_id)",
    ],
}


class Database:
    """线程安全的 SQLite 封装：内部锁 + 每操作独立连接短事务。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._session(write=True) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            self._migrate(conn)
            # 异常恢复：上次会话遗留的 running 扫描标记为 interrupted
            conn.execute(
                "UPDATE scan_runs SET status='interrupted', finished_at=?, "
                "error_text='应用中断，重启时自动恢复为中断状态' WHERE status='running'",
                (utcnow_iso(),),
            )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """分级迁移：user_version 记录版本，全部语句幂等，旧数据零改动。

        - user_version=0 且已存在 projects 表 → v0.1.0 旧库，视为版本 1。
        - 每个迁移级为有序 DDL 列表，单个事务内执行，失败整体回滚。
        """
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if ver == 0 and conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='projects'"
        ).fetchone():
            ver = 1  # v0.1.0 旧库：表已建但未记录版本
        for stmt in _BASE_STATEMENTS:
            conn.execute(stmt)
        for target in sorted(_MIGRATIONS):
            if ver < target:
                for stmt in _MIGRATIONS[target]:
                    conn.execute(stmt)
        if ver < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version={int(SCHEMA_VERSION)}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def _session(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """独占锁 + 独立连接；write=True 时整体一个事务，结束提交并关闭。"""
        with self._lock:
            conn = self._connect()
            try:
                if write:
                    with conn:
                        yield conn
                else:
                    yield conn
            finally:
                conn.close()

    def query(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        """只读查询（供模块与测试使用）。"""
        with self._session() as conn:
            return conn.execute(sql, args).fetchall()

    # ---------- 项目 ----------
    def add_project(self, abs_path: str, name: str | None = None) -> tuple[int, bool]:
        """登记项目。若路径已存在则返回既有 id。返回 (id, created)。"""
        path = str(Path(abs_path))
        with self._session(write=True) as conn:
            row = conn.execute("SELECT id FROM projects WHERE path=?", (path,)).fetchone()
            if row:
                return row["id"], False
            cur = conn.execute(
                "INSERT INTO projects(path, name, created_at) VALUES(?,?,?)",
                (path, name or Path(path).name, utcnow_iso()),
            )
            return cur.lastrowid, True

    def remove_project(self, project_id: int) -> None:
        with self._session(write=True) as conn:
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))

    def get_project(self, project_id: int) -> sqlite3.Row | None:
        return (self.query("SELECT * FROM projects WHERE id=?", (project_id,)) or [None])[0]

    def list_projects(self) -> list[sqlite3.Row]:
        sql = """
        SELECT p.id, p.path, p.name, p.created_at, p.last_scan_at,
               (SELECT COUNT(*) FROM snapshots s WHERE s.project_id=p.id) AS snapshot_count,
               (SELECT s.id FROM snapshots s JOIN scan_runs r ON r.id=s.run_id
                 WHERE s.project_id=p.id AND r.status='completed'
                 ORDER BY s.id DESC LIMIT 1) AS latest_snapshot_id,
               (SELECT r.status FROM scan_runs r WHERE r.project_id=p.id
                 ORDER BY r.id DESC LIMIT 1) AS last_run_status
        FROM projects p ORDER BY p.name
        """
        return self.query(sql)

    # ---------- 扫描运行 ----------
    def start_run(self, project_id: int) -> int:
        with self._session(write=True) as conn:
            cur = conn.execute(
                "INSERT INTO scan_runs(project_id, started_at, status) VALUES(?,?, 'running')",
                (project_id, utcnow_iso()),
            )
            return cur.lastrowid

    def create_snapshot(self, project_id: int, run_id: int) -> int:
        with self._session(write=True) as conn:
            cur = conn.execute(
                "INSERT INTO snapshots(project_id, run_id, created_at) VALUES(?,?,?)",
                (project_id, run_id, utcnow_iso()),
            )
            return cur.lastrowid

    def insert_file_records(self, snapshot_id: int, records: list[dict]) -> None:
        rows = [
            (snapshot_id, r["rel_path"], r["size"], r["mtime"], r["ext"], r["category"],
             r.get("sha256"), int(r.get("is_symlink", 0)), r.get("link_target"),
             r.get("read_error"))
            for r in records
        ]
        with self._session(write=True) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO file_records"
                "(snapshot_id, rel_path, size, mtime, ext, category, sha256, is_symlink, link_target, read_error) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def complete_run(self, run_id: int, snapshot_id: int, files_scanned: int,
                     bytes_hashed: int, error_count: int) -> None:
        with self._session(write=True) as conn:
            conn.execute(
                "UPDATE scan_runs SET status='completed', finished_at=?, snapshot_id=?, "
                "files_scanned=?, bytes_hashed=?, error_count=? WHERE id=?",
                (utcnow_iso(), snapshot_id, files_scanned, bytes_hashed, error_count, run_id),
            )
            conn.execute(
                "UPDATE projects SET last_scan_at=? "
                "WHERE id=(SELECT project_id FROM scan_runs WHERE id=?)",
                (utcnow_iso(), run_id),
            )

    def cancel_run(self, run_id: int) -> None:
        """取消：回滚未完成的快照（含文件记录级联删除），运行标记为 cancelled。"""
        with self._session(write=True) as conn:
            conn.execute("DELETE FROM snapshots WHERE run_id=?", (run_id,))
            conn.execute(
                "UPDATE scan_runs SET status='cancelled', finished_at=?, "
                "error_text='用户取消，部分快照已回滚' WHERE id=?",
                (utcnow_iso(), run_id),
            )

    def fail_run(self, run_id: int, error_text: str) -> None:
        with self._session(write=True) as conn:
            conn.execute("DELETE FROM snapshots WHERE run_id=?", (run_id,))
            conn.execute(
                "UPDATE scan_runs SET status='error', finished_at=?, error_text=? WHERE id=?",
                (utcnow_iso(), error_text[:2000], run_id),
            )

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return (self.query("SELECT * FROM scan_runs WHERE id=?", (run_id,)) or [None])[0]

    # ---------- 快照 ----------
    def latest_snapshot(self, project_id: int) -> sqlite3.Row | None:
        sql = """SELECT s.* FROM snapshots s JOIN scan_runs r ON r.id=s.run_id
                 WHERE s.project_id=? AND r.status='completed' ORDER BY s.id DESC LIMIT 1"""
        return (self.query(sql, (project_id,)) or [None])[0]

    def previous_completed_snapshot(self, project_id: int,
                                    before_snapshot_id: int) -> sqlite3.Row | None:
        sql = """SELECT s.* FROM snapshots s JOIN scan_runs r ON r.id=s.run_id
                 WHERE s.project_id=? AND r.status='completed' AND s.id<?
                 ORDER BY s.id DESC LIMIT 1"""
        return (self.query(sql, (project_id, before_snapshot_id)) or [None])[0]

    def list_snapshots(self, project_id: int) -> list[sqlite3.Row]:
        sql = """SELECT s.id, s.created_at, r.status AS run_status, r.files_scanned,
                        r.error_count,
                        (SELECT COUNT(*) FROM file_records f WHERE f.snapshot_id=s.id) AS file_count,
                        (SELECT COALESCE(SUM(f.size),0) FROM file_records f WHERE f.snapshot_id=s.id) AS total_size,
                        (SELECT COUNT(*) FROM events e WHERE e.run_id=s.run_id) AS event_count
                 FROM snapshots s JOIN scan_runs r ON r.id=s.run_id
                 WHERE s.project_id=? AND r.status='completed'
                 ORDER BY s.id DESC"""
        return self.query(sql, (project_id,))

    def snapshot_stats(self, snapshot_id: int) -> dict:
        total = self.query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS sz, "
            "SUM(CASE WHEN read_error IS NOT NULL THEN 1 ELSE 0 END) AS errs "
            "FROM file_records WHERE snapshot_id=?", (snapshot_id,))[0]
        by_cat = self.query(
            "SELECT category, COUNT(*) AS n, COALESCE(SUM(size),0) AS sz, "
            "MAX(mtime) AS last_mtime, "
            "MAX(CASE WHEN read_error IS NULL THEN mtime ELSE NULL END) AS last_ok_mtime "
            "FROM file_records WHERE snapshot_id=? GROUP BY category ORDER BY n DESC",
            (snapshot_id,))
        return {
            "files": total["n"] or 0,
            "total_size": total["sz"] or 0,
            "errors": total["errs"] or 0,
            "by_category": [(r["category"], r["n"], r["sz"], r["last_ok_mtime"] or "")
                            for r in by_cat],
        }

    def find_file_in_snapshot(self, snapshot_id: int, rel_path: str) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM file_records WHERE snapshot_id=? AND rel_path=?",
                          (snapshot_id, rel_path))
        return rows[0] if rows else None

    # ---------- 事件 ----------
    def record_events(self, events: list[dict]) -> int:
        """批量写入事件，(run_id, event_type, rel_path) 冲突时忽略。返回实际新增数。"""
        if not events:
            return 0
        rows = [
            (e["project_id"], e["run_id"], e["event_type"], e["rel_path"],
             e.get("category", "other"), e["observed_at"], e.get("fs_mtime"),
             e.get("certainty", "observed"),
             json.dumps(e.get("evidence", {}), ensure_ascii=False))
            for e in events
        ]
        with self._session(write=True) as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO events"
                "(project_id, run_id, event_type, rel_path, category, observed_at, fs_mtime, certainty, evidence) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                rows,
            )
            return cur.rowcount

    def count_events(self, project_id: int | None = None, event_type: str | None = None,
                     keyword: str | None = None, since: str | None = None,
                     until: str | None = None, category: str | None = None) -> int:
        sql, args = self._event_query("COUNT(*)", project_id, event_type, keyword,
                                      since, until, category)
        return self.query(sql, args)[0][0]

    def list_events(self, project_id: int | None = None, event_type: str | None = None,
                    keyword: str | None = None, since: str | None = None,
                    until: str | None = None, offset: int = 0, limit: int = 200,
                    category: str | None = None) -> list[sqlite3.Row]:
        sql, args = self._event_query(
            "e.id, e.project_id, e.run_id, e.event_type, e.rel_path, e.category, "
            "e.observed_at, e.fs_mtime, e.certainty, e.evidence, p.name AS project_name",
            project_id, event_type, keyword, since, until, category)
        sql += f" ORDER BY e.id DESC LIMIT {int(limit)} OFFSET {int(offset)}"
        return self.query(sql, args)

    @staticmethod
    def _event_query(select: str, project_id, event_type, keyword, since, until,
                     category=None) -> tuple[str, list]:
        sql = f"SELECT {select} FROM events e JOIN projects p ON p.id=e.project_id WHERE 1=1"
        args: list = []
        if project_id is not None:
            sql += " AND e.project_id=?"
            args.append(project_id)
        if event_type:
            sql += " AND e.event_type=?"
            args.append(event_type)
        if category:
            sql += " AND e.category=?"
            args.append(category)
        if keyword:
            sql += " AND e.rel_path LIKE ? ESCAPE '\\'"
            args.append("%" + keyword.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%")
        if since:
            sql += " AND substr(e.observed_at,1,10)>=?"
            args.append(since)
        if until:
            sql += " AND substr(e.observed_at,1,10)<=?"
            args.append(until)
        return sql, args

    def count_events_for_run(self, run_id: int) -> int:
        return self.query("SELECT COUNT(*) FROM events WHERE run_id=?", (run_id,))[0][0]

    # ---------- 快照对比 ----------
    def compare_snapshots(self, old_id: int, new_id: int, limit: int = 2000) -> dict:
        """对比两个快照：added / removed / modified。modified 以内容签名
        （sha256，缺失时退化为 size+mtime）判断，文件系统 mtime 不作为内容变化依据。"""
        old_rows = self.query(
            "SELECT rel_path, size, mtime, sha256, category FROM file_records WHERE snapshot_id=?",
            (old_id,))
        new_rows = self.query(
            "SELECT rel_path, size, mtime, sha256, category FROM file_records WHERE snapshot_id=?",
            (new_id,))
        old_map = {r["rel_path"]: r for r in old_rows}
        new_map = {r["rel_path"]: r for r in new_rows}

        added = [{"rel_path": r["rel_path"], "size": r["size"], "category": r["category"],
                  "sha256": r["sha256"], "mtime": r["mtime"]}
                 for p, r in new_map.items() if p not in old_map]
        removed = [{"rel_path": r["rel_path"], "size": r["size"], "category": r["category"],
                    "sha256": r["sha256"], "mtime": r["mtime"]}
                   for p, r in old_map.items() if p not in new_map]
        modified = []
        for p, n in new_map.items():
            o = old_map.get(p)
            if o is None:
                continue
            if n["sha256"] and o["sha256"]:
                changed = n["sha256"] != o["sha256"]
            else:
                changed = n["size"] != o["size"] or n["mtime"] != o["mtime"]
            if changed:
                modified.append({
                    "rel_path": p, "old_size": o["size"], "new_size": n["size"],
                    "old_sha256": o["sha256"], "new_sha256": n["sha256"],
                    "old_mtime": o["mtime"], "new_mtime": n["mtime"],
                    "category": n["category"],
                })
        for lst in (added, removed, modified):
            lst.sort(key=lambda x: x["rel_path"])
        return {"added": added[:limit], "removed": removed[:limit],
                "modified": modified[:limit],
                "counts": (len(added), len(removed), len(modified))}

    # ---------- 成果发现 ----------
    def record_findings(self, findings: list[dict]) -> int:
        if not findings:
            return 0
        rows = [
            (f["project_id"], f["snapshot_id"], f["rel_path"], f["kind"], f["confidence"],
             f.get("status", "auto"), f["basis"],
             json.dumps(f.get("evidence", {}), ensure_ascii=False), f["detected_at"])
            for f in findings
        ]
        with self._session(write=True) as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO findings"
                "(project_id, snapshot_id, rel_path, kind, confidence, status, basis, evidence, detected_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                rows,
            )
            return cur.rowcount

    def list_findings(self, project_id: int, snapshot_id: int | None = None,
                      limit: int = 500) -> list[sqlite3.Row]:
        sql = "SELECT * FROM findings WHERE project_id=?"
        args: list = [project_id]
        if snapshot_id is not None:
            sql += " AND snapshot_id=?"
            args.append(snapshot_id)
        sql += " ORDER BY status='rejected', kind, confidence DESC, id DESC LIMIT ?"
        args.append(limit)
        return self.query(sql, args)

    def set_finding_status(self, finding_id: int, status: str) -> None:
        """人工确认/否决（manual confirmation），与自动判断区分。"""
        with self._session(write=True) as conn:
            conn.execute("UPDATE findings SET status=? WHERE id=?", (status, finding_id))

    # ---------- 成果档案（v2） ----------
    def upsert_artifact(self, project_id: int, kind: str, name: str, primary_path: str,
                        confidence: str, basis: str, details: str, snapshot_id: int,
                        now: str) -> tuple[int, bool]:
        """按 (project_id, kind, primary_path) 聚合：已存在则更新最近发现，否则新建。
        返回 (artifact_id, created)。"""
        with self._session(write=True) as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE project_id=? AND kind=? AND primary_path=?",
                (project_id, kind, primary_path)).fetchone()
            if row:
                conn.execute(
                    "UPDATE artifacts SET name=?, confidence=?, last_snapshot_id=?, "
                    "last_seen_at=?, details=?, updated_at=? WHERE id=?",
                    (name, confidence, snapshot_id, now, details, now, row["id"]))
                return row["id"], False
            cur = conn.execute(
                "INSERT INTO artifacts(project_id, kind, name, primary_path, confidence, "
                "status, first_snapshot_id, last_snapshot_id, first_seen_at, last_seen_at, "
                "basis, details, created_at, updated_at) "
                "VALUES(?,?,?,?,?,'auto',?,?,?,?,?,?,?,?)",
                (project_id, kind, name, primary_path, confidence, snapshot_id, snapshot_id,
                 now, now, basis, details, now, now))
            return cur.lastrowid, True

    def replace_artifact_files(self, artifact_id: int, snapshot_id: int,
                               files: list[dict]) -> None:
        """替换某成果在某快照下的关联文件（分析是确定性的，可安全重跑）。"""
        with self._session(write=True) as conn:
            conn.execute("DELETE FROM artifact_files WHERE artifact_id=? AND snapshot_id=?",
                         (artifact_id, snapshot_id))
            conn.executemany(
                "INSERT OR IGNORE INTO artifact_files"
                "(artifact_id, snapshot_id, rel_path, role, basis, confidence) "
                "VALUES(?,?,?,?,?,?)",
                [(artifact_id, snapshot_id, f["rel_path"], f["role"], f.get("basis", ""),
                  f.get("confidence", "medium")) for f in files])

    def list_artifacts(self, project_id: int, kind: str | None = None,
                       status: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        sql = "SELECT * FROM artifacts WHERE project_id=?"
        args: list = [project_id]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY status='rejected', last_seen_at DESC, id DESC LIMIT ?"
        args.append(limit)
        return self.query(sql, args)

    def get_artifact(self, artifact_id: int) -> sqlite3.Row | None:
        return (self.query("SELECT * FROM artifacts WHERE id=?", (artifact_id,)) or [None])[0]

    def set_artifact_status(self, artifact_id: int, status: str) -> None:
        with self._session(write=True) as conn:
            conn.execute("UPDATE artifacts SET status=?, updated_at=? WHERE id=?",
                         (status, utcnow_iso(), artifact_id))

    def list_artifact_files(self, artifact_id: int, snapshot_id: int | None = None
                            ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM artifact_files WHERE artifact_id=?"
        args: list = [artifact_id]
        if snapshot_id is not None:
            sql += " AND snapshot_id=?"
            args.append(snapshot_id)
        sql += " ORDER BY role, rel_path"
        return self.query(sql, args)

    # ---------- 关系图谱（v2） ----------
    def replace_relations(self, project_id: int, snapshot_id: int, edges: list[dict],
                          sources: list[str] | None = None) -> int:
        """替换某快照下由指定规则来源产生的边（每条规则独立替换，规则可单独重跑）。"""
        with self._session(write=True) as conn:
            if sources is None:
                conn.execute("DELETE FROM relations WHERE project_id=? AND snapshot_id=?",
                             (project_id, snapshot_id))
            else:
                for s in sources:
                    conn.execute(
                        "DELETE FROM relations WHERE project_id=? AND snapshot_id=? AND source=?",
                        (project_id, snapshot_id, s))
            if not edges:
                return 0
            cur = conn.executemany(
                "INSERT OR IGNORE INTO relations"
                "(project_id, snapshot_id, src_path, dst_path, relation, source, basis, "
                "confidence, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                [(project_id, snapshot_id, e["src_path"], e["dst_path"], e["relation"],
                  e["source"], e.get("basis", ""), float(e.get("confidence", 0.5)),
                  e.get("created_at", utcnow_iso())) for e in edges])
            return cur.rowcount

    def list_relations(self, project_id: int, snapshot_id: int,
                       src_path: str | None = None, dst_path: str | None = None,
                       relation: str | None = None, limit: int = 2000) -> list[sqlite3.Row]:
        sql = "SELECT * FROM relations WHERE project_id=? AND snapshot_id=?"
        args: list = [project_id, snapshot_id]
        if src_path:
            sql += " AND src_path=?"
            args.append(src_path)
        if dst_path:
            sql += " AND dst_path=?"
            args.append(dst_path)
        if relation:
            sql += " AND relation=?"
            args.append(relation)
        sql += " ORDER BY confidence DESC, id LIMIT ?"
        args.append(limit)
        return self.query(sql, args)

    # ---------- 复现检查 ----------
    def save_repro_check(self, project_id: int, snapshot_id: int, target_path: str,
                         level: str, score: int, checks: str, artifact_id: int | None,
                         now: str) -> int:
        """保存（或整行替换）一条复现检查结果，按 (project, snapshot, target) 幂等。"""
        with self._session(write=True) as conn:
            conn.execute(
                "INSERT INTO repro_checks(project_id, snapshot_id, artifact_id, target_path, "
                "level, score, checks, generated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id, snapshot_id, target_path) DO UPDATE SET "
                "artifact_id=excluded.artifact_id, level=excluded.level, score=excluded.score, "
                "checks=excluded.checks, generated_at=excluded.generated_at",
                (project_id, snapshot_id, artifact_id, target_path, level, score, checks, now))
            row = conn.execute(
                "SELECT id FROM repro_checks WHERE project_id=? AND snapshot_id=? AND target_path=?",
                (project_id, snapshot_id, target_path)).fetchone()
            return row[0]

    def list_repro_checks(self, project_id: int, snapshot_id: int | None = None,
                          limit: int = 200) -> list[sqlite3.Row]:
        sql = "SELECT * FROM repro_checks WHERE project_id=?"
        args: list = [project_id]
        if snapshot_id:
            sql += " AND snapshot_id=?"
            args.append(snapshot_id)
        sql += " ORDER BY generated_at DESC, id LIMIT ?"
        args.append(limit)
        return self.query(sql, args)

    def get_repro_check(self, check_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM repro_checks WHERE id=?", (check_id,))
        return rows[0] if rows else None
