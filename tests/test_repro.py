"""实验复现检查器测试（2.0 第三步）。

覆盖：A/B/C/D 四级判定、公开评分规则、矛盾证据（D）、重复运行差异、
幂等持久化、报告可解释性。全部经真实临时目录 + 真实扫描链路。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from time_arch.db import Database, SCHEMA_VERSION
from time_arch.repro import (CRITICAL_ITEMS, ITEM_WEIGHTS, LEVEL_LABELS,
                             build_repro_report, check_repro, load_repro_check,
                             run_repro_check, scoring_rules_text)
from time_arch.scanner import run_scan


def make_project(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="ta_repro_"))
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
    return root


COMPLETE = {
    "src/train.py": "import numpy as np\nEPOCHS = 10\n",
    "requirements.txt": "numpy==1.26.4\npandas==2.1.1\n",
    "runs/exp01/params.yaml": "seed: 42\nepochs: 10\nlr: 0.001\n",
    "runs/exp01/data/train.csv": "x,y\n1,2\n3,4\n",
    "runs/exp01/train.log": "2026-10-27 10:00:01 start training epochs=10\n"
                            "epoch 1 loss 0.50\n2026-10-27 10:05:00 finish: completed\n",
    "runs/exp01/metrics.json": '{"accuracy": 0.91}\n',
}
MINIMAL = {"runs/exp02/metrics.json": '{"accuracy": 0.5}\n'}
CONTRADICTION = {
    "runs/exp03/metrics.json": '{"accuracy": 0.99}\n',
    "runs/exp03/train.log": "start training\nTraceback (most recent call last)\n"
                            "ValueError: bad input data\n",
}
REPEATS = {
    "runs/exp04/metrics.json": '{"accuracy": 0.91}\n',
    "runs/exp05/metrics.json": '{"accuracy": 0.91}\n',
    "runs/exp06/metrics.json": '{"accuracy": 0.77}\n',
}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ta_repro_db_")
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")

    def scan(self, files):
        root = make_project(files)
        pid, _ = self.db.add_project(str(root), "实验项目")
        r = run_scan(self.db, pid)
        return pid, r.snapshot_id


class TestReproLevels(_Base):
    def test_complete_run_levels_and_score(self):
        pid, snap = self.scan(COMPLETE)
        r = check_repro(self.db, pid, snap, "runs/exp01/metrics.json")
        by = {c["item"]: c for c in r["checks"]}
        self.assertEqual(set(by), set(ITEM_WEIGHTS))          # 九项齐全
        self.assertEqual(by["实验参数"]["level"], "A")
        self.assertEqual(by["输入数据"]["level"], "A")
        self.assertEqual(by["随机种子"]["level"], "A")
        self.assertTrue(any("seed: 42" in e for e in by["随机种子"]["evidence"]))
        self.assertEqual(by["源码快照"]["level"], "A")
        self.assertEqual(by["运行环境与依赖"]["level"], "A")   # requirements 带 ==
        self.assertEqual(by["开始与结束记录"]["level"], "A")
        self.assertEqual(by["输出结果"]["level"], "A")
        self.assertEqual(by["错误与警告"]["level"], "A")
        self.assertEqual(by["重复实验差异"]["level"], "C")     # 无对比样本
        self.assertEqual(r["score"], 90)                      # 100 - 重复实验差异(权重10, C)
        self.assertEqual(r["level"], "A")                     # 关键项全 A 且 ≥90
        self.assertEqual(r["run_dir"], "runs/exp01")

    def test_minimal_run_is_C(self):
        pid, snap = self.scan(MINIMAL)
        r = check_repro(self.db, pid, snap, "runs/exp02/metrics.json")
        by = {c["item"]: c["level"] for c in r["checks"]}
        self.assertEqual(by["输出结果"], "A")
        self.assertEqual(by["实验参数"], "C")
        self.assertEqual(by["源码快照"], "C")
        self.assertEqual(r["level"], "C")
        self.assertLess(r["score"], 50)

    def test_contradiction_is_D(self):
        pid, snap = self.scan(CONTRADICTION)
        r = check_repro(self.db, pid, snap, "runs/exp03/metrics.json")
        by = {c["item"]: c for c in r["checks"]}
        self.assertEqual(by["错误与警告"]["level"], "D")
        self.assertTrue(any("Traceback" in e for e in by["错误与警告"]["evidence"]))
        self.assertEqual(r["level"], "D")                     # 任一 D → 总级别 D
        self.assertIn("矛盾", by["错误与警告"]["detail"])

    def test_repeat_runs_identical_and_different(self):
        pid, snap = self.scan(REPEATS)
        same = check_repro(self.db, pid, snap, "runs/exp04/metrics.json")
        diff = check_repro(self.db, pid, snap, "runs/exp06/metrics.json")
        rep_same = next(c for c in same["checks"] if c["item"] == "重复实验差异")
        rep_diff = next(c for c in diff["checks"] if c["item"] == "重复实验差异")
        self.assertEqual(rep_same["level"], "A")              # exp05 与 exp04 哈希一致
        self.assertIn("exp05", " ".join(rep_same["evidence"]))
        self.assertEqual(rep_diff["level"], "B")              # exp04 与 exp06 内容不同
        self.assertIn("exp04", " ".join(rep_diff["evidence"]))

    def test_root_level_target_no_run_dir(self):
        pid, snap = self.scan({"metrics.json": '{"acc": 1}\n'})
        r = check_repro(self.db, pid, snap, "metrics.json")
        self.assertEqual(r["run_dir"], "(项目根)")
        rep = next(c for c in r["checks"] if c["item"] == "重复实验差异")
        self.assertEqual(rep["level"], "C")
        self.assertEqual(len(r["checks"]), len(ITEM_WEIGHTS))


class TestReproScoringAndPersist(_Base):
    def test_scoring_rules_public_and_report(self):
        self.assertEqual(sum(ITEM_WEIGHTS.values()), 100)
        pid, snap = self.scan(COMPLETE)
        r = run_repro_check(self.db, pid, snap, "runs/exp01/metrics.json")
        report = build_repro_report(r)
        for token in ("评分规则", "权重", "A=1.0", "关键项", "runs/exp01/metrics.json",
                      "不自动执行任何程序", LEVEL_LABELS["A"]):
            self.assertIn(token, report)
        for item in ITEM_WEIGHTS:
            self.assertIn(item, report)
        for t in scoring_rules_text():
            self.assertIn(t.split("：")[0], report)

    def test_persist_idempotent_and_load(self):
        pid, snap = self.scan(COMPLETE)
        r1 = run_repro_check(self.db, pid, snap, "runs/exp01/metrics.json")
        r2 = run_repro_check(self.db, pid, snap, "runs/exp01/metrics.json")
        self.assertEqual(r1["id"], r2["id"])
        self.assertEqual(r1["level"], r2["level"])
        rows = self.db.list_repro_checks(pid, snapshot_id=snap)
        self.assertEqual(len(rows), 1)                        # 幂等：同目标整行替换
        loaded = load_repro_check(self.db, r1["id"])
        self.assertEqual(loaded["level"], r1["level"])
        self.assertEqual(loaded["score"], r1["score"])
        self.assertEqual(len(loaded["checks"]), len(ITEM_WEIGHTS))

    def test_deterministic_rerun_same_result(self):
        pid, snap = self.scan(COMPLETE)
        a = check_repro(self.db, pid, snap, "runs/exp01/metrics.json")
        b = check_repro(self.db, pid, snap, "runs/exp01/metrics.json")
        strip = lambda r: [{k: v for k, v in c.items()} for c in r["checks"]]
        self.assertEqual(strip(a), strip(b))                  # 同输入同输出（确定性）
        self.assertEqual(a["level"], b["level"])
        self.assertEqual(a["score"], b["score"])

    def test_critical_items_are_subset_of_weights(self):
        self.assertTrue(set(CRITICAL_ITEMS) <= set(ITEM_WEIGHTS))
        self.assertTrue(set("ABCD") <= set(LEVEL_LABELS))


if __name__ == "__main__":
    unittest.main()
