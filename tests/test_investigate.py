"""调查规则引擎测试（2.0 第四步）。

用真实临时目录 + 真实扫描链路验证：每条规则的正例、反例、边界情况，
规则启停配置、幂等重跑、人工状态保留、线索字段完整性。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from time_arch.db import Database
from time_arch.investigate import (LEADS_RULES, build_leads, lead_view,
                                   run_lead_analysis)
from time_arch.scanner import run_scan


def make_project(files: dict[str, str | bytes]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ta_inv_"))
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return root


METRICS = '{"accuracy": 0.9, "samples": 100}'


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ta_invdb_")
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")

    def scan(self, files: dict) -> int:
        root = make_project(files)
        pid, _ = self.db.add_project(str(root), "调查项目")
        result = run_scan(self.db, pid)
        return pid, result.snapshot_id

    def leads(self, pid: int, snap: int, rule: str) -> list[dict]:
        return [l for l in build_leads(self.db, pid, snap, enabled={rule})
                if l["rule_id"] == rule]


class TestRuleConfig(_Base):
    def test_default_enabled_and_toggle(self):
        pid, snap = self.scan({"src/train.py": "print(1)\n",
                               "tests/test_train.py": "import src.train\n"})
        # 默认启用：触发规则2
        r1 = run_lead_analysis(self.db, pid, snap)
        self.assertEqual(r1["by_rule"].get("test_without_result"), 1)
        # 关闭后不再触发
        self.db.set_rule_enabled("test_without_result", False, "2026-01-01T00:00:00+00:00")
        self.assertFalse(self.db.get_rule_enabled("test_without_result"))
        r2 = run_lead_analysis(self.db, pid, snap)
        self.assertNotIn("test_without_result", r2["by_rule"])
        # 显式 enabled 越过配置（引擎层可控）
        leads = build_leads(self.db, pid, snap, enabled={"test_without_result"})
        self.assertEqual(len(leads), 1)
        # 重新开启
        self.db.set_rule_enabled("test_without_result", True, "2026-01-02T00:00:00+00:00")
        self.assertTrue(self.db.get_rule_enabled("test_without_result"))

    def test_unknown_rule_defaults_enabled(self):
        self.assertTrue(self.db.get_rule_enabled("not_registered_yet"))


class TestRule1ResultWithoutInput(_Base):
    def test_positive_and_negative(self):
        pid, snap = self.scan({"runs/exp01/metrics.json": METRICS})
        hits = self.leads(pid, snap, "result_without_input")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["target_path"], "runs/exp01")
        self.assertIn("runs/exp01/metrics.json", hits[0]["evidence"])
        self.assertTrue(hits[0]["possible_explanations"])
        # 补上参数文件后不再触发
        pid2, snap2 = self.scan({"runs/exp01/metrics.json": METRICS,
                                 "runs/exp01/params.yaml": "seed: 1\n"})
        self.assertEqual(self.leads(pid2, snap2, "result_without_input"), [])

    def test_input_data_also_satisfies(self):
        pid, snap = self.scan({"runs/exp01/metrics.json": METRICS,
                               "runs/exp01/data/train.csv": "a,b\n1,2\n"})
        self.assertEqual(self.leads(pid, snap, "result_without_input"), [])


class TestRule2TestWithoutResult(_Base):
    def test_positive(self):
        pid, snap = self.scan({"src/train.py": "print(1)\n",
                               "tests/test_train.py": "import src.train\n"})
        hits = self.leads(pid, snap, "test_without_result")
        self.assertEqual(len(hits), 1)
        self.assertIn("tests/test_train.py", hits[0]["evidence"])

    def test_negative_with_junit_report(self):
        pid, snap = self.scan({"src/train.py": "print(1)\n",
                               "tests/test_train.py": "import src.train\n",
                               "reports/junit-results.xml": "<testsuite tests='2'/>\n"})
        self.assertEqual(self.leads(pid, snap, "test_without_result"), [])

    def test_no_tests_no_lead(self):
        pid, snap = self.scan({"src/train.py": "print(1)\n"})
        self.assertEqual(self.leads(pid, snap, "test_without_result"), [])


class TestRule3ErrorButComplete(_Base):
    def test_positive(self):
        pid, snap = self.scan({
            "runs/exp01/metrics.json": METRICS,
            "runs/exp01/train.log": "start\nTraceback (most recent call last):\n  File \"x\"\nValueError: bad\n"})
        hits = self.leads(pid, snap, "error_but_complete_result")
        self.assertEqual(len(hits), 1)
        self.assertTrue(any("Traceback" in e for e in hits[0]["evidence"]))
        self.assertIn("runs/exp01/metrics.json", hits[0]["evidence"])

    def test_negative_clean_log(self):
        pid, snap = self.scan({"runs/exp01/metrics.json": METRICS,
                               "runs/exp01/train.log": "start\nfinish: completed\n"})
        self.assertEqual(self.leads(pid, snap, "error_but_complete_result"), [])

    def test_negative_error_without_output(self):
        pid, snap = self.scan({
            "runs/exp01/train.log": "Traceback (most recent call last):\nValueError: bad\n"})
        self.assertEqual(self.leads(pid, snap, "error_but_complete_result"), [])


class TestRule4Duplicates(_Base):
    def test_identical_content_across_run_dirs(self):
        pid, snap = self.scan({"runs/exp01/metrics.json": METRICS,
                               "runs/exp02/metrics.json": METRICS})
        hits = self.leads(pid, snap, "duplicate_results")
        self.assertEqual(len(hits), 1)
        self.assertIn("runs/exp01/metrics.json", hits[0]["evidence"])
        self.assertIn("runs/exp02/metrics.json", hits[0]["evidence"])
        self.assertIn("SHA-256", hits[0]["basis"])

    def test_similar_name_without_relation_edge(self):
        # 文件名避开 metrics/result/summary 等输出命名特征，否则会被当作结果文件跳过
        pid, snap = self.scan({"results/a/table.csv": "x\n1\n",
                               "results/b/table.csv": "y\n2\n"})
        hits = self.leads(pid, snap, "duplicate_results")
        self.assertEqual(len(hits), 1)
        evidence_text = " ".join(hits[0]["evidence"])
        self.assertIn("results/a/table.csv", evidence_text)
        self.assertIn("results/b/table.csv", evidence_text)

    def test_negative_related_pair_no_lead(self):
        # 同词干 doc 对（report-v1/v2）会由图谱产生 evolves_to 边 → 关系已知，不触发
        root = make_project({"reports/report-v1.md": "# v1\nacc 0.8\n",
                             "reports/report-v2.md": "# v2\nacc 0.9\n"})
        db = Database(Path(self.tmp.name) / "t2.db")
        pid, _ = db.add_project(str(root), "图")
        from time_arch.graph import run_relation_analysis
        result = run_scan(db, pid)
        run_relation_analysis(db, pid, result.snapshot_id)
        hits = [l for l in build_leads(db, pid, result.snapshot_id,
                                       enabled={"duplicate_results"})]
        self.assertEqual(hits, [])


class TestRule5DocClaims(_Base):
    def test_positive_claim_without_evidence(self):
        pid, snap = self.scan({"README.md": "# 项目\n本功能已实现并通过测试。\n",
                               "src/main.py": "print(1)\n"})
        hits = self.leads(pid, snap, "doc_claims_without_evidence")
        self.assertEqual(len(hits), 1)
        self.assertTrue(any("README.md:2" in e for e in hits[0]["evidence"]))

    def test_negative_with_test_report_and_artifact(self):
        pid, snap = self.scan({"README.md": "# 项目\n功能已实现并通过测试。\n",
                               "src/main.py": "print(1)\n",
                               "reports/junit-results.xml": "<testsuite tests='2'/>\n",
                               "dist/app.o": b"\x01\x02\x03" * 100})
        self.assertEqual(self.leads(pid, snap, "doc_claims_without_evidence"), [])

    def test_negative_plain_doc(self):
        pid, snap = self.scan({"README.md": "# 项目\n这是一个数据整理工具。\n"})
        self.assertEqual(self.leads(pid, snap, "doc_claims_without_evidence"), [])


class TestRule6DependencyConflict(_Base):
    def test_positive_conflict(self):
        pid, snap = self.scan({"requirements.txt": "numpy==1.21.0\npandas>=1.0\n",
                               "env/requirements-prod.txt": "numpy==1.24.0\n"})
        hits = self.leads(pid, snap, "dependency_conflict")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["target_path"], "numpy")
        self.assertTrue(any("requirements.txt:1" in e for e in hits[0]["evidence"]))

    def test_negative_consistent_pins(self):
        pid, snap = self.scan({"requirements.txt": "numpy==1.21.0\n",
                               "env/requirements-prod.txt": "numpy==1.21.0\n"})
        self.assertEqual(self.leads(pid, snap, "dependency_conflict"), [])

    def test_name_normalization(self):
        # scikit-learn 与 scikit_learn 是同一个包
        pid, snap = self.scan({"requirements.txt": "scikit-learn==1.0\n",
                               "env/pip-freeze.txt": "scikit_learn==1.2\n"})
        hits = self.leads(pid, snap, "dependency_conflict")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["target_path"], "scikit-learn")


class TestRule7ResultChanged(_Base):
    def _project(self):
        root = make_project({"runs/exp01/metrics.json": METRICS})
        pid, _ = self.db.add_project(str(root), "变化项目")
        return root, pid

    def test_positive_change_without_log(self):
        root, pid = self._project()
        s1 = run_scan(self.db, pid).snapshot_id
        p = root / "runs" / "exp01" / "metrics.json"
        p.write_text('{"accuracy": 0.95, "samples": 100}', encoding="utf-8")
        s2 = run_scan(self.db, pid).snapshot_id
        hits = self.leads(pid, s2, "result_changed_without_record")
        self.assertEqual(len(hits), 1)
        self.assertTrue(any("SHA-256" in e for e in hits[0]["evidence"]))

    def test_negative_log_present(self):
        root, pid = self._project()
        run_scan(self.db, pid)
        (root / "runs" / "exp01" / "metrics.json").write_text(
            '{"accuracy": 0.95, "samples": 100}', encoding="utf-8")
        (root / "runs" / "exp01" / "train.log").write_text("rerun ok\n", encoding="utf-8")
        s2 = run_scan(self.db, pid).snapshot_id
        self.assertEqual(self.leads(pid, s2, "result_changed_without_record"), [])

    def test_negative_unchanged(self):
        root, pid = self._project()
        s1 = run_scan(self.db, pid).snapshot_id
        s2 = run_scan(self.db, pid).snapshot_id
        self.assertEqual(self.leads(pid, s2, "result_changed_without_record"), [])

    def test_first_snapshot_no_lead(self):
        root, pid = self._project()
        s1 = run_scan(self.db, pid).snapshot_id
        self.assertEqual(self.leads(pid, s1, "result_changed_without_record"), [])


class TestRule8DocConfigOnly(_Base):
    def _project(self):
        files = {"docs/plan.md": "# 计划\nv1\n", "docs/notes.md": "v1\n",
                 "config/settings.ini": "[a]\nb=1\n", "src/app.py": "print(1)\n"}
        root = make_project(files)
        pid, _ = self.db.add_project(str(root), "阶段项目")
        return root, pid

    def test_positive_doc_only_changes(self):
        root, pid = self._project()
        run_scan(self.db, pid)
        (root / "docs" / "plan.md").write_text("# 计划\nv2\n", encoding="utf-8")
        (root / "docs" / "notes.md").write_text("v2\n", encoding="utf-8")
        (root / "config" / "settings.ini").write_text("[a]\nb=2\n", encoding="utf-8")
        s2 = run_scan(self.db, pid).snapshot_id
        hits = self.leads(pid, s2, "doc_config_only_changes")
        self.assertEqual(len(hits), 1)
        # 非判断性措辞：仅陈述观察
        self.assertIn("不构成", hits[0]["false_positive"])

    def test_negative_source_change(self):
        root, pid = self._project()
        run_scan(self.db, pid)
        (root / "docs" / "plan.md").write_text("# 计划\nv2\n", encoding="utf-8")
        (root / "src" / "app.py").write_text("print(2)\n", encoding="utf-8")
        s2 = run_scan(self.db, pid).snapshot_id
        self.assertEqual(self.leads(pid, s2, "doc_config_only_changes"), [])

    def test_negative_too_few_changes(self):
        root, pid = self._project()
        run_scan(self.db, pid)
        (root / "docs" / "plan.md").write_text("# 计划\nv2\n", encoding="utf-8")
        s2 = run_scan(self.db, pid).snapshot_id
        self.assertEqual(self.leads(pid, s2, "doc_config_only_changes"), [])

    def test_first_snapshot_no_lead(self):
        root, pid = self._project()
        s1 = run_scan(self.db, pid).snapshot_id
        self.assertEqual(self.leads(pid, s1, "doc_config_only_changes"), [])


class TestEnginePersistence(_Base):
    def setUp(self):
        super().setUp()
        self.pid, self.snap = self.scan({
            "runs/exp01/metrics.json": METRICS,
            "src/train.py": "print(1)\n",
            "tests/test_train.py": "import src.train\n",
            "README.md": "# 项目\n功能已实现并通过测试。\n",
        })

    def test_all_leads_have_required_fields(self):
        leads = build_leads(self.db, self.pid, self.snap)
        self.assertGreaterEqual(len(leads), 2)
        for l in leads:
            self.assertTrue(l["title"])
            self.assertTrue(l["basis"])
            self.assertTrue(l["evidence"])
            self.assertTrue(l["possible_explanations"])
            self.assertTrue(l["next_steps"])
            self.assertTrue(l["false_positive"])
            self.assertIn("rule_version", l)
        self.assertEqual({l["rule_version"] for l in leads}, {1})

    def test_rules_registry_shape(self):
        self.assertGreaterEqual(len(LEADS_RULES), 8)
        seen = set()
        for rid, ver, fn, desc in LEADS_RULES:
            self.assertNotIn(rid, seen)
            seen.add(rid)
            self.assertGreaterEqual(ver, 1)
            self.assertTrue(callable(fn))
            self.assertTrue(desc)

    def test_persist_idempotent_and_status_preserved(self):
        r1 = run_lead_analysis(self.db, self.pid, self.snap)
        n1 = r1["leads"]
        self.assertGreater(n1, 0)
        rows = self.db.list_leads(self.pid, self.snap)
        self.assertEqual(len(rows), n1)
        first_id = rows[0]["id"]
        created = rows[0]["created_at"]
        self.db.set_lead_status(first_id, "acknowledged")
        r2 = run_lead_analysis(self.db, self.pid, self.snap)
        self.assertEqual(r2["leads"], n1)
        row = self.db.query("SELECT * FROM leads WHERE id=?", (first_id,))[0]
        self.assertEqual(row["status"], "acknowledged")   # 人工状态不被重跑覆盖
        self.assertEqual(row["created_at"], created)      # 首次发现时间不变
        self.assertEqual(len(self.db.list_leads(self.pid, self.snap)), n1)

    def test_status_filter_and_invalid_status(self):
        run_lead_analysis(self.db, self.pid, self.snap)
        lid = self.db.list_leads(self.pid, self.snap)[0]["id"]
        self.db.set_lead_status(lid, "dismissed")
        self.assertEqual(len(self.db.list_leads(self.pid, self.snap, status="dismissed")), 1)
        self.assertEqual(len(self.db.list_leads(self.pid, self.snap, status="open")),
                         len(self.db.list_leads(self.pid, self.snap)) - 1)
        with self.assertRaises(ValueError):
            self.db.set_lead_status(lid, "done")

    def test_lead_view_parses_json(self):
        run_lead_analysis(self.db, self.pid, self.snap)
        lid = self.db.list_leads(self.pid, self.snap)[0]["id"]
        v = lead_view(self.db, lid)
        self.assertIsInstance(v["evidence_list"], list)
        self.assertTrue(v["evidence_list"])
        self.assertIn("possible_explanations", v)
        self.assertIn("false_positive", v)
        self.assertEqual(v["rule_label"], "结果无输入/参数")
        self.assertIsNone(lead_view(self.db, 999999))


if __name__ == "__main__":
    unittest.main()
