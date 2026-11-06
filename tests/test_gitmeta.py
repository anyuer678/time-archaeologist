"""Git 元数据只读解析测试：HEAD、refs、packed-refs、reflog、gitdir 间接。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from time_arch.gitmeta import find_git_dir, read_git_meta

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


class GitMetaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ta_gitmeta_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        (self.root / ".git" / "refs" / "heads").mkdir(parents=True)
        (self.root / ".git" / "refs" / "tags").mkdir(parents=True)
        (self.root / ".git" / "logs" / "refs" / "heads").mkdir(parents=True)
        (self.root / ".git" / "HEAD").write_text("ref: refs/heads/main\n",
                                                 encoding="utf-8")
        (self.root / ".git" / "refs" / "heads" / "main").write_text(SHA_B + "\n",
                                                                    encoding="utf-8")
        (self.root / ".git" / "refs" / "tags" / "v1.0").write_text(SHA_A + "\n",
                                                                   encoding="utf-8")
        (self.root / ".git" / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted \n"
            f"{SHA_C} refs/remotes/origin/main\n^" + SHA_C + "\n",
            encoding="utf-8")
        (self.root / ".git" / "logs" / "HEAD").write_text(
            "0" * 40 + f" {SHA_A} Alice <a@example.com> 1767225600 +0800\t"
            "commit (initial): init\n"
            f"{SHA_A} {SHA_B} Alice <a@example.com> 1767229200 +0800\t"
            "commit: add report\n",
            encoding="utf-8")
        (self.root / ".git" / "logs" / "refs" / "heads" / "main").write_text(
            f"{SHA_A} {SHA_B} Alice <a@example.com> 1767229200 +0800\t"
            "commit: add report\n",
            encoding="utf-8")

    def test_reads_head_refs_and_reflog(self):
        meta = read_git_meta(str(self.root))
        self.assertIsNotNone(meta)
        self.assertEqual(meta["branch"], "refs/heads/main")
        self.assertEqual(meta["head_sha"], SHA_B)
        self.assertEqual(meta["refs"]["refs/heads/main"], SHA_B)
        self.assertEqual(meta["refs"]["refs/tags/v1.0"], SHA_A)
        self.assertEqual(meta["refs"]["refs/remotes/origin/main"], SHA_C)
        # HEAD 与分支 reflog 重复记录同一动作 → 去重后 2 条，按时间倒序
        self.assertEqual(len(meta["reflog"]), 2)
        self.assertEqual(meta["reflog"][0]["message"], "commit: add report")
        self.assertEqual(meta["reflog"][1]["message"], "commit (initial): init")
        self.assertTrue(meta["reflog"][0]["time_utc"].startswith("2026-"))

    def test_not_a_repo(self):
        other = Path(self.tmp.name) / "plain"
        other.mkdir()
        self.assertIsNone(find_git_dir(str(other)))
        self.assertIsNone(read_git_meta(str(other)))

    def test_gitdir_indirection(self):
        store = Path(self.tmp.name) / "store"
        (store / "refs" / "heads").mkdir(parents=True)
        (store / "HEAD").write_text("ref: refs/heads/dev\n", encoding="utf-8")
        (store / "refs" / "heads" / "dev").write_text(SHA_C + "\n", encoding="utf-8")
        worktree = Path(self.tmp.name) / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {store}\n", encoding="utf-8")
        self.assertEqual(find_git_dir(str(worktree)), str(store))
        meta = read_git_meta(str(worktree))
        self.assertEqual(meta["branch"], "refs/heads/dev")
        self.assertEqual(meta["head_sha"], SHA_C)

    def test_malformed_reflog_ignored(self):
        (self.root / ".git" / "logs" / "HEAD").write_text(
            "not a reflog line\n"
            f"{SHA_A} {SHA_B} Bob <b@example.com> 1767229200 +0000\tcommit: ok\n",
            encoding="utf-8")
        meta = read_git_meta(str(self.root))
        # 畸形行被忽略；HEAD 新记录 + 分支原有记录共 2 条
        self.assertEqual(len(meta["reflog"]), 2)
        self.assertTrue(any(r["author"] == "Bob" and r["message"] == "commit: ok"
                            for r in meta["reflog"]))


if __name__ == "__main__":
    unittest.main()
