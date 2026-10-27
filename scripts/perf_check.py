# -*- coding: utf-8 -*-
"""2.0 性能实测：合成大目录（仅作性能基准，非展示数据）上测量扫描与各 2.0 分析耗时。"""
import os
import sys
import shutil
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from time_arch.db import Database
from time_arch.scanner import run_scan
from time_arch.graph import run_relation_analysis
from time_arch.investigate import run_lead_analysis
from time_arch.dossier import run_dossier_analysis
from time_arch.phases import suggest_phases
from time_arch.tasks import generate_tasks

N_DIRS = 60
PER_DIR = 50
tmp = tempfile.mkdtemp(prefix="perf20_")
root = os.path.join(tmp, "big")
for d in range(N_DIRS):
    rd = os.path.join(root, f"runs/exp{d:03d}")
    os.makedirs(os.path.join(root, f"src/mod{d:02d}"), exist_ok=True)
    os.makedirs(os.path.join(root, f"tests"), exist_ok=True)
    os.makedirs(rd, exist_ok=True)
    with open(os.path.join(root, f"src/mod{d:02d}/core.py"), "w", encoding="utf-8") as f:
        f.write(f"def run_{d}():\n    return {d}\n" * 40)
    with open(os.path.join(root, "tests", f"test_mod{d:02d}.py"), "w", encoding="utf-8") as f:
        f.write(f"from src.mod{d:02d}.core import run_{d}\n" * 10)
    with open(os.path.join(rd, "params.yaml"), "w", encoding="utf-8") as f:
        f.write(f"seed: {d}\nepochs: {10 + d}\n")
    with open(os.path.join(rd, "metrics.json"), "w", encoding="utf-8") as f:
        f.write(f'{{"acc": {d / 100:.2f}, "run": {d}}}\n')
    with open(os.path.join(rd, "train.log"), "w", encoding="utf-8") as f:
        f.write(f"epoch 1 done\n" * 30)
print(f"生成基准目录：{N_DIRS} 个模块目录 + {N_DIRS} 个运行目录，"
      f"共 {N_DIRS * PER_DIR} 个文件（合成数据，仅测性能）")

db = Database(os.path.join(tmp, "perf.db"))
pid = db.add_project(root)[0]
t0 = time.time(); run_scan(db, pid); scan_t = time.time() - t0
snap = db.latest_snapshot(pid)["id"]
t0 = time.time(); run_scan(db, pid); rescan_t = time.time() - t0
t0 = time.time(); rel = run_relation_analysis(db, pid, snap); rel_t = time.time() - t0
t0 = time.time(); run_lead_analysis(db, pid, snap); lead_t = time.time() - t0
t0 = time.time(); dos = run_dossier_analysis(db, pid, snap); dos_t = time.time() - t0
t0 = time.time(); segs = suggest_phases(db, pid); ph_t = time.time() - t0
t0 = time.time(); tk = generate_tasks(db, pid); tk_t = time.time() - t0

print(f"PERF 首次扫描（全量 SHA-256）: {N_DIRS * PER_DIR} 文件 {scan_t:.2f}s "
      f"（{N_DIRS * PER_DIR / max(scan_t, 1e-9):.0f} 文件/秒）")
print(f"PERF 无变化重扫（增量哈希复用）: {rescan_t:.2f}s")
print(f"PERF 关系分析: {rel['edges']} 边 {rel_t:.2f}s")
print(f"PERF 线索分析: {lead_t:.2f}s")
print(f"PERF 成果档案: {dos['total']} 张 {dos_t:.2f}s")
print(f"PERF 阶段建议: {len(segs)} 片段 {ph_t:.2f}s")
print(f"PERF 任务生成: {tk['total']} 项 {tk_t:.2f}s")
print(f"PERF 2.0 分析合计（不含扫描）: {rel_t + lead_t + dos_t + ph_t + tk_t:.2f}s")
shutil.rmtree(tmp, ignore_errors=True)
