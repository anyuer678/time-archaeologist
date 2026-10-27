"""演化阶段与里程碑测试（2.0 第五步）。

真实临时目录 + 真实扫描链路：阶段建议正例/反例/边界、里程碑首次出现与幂等、
人工确认分离、Markdown 导出完整性。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from time_arch.db import Database
from time_arch.phases import (PHASE_DEFS, export_timeline_markdown,
                              persist_milestone_suggestions, suggest_milestones,
                              suggest_phases)
from time_arch.scanner import run_scan

METRICS = '{"accuracy": 0.9, "samples": 100}'


def make_project(files: dict[str, str | bytes]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ta_phase_"))
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return root


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ta_phasedb_")
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")

    def register(self, files: dict) -> tuple[int, Path]:
        root = make_project(files)
        pid, _ = self.db.add_project(str(root), "阶段项目")
        r = run_scan(self.db, pid)
        self.pid = pid
        self.root = root
        self.snaps = [r.snapshot_id]
        return pid, root

    def rescan(self, files: dict, remove: list[str] | None = None) -> int:
        for rel in remove or []:
            (self.root / rel).unlink()
        for rel, content in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content if isinstance(content, bytes)
                          else content.encode("utf-8"))
        r = run_scan(self.db, self.pid)
        self.snaps.append(r.snapshot_id)
        return r.snapshot_id

    def phase(self, key: str):
        for ph in suggest_phases(self.db, self.pid):
            if ph["key"] == key:
                return ph
        raise AssertionError(f"阶段 {key} 不在 PHASE_DEFS")


class TestPhaseSuggestions(_Base):
    def test_empty_project_no_snapshots(self):
        root = make_project({})
        pid, _ = self.db.add_project(str(root), "空项目")
        self.assertEqual(suggest_phases(self.db, pid), [])
        self.assertEqual(suggest_milestones(self.db, pid), [])

    def test_docs_only_gives_requirements_design_not_implementation(self):
        self.register({"docs/需求说明.md": "需求", "docs/设计方案.md": "设计"})
        req = self.phase("requirements")
        self.assertEqual(len(req["segments"]), 1)
        self.assertEqual(req["segments"][0]["confidence"], "medium")
        self.assertTrue(any("需求说明" in s for s in req["segments"][0]["evidence_samples"]))
        design = self.phase("design")
        self.assertEqual(len(design["segments"]), 1)
        self.assertTrue(any("设计方案" in s for s in design["segments"][0]["evidence_samples"]))
        self.assertEqual(self.phase("implementation")["segments"], [])
        self.assertEqual(self.phase("release")["segments"], [])

    def test_segment_boundaries_follow_snapshots(self):
        self.register({"docs/需求说明.md": "需求"})
        self.rescan({"src/train.py": "print(1)\n"})
        self.rescan({}, remove=["docs/需求说明.md"])  # 文档删除 → 需求阶段证据消失
        s1, s2, s3 = self.snaps
        req = self.phase("requirements")
        self.assertEqual([(g["start_snapshot"], g["end_snapshot"])
                          for g in req["segments"]], [(s1, s2)])
        imp = self.phase("implementation")
        self.assertEqual([(g["start_snapshot"], g["end_snapshot"])
                          for g in imp["segments"]], [(s2, s3)])

    def test_experiment_direct_evidence_high_confidence(self):
        self.register({"runs/exp01/params.yaml": "seed: 1\n",
                       "runs/exp01/metrics.json": METRICS})
        exp = self.phase("experiment")
        self.assertEqual(len(exp["segments"]), 1)
        self.assertEqual(exp["segments"][0]["confidence"], "high")

    def test_experiment_without_params_medium(self):
        self.register({"runs/exp01/metrics.json": METRICS})
        exp = self.phase("experiment")
        self.assertEqual(exp["segments"][0]["confidence"], "medium")

    def test_release_dir_high_archive_alone_medium(self):
        self.register({"dist/app-1.0.zip": b"PK\x03\x04", "pkg/bundle.zip": b"PK\x03\x04"})
        rel = self.phase("release")
        self.assertEqual(len(rel["segments"]), 1)
        self.assertEqual(rel["segments"][0]["confidence"], "high")
        self.assertTrue(any("dist/" in s for s in rel["segments"][0]["evidence_samples"]))

    def test_single_snapshot_segment_start_equals_end(self):
        self.register({"src/main.py": "print(1)\n"})
        imp = self.phase("implementation")
        self.assertEqual(imp["segments"][0]["start_snapshot"],
                         imp["segments"][0]["end_snapshot"])
        self.assertEqual(imp["segments"][0]["confidence"], "medium")

    def test_git_internal_files_ignored(self):
        self.register({".git/设计说明.md": "内部文件", "docs/设计方案.md": "设计"})
        design = self.phase("design")
        samples = [s for g in design["segments"] for s in g["evidence_samples"]]
        self.assertTrue(any("docs/设计方案" in s for s in samples))
        self.assertFalse(any(".git" in s for s in samples))
        self.assertEqual(self.phase("requirements")["segments"], [])

    def test_phase_model_is_explicit_and_optional(self):
        keys = [k for k, *_ in PHASE_DEFS]
        self.assertEqual(keys, ["requirements", "design", "implementation",
                                "testing", "experiment", "output", "release"])
        self.assertIn("不代表发布成功", dict((k, d) for k, _, d in PHASE_DEFS)["release"])


class TestMilestones(_Base):
    def test_metrics_only_run_not_counted_as_complete(self):
        # 只有 metrics.json（json→config 怪癖）不算「含参数与输出的完整实验运行」
        self.register({"runs/exp01/metrics.json": METRICS})
        cands = suggest_milestones(self.db, self.pid)
        self.assertNotIn("首个含参数与输出的完整实验运行", {c["title"] for c in cands})

    def test_first_appearance_and_persist_idempotent(self):
        self.register({"src/a.py": "x=1\n"})
        self.assertEqual(suggest_milestones(self.db, self.pid), [])
        self.rescan({"tests/test_a.py": "import a\n",
                     "reports/junit-results.xml": "<testsuite/>",
                     "runs/exp01/params.yaml": "seed: 1\n",
                     "runs/exp01/metrics.json": METRICS,
                     "build/lib/a.pyc": b"\x00", "dist/app.zip": b"PK"})
        cands = suggest_milestones(self.db, self.pid)
        titles = {c["title"] for c in cands}
        self.assertEqual(len(titles), 4)
        for c in cands:
            self.assertEqual(c["snapshot_id"], self.snaps[-1])
            self.assertTrue(c["evidence"])
        created = persist_milestone_suggestions(self.db, self.pid)
        self.assertEqual(created, 4)
        self.assertEqual(persist_milestone_suggestions(self.db, self.pid), 0)
        rows = self.db.list_milestones(self.pid)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["status"] == "suggested" and r["origin"] == "machine"
                            for r in rows))

    def test_confirm_survives_persist_rerun(self):
        self.register({"reports/junit-results.xml": "<testsuite/>"})
        persist_milestone_suggestions(self.db, self.pid)
        row = self.db.list_milestones(self.pid)[0]
        self.db.confirm_milestone(row["id"], "2026-01-01T00:00:00+00:00")
        persist_milestone_suggestions(self.db, self.pid)
        rows = self.db.list_milestones(self.pid, status="confirmed")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["confirmed_at"], "2026-01-01T00:00:00+00:00")

    def test_user_milestone_null_snapshot_idempotent(self):
        self.register({"src/a.py": "x=1\n"})
        now = "2026-01-01T00:00:00+00:00"
        self.assertTrue(self.db.upsert_milestone(self.pid, None, "人工里程碑", "备注",
                                                 "[]", now, origin="user"))
        self.assertFalse(self.db.upsert_milestone(self.pid, None, "人工里程碑", "备注",
                                                  "[]", now, origin="user"))
        rows = self.db.list_milestones(self.pid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["origin"], "user")
        self.db.remove_milestone(rows[0]["id"])
        self.assertEqual(self.db.list_milestones(self.pid), [])


class TestPhaseStore(_Base):
    def test_add_list_remove_and_upsert(self):
        now = "2026-01-01T00:00:00+00:00"
        pid, _ = self.register({"src/a.py": "x=1\n"})
        s1 = self.snaps[0]
        ph_id = self.db.add_phase(pid, "implementation", "实现", s1, s1, "", "confirmed", now)
        self.db.add_phase(pid, "implementation", "实现", s1, s1, "", "confirmed", now)
        rows = self.db.list_phases(pid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "confirmed")
        self.db.add_phase(pid, "", "自定义阶段", s1, None, "人工补充", "user", now)
        self.assertEqual(len(self.db.list_phases(pid)), 2)
        self.db.remove_phase(ph_id)
        self.assertEqual([r["label"] for r in self.db.list_phases(pid)], ["自定义阶段"])


class TestExportMarkdown(_Base):
    def test_export_contains_all_sections_and_is_repeatable(self):
        self.register({"docs/需求说明.md": "需求"})
        self.rescan({"src/train.py": "print(1)\n",
                     "tests/test_train.py": "import train\n",
                     "reports/junit-results.xml": "<testsuite/>",
                     "runs/exp01/params.yaml": "seed: 1\n",
                     "runs/exp01/metrics.json": METRICS})
        persist_milestone_suggestions(self.db, self.pid)
        row = [r for r in self.db.list_milestones(self.pid)
               if "测试结果报告" in r["title"]][0]
        self.db.confirm_milestone(row["id"], "2026-01-01T00:00:00+00:00")
        out = Path(self.tmp.name) / "timeline.md"
        r1 = export_timeline_markdown(self.db, self.pid, str(out))
        self.assertEqual(r1["snapshots"], 2)
        text = out.read_text(encoding="utf-8")
        for needle in ("# 项目演化时间线报告", "无法完整还原", "## 快照概览",
                       "## 机器阶段建议", "置信度", "## 人工确认 / 自定义阶段",
                       "## 里程碑", "[机器建议·已确认]", "## 阶段间变化",
                       "## 遗留问题"):
            self.assertIn(needle, text)
        out2 = Path(self.tmp.name) / "timeline2.md"
        export_timeline_markdown(self.db, self.pid, str(out2))
        t1 = [ln for ln in text.splitlines() if not ln.startswith("- 生成时间")]
        t2 = [ln for ln in out2.read_text(encoding="utf-8").splitlines()
              if not ln.startswith("- 生成时间")]
        self.assertEqual(t1, t2)

    def test_export_empty_project(self):
        self.register({})
        out = Path(self.tmp.name) / "empty.md"
        export_timeline_markdown(self.db, self.pid, str(out))
        text = out.read_text(encoding="utf-8")
        self.assertIn("（尚无里程碑。）", text)
        self.assertIn("证据不足，未生成任何阶段建议", text)

    def test_export_unknown_project_raises(self):
        with self.assertRaises(ValueError):
            export_timeline_markdown(self.db, 99999, str(Path(self.tmp.name) / "x.md"))


if __name__ == "__main__":
    unittest.main()
