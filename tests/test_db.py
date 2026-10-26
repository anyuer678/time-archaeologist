"""数据库层测试：持久化、恢复、事件去重、快照对比。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from time_arch.db import Database


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "a.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        pid, _ = self.db.add_project(str(Path(self.tmp.name)), name="demo")
        run = self.db.start_run(pid)
        snap = self.db.create_snapshot(pid, run)
        self.db.insert_file_records(snap, [
            {"rel_path": "a.py", "size": 10, "mtime": "2026-01-01T00:00:00+00:00",
             "ext": ".py", "category": "source", "sha256": "x1"},
            {"rel_path": "b.log", "size": 5, "mtime": "2026-01-02T00:00:00+00:00",
             "ext": ".log", "category": "log", "sha256": "x2"},
        ])
        self.db.complete_run(run, snap, 2, 15, 0)
        return pid, run, snap

    def test_project_persist_and_reopen(self):
        pid, _, _ = self._seed()
        db2 = Database(Path(self.tmp.name) / "a.db")  # 模拟重启
        rows = db2.list_projects()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], pid)
        self.assertEqual(rows[0]["snapshot_count"], 1)
        self.assertIsNotNone(db2.latest_snapshot(pid))

    def test_add_project_duplicate_path(self):
        p1, created1 = self.db.add_project("/x/y")
        p2, created2 = self.db.add_project("/x/y")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(p1, p2)

    def test_interrupted_run_recovery(self):
        pid, _, _ = self._seed()
        run2 = self.db.start_run(pid)  # 模拟进程崩溃时遗留 running
        db2 = Database(Path(self.tmp.name) / "a.db")
        row = db2.get_run(run2)
        self.assertEqual(row["status"], "interrupted")

    def test_event_dedup_same_run(self):
        pid, run, snap = self._seed()
        ev = {"project_id": pid, "run_id": run, "event_type": "file_added",
              "rel_path": "a.py", "category": "source",
              "observed_at": "2026-01-01T00:00:00+00:00",
              "certainty": "observed", "evidence": {}}
        n1 = self.db.record_events([ev])
        n2 = self.db.record_events([dict(ev)])
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)  # 同一 run 同一事件不重复
        self.assertEqual(self.db.count_events_for_run(run), 1)

    def test_compare_snapshots(self):
        pid, run, snap1 = self._seed()
        # 第二次快照：a.py 修改，b.log 删除，c.md 新增
        run2 = self.db.start_run(pid)
        snap2 = self.db.create_snapshot(pid, run2)
        self.db.insert_file_records(snap2, [
            {"rel_path": "a.py", "size": 99, "mtime": "2026-01-01T00:00:00+00:00",
             "ext": ".py", "category": "source", "sha256": "x1-changed"},
            {"rel_path": "c.md", "size": 3, "mtime": "2026-01-03T00:00:00+00:00",
             "ext": ".md", "category": "doc", "sha256": "x3"},
        ])
        self.db.complete_run(run2, snap2, 2, 102, 0)
        d = self.db.compare_snapshots(snap1, snap2)
        self.assertEqual(d["counts"], (1, 1, 1))
        self.assertEqual(d["added"][0]["rel_path"], "c.md")
        self.assertEqual(d["removed"][0]["rel_path"], "b.log")
        self.assertEqual(d["modified"][0]["rel_path"], "a.py")
        self.assertEqual(d["modified"][0]["old_sha256"], "x1")
        self.assertEqual(d["modified"][0]["new_sha256"], "x1-changed")

    def test_mtime_only_change_not_modified(self):
        """仅 mtime 变化、内容签名相同 → 不算修改（mtime 不作为内容证据）。"""
        pid, run, snap1 = self._seed()
        run2 = self.db.start_run(pid)
        snap2 = self.db.create_snapshot(pid, run2)
        self.db.insert_file_records(snap2, [
            {"rel_path": "a.py", "size": 10, "mtime": "2030-05-05T00:00:00+00:00",
             "ext": ".py", "category": "source", "sha256": "x1"},
            {"rel_path": "b.log", "size": 5, "mtime": "2026-01-02T00:00:00+00:00",
             "ext": ".log", "category": "log", "sha256": "x2"},
        ])
        self.db.complete_run(run2, snap2, 2, 15, 0)
        d = self.db.compare_snapshots(snap1, snap2)
        self.assertEqual(d["counts"], (0, 0, 0))

    def test_remove_project_cascades(self):
        pid, _, _ = self._seed()
        self.db.record_events([{"project_id": pid, "run_id": 1, "event_type": "file_added",
                                "rel_path": "a.py", "observed_at": "t", "evidence": {}}])
        self.db.remove_project(pid)
        self.assertEqual(self.db.list_projects(), [])
        self.assertEqual(self.db.count_events(), 0)

    def test_event_filters_and_paging(self):
        pid, run, snap = self._seed()
        evs = [{"project_id": pid, "run_id": run, "event_type": "file_added",
                "rel_path": f"f{i}.py", "category": "source",
                "observed_at": "2026-01-01T00:00:00+00:00", "certainty": "observed",
                "evidence": {}} for i in range(25)]
        self.db.record_events(evs)
        self.assertEqual(self.db.count_events(project_id=pid, keyword="f19"), 1)
        self.assertEqual(self.db.count_events(project_id=pid, keyword="f1."), 1)
        page1 = self.db.list_events(project_id=pid, limit=10, offset=0)
        page3 = self.db.list_events(project_id=pid, limit=10, offset=20)
        self.assertEqual(len(page1), 10)
        self.assertEqual(len(page3), 5)
        self.assertEqual(self.db.count_events(project_id=pid, event_type="file_added"), 25)


if __name__ == "__main__":
    unittest.main()
