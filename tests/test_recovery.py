"""异常恢复测试：进程被硬杀后数据库不损坏、可自动恢复并继续扫描。"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from time_arch.db import Database
from time_arch.scanner import run_scan

CHILD = Path(__file__).parent / "_crash_child.py"


class TestCrashRecovery(unittest.TestCase):
    def test_hard_kill_mid_scan_recovers(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name) / "big"
            for i in range(60):
                d = root / f"d{i:02d}"
                d.mkdir(parents=True)
                for j in range(25):
                    (d / f"f{j:02d}.txt").write_bytes(b"x" * 64)
            db_path = Path(tmp.name) / "crash.db"
            db = Database(db_path)
            pid, _ = db.add_project(str(root), name="crash_target")

            # 子进程扫描中硬杀（os._exit，无清理），模拟断电/被任务管理器结束
            proc = subprocess.run(
                [sys.executable, str(CHILD), str(db_path), str(pid), "200"],
                capture_output=True, text=True, timeout=120,
                cwd=str(Path(__file__).resolve().parents[1]))
            self.assertNotEqual(proc.returncode, 0, "子进程应被硬杀而非正常结束")
            self.assertIn("kill at", proc.stdout)

            # 重新打开数据库：自动恢复 + 完整性检查
            db2 = Database(db_path)
            integrity = db2.query("PRAGMA integrity_check")[0][0]
            self.assertEqual(integrity, "ok")
            runs = db2.query("SELECT status FROM scan_runs WHERE project_id=?", (pid,))
            self.assertIn("interrupted", [r["status"] for r in runs])
            # 被中断的部分快照不算完成快照，不参与对比
            self.assertIsNone(db2.latest_snapshot(pid))

            # 崩溃后能直接安全重扫
            res = run_scan(db2, pid)
            self.assertEqual(res.status, "completed")
            self.assertEqual(res.files, 1500)
            self.assertIsNotNone(db2.latest_snapshot(pid))
        finally:
            tmp.cleanup()

    def test_category_filter(self):
        """时间线按文件类别过滤（全局搜索的文件类型维度）。"""
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name) / "p"
            root.mkdir()
            (root / "a.py").write_text("1", encoding="utf-8")
            (root / "b.log").write_text("1", encoding="utf-8")
            db = Database(Path(tmp.name) / "f.db")
            pid, _ = db.add_project(str(root))
            run_scan(db, pid)
            self.assertEqual(db.count_events(project_id=pid, category="log"),
                             2)  # file_added + log_generated
            self.assertEqual(db.count_events(project_id=pid, category="source"), 1)
            rows = db.list_events(project_id=pid, category="log", limit=10)
            self.assertTrue(all(r["category"] == "log" for r in rows))
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
