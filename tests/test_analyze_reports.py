"""成果考古与报告导出测试。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from time_arch.analyze import detect_findings
from time_arch.db import Database
from time_arch.reports import build_json, build_markdown, export_report
from time_arch.scanner import run_scan


class TestAnalyze(unittest.TestCase):
    def test_executable_high(self):
        f = detect_findings("dist/app.exe", 1024, "abc", "build")
        self.assertTrue(any(x["kind"] == "executable" and x["confidence"] == "high" for x in f))

    def test_release_package(self):
        f = detect_findings("release/mytool-1.2.0.zip", 1024, "abc", "data")
        kinds = {(x["kind"], x["confidence"]) for x in f}
        self.assertIn(("release_package", "high"), kinds)

    def test_test_report(self):
        f = detect_findings("reports/junit-results.xml", 512, "abc", "other")
        self.assertTrue(any(x["kind"] == "test_report" for x in f))
        f2 = detect_findings("coverage.xml", 512, "abc", "config")
        self.assertTrue(any(x["kind"] == "test_report" for x in f2))

    def test_experiment_output(self):
        f = detect_findings("runs/exp1/metrics.json", 256, "abc", "data")
        self.assertTrue(any(x["kind"] == "experiment_output" and x["confidence"] == "high" for x in f))
        f2 = detect_findings("notebooks/accuracy.json", 100, "abc", "data")
        self.assertTrue(any(x["kind"] == "experiment_output" for x in f2))

    def test_plain_source_no_finding(self):
        self.assertEqual(detect_findings("src/main.py", 100, "abc", "source"), [])

    def test_every_finding_has_basis(self):
        for path in ["a.exe", "dist/x.tgz", "results/r.csv", "app.log", "metrics.json"]:
            for f in detect_findings(path, 10, "h", "other"):
                self.assertTrue(f["basis"])
                self.assertIn("note", f["evidence"])


class TestReports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "p"
        self.root.mkdir()
        (self.root / "main.py").write_text("x=1\n", encoding="utf-8")
        (self.root / "dist").mkdir()
        (self.root / "dist" / "tool-1.0.zip").write_bytes(b"zipdata")
        self.db = Database(Path(self.tmp.name) / "r.db")
        self.pid, _ = self.db.add_project(str(self.root), name="rp")
        self.res = run_scan(self.db, self.pid)

    def tearDown(self):
        self.tmp.cleanup()

    def test_markdown_report_content(self):
        md = build_markdown(self.db, self.pid)
        self.assertIn("项目考古报告", md)
        self.assertIn("dist/tool-1.0.zip", md)
        self.assertIn("发布包", md)
        self.assertIn("不代表真实工作时间", md)   # 免责声明
        self.assertIn("启发式", md)               # 区分推测
        self.assertIn("快照历史", md)

    def test_json_report_structure(self):
        data = json.loads(build_json(self.db, self.pid))
        self.assertIn("findings", data)
        self.assertGreater(len(data["findings"]), 0)
        self.assertIn("evidence", data["findings"][0])
        self.assertEqual(data["findings"][0]["evidence"]["rel_path"], "dist/tool-1.0.zip")
        self.assertIn("disclaimer", data["meta"])

    def test_export_writes_file(self):
        out = Path(self.tmp.name) / "report.md"
        export_report(self.db, self.pid, "markdown", str(out))
        self.assertTrue(out.exists())
        self.assertIn("成果考古", out.read_text(encoding="utf-8"))

    def test_finding_manual_confirm_flow(self):
        rows = self.db.list_findings(self.pid)
        self.assertEqual(rows[0]["status"], "auto")
        self.db.set_finding_status(rows[0]["id"], "confirmed")
        md = build_markdown(self.db, self.pid)
        self.assertIn("人工确认", md)


if __name__ == "__main__":
    unittest.main()
