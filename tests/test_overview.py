"""跨项目摘要测试：最新快照、待查看线索、待处理任务与档案数量。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from time_arch.db import Database
from time_arch.dossier import run_dossier_analysis
from time_arch.investigate import run_lead_analysis
from time_arch.overview import project_summaries
from time_arch.scanner import run_scan
from time_arch.tasks import generate_tasks

FILES = {
    "src/train.py": "def train():\n    return 1\n",
    "tests/test_train.py": "from src.train import train\n",
    "runs/exp01/config.json": '{"seed": 42}\n',
    "runs/exp01/metrics.json": '{"accuracy": 0.9}\n',
}


class OverviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ta_overview_")
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "o.db")
        self.root = Path(self.tmp.name) / "proj"
        for rel, content in FILES.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    def test_empty(self):
        self.assertEqual(project_summaries(self.db), [])

    def test_counts_follow_project_state(self):
        pid, _ = self.db.add_project(str(self.root), "摘要项目")
        snap = run_scan(self.db, pid).snapshot_id
        run_lead_analysis(self.db, pid, snap)
        run_dossier_analysis(self.db, pid, snap)
        generate_tasks(self.db, pid, snap)
        row = project_summaries(self.db)[0]
        self.assertEqual(row["id"], pid)
        self.assertEqual(row["latest_snapshot_id"], snap)
        self.assertEqual(row["files"], len(FILES))
        self.assertGreaterEqual(row["open_leads"], 1)
        self.assertGreaterEqual(row["open_tasks"], 1)
        self.assertGreaterEqual(row["artifacts"], 1)
        # 人工完成后待处理任务数下降
        task = self.db.list_tasks(pid)[0]
        self.db.set_task_status(task["id"], "done", "2026-11-06T00:00:00+00:00")
        self.assertEqual(project_summaries(self.db)[0]["open_tasks"],
                         row["open_tasks"] - 1)


if __name__ == "__main__":
    unittest.main()
