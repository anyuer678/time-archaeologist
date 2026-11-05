"""无头命令行入口：不启动 Tk，复用与 GUI 完全相同的只读扫描与分析 API。

典型用法（`--db` 需放在子命令之前；省略时用默认数据库）：
  python -m time_arch projects
  python -m time_arch scan <目录> [--name 名称] [--no-analyze]
  python -m time_arch analyze <项目ID或路径> [--snapshot N] [--only leads,relations,...]
  python -m time_arch report <项目> [--format markdown|json] [-o 文件]
  python -m time_arch leads <项目> [--status open] [--json]
  python -m time_arch dossiers <项目> [--json]
  python -m time_arch repro <项目> <目标文件> [--export 报告.md] [--json]
  python -m time_arch tasks <项目> [--status open] [--export 清单.md]
  python -m time_arch timeline <项目> [-o 时间线.md]
  python -m time_arch forensics <项目> [-o 取证报告.md]
  python -m time_arch diff <项目> [--json]
  python -m time_arch maintain [--integrity] [--backup 备份.db] [--vacuum]

所有分析均为只读：不执行被扫描目录中的任何程序、不联网。
退出码：0 成功；2 参数/项目/快照问题；1 运行时异常。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

from . import __version__
from .db import Database
from .dossier import dossier_view, run_dossier_analysis
from .forensics import build_forensics_markdown, export_forensics_markdown
from .graph import run_relation_analysis
from .investigate import RULE_LABELS, run_lead_analysis
from .phases import (persist_milestone_suggestions, suggest_milestones,
                     suggest_phases, export_timeline_markdown)
from .reports import build_json, build_markdown, export_report
from .repro import LEVEL_LABELS, build_repro_report, run_repro_check
from .scanner import run_scan
from .tasks import TASK_STATUS, export_tasks_markdown, generate_tasks, task_view
from .util import default_db_path, utcnow_iso

LEAD_STATUS_LABELS = {"open": "待查看", "acknowledged": "已知悉", "dismissed": "已忽略"}


class CliError(Exception):
    """可向用户解释的错误（退出码 2）。"""


def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(p))))


def _resolve_project(db: Database, ref: str):
    s = str(ref)
    if s.isdigit():
        p = db.get_project(int(s))
        if p is not None:
            return p
        raise CliError(f"项目 #{s} 不存在")
    projects = db.list_projects()
    hits = [p for p in projects if _norm_path(p["path"]) == _norm_path(s)]
    if not hits:
        hits = [p for p in projects if p["name"] == s]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise CliError(f"「{s}」对应多个项目，请改用 #ID 指定")
    raise CliError(f"未登记的项目：{s}（可先执行 scan 登记并扫描）")


def _snapshot(db: Database, project_id: int, snapshot_id: int | None):
    if snapshot_id is None:
        snap = db.latest_snapshot(project_id)
        if snap is None:
            raise CliError("该项目还没有完成的扫描快照，请先执行 scan")
        return snap
    for s in db.list_snapshots(project_id):
        if s["id"] == snapshot_id:
            return s
    raise CliError(f"快照 #{snapshot_id} 不存在或不属于该项目")


def _json_out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _parse_json_fields(d: dict, fields: tuple[str, ...]) -> dict:
    for k in fields:
        if k in d:
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                pass
    return d


# ---------------------------------------------------------------- 子命令实现
def cmd_projects(db: Database, args) -> int:
    rows = db.list_projects()
    if args.json:
        _json_out([dict(r) for r in rows])
        return 0
    if not rows:
        print("（尚未登记任何项目）")
        return 0
    print(f"{'#':>3}  {'项目':<20} {'快照':>4} {'最近扫描':<20} 路径")
    for r in rows:
        print(f"{r['id']:>3}  {r['name'][:20]:<20} {r['snapshot_count']:>4} "
              f"{(r['last_scan_at'] or '-')[:19]:<20} {r['path']}")
    return 0


def cmd_scan(db: Database, args) -> int:
    root = os.path.abspath(os.path.expanduser(args.path))
    if not os.path.isdir(root):
        raise CliError(f"目录不存在或不可访问：{root}")
    pid, created = db.add_project(root, args.name)
    if not args.json:
        print(f"{'已登记' if created else '已存在'}项目 #{pid}"
              f"「{db.get_project(pid)['name']}」")
    progress = None
    if args.progress and not args.json:
        last = [0.0]

        def progress(p):  # 每秒最多一行，避免刷屏
            now = time.time()
            if now - last[0] >= 1.0:
                last[0] = now
                print(f"  扫描中：{p.files} 个文件 · {p.bytes_hashed} 字节已哈希"
                      f" · {p.errors} 个错误", file=sys.stderr)
    result = run_scan(db, pid, progress_cb=progress, analyze=not args.no_analyze)
    if result.status != "completed":
        detail = "；".join(result.error_messages[:3]) or result.status
        raise CliError(f"扫描未完成（{result.status}）：{detail}")
    data = {
        "project_id": pid, "snapshot_id": result.snapshot_id,
        "files": result.files, "bytes_hashed": result.bytes_hashed,
        "files_cached": result.files_cached, "errors": result.errors,
        "events_created": result.events_created,
        "findings_created": result.findings_created,
    }
    if args.json:
        _json_out(data)
        return 0
    cached = f"（复用缓存 {result.files_cached} 个）" if result.files_cached else ""
    print(f"扫描完成：快照 #{result.snapshot_id}，{result.files} 个文件{cached}，"
          f"错误 {result.errors}，新增事件 {result.events_created}，"
          f"成果线索 {result.findings_created}")
    return 0


def cmd_analyze(db: Database, args) -> int:
    p = _resolve_project(db, args.project)
    snap = _snapshot(db, p["id"], args.snapshot)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    valid = {"leads", "relations", "dossiers", "tasks", "phases"}
    unknown = only - valid
    if unknown:
        raise CliError(f"未知分析类型：{', '.join(sorted(unknown))}（可选：{', '.join(sorted(valid))}）")
    summary: dict = {"project_id": p["id"], "snapshot_id": snap["id"]}
    if "relations" in only:
        summary["relations_edges"] = run_relation_analysis(db, p["id"], snap["id"])["edges"]
    if "leads" in only:
        summary["leads"] = run_lead_analysis(db, p["id"], snap["id"])["leads"]
    if "dossiers" in only:
        summary["dossiers"] = run_dossier_analysis(db, p["id"], snap["id"])["total"]
    if "tasks" in only:
        t = generate_tasks(db, p["id"], snap["id"])
        summary.update({"tasks_created": t["created"], "tasks_updated": t["updated"],
                        "tasks_total": t["total"]})
    if "phases" in only:
        summary["phase_segments"] = len(suggest_phases(db, p["id"]))
        summary["milestone_suggestions"] = len(suggest_milestones(db, p["id"]))
        if args.persist_milestones:
            summary["milestones_created"] = persist_milestone_suggestions(db, p["id"])
    if args.json:
        _json_out(summary)
        return 0
    print(f"分析完成：项目 #{p['id']}「{p['name']}」· 快照 #{snap['id']}")
    labels = {"relations_edges": "关系边", "leads": "线索", "dossiers": "成果档案",
              "tasks_created": "新建任务", "tasks_updated": "更新任务",
              "phase_segments": "阶段片段", "milestone_suggestions": "里程碑建议",
              "milestones_created": "已保存里程碑"}
    for k, label in labels.items():
        if k in summary:
            print(f"  {label}：{summary[k]}")
    return 0


def cmd_report(db: Database, args) -> int:
    p = _resolve_project(db, args.project)
    if args.out:
        export_report(db, p["id"], args.format, args.out)
        print(f"报告已导出：{args.out}")
    else:
        sys.stdout.write(build_json(db, p["id"]) if args.format == "json"
                         else build_markdown(db, p["id"]))
    return 0


def cmd_leads(db: Database, args) -> int:
    p = _resolve_project(db, args.project)
    if args.set_status:
        raw_id, status = args.set_status
        if status not in LEAD_STATUS_LABELS:
            raise CliError(f"未知状态：{status}（可选：open/acknowledged/dismissed）")
        lid = int(raw_id) if raw_id.isdigit() else -1
        row = db.query("SELECT id FROM leads WHERE id=? AND project_id=?",
                       (lid, p["id"]))
        if not row:
            raise CliError(f"线索 #{raw_id} 不存在或不属于该项目")
        db.set_lead_status(lid, status)
        print(f"线索 #{lid} 状态已设为：{LEAD_STATUS_LABELS[status]}")
        return 0
    rows = db.list_leads(p["id"], rule_id=args.rule, status=args.status, limit=10000)
    if args.json:
        _json_out([_parse_json_fields(dict(r), ("evidence", "details")) for r in rows])
        return 0
    print(f"项目 #{p['id']}「{p['name']}」：{len(rows)} 条线索"
          "（线索 = 值得查看的问题，不是结论）")
    for r in rows:
        print(f"#{r['id']} [{LEAD_STATUS_LABELS.get(r['status'], r['status'])}] "
              f"{RULE_LABELS.get(r['rule_id'], r['rule_id'])} — {r['title']}"
              + (f" · {r['target_path']}" if r["target_path"] else ""))
    return 0


def cmd_dossiers(db: Database, args) -> int:
    p = _resolve_project(db, args.project)
    rows = db.list_artifacts(p["id"])
    views = [v for v in (dossier_view(db, r["id"]) for r in rows) if v]
    if args.json:
        _json_out(views)
        return 0
    print(f"项目 #{p['id']}「{p['name']}」：{len(views)} 份成果档案"
          "（基于快照记录，非执行验证）")
    for v in views:
        print(f"#{v['id']} [{v['status']}] {v['kind_label']} · {v['name']} "
              f"· 置信度 {v['confidence']} · {v['primary_path']}")
        missing = [c["item"] for c in v["completeness"] if c["state"] == "missing"]
        if missing:
            print(f"    待调查（缺失证据）：{'、'.join(missing)}")
    return 0


def cmd_repro(db: Database, args) -> int:
    p = _resolve_project(db, args.project)
    snap = _snapshot(db, p["id"], args.snapshot)
    result = run_repro_check(db, p["id"], snap["id"], args.target)
    if args.export:
        with open(args.export, "w", encoding="utf-8") as f:
            f.write(build_repro_report(result))
    if args.json:
        _json_out(result)
        return 0
    print(f"复现检查：{result['target_path']} · 快照 #{result['snapshot_id']}")
    print(f"总级别：{LEVEL_LABELS.get(result['level'], result['level'])}"
          f" · 总分 {result['score']}/100 · 运行目录 {result['run_dir']}")
    for c in result["checks"]:
        print(f"  [{c['level']}] {c['item']}：{c['detail']}")
    print("评分规则（完全公开）：")
    for line in result.get("scoring_rules") or []:
        print(f"  · {line}")
    if args.export:
        print(f"报告已导出：{args.export}")
    return 0


def cmd_tasks(db: Database, args) -> int:
    p = _resolve_project(db, args.project)
    actions = [("done", args.complete), ("open", args.reopen),
               ("dismissed", args.dismiss)]
    chosen = [(status, tid) for status, tid in actions if tid is not None]
    if chosen:
        if len(chosen) != 1:
            raise CliError("--complete / --reopen / --dismiss 一次只能指定一个")
        status, tid = chosen[0]
        row = db.query("SELECT id FROM tasks WHERE id=? AND project_id=?",
                       (tid, p["id"]))
        if not row:
            raise CliError(f"任务 #{tid} 不存在或不属于该项目")
        db.set_task_status(tid, status, utcnow_iso())
        note = "（仅表示你本人已完成调查并确认结论）" if status == "done" else ""
        print(f"任务 #{tid} 状态已设为：{TASK_STATUS.get(status, status)}{note}")
        return 0
    if args.export:
        r = export_tasks_markdown(db, p["id"], args.export)
        print(f"任务清单已导出：{r['path']}（{r['tasks']} 项，待处理 {r['open']} 项）")
        return 0
    rows = db.list_tasks(p["id"], status=args.status, limit=10000)
    if args.json:
        _json_out([_parse_json_fields(dict(r), ("evidence", "manual_steps")) for r in rows])
        return 0
    print(f"项目 #{p['id']}「{p['name']}」：{len(rows)} 项任务"
          "（完成状态需人工确认，系统不执行验证命令）")
    for r in rows:
        v = task_view(db, r["id"])
        print(f"#{r['id']} [{TASK_STATUS.get(r['status'], r['status'])}] {r['title']}"
              + (f" · {r['target_key']}" if r["target_key"] else ""))
        if v and v.get("verify_command"):
            print(f"    只读验证建议（不会自动执行）：{v['verify_command']}")
    return 0


def cmd_timeline(db: Database, args) -> int:
    p = _resolve_project(db, args.project)
    if args.out:
        r = export_timeline_markdown(db, p["id"], args.out)
        print(f"时间线已导出：{r['path']}（{r['snapshots']} 个快照）")
        return 0
    fd, tmp = tempfile.mkstemp(prefix="ta_timeline_", suffix=".md")
    os.close(fd)
    try:
        export_timeline_markdown(db, p["id"], tmp)
        with open(tmp, encoding="utf-8") as f:
            sys.stdout.write(f.read())
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return 0


def cmd_forensics(db: Database, args) -> int:
    p = _resolve_project(db, args.project)
    if args.out:
        export_forensics_markdown(db, p["id"], args.out, args.snapshot)
        print(f"取证报告已导出：{args.out}")
    else:
        sys.stdout.write(build_forensics_markdown(db, p["id"], args.snapshot))
    return 0


def cmd_maintain(db: Database, args) -> int:
    if not (args.integrity or args.backup or args.vacuum):
        raise CliError("请至少指定 --integrity / --backup / --vacuum 之一")
    out: dict = {}
    if args.integrity:
        out["integrity"] = db.integrity_check()
    if args.backup:
        out["backup"] = db.backup(args.backup)
    if args.vacuum:
        db.vacuum()
        out["vacuum"] = "done"
    if args.json:
        _json_out(out)
        return 0
    if "integrity" in out:
        print(f"完整性检查：{out['integrity']}")
    if "backup" in out:
        print(f"备份已写入：{out['backup']}")
    if "vacuum" in out:
        print("数据库已压缩（VACUUM）")
    return 0


def cmd_diff(db: Database, args) -> int:
    p = _resolve_project(db, args.project)
    latest = _snapshot(db, p["id"], args.snapshot)
    prev = db.previous_completed_snapshot(p["id"], latest["id"])
    if prev is None:
        raise CliError("不足两次完成的快照，无法对比（请先重新扫描）")
    d = db.compare_snapshots(prev["id"], latest["id"], limit=args.limit)
    added, removed, modified = d["counts"]
    if args.json:
        _json_out({"old_snapshot": prev["id"], "new_snapshot": latest["id"],
                   "counts": {"added": added, "removed": removed, "modified": modified},
                   "added": d["added"], "removed": d["removed"], "modified": d["modified"]})
        return 0
    print(f"快照 #{prev['id']} → #{latest['id']}：新增 {added}，删除 {removed}，修改 {modified}"
          f"（最多列出 {args.limit} 条）")
    for f in d["added"]:
        print(f"  + {f['rel_path']} ({f['size']} B)")
    for f in d["removed"]:
        print(f"  - {f['rel_path']}")
    for f in d["modified"]:
        print(f"  ~ {f['rel_path']}：{f['old_size']}B → {f['new_size']}B")
    return 0


# ---------------------------------------------------------------- 解析器
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m time_arch", description="时光考古机无头命令行（只读扫描与分析）")
    parser.add_argument("--db", help="数据库路径（默认 %%LOCALAPPDATA%%\\TimeArchaeologist"
                                     "\\time_arch.db 或 TIME_ARCH_DB）")
    parser.add_argument("--version", action="version", version=f"Time Archaeologist {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("projects", help="列出已登记项目")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("scan", help="登记并扫描一个目录")
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--no-analyze", action="store_true", help="只扫描，不做成果启发式")
    p.add_argument("--progress", action="store_true", help="每秒输出一行扫描进度到 stderr")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("analyze", help="对最新（或指定）快照运行 2.0 分析")
    p.add_argument("project")
    p.add_argument("--snapshot", type=int)
    p.add_argument("--only", default="relations,leads,dossiers,tasks,phases",
                   help="逗号分隔：relations,leads,dossiers,tasks,phases")
    p.add_argument("--persist-milestones", action="store_true",
                   help="把机器建议的里程碑写入数据库（默认只统计不写库）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("report", help="导出 v0.1.0 项目考古报告")
    p.add_argument("project")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("leads", help="列出调查线索或变更其状态")
    p.add_argument("project")
    p.add_argument("--status", choices=["open", "acknowledged", "dismissed"])
    p.add_argument("--rule")
    p.add_argument("--set-status", nargs=2, metavar=("ID", "STATUS"),
                   help="变更单条线索状态（人工操作）：open/acknowledged/dismissed")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_leads)

    p = sub.add_parser("dossiers", help="列出成果档案")
    p.add_argument("project")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dossiers)

    p = sub.add_parser("repro", help="运行实验复现就绪度检查")
    p.add_argument("project")
    p.add_argument("target", help="项目内相对路径（通常是运行目录中的结果文件）")
    p.add_argument("--snapshot", type=int)
    p.add_argument("--export", help="把 Markdown 报告写入该路径")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_repro)

    p = sub.add_parser("tasks", help="列出/导出调查任务，或人工变更状态")
    p.add_argument("project")
    p.add_argument("--status", choices=["open", "done", "dismissed"])
    p.add_argument("--export", help="导出 Markdown 任务清单到该路径")
    p.add_argument("--complete", type=int, metavar="ID",
                   help="人工确认完成（仅由你本人确认，系统不执行任务）")
    p.add_argument("--reopen", type=int, metavar="ID", help="重新打开任务")
    p.add_argument("--dismiss", type=int, metavar="ID", help="忽略任务")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("timeline", help="导出演化时间线 Markdown")
    p.add_argument("project")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("forensics", help="导出完整取证报告（档案/线索/任务/复现/阶段）")
    p.add_argument("project")
    p.add_argument("-o", "--out", help="默认输出到标准输出")
    p.add_argument("--snapshot", type=int)
    p.set_defaults(func=cmd_forensics)

    p = sub.add_parser("maintain", help="数据库维护：完整性检查/在线备份/压缩")
    p.add_argument("--integrity", action="store_true")
    p.add_argument("--backup", metavar="PATH")
    p.add_argument("--vacuum", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_maintain)

    p = sub.add_parser("diff", help="对比最近两次完成的快照")
    p.add_argument("project")
    p.add_argument("--snapshot", type=int, help="以该快照为“新”端（默认最新）")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_diff)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    db = Database(args.db or default_db_path())
    try:
        return args.func(db, args)
    except CliError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    except Exception as e:  # 底层异常转为可读错误，避免堆栈淹没输出
        print(f"错误：{type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
