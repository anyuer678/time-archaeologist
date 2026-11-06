"""完整取证报告测试：汇总成果档案、线索、任务、复现检查、阶段与里程碑。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from time_arch.db import Database
from time_arch.dossier import run_dossier_analysis
from time_arch.forensics import (build_forensics_json, build_forensics_markdown,
                                 export_forensics_json, export_forensics_markdown)
from time_arch.graph import run_relation_analysis
from time_arch.investigate import run_lead_analysis
from time_arch.phases import persist_milestone_suggestions
from time_arch.repro import run_repro_check
from time_arch.scanner import run_scan
from time_arch.tasks import generate_tasks

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


def _strip_generated(text: str) -> str:
    return "\n".join(line for line in text.splitlines()
                     if not line.startswith("- 报告生成："))


class ForensicsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ta_forensics_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        for rel, content in FILES.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        self.db = Database(Path(self.tmp.name) / "f.db")
        self.pid = self.db.add_project(str(self.root))[0]
        self.snap = run_scan(self.db, self.pid).snapshot_id

    def analyze_all(self):
        run_relation_analysis(self.db, self.pid, self.snap)
        run_lead_analysis(self.db, self.pid, self.snap)
        run_dossier_analysis(self.db, self.pid, self.snap)
        generate_tasks(self.db, self.pid, self.snap)
        persist_milestone_suggestions(self.db, self.pid)
        run_repro_check(self.db, self.pid, self.snap, "runs/exp01/metrics.json")

    def test_sections_and_content(self):
        self.analyze_all()
        md = build_forensics_markdown(self.db, self.pid)
        self.assertTrue(md.startswith("# 项目取证报告"))
        for section in ("## 一、项目画像", "## 二、成果档案", "## 三、调查线索",
                        "## 四、调查任务", "## 五、复现检查",
                        "## 六、演化阶段建议与里程碑"):
            self.assertIn(section, md)
        self.assertIn("诚实边界", md)
        self.assertIn("mtime", md)
        self.assertIn("不代表实验成功或失败", md)
        self.assertIn("实验结果与统计报告", md)
        self.assertIn("有测试无结果", md)  # 线索规则
        self.assertIn("评分规则（完全公开）", md)
        self.assertIn("| 实验参数 |", md)
        self.assertIn("### 里程碑", md)

    def test_without_scan(self):
        db2 = Database(Path(self.tmp.name) / "empty.db")
        pid = db2.add_project(str(self.root))[0]
        md = build_forensics_markdown(db2, pid)
        self.assertIn("尚无完成的扫描快照", md)
        self.assertIn("## 二、成果档案（0 份）", md)

    def test_export_and_determinism(self):
        self.analyze_all()
        a = build_forensics_markdown(self.db, self.pid)
        b = build_forensics_markdown(self.db, self.pid)
        self.assertEqual(_strip_generated(a), _strip_generated(b))
        out = Path(self.tmp.name) / "forensics.md"
        export_forensics_markdown(self.db, self.pid, str(out))
        self.assertEqual(_strip_generated(out.read_text(encoding="utf-8")),
                         _strip_generated(a))

    def test_json_report_and_export(self):
        self.analyze_all()
        data = json.loads(build_forensics_json(self.db, self.pid))
        for key in ("project", "snapshot", "stats", "artifacts", "leads",
                    "tasks", "repro_checks", "phase_suggestions",
                    "milestones", "git", "disclaimer", "generated_at"):
            self.assertIn(key, data)
        self.assertEqual(data["project"]["id"], self.pid)
        self.assertEqual(data["snapshot"]["id"], self.snap)
        self.assertTrue(data["artifacts"])
        self.assertTrue(data["leads"])
        self.assertTrue(data["tasks"])
        self.assertTrue(data["repro_checks"])
        self.assertTrue(data["phase_suggestions"])
        self.assertIn("mtime", data["disclaimer"])
        out = Path(self.tmp.name) / "forensics.json"
        export_forensics_json(self.db, self.pid, str(out))
        reloaded = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["project"]["id"], self.pid)

    def test_git_metadata_section(self):
        git = self.root / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "refs" / "heads" / "main").write_text("a" * 40 + "\n",
                                                     encoding="utf-8")
        md = build_forensics_markdown(self.db, self.pid)
        self.assertIn("## 七、Git 元数据", md)
        data = json.loads(build_forensics_json(self.db, self.pid))
        self.assertEqual(data["git"]["branch"], "refs/heads/main")
        self.assertEqual(data["git"]["head_sha"], "a" * 40)

    def test_snapshot_scope_and_errors(self):
        second = run_scan(self.db, self.pid).snapshot_id
        md = build_forensics_markdown(self.db, self.pid, self.snap)
        self.assertIn(f"报告快照：#{self.snap}", md)
        with self.assertRaises(ValueError):
            build_forensics_markdown(self.db, self.pid, 9999)
        with self.assertRaises(ValueError):
            build_forensics_markdown(self.db, 9999)
        self.assertGreater(second, self.snap)


if __name__ == "__main__":
    unittest.main()
