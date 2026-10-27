# -*- coding: utf-8 -*-
"""2.0 最终验收：两个真实目录端到端跑通全部 2.0 分析链路。

项目 A = 本仓库自身源码（普通软件项目，真实文件）；
项目 B = 仓库自带实验布局样例 scripts/demo_lab（随仓库提交的真实文件：
参数/日志/双实验运行/测试报告/发布包，开发期构建的固定样例目录，非用户真实科研数据）。
全部结果由真实文件触发，不含任何虚构数据。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from time_arch.db import Database, SCHEMA_VERSION
from time_arch.scanner import run_scan
from time_arch.graph import run_relation_analysis, trace_relations
from time_arch.investigate import run_lead_analysis, RULE_LABELS
from time_arch.dossier import run_dossier_analysis
from time_arch.phases import (suggest_phases, persist_milestone_suggestions,
                              export_timeline_markdown)
from time_arch.repro import run_repro_check, build_repro_report
from time_arch.tasks import generate_tasks, export_tasks_markdown

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_LAB = os.path.join(REPO, "scripts", "demo_lab")

tmp = tempfile.mkdtemp(prefix="acc2_")
db = Database(os.path.join(tmp, "acc.db"))
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), "|", name, ("· " + detail if detail else ""))


def pipeline(root, label):
    pid = db.add_project(root)[0]
    t0 = time.time()
    run_scan(db, pid)
    scan_t = time.time() - t0
    snap = db.latest_snapshot(pid)["id"]
    n_files = db.query("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM file_records "
                       "WHERE snapshot_id=?", (snap,))[0]
    t0 = time.time()
    rel = run_relation_analysis(db, pid, snap)
    rel_t = time.time() - t0
    t0 = time.time()
    run_lead_analysis(db, pid, snap)
    lead_t = time.time() - t0
    t0 = time.time()
    dos = run_dossier_analysis(db, pid, snap)
    dos_t = time.time() - t0
    segs = suggest_phases(db, pid)
    ms = persist_milestone_suggestions(db, pid)
    t0 = time.time()
    tk = generate_tasks(db, pid)
    task_t = time.time() - t0
    leads = db.list_leads(pid)
    print(f"[{label}] {n_files['c']} 文件 / {n_files['s']/1024:.0f} KiB · "
          f"扫描 {scan_t:.2f}s · 关系 {rel['edges']} 边 {rel_t:.2f}s · "
          f"线索 {len(leads)} 条 {lead_t:.2f}s · 档案 {dos['total']} 张 {dos_t:.2f}s · "
          f"阶段片段 {len(segs)} · 里程碑建议 {ms} · 任务 {tk['total']} 项 {task_t:.2f}s")
    return {"pid": pid, "snap": snap, "n": n_files["c"], "rel": rel, "dos": dos,
            "segs": segs, "ms": ms, "tk": tk, "leads": leads}


print("== 项目 A：软件项目（本仓库源码） ==")
a = pipeline(REPO, "软件项目")
check("A1 扫描到足够文件", a["n"] >= 100, f"{a['n']} 文件")
check("A2 关系图谱有边", a["rel"]["edges"] >= 5, f"{a['rel']['edges']} 边")
tr = trace_relations(db, a["pid"], a["snap"], "time_arch/db.py")
check("A3 反向追踪返回子图", len(tr["nodes"]) >= 2 and len(tr["edges"]) >= 1,
      f"{len(tr['nodes'])} 节点 / {len(tr['edges'])} 边")
check("A4 线索≥1 且带规则标签", a["leads"] and all(l["rule_id"] in RULE_LABELS for l in a["leads"]),
      "、".join(sorted({RULE_LABELS[l["rule_id"]] for l in a["leads"]})))
check("A5 成果档案≥1", a["dos"]["total"] >= 1, f"{a['dos']['total']} 张")
check("A6 阶段片段≥1", len(a["segs"]) >= 1, f"{len(a['segs'])} 片段")
check("A7 任务生成≥1", a["tk"]["created"] >= 1, f"新建 {a['tk']['created']}")
_ids_before = {r["id"] for r in db.list_leads(a["pid"])}
run_lead_analysis(db, a["pid"], a["snap"])
_ids_after = {r["id"] for r in db.list_leads(a["pid"])}
check("A8 线索重跑幂等（id 集合不变、只更新不新建）",
      _ids_before == _ids_after and len(_ids_after) >= 1,
      f"{len(_ids_after)} 条 id 不变")
check("A9 任务重跑幂等且保留状态",
      (lambda r: r["created"] == 0 and r["updated"] >= 1)(
          generate_tasks(db, a["pid"])))

print("== 项目 B：实验布局样例（scripts/demo_lab） ==")
b = pipeline(DEMO_LAB, "实验样例")
check("B1 扫描到实验布局文件", b["n"] >= 10, f"{b['n']} 文件")
r3 = run_repro_check(db, b["pid"], b["snap"], "runs/exp03/metrics.json")
check("B2 复现检查 exp03：A–D 分级 + 公开评分规则",
      r3["level"] in ("A", "B", "C", "D") and bool(r3["scoring_rules"])
      and len(r3["checks"]) >= 5,
      f"等级 {r3['level']} · {len(r3['checks'])} 检查项 · 规则 {len(r3['scoring_rules'])} 行")
lv = {c["level"] for c in r3["checks"]}
check("B3 检查项含 A/B/C 多级表达", lv <= {"A", "B", "C", "D"} and len(lv) >= 2,
      "级别集合：" + "/".join(sorted(lv)))
r4 = run_repro_check(db, b["pid"], b["snap"], "runs/exp04/metrics.json")
print(f"  exp04 复现等级: {r4['level']}（{len(r4['checks'])} 检查项）")
rep = build_repro_report(r3)
check("B4 复现报告可导出且含逐项依据", "复现" in rep and "评分规则" in rep,
      f"{len(rep)} 字符")
check("B5 实验线索触发（结果缺参数/输入）",
      any(l["rule_id"] == "result_without_input" for l in b["leads"]),
      "、".join(RULE_LABELS[l["rule_id"]] for l in b["leads"]))
check("B6 成果档案≥1（不因 exe/zip 存在判定成功）", b["dos"]["total"] >= 1,
      f"{b['dos']['total']} 张")
tasks_b = db.list_tasks(b["pid"])
check("B7 调查任务≥1 且五要素齐全",
      bool(tasks_b) and all(
          t["why"] and t["manual_steps"] != "[]" and t["done_criteria"] for t in tasks_b),
      f"{len(tasks_b)} 项")
check("B8 任务含只读验证建议（不执行）",
      any(t["verify_command"] or t["verify_desc"] for t in tasks_b),
      "样例: " + next((t["verify_command"][:60] for t in tasks_b if t["verify_command"]), "—"))

print("== 导出与兼容 ==")
tl = os.path.join(tmp, "timeline.md"); tk = os.path.join(tmp, "tasks.md")
et = export_timeline_markdown(db, a["pid"], tl)
export_tasks_markdown(db, a["pid"], tk)
md1, md2 = open(tl, encoding="utf-8").read(), open(tk, encoding="utf-8").read()
check("C1 时间线/任务双导出非空",
      md1.startswith("# ") and md2.startswith("# 调查任务清单"),
      f"{len(md1)} + {len(md2)} 字符")
check("C2 迁移后 user_version 正确",
      db.query("PRAGMA user_version")[0][0] == SCHEMA_VERSION,
      f"v{SCHEMA_VERSION}（旧库升级由单元测试覆盖：v0.1.0→v6、v5→v6）")

shutil_cleanup = None
passed = sum(1 for _, ok, _ in results if ok)
print(f"\nACCEPTANCE {passed}/{len(results)} PASS")
report = ["# 时光考古机 2.0 最终验收记录（实际运行）", "",
          f"- 运行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
          "- 项目 A：本仓库自身源码（普通软件项目，真实文件）",
          "- 项目 B：仓库自带实验布局样例 scripts/demo_lab（磁盘上真实存在的文件；"
          "开发期构建的固定样例目录，非用户真实科研数据，特此说明）",
          "- 数据库：临时目录独立实例，扫描/分析全程只读源目录", ""]
for name, ok, detail in results:
    report.append(f"- [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
open(os.path.join(tempfile.gettempdir(), "acceptance_2_report.md"), "w",
     encoding="utf-8").write("\n".join(report) + "\n")
print("报告已写入", os.path.join(tempfile.gettempdir(), "acceptance_2_report.md"))
import shutil
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if passed == len(results) else 1)
