"""无头命令行测试：扫描/分析/报告/线索/档案/复现/任务/时间线/对比。

全部走真实临时目录与真实 SQLite 链路；CLI 只读扫描，不执行项目内任何程序。
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from time_arch import cli

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
