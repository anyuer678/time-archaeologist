"""为截图/演示预置一个有内容的演示数据库。

用法：python scripts/_demo_seed.py <db_path> <demo_root>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from time_arch.db import Database
from time_arch.scanner import run_scan


def main() -> None:
    db_path, demo_root = Path(sys.argv[1]), Path(sys.argv[2])
    demo_root.mkdir(parents=True, exist_ok=True)
    # 一个"有故事"的项目：源码、日志、发布包、测试报告、实验结果、文档
    files = {
        "src/main.py": "def main():\n    print('archaeology')\n",
        "src/scanner.py": "import hashlib\n",
        "src/classify.py": "RULES = {}\n",
        "tests/test_main.py": "def test_main():\n    assert True\n",
        "README.md": "# Demo Lab\n",
        "config/settings.json": '{"mode": "strict"}\n',
        "logs/train-2026-03.log": "epoch 1 loss 0.5\nepoch 2 loss 0.3\n" * 40,
        "logs/eval-2026-03.log": "acc 0.87\n" * 20,
        "dist/labtool-1.2.0.zip": b"PK-demo-zip-bytes" * 100,
        "dist/labtool.exe": b"MZ" + b"x" * 300,
        "reports/junit-results.xml": "<testsuite tests='12' failures='1'/>",
        "runs/exp03/metrics.json": '{"accuracy": 0.912, "f1": 0.88}',
        "runs/exp03/loss.csv": "step,loss\n1,0.5\n2,0.4\n",
        "data/raw.tsv": "a\tb\n1\t2\n",
        "notes.md": "- TODO: 补充 exp04\n- 未完成：模型导出\n",
    }
    for rel, content in files.items():
        p = demo_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())

    db = Database(db_path)
    pid, _ = db.add_project(str(demo_root), name="demo_lab（实验项目）")
    r1 = run_scan(db, pid)
    assert r1.status == "completed", r1
    # 第二次扫描制造真实"变化"事件，让时间线更有内容
    (demo_root / "runs" / "exp04").mkdir(parents=True, exist_ok=True)
    (demo_root / "runs" / "exp04" / "metrics.json").write_text('{"accuracy": 0.93}', encoding="utf-8")
    (demo_root / "logs" / "train-2026-04.log").write_text("epoch 1 loss 0.2\n", encoding="utf-8")
    (demo_root / "notes.md").unlink()
    r2 = run_scan(db, pid)
    assert r2.status == "completed" and r2.events_created > 0
    # 人工确认一条成果，演示三种状态
    from time_arch.db import Database as _Db  # noqa: F401
    fds = db.list_findings(pid)
    for f in fds:
        if f["kind"] == "executable" and f["status"] == "auto":
            db.set_finding_status(f["id"], "confirmed")
            break
    print(f"demo db ready: {db_path} (events={db.count_events(project_id=pid)}, "
          f"findings={len(db.list_findings(pid))})")


if __name__ == "__main__":
    main()
