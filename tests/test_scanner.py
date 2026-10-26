"""扫描器测试：只读、哈希正确、事件生成、取消回滚、错误恢复、符号链接。"""
import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from time_arch.db import Database
from time_arch.scanner import ScanCancelled, run_scan


def make_tree(base: Path) -> dict:
    """构造测试项目目录，返回 {相对路径: 内容}。"""
    files = {
        "main.py": "print('hello')\n" * 10,
        "README.md": "# demo\n",
        "logs/app.log": "2026-01-01 start\n2026-01-01 done\n",
        "src/util.py": "def f():\n    return 1\n",
        "data/metrics.csv": "a,b\n1,2\n",
    }
    out = {}
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        out[rel.replace("/", os.sep)] = content
    return out


class TestScanner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.pid, _ = self.db.add_project(str(self.root), name="proj")
        self.files = make_tree(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _snapshot_files(self, snap_id):
        rows = self.db.query("SELECT * FROM file_records WHERE snapshot_id=?", (snap_id,))
        return {r["rel_path"]: dict(r) for r in rows}

    def test_scan_records_and_hash(self):
        res = run_scan(self.db, self.pid)
        self.assertEqual(res.status, "completed")
        self.assertEqual(res.files, len(self.files))
        recs = self._snapshot_files(res.snapshot_id)
        self.assertEqual(set(recs), set(self.files))
        # 与磁盘真实字节比对（write_text 在 Windows 会写入 CRLF，故按字节验证）
        expect = hashlib.sha256((self.root / "main.py").read_bytes()).hexdigest()
        self.assertEqual(recs["main.py"]["sha256"], expect)
        self.assertEqual(recs["main.py"]["category"], "source")
        self.assertEqual(recs[os.path.join("logs", "app.log")]["category"], "log")

    def test_readonly_guarantee(self):
        before = {p: (p.stat().st_mtime_ns, p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest())
                  for p in self.root.rglob("*") if p.is_file()}
        run_scan(self.db, self.pid)
        after = {p: (p.stat().st_mtime_ns, p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest())
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)  # 时间戳、大小、内容均未变

    def test_rescan_no_change_no_events(self):
        r1 = run_scan(self.db, self.pid)
        self.assertGreater(r1.events_created, 0)  # 首扫记录现存文件
        r2 = run_scan(self.db, self.pid)
        self.assertEqual(r2.events_created, 0)    # 无变化 → 0 新事件
        self.assertEqual(self.db.count_events(project_id=self.pid), r1.events_created)

    def test_diff_events_add_modify_remove(self):
        run_scan(self.db, self.pid)
        # 变更：修改 main.py、新增 new.py、删除 README.md
        (self.root / "main.py").write_text("print('changed')\n", encoding="utf-8")
        (self.root / "new.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "README.md").unlink()
        res = run_scan(self.db, self.pid)
        self.assertEqual(res.events_created, 3)
        types = {}
        for r in self.db.query("SELECT event_type, rel_path, certainty FROM events WHERE run_id=?",
                               (res.run_id,)):
            types[r["event_type"]] = (r["rel_path"], r["certainty"])
        self.assertEqual(types["file_modified"][0], "main.py")
        self.assertEqual(types["file_added"][0], "new.py")
        self.assertEqual(types["file_removed"][0], "README.md")
        for _, cert in types.values():
            self.assertEqual(cert, "observed")

    def test_inferred_events_for_logs(self):
        run_scan(self.db, self.pid)
        (self.root / "logs" / "new.log").write_text("line\n", encoding="utf-8")
        res = run_scan(self.db, self.pid)
        inferred = self.db.query(
            "SELECT * FROM events WHERE run_id=? AND certainty='inferred'", (res.run_id,))
        kinds = {r["event_type"] for r in inferred}
        self.assertIn("log_generated", kinds)
        for r in inferred:  # 推测事件必须带依据说明
            self.assertIn("启发式", r["evidence"])

    def test_cancel_rolls_back_and_retry(self):
        for i in range(300):
            (self.root / f"g{i:03d}.dat").write_bytes(b"0" * 2048)
        cancel = threading.Event()

        stop_at = {"n": 0}

        def cb(pr):
            stop_at["n"] += 1
            if stop_at["n"] >= 2:
                cancel.set()

        res = run_scan(self.db, self.pid, cancel_event=cancel, progress_cb=cb)
        self.assertEqual(res.status, "cancelled")
        self.assertIsNone(res.snapshot_id)
        run = self.db.get_run(res.run_id)
        self.assertEqual(run["status"], "cancelled")
        self.assertIsNone(self.db.latest_snapshot(self.pid))  # 部分快照已回滚
        # 安全重试：不取消可完整扫完
        res2 = run_scan(self.db, self.pid)
        self.assertEqual(res2.status, "completed")
        self.assertEqual(res2.files, 5 + 300)

    def test_unexpected_error_fails_run(self):
        with mock.patch("time_arch.scanner.iter_files",
                        side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                run_scan(self.db, self.pid)
        status = self.db.query("SELECT status FROM scan_runs WHERE project_id=?",
                               (self.pid,))[0][0]
        self.assertEqual(status, "error")

    def test_scan_error_directory_recorded(self):
        """某个子目录不可读：记录错误并继续。"""
        (self.root / "sub").mkdir()
        (self.root / "sub" / "x.py").write_text("1\n", encoding="utf-8")
        real_scandir = os.scandir

        def fake_scandir(path):
            path_norm = str(path).replace("\\\\?\\", "")
            if path_norm.endswith("sub"):
                raise PermissionError(13, "拒绝访问")
            return real_scandir(path)

        with mock.patch("time_arch.scanner.os.scandir", side_effect=fake_scandir):
            res = run_scan(self.db, self.pid)
        self.assertEqual(res.status, "completed")
        self.assertGreaterEqual(res.errors, 1)
        recs = self._snapshot_files(res.snapshot_id)
        err_rec = [r for r in recs.values() if r["read_error"]]
        self.assertTrue(err_rec)
        # 主目录其他文件仍被扫描
        self.assertIn("main.py", recs)

    def test_symlink_recorded_not_followed(self):
        try:
            link = self.root / "link_to_src"
            link.symlink_to(self.root / "src", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("此环境无法创建符号链接")
        res = run_scan(self.db, self.pid)
        recs = self._snapshot_files(res.snapshot_id)
        key = "link_to_src"
        self.assertIn(key, recs)
        self.assertEqual(recs[key]["is_symlink"], 1)
        self.assertIsNotNone(recs[key]["link_target"])
        # 未通过链接递归（不会出现 link_to_src\util.py 之类的路径）
        self.assertFalse([k for k in recs if k.startswith(key + os.sep) or k.startswith(key + "/")])
        src_files = [k for k in recs if k.startswith("src")]
        self.assertTrue(src_files)

    def test_file_changed_during_hash(self):
        """哈希中文件被删除：记录读取错误，扫描继续。"""
        target = self.root / "tmp_file.bin"
        target.write_bytes(b"a" * (1 << 21))  # 2MB，跨多个块
        real_open = open

        def fake_open(file, mode="r", *a, **kw):
            if str(file).endswith("tmp_file.bin") and "b" in mode and "r" in mode:
                class F:
                    def __init__(self):
                        self.n = 0

                    def read(self, n):
                        self.n += 1
                        if self.n == 1:
                            return b"a" * (1 << 20)
                        raise FileNotFoundError("gone during scan")

                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                    def close(self):
                        pass
                return F()
            return real_open(file, mode, *a, **kw)

        with mock.patch("time_arch.scanner.open", side_effect=fake_open):
            res = run_scan(self.db, self.pid)
        self.assertEqual(res.status, "completed")
        self.assertGreaterEqual(res.errors, 1)
        recs = self._snapshot_files(res.snapshot_id)
        self.assertTrue(recs["tmp_file.bin"]["read_error"])
        self.assertIsNone(recs["tmp_file.bin"]["sha256"])

    def test_restart_persistence_full_cycle(self):
        """重启（重建 Database）后项目与快照仍在，且能继续扫描。"""
        res = run_scan(self.db, self.pid)
        db2 = Database(Path(self.tmp.name) / "test.db")
        self.assertIsNotNone(db2.latest_snapshot(self.pid))
        (self.root / "after_restart.py").write_text("1\n", encoding="utf-8")
        res2 = run_scan(db2, self.pid)
        self.assertEqual(res2.events_created, 1)

    def test_incremental_hash_reuse(self):
        """size+mtime 未变的文件复用上次 SHA-256；变化的文件重新哈希且能检出修改。"""
        r1 = run_scan(self.db, self.pid)
        self.assertEqual(r1.files_cached, 0)
        total = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        self.assertEqual(r1.bytes_hashed, total)  # 首扫全量哈希
        # 无变化重扫：全部复用缓存，零字节新哈希
        r2 = run_scan(self.db, self.pid)
        self.assertEqual(r2.files_cached, len(self.files))
        self.assertEqual(r2.bytes_hashed, 0)
        self.assertEqual(r2.events_created, 0)
        # 改一个文件：仅它重新哈希，且 SHA 变化被检出
        old_hash = hashlib.sha256((self.root / "main.py").read_bytes()).hexdigest()
        (self.root / "main.py").write_text("print('v2')\n", encoding="utf-8")
        r3 = run_scan(self.db, self.pid)
        self.assertEqual(r3.files_cached, len(self.files) - 1)
        self.assertGreater(r3.bytes_hashed, 0)
        self.assertEqual(r3.events_created, 1)
        recs = self._snapshot_files(r3.snapshot_id)
        self.assertNotEqual(recs["main.py"]["sha256"], old_hash)
        # 仅 utime 变化：缓存失效会重哈希，但内容签名相同 → 不误报修改
        p = self.root / "src" / "util.py"
        os.utime(p, (p.stat().st_atime + 7200, p.stat().st_mtime + 7200))
        r4 = run_scan(self.db, self.pid)
        self.assertEqual(r4.events_created, 0)
        # 缓存的哈希值与真实文件内容一致（防止缓存张冠李戴）
        self.assertEqual(r4.files_cached, len(self.files) - 1)
        recs4 = self._snapshot_files(r4.snapshot_id)
        self.assertEqual(recs4["main.py"]["sha256"],
                         hashlib.sha256((self.root / "main.py").read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
