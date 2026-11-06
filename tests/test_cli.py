"""无头命令行测试：扫描/分析/报告/线索/档案/复现/任务/时间线/对比。

全部走真实临时目录与真实 SQLite 链路；CLI 只读扫描，不执行项目内任何程序。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from time_arch import cli
from time_arch.db import Database

FILES = {
    "src/train.py": "def train():\n    return 1\n",
    "tests/test_train.py": "from src.train import train\n\ndef test_ok():\n"
                           "    assert train() == 1\n",
    "runs/exp01/config.json": '{"lr": 0.01, "seed": 42}\n',
    "runs/exp01/data/raw.tsv": "id\tvalue\n1\t0.5\n2\t0.7\n",
    "runs/exp01/logs/train.log": "started seed=42\nfinished epoch=3\n",
    "runs/exp01/metrics.json": '{"accuracy": 0.91}\n',
    "README.md": "# 示例项目\n\n实验已完成。\n",
}


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ta_cli_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        for rel, content in FILES.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        self.db_path = str(Path(self.tmp.name) / "cli.db")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", self.db_path, *argv])
        return code, out.getvalue(), err.getvalue()

    def scan(self) -> dict:
        code, out, err = self.run_cli("scan", str(self.root), "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_scan_and_projects(self):
        data = self.scan()
        self.assertEqual(data["files"], len(FILES))
        self.assertGreaterEqual(data["events_created"], 1)
        code, out, err = self.run_cli("projects", "--json")
        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["latest_snapshot_id"], data["snapshot_id"])
        # 按路径与按名称都能解析到项目
        code, out, _ = self.run_cli("leads", str(self.root))
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("leads", "proj")
        self.assertEqual(code, 0)

    def test_scan_exclude(self):
        (self.root / ".git" / "objects").mkdir(parents=True)
        (self.root / ".git" / "objects" / "aa").write_text("x", encoding="utf-8")
        data = self.scan()  # 默认包含 .git
        self.assertEqual(data["files"], len(FILES) + 1)
        code, out, err = self.run_cli("scan", str(self.root), "--exclude", ".git", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["files"], len(FILES))

    def test_project_excludes_config(self):
        (self.root / ".git" / "objects").mkdir(parents=True)
        (self.root / ".git" / "objects" / "aa").write_text("x", encoding="utf-8")
        (self.root / "node_modules" / "pkg").mkdir(parents=True)
        (self.root / "node_modules" / "pkg" / "i.js").write_text("y", encoding="utf-8")
        self.scan()
        code, out, err = self.run_cli("config", "proj")
        self.assertEqual(code, 0, err)
        self.assertIn("（无）", out)
        code, out, err = self.run_cli("config", "proj", "--excludes", ".git")
        self.assertEqual(code, 0, err)
        self.assertEqual(Database(self.db_path).get_project(1)["excludes"], ".git")
        # 不传 --exclude 时应用项目设置：跳过 .git，保留 node_modules
        code, out, err = self.run_cli("scan", str(self.root), "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["files"], len(FILES) + 1)
        # --exclude 覆盖项目设置（本次只跳 node_modules）
        code, out, err = self.run_cli("scan", str(self.root),
                                      "--exclude", "node_modules", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["files"], len(FILES) + 1)
        db = Database(self.db_path)
        latest = db.latest_snapshot(1)
        paths = {r["rel_path"].replace("\\", "/") for r in
                 db.query("SELECT rel_path FROM file_records WHERE snapshot_id=?",
                          (latest["id"],))}
        self.assertTrue(any(p.startswith(".git/") for p in paths))
        self.assertFalse(any(p.startswith("node_modules/") for p in paths))
        code, out, err = self.run_cli("config", "proj", "--clear")
        self.assertEqual(code, 0, err)
        self.assertEqual(Database(self.db_path).get_project(1)["excludes"], "")
        code, out, err = self.run_cli("config", "proj", "--excludes", ".git", "--clear")
        self.assertEqual(code, 2)
        self.assertIn("只能指定一个", err)

    def test_analyze_lists_and_exports(self):
        self.scan()
        code, out, err = self.run_cli("analyze", "proj", "--json")
        self.assertEqual(code, 0, err)
        summary = json.loads(out)
        for key in ("relations_edges", "leads", "dossiers", "tasks_total",
                    "phase_segments"):
            self.assertIn(key, summary)
        self.assertGreaterEqual(summary["leads"], 1)
        self.assertGreaterEqual(summary["dossiers"], 1)

        code, out, err = self.run_cli("leads", "proj", "--status", "open", "--json")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out))

        code, out, err = self.run_cli("dossiers", "proj", "--json")
        self.assertEqual(code, 0, err)
        views = json.loads(out)
        self.assertTrue(views)
        self.assertIn("completeness", views[0])

        code, out, err = self.run_cli("tasks", "proj", "--json")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out))

        md = Path(self.tmp.name) / "timeline.md"
        code, out, err = self.run_cli("timeline", "proj", "-o", str(md))
        self.assertEqual(code, 0, err)
        self.assertIn("项目演化时间线报告", md.read_text(encoding="utf-8"))

        rep = Path(self.tmp.name) / "report.md"
        code, out, err = self.run_cli("report", "proj", "-o", str(rep))
        self.assertEqual(code, 0, err)
        self.assertIn("项目考古报告", rep.read_text(encoding="utf-8"))

        tasks_md = Path(self.tmp.name) / "tasks.md"
        code, out, err = self.run_cli("tasks", "proj", "--export", str(tasks_md))
        self.assertEqual(code, 0, err)
        self.assertIn("调查任务清单", tasks_md.read_text(encoding="utf-8"))

        forensics = Path(self.tmp.name) / "forensics.md"
        code, out, err = self.run_cli("forensics", "proj", "-o", str(forensics))
        self.assertEqual(code, 0, err)
        text = forensics.read_text(encoding="utf-8")
        self.assertIn("# 项目取证报告", text)
        self.assertIn("## 二、成果档案", text)
        self.assertIn("## 四、调查任务", text)

    def test_repro_check_and_export(self):
        self.scan()
        report = Path(self.tmp.name) / "repro.md"
        code, out, err = self.run_cli(
            "repro", "proj", "runs/exp01/metrics.json",
            "--export", str(report), "--json")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertIn(result["level"], ("A", "B", "C", "D"))
        self.assertEqual(len(result["checks"]), 9)
        self.assertIsInstance(result["score"], int)
        text = report.read_text(encoding="utf-8")
        self.assertIn("复现就绪度报告", text)
        self.assertIn("评分规则（完全公开）", text)

    def test_diff_two_snapshots(self):
        first = self.scan()
        (self.root / "src" / "train.py").write_text("def train():\n    return 2\n",
                                                    encoding="utf-8")
        (self.root / "src" / "new_file.py").write_text("x = 1\n", encoding="utf-8")
        second = self.scan()
        self.assertGreater(second["snapshot_id"], first["snapshot_id"])
        code, out, err = self.run_cli("diff", "proj", "--json")
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertEqual(d["counts"]["added"], 1)
        self.assertEqual(d["counts"]["modified"], 1)

        diff_md = Path(self.tmp.name) / "diff.md"
        code, out, err = self.run_cli("diff", "proj", "--export", str(diff_md))
        self.assertEqual(code, 0, err)
        text = diff_md.read_text(encoding="utf-8")
        self.assertIn("# 快照对比报告", text)
        self.assertIn("new_file.py", text)

    def test_maintain_backup_integrity_vacuum(self):
        self.scan()
        backup = Path(self.tmp.name) / "backup" / "copy.db"
        code, out, err = self.run_cli("maintain", "--integrity", "--backup", str(backup),
                                      "--vacuum", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["integrity"], "ok")
        self.assertEqual(data["vacuum"], "done")
        self.assertTrue(Path(data["backup"]).exists())
        copy = Database(str(backup))
        self.assertEqual(len(copy.list_projects()), 1)
        self.assertEqual(len(copy.list_snapshots(1)), 1)
        code, out, err = self.run_cli("maintain")
        self.assertEqual(code, 2)
        self.assertIn("至少指定", err)

    def test_overview_and_rules(self):
        self.scan()
        self.run_cli("analyze", "proj")
        code, out, err = self.run_cli("overview", "--json")
        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0]["open_leads"], 1)
        self.assertGreaterEqual(rows[0]["open_tasks"], 1)
        self.assertGreaterEqual(rows[0]["artifacts"], 1)

        code, out, err = self.run_cli("rules", "--json")
        self.assertEqual(code, 0, err)
        rules = json.loads(out)
        self.assertEqual(len(rules), 8)
        self.assertTrue(all(r["enabled"] for r in rules))
        code, out, err = self.run_cli("rules", "--disable", "test_without_result")
        self.assertEqual(code, 0, err)
        self.assertFalse(Database(self.db_path).get_rule_enabled("test_without_result"))
        code, out, err = self.run_cli("rules", "--enable", "test_without_result")
        self.assertEqual(code, 0, err)
        self.assertTrue(Database(self.db_path).get_rule_enabled("test_without_result"))
        code, out, err = self.run_cli("rules", "--enable", "bogus_rule")
        self.assertEqual(code, 2)
        self.assertIn("未知规则", err)

    def test_detail_views(self):
        self.scan()
        self.run_cli("analyze", "proj")
        db = Database(self.db_path)
        lid = db.list_leads(1)[0]["id"]
        code, out, err = self.run_cli("leads", "proj", "--detail", str(lid))
        self.assertEqual(code, 0, err)
        self.assertIn("触发原因", out)
        self.assertIn("误报可能性", out)
        tid = db.list_tasks(1)[0]["id"]
        code, out, err = self.run_cli("tasks", "proj", "--detail", str(tid))
        self.assertEqual(code, 0, err)
        self.assertIn("为什么调查", out)
        self.assertIn("完成条件", out)
        aid = db.list_artifacts(1)[0]["id"]
        code, out, err = self.run_cli("dossiers", "proj", "--detail", str(aid))
        self.assertEqual(code, 0, err)
        self.assertIn("已验证事实", out)
        self.assertIn("完整性检查", out)
        # 复现：运行 -> 历史 -> 载入
        code, out, err = self.run_cli("repro", "proj", "runs/exp01/metrics.json")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("repro", "proj", "--history")
        self.assertEqual(code, 0, err)
        self.assertIn("复现检查历史", out)
        rid = db.list_repro_checks(1)[0]["id"]
        code, out, err = self.run_cli("repro", "proj", "--load", str(rid))
        self.assertEqual(code, 0, err)
        self.assertIn("评分规则", out)
        code, out, err = self.run_cli("repro", "proj")
        self.assertEqual(code, 2)
        self.assertIn("请提供目标文件", err)
        code, out, err = self.run_cli("leads", "proj", "--detail", "99999")
        self.assertEqual(code, 2)

    def test_status_actions(self):
        self.scan()
        self.run_cli("analyze", "proj")
        db = Database(self.db_path)
        leads = db.list_leads(1)
        self.assertTrue(leads)
        lid = leads[0]["id"]
        code, out, err = self.run_cli("leads", "proj", "--set-status",
                                      str(lid), "acknowledged")
        self.assertEqual(code, 0, err)
        self.assertEqual(db.query("SELECT status FROM leads WHERE id=?",
                                  (lid,))[0][0], "acknowledged")
        code, out, err = self.run_cli("leads", "proj", "--set-status", str(lid), "bogus")
        self.assertEqual(code, 2)
        self.assertIn("未知状态", err)

        tasks = db.list_tasks(1)
        self.assertTrue(tasks)
        tid = tasks[0]["id"]
        code, out, err = self.run_cli("tasks", "proj", "--complete", str(tid))
        self.assertEqual(code, 0, err)
        row = db.query("SELECT status, completed_at FROM tasks WHERE id=?", (tid,))[0]
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["completed_at"])
        code, out, err = self.run_cli("tasks", "proj", "--reopen", str(tid))
        self.assertEqual(code, 0, err)
        row = db.query("SELECT status, completed_at FROM tasks WHERE id=?", (tid,))[0]
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["completed_at"])

        code, out, err = self.run_cli("tasks", "proj", "--complete", str(tid),
                                      "--reopen", str(tid))
        self.assertEqual(code, 2)
        self.assertIn("只能指定一个", err)
        code, out, err = self.run_cli("tasks", "proj", "--complete", "99999")
        self.assertEqual(code, 2)
        self.assertIn("不存在", err)

    def test_module_routing(self):
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONIOENCODING="utf-8")

        def run(*flags):
            return subprocess.run(
                [sys.executable, "-m", "time_arch", *flags], cwd=repo,
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=60, env=env)

        r = run("--version")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Time Archaeologist", r.stdout)
        r = run("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("scan", r.stdout)

    def test_error_codes(self):
        code, out, err = self.run_cli("scan", str(Path(self.tmp.name) / "missing"))
        self.assertEqual(code, 2)
        self.assertIn("目录不存在", err)
        code, out, err = self.run_cli("leads", "不存在的项目")
        self.assertEqual(code, 2)
        self.assertIn("未登记", err)
        self.scan()
        code, out, err = self.run_cli("repro", "proj", "runs/exp01/metrics.json",
                                      "--snapshot", "9999")
        self.assertEqual(code, 2)
        self.assertIn("快照", err)


if __name__ == "__main__":
    unittest.main()
