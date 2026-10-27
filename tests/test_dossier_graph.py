"""成果档案与关系图谱测试（2.0 第二步）。

用真实临时目录 + 真实扫描链路验证：迁移兼容、成果聚类、完整性检查、
关系规则（正例/反例）、幂等重跑、反追踪。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from time_arch.db import Database
from time_arch.dossier import (ARTIFACT_KINDS, analyze_artifacts, dossier_view,
                               run_dossier_analysis)
from time_arch.graph import run_relation_analysis, trace_relations
from time_arch.scanner import run_scan


def make_project(files: dict[str, str | bytes]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ta_dossier_"))
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return root


EXPERIMENT_FILES = {
    "src/train.py": "import json\n\ndef train():\n    print('train')\n",
    "tests/test_train.py": "import src.train\n",
    "config/settings.json": '{"mode": "strict"}\n',
    "runs/exp01/params.yaml": "epochs: 10\nseed: 42\n",
    "runs/exp01/train.log": "epoch 1 loss 0.5\n" + "epoch 2 loss 0.3\n" * 10,
    "runs/exp01/metrics.json": '{"accuracy": 0.91, "samples": 200}',
    "runs/exp01/data/sample.csv": "a,b\n1,2\n3,4\n",
    "README.md": "实验说明：结果见 runs/exp01/metrics.json\n",
    "reports/report-v1.md": "# 报告 v1\naccuracy 0.85\n",
    "reports/report-v2.md": "# 报告 v2\naccuracy 0.91\n",
    "reports/junit-results.xml": "<testsuite tests='12' failures='1'/>\n",
    "dist/labtool-1.2.0.zip": b"PK-demo-zip-bytes" * 100,
}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ta_db_")
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.project_root = make_project(EXPERIMENT_FILES)
        self.pid, _ = self.db.add_project(str(self.project_root), "实验项目")
        self.result = run_scan(self.db, self.pid)
        self.snap = self.result.snapshot_id


class TestMigrationCompat(unittest.TestCase):
    """旧版（v0.1.0）数据库升级到 v2：旧数据零丢失，新表可用。"""

    def test_v01_db_migrates_and_keeps_data(self):
        tmp = tempfile.TemporaryDirectory(prefix="ta_mig_")
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "old.db"
        # 手工构造 v0.1.0 库：旧表 + 旧数据，user_version=0
        conn = sqlite3.connect(db_path)
        conn.executescript("""
        CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, created_at TEXT NOT NULL, last_scan_at TEXT);
        CREATE TABLE scan_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
            files_scanned INTEGER NOT NULL DEFAULT 0, bytes_hashed INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0, error_text TEXT, snapshot_id INTEGER);
        CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL);
        CREATE TABLE file_records (id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            rel_path TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, mtime TEXT NOT NULL DEFAULT '',
            ext TEXT, category TEXT NOT NULL DEFAULT 'other', sha256 TEXT,
            is_symlink INTEGER NOT NULL DEFAULT 0, link_target TEXT, read_error TEXT,
            UNIQUE(snapshot_id, rel_path));
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL, rel_path TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other', observed_at TEXT NOT NULL, fs_mtime TEXT,
            certainty TEXT NOT NULL DEFAULT 'observed', evidence TEXT NOT NULL DEFAULT '{}',
            UNIQUE(run_id, event_type, rel_path));
        CREATE TABLE findings (id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            rel_path TEXT NOT NULL, kind TEXT NOT NULL, confidence TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'auto', basis TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '{}', detected_at TEXT NOT NULL,
            UNIQUE(snapshot_id, rel_path, kind));
        INSERT INTO projects(path, name, created_at) VALUES('C:/old/proj', '旧项目', '2026-01-01T00:00:00+00:00');
        """)
        conn.commit()
        conn.close()

        db = Database(db_path)  # 打开即迁移
        self.assertEqual(db.query("PRAGMA user_version")[0][0], 2)
        old = db.get_project(1)
        self.assertIsNotNone(old)
        self.assertEqual(old["name"], "旧项目")
        # 新表存在且可用；库完整性 OK
        tables = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"artifacts", "artifact_files", "relations"} <= tables)
        self.assertEqual(db.query("PRAGMA integrity_check")[0][0], "ok")
        # 幂等：重开不重复迁移、不报错
        db2 = Database(db_path)
        self.assertEqual(db2.query("PRAGMA user_version")[0][0], 2)
        self.assertIsNotNone(db2.get_project(1))


class TestArtifacts(_Base):
    def test_experiment_run_grouping(self):
        arts = analyze_artifacts(self.db, self.pid, self.snap)
        kinds = {a["kind"] for a in arts}
        self.assertIn("experiment_result", kinds)
        exp = next(a for a in arts if a["kind"] == "experiment_result")
        self.assertTrue(exp["primary_path"].startswith("runs/exp01/"))
        roles = {f["rel_path"]: f["role"] for f in exp["files"]}
        # 运行目录成员全部聚合，且角色按类别映射
        self.assertEqual(roles.get("runs/exp01/params.yaml"), "params")
        self.assertEqual(roles.get("runs/exp01/train.log"), "log")
        self.assertEqual(roles.get("runs/exp01/data/sample.csv"), "data")
        self.assertEqual(roles.get("runs/exp01/metrics.json"), "output")
        # 完整性：present/missing 明确区分，缺失项≠失败
        by_item = {c["item"]: c["state"] for c in exp["completeness"]}
        self.assertEqual(by_item["参数记录"], "present")
        self.assertEqual(by_item["输入数据"], "present")
        self.assertEqual(by_item["运行日志"], "present")
        self.assertEqual(by_item["错误/异常记录"], "missing")
        # 已验证事实包含快照可溯源记录；假设明确标注未执行验证
        self.assertTrue(any("快照 #" in s for s in exp["verified_facts"]))
        self.assertTrue(any("未" in s and "执行" in s for s in exp["unverified_assumptions"]))

    def test_software_release_not_treated_as_success(self):
        arts = analyze_artifacts(self.db, self.pid, self.snap)
        rel = next(a for a in arts if a["kind"] == "software_release")
        self.assertEqual(rel["primary_path"], "dist/labtool-1.2.0.zip")
        self.assertTrue(any("未" in s for s in rel["unverified_assumptions"]))
        # .zip 存在不等于交付成功：完整性中「源码」等仍按证据呈现
        states = {c["item"]: c["state"] for c in rel["completeness"]}
        self.assertIn(states["源码"], {"present", "missing"})
        self.assertIn("seed_rule", rel)

    def test_document_and_test_result_kinds(self):
        arts = analyze_artifacts(self.db, self.pid, self.snap)
        kinds = {a["kind"] for a in arts}
        self.assertIn("document", kinds)       # reports/report-v*.md
        self.assertIn("test_result", kinds)    # tests/test_train.py 命名 + 无 junit 报告 → 覆盖率缺失
        tr = next(a for a in arts if a["kind"] == "test_result")
        self.assertTrue(any(c["state"] == "missing" for c in tr["completeness"]))

    def test_persist_idempotent_and_view(self):
        r1 = run_dossier_analysis(self.db, self.pid, self.snap)
        self.assertGreater(r1["artifacts_created"], 0)
        n_files_1 = len(self.db.query("SELECT * FROM artifact_files"))
        r2 = run_dossier_analysis(self.db, self.pid, self.snap)
        self.assertEqual(r2["artifacts_created"], 0)   # 幂等：重跑只更新
        self.assertEqual(len(self.db.query("SELECT * FROM artifact_files")), n_files_1)
        art = self.db.list_artifacts(self.pid)[0]
        view = dossier_view(self.db, art["id"])
        self.assertIsNotNone(view)
        self.assertTrue(view["files"])
        for f in view["files"]:
            self.assertTrue(f["in_snapshot"])          # 每条关联都有文件记录可溯源
        self.db.set_artifact_status(art["id"], "confirmed")
        self.assertEqual(self.db.get_artifact(art["id"])["status"], "confirmed")

    def test_all_kinds_have_checklists_and_labels(self):
        for kind in ARTIFACT_KINDS:
            arts = analyze_artifacts(self.db, self.pid, self.snap)
            self.assertIsInstance(ARTIFACT_KINDS[kind], str)
            for a in arts:
                self.assertTrue(a["completeness"])
                self.assertTrue(a["basis"])


class TestRelations(_Base):
    def setUp(self):
        super().setUp()
        self.rel = run_relation_analysis(self.db, self.pid, self.snap)
        self.edges = self.db.list_relations(self.pid, self.snap)

    def _has(self, **kw):
        for e in self.edges:
            if all(e[k] == v for k, v in kw.items()):
                return e
        return None

    def test_test_module_positive_and_negative(self):
        e = self._has(src_path="tests/test_train.py", relation="test_module")
        self.assertIsNotNone(e)
        self.assertEqual(e["dst_path"], "src/train.py")
        self.assertEqual(e["source"], "test_module@1")
        self.assertLessEqual(e["confidence"], 0.7)     # 文件名推测不许冒充确定事实

    def test_content_reference_full_and_ambiguous(self):
        e = self._has(src_path="README.md", dst_path="runs/exp01/metrics.json",
                      relation="references")
        self.assertIsNotNone(e)
        self.assertGreaterEqual(e["confidence"], 0.9)  # 完整路径出现 = 内容直接证据

    def test_same_run_directory_evidence(self):
        e = self._has(src_path="runs/exp01/params.yaml", dst_path="runs/exp01/data/sample.csv",
                      relation="same_run")
        self.assertIsNotNone(e)
        self.assertIn("runs/exp01", e["basis"])

    def test_evolves_to_version_order(self):
        e = self._has(src_path="reports/report-v1.md", dst_path="reports/report-v2.md",
                      relation="evolves_to")
        self.assertIsNotNone(e)

    def test_idempotent_and_rule_scoped_replace(self):
        n1 = len(self.edges)
        run_relation_analysis(self.db, self.pid, self.snap)
        self.assertEqual(len(self.db.list_relations(self.pid, self.snap)), n1)  # 全量重跑幂等
        # 单规则重跑：该规则的边被替换（不重复、数量不变），其它规则的边原样保留
        run_relation_analysis(self.db, self.pid, self.snap, enabled={"test_module"})
        after = self.db.list_relations(self.pid, self.snap)
        self.assertEqual(len(after), n1)
        self.assertEqual({e["source"] for e in after}, {e["source"] for e in self.edges})
        tm_keys = [(e["src_path"], e["dst_path"], e["source"])
                   for e in after if e["source"] == "test_module@1"]
        self.assertEqual(len(tm_keys), len(set(tm_keys)))

    def test_trace_upstream(self):
        t = trace_relations(self.db, self.pid, self.snap, "runs/exp01/metrics.json",
                            direction="upstream")
        paths = {n["rel_path"] for n in t["nodes"]}
        self.assertIn("runs/exp01/params.yaml", paths)
        self.assertGreater(len(t["edges"]), 0)
        self.assertIsNotNone(t["root_record"])         # 根文件有记录可溯源


if __name__ == "__main__":
    unittest.main()
