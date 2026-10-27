"""调查任务生成测试（2.0 第六步）。

用真实临时目录 + 真实扫描 + 真实线索链路验证：任务生成正例/反例、五要素完整性、
幂等与人工状态保留、已忽略线索跳过、跨快照更新不重复、用户任务、
通用回退生成器、验证命令只读性守卫、Markdown 导出、v5→v6 增量迁移。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from time_arch.db import SCHEMA_VERSION, Database
from time_arch.investigate import run_lead_analysis
from time_arch.scanner import run_scan
from time_arch.tasks import (TASK_GENERATORS, export_tasks_markdown,
                             generate_tasks, get_generator, task_view)


def make_project(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ta_task_"))
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
    return root


METRICS = '{"accuracy": 0.9, "samples": 100}'


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ta_taskdb_")
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")

    def scan(self, files: dict) -> tuple[int, int]:
        root = make_project(files)
        pid, _ = self.db.add_project(str(root), "任务项目")
        result = run_scan(self.db, pid)
        return pid, result.snapshot_id

    def seed_lead(self, files: dict | None = None) -> tuple[int, int]:
        """扫描 + 线索分析，返回 (project_id, snapshot_id)。"""
        pid, snap = self.scan(files or {"runs/exp01/metrics.json": METRICS})
        run_lead_analysis(self.db, pid, snap)
        return pid, snap


class TestGeneration(_Base):
    def test_generate_from_real_lead_with_five_fields(self):
        pid, snap = self.seed_lead()
        r = generate_tasks(self.db, pid)
        self.assertEqual(r["created"], 1)
        self.assertEqual(r["total"], 1)
        rows = self.db.list_tasks(pid)
        t = task_view(self.db, rows[0]["id"])
        # 五要素全部非空（验证命令可为空，但必须有说明）
        self.assertTrue(t["title"])
        self.assertTrue(t["why"])
        self.assertTrue(t["evidence_list"])
        self.assertTrue(t["manual_steps_list"])
        self.assertTrue(t["done_criteria"])
        self.assertTrue(t["verify_command"] or t["verify_desc"])
        self.assertEqual(t["status"], "open")
        self.assertEqual(t["origin"], "machine")
        self.assertIsNotNone(t["lead_id"])
        self.assertEqual(t["task_key"], "result_without_input")
        self.assertEqual(t["target_key"], "runs/exp01")
        # 关联线索来自当前快照
        self.assertTrue(t["lead_current"])

    def test_no_leads_no_tasks(self):
        pid, _ = self.scan({"src/a.py": "print(1)\n"})
        r = generate_tasks(self.db, pid)
        self.assertEqual(r["created"], 0)
        self.assertEqual(r["total"], 0)

    def test_idempotent_and_status_preserved(self):
        pid, _ = self.seed_lead()
        r1 = generate_tasks(self.db, pid)
        self.assertEqual(r1["created"], 1)
        tid = self.db.list_tasks(pid)[0]["id"]
        self.db.set_task_status(tid, "done", "2026-01-02T00:00:00+00:00")
        r2 = generate_tasks(self.db, pid)
        self.assertEqual((r2["created"], r2["updated"]), (0, 1))
        self.assertEqual(r2["total"], 1)
        t = task_view(self.db, tid)
        self.assertEqual(t["status"], "done")           # 重跑不覆盖人工状态
        self.assertEqual(t["completed_at"], "2026-01-02T00:00:00+00:00")

    def test_dismissed_lead_skipped(self):
        pid, _ = self.seed_lead()
        self.db.set_lead_status(self.db.list_leads(pid)[0]["id"], "dismissed")
        r = generate_tasks(self.db, pid)
        self.assertEqual(r["created"], 0)

    def test_regen_across_snapshots_updates_not_duplicates(self):
        files = {"runs/exp01/metrics.json": METRICS}
        pid, _ = self.seed_lead(files)
        generate_tasks(self.db, pid)
        # 新快照重扫同一目录：线索再次触发 → 任务应更新而非新建
        result = run_scan(self.db, pid)
        run_lead_analysis(self.db, pid, result.snapshot_id)
        r2 = generate_tasks(self.db, pid)
        self.assertEqual((r2["created"], r2["updated"]), (0, 1))
        self.assertEqual(r2["total"], 1)
        t = task_view(self.db, self.db.list_tasks(pid)[0]["id"])
        self.assertTrue(t["lead_current"])   # lead_id 已指向最新快照的线索

    def test_existing_task_not_deleted_when_lead_disappears(self):
        pid, _ = self.seed_lead()
        generate_tasks(self.db, pid)
        # 补上参数文件后问题消失，但已生成的任务不会被自动删除（由人工收尾）
        root = Path(self.db.get_project(pid)["path"])
        (root / "runs" / "exp01" / "params.yaml").write_text("seed: 1\n",
                                                             encoding="utf-8")
        result = run_scan(self.db, pid)
        run_lead_analysis(self.db, pid, result.snapshot_id)
        r = generate_tasks(self.db, pid)
        self.assertEqual(r["created"], 0)
        self.assertEqual(r["total"], 1)      # 任务保留，等待人工确认完成


class TestUserTasks(_Base):
    def test_user_task_and_status_transitions(self):
        pid, _ = self.seed_lead()
        tid = self.db.add_user_task(pid, "人工核对实验目录", "想确认输出是否完整",
                                    "2026-01-01T00:00:00+00:00",
                                    manual_steps=["打开目录查看"],
                                    done_criteria="人工确认")
        rows = self.db.list_tasks(pid)
        self.assertEqual(len(rows), 1)
        t = task_view(self.db, tid)
        self.assertEqual(t["origin"], "user")
        self.assertEqual(t["lead_current"], None)     # 无关联线索
        self.db.set_task_status(tid, "done", "2026-01-03T00:00:00+00:00")
        self.assertEqual(task_view(self.db, tid)["completed_at"],
                         "2026-01-03T00:00:00+00:00")
        self.db.set_task_status(tid, "open", "2026-01-04T00:00:00+00:00")
        self.assertIsNone(task_view(self.db, tid)["completed_at"])
        with self.assertRaises(ValueError):
            self.db.set_task_status(tid, "auto_done", "x")   # 非法状态拒绝

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            self.db.set_task_status(1, "fabricated", "x")


class TestGenerators(_Base):
    _LEAD = {"rule_id": "x", "target_path": "runs/exp01", "title": "线索标题",
             "basis": "触发原因", "evidence_list": ["runs/exp01/metrics.json"],
             "possible_explanations": ["解释"], "next_steps": ["下一步"],
             "false_positive": "误报说明"}

    def test_all_eight_rules_have_generators(self):
        from time_arch.investigate import LEADS_RULES
        self.assertEqual({rid for rid, *_ in LEADS_RULES}, set(TASK_GENERATORS))

    def test_generators_output_read_only_verify_commands(self):
        banned = ("install", "python ", "python3", "pip install", "rm ", "del ",
                  "Remove-Item", "start ", "curl", "wget", "& ", "| ")
        for rid, fn in TASK_GENERATORS.items():
            lead = dict(self._LEAD, rule_id=rid)
            t = fn(lead)
            for key in ("task_key", "title", "why", "evidence",
                        "manual_steps", "done_criteria"):
                self.assertTrue(t[key], f"{rid} 缺少 {key}")
            self.assertIsInstance(t["target_key"], str)   # 项目级任务允许为空
            cmd = t["verify_command"].lower()
            for b in banned:
                self.assertNotIn(b, cmd, f"{rid} 的验证命令包含非只读模式: {b}")

    def test_generic_fallback_for_unknown_rule(self):
        pid, _ = self.seed_lead()
        # 直接插入一条未知规则的线索（模拟未来新增规则）
        self.db.upsert_lead(pid, 1, "future_rule", 1, "未来规则线索", "somewhere",
                            "未来规则的触发原因", '["a.txt"]',
                            '{"possible_explanations": [], "next_steps": ["查看 X"], '
                            '"false_positive": ""}', "2026-01-01T00:00:00+00:00")
        r = generate_tasks(self.db, pid)
        self.assertEqual(r["created"], 2)     # 既有规则 1 条 + 未知规则 1 条
        t = [task_view(self.db, row["id"]) for row in self.db.list_tasks(pid)]
        fut = [x for x in t if x["task_key"] == "future_rule"]
        self.assertEqual(len(fut), 1)
        self.assertEqual(fut[0]["manual_steps_list"], ["查看 X"])
        self.assertTrue(fut[0]["done_criteria"])
        from time_arch.tasks import _task_generic
        self.assertIs(get_generator("never_registered"), _task_generic)

    def test_export_markdown(self):
        pid, _ = self.seed_lead()
        generate_tasks(self.db, pid)
        out = Path(self.tmp.name) / "tasks.md"
        info = export_tasks_markdown(self.db, pid, str(out))
        self.assertEqual(info["tasks"], 1)
        text = out.read_text(encoding="utf-8")
        self.assertIn("# 调查任务清单", text)
        self.assertIn("runs/exp01", text)
        self.assertIn("完成条件", text)
        self.assertIn("只读", text)


class TestMigration(_Base):
    def test_v5_db_upgrades_to_v6_and_keeps_data(self):
        db_path = Path(self.tmp.name) / "old.db"
        db = Database(db_path)
        root = make_project({"runs/exp01/metrics.json": METRICS})
        pid, _ = db.add_project(str(root), "迁移项目")
        result = run_scan(db, pid)
        run_lead_analysis(db, pid, result.snapshot_id)
        generate_tasks(db, pid)
        tid = db.list_tasks(pid)[0]["id"]
        db.set_task_status(tid, "done", "2026-06-01T00:00:00+00:00")
        # 模拟 v5 旧库：回退版本号并删除 v6 表
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("DROP TABLE tasks")
        conn.execute("PRAGMA user_version=5")
        conn.commit()
        conn.close()
        db2 = Database(db_path)         # 重新打开 → 增量迁移到 v6
        self.assertEqual(db2.query("PRAGMA user_version")[0][0], SCHEMA_VERSION)
        # v5 时代的数据完好（v5 从未有任务表，任务数据随模拟 DROP 消失是预期）
        self.assertEqual(len(db2.list_projects()), 1)
        self.assertEqual(len(db2.list_leads(pid)), 1)
        self.assertEqual(len(db2.list_tasks(pid)), 0)
        # v6 新表在迁移后立即可用
        r = generate_tasks(db2, pid)
        self.assertEqual(r["created"], 1)


if __name__ == "__main__":
    unittest.main()
