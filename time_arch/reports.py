"""项目考古报告导出：Markdown / JSON。

报告中严格区分：文件系统事实（快照记录）、启发式判断（findings，confidence）与
人工确认（status=confirmed/rejected），并显式声明 mtime ≠ 真实工作时间。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .analyze import KIND_LABELS
from .classify import CATEGORY_LABELS
from .db import Database

DISCLAIMER = (
    "> 声明：本报告由 Time Archaeologist 只读扫描生成。文件修改时间(mtime)是文件系统"
    "时间戳，不代表真实工作时间；标注“启发式”的结论是推测，需人工核实；“人工确认”"
    "条目为用户在界面中确认过的结论。"
)

EVENT_LABELS = {
    "file_added": "文件新增",
    "file_modified": "文件修改",
    "file_removed": "文件删除",
    "log_generated": "日志生成(推测)",
    "artifact_appeared": "产物出现(推测)",
}

CERTAINTY_LABELS = {"observed": "观察事实", "inferred": "推测"}


def _norm_path(rel: str) -> str:
    """报告内统一用正斜杠，便于跨平台阅读。"""
    return str(rel).replace("\\", "/")


def _collect(db: Database, project_id: int) -> dict:
    project = db.get_project(project_id)
    snapshots = db.list_snapshots(project_id)
    latest = db.latest_snapshot(project_id)
    stats = db.snapshot_stats(latest["id"]) if latest else {"files": 0, "total_size": 0, "errors": 0, "by_category": []}
    events = db.list_events(project_id=project_id, limit=500)
    findings = db.list_findings(project_id, limit=1000)
    prev = db.previous_completed_snapshot(project_id, latest["id"]) if latest else None
    diff = db.compare_snapshots(prev["id"], latest["id"], limit=500) if prev and latest else None
    return {
        "project": dict(project) if project else {},
        "snapshots": [dict(s) for s in snapshots],
        "latest_snapshot": dict(latest) if latest else None,
        "stats": stats, "events": [dict(e) for e in events],
        "findings": [dict(f) for f in findings], "diff": diff,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_markdown(db: Database, project_id: int) -> str:
    d = _collect(db, project_id)
    p = d["project"]
    lines: list[str] = []
    lines.append(f"# 项目考古报告：{p.get('name', '?')}")
    lines.append("")
    lines.append(f"- 项目路径：`{p.get('path', '?')}`")
    lines.append(f"- 登记时间：{p.get('created_at', '-')}")
    lines.append(f"- 最近扫描：{p.get('last_scan_at', '-')}")
    lines.append(f"- 快照数量：{len(d['snapshots'])}")
    lines.append(f"- 报告生成时间：{d['generated_at']} (UTC)")
    lines.append("")
    lines.append(DISCLAIMER)
    lines.append("")

    lines.append("## 一、项目画像（最近快照）")
    if d["latest_snapshot"]:
        st = d["stats"]
        lines.append(f"- 文件总数：{st['files']}，总体积：{st['total_size']} 字节，读取错误：{st['errors']}")
        lines.append("")
        lines.append("| 类别 | 文件数 | 体积(字节) |")
        lines.append("|---|---|---|")
        for cat, n, sz, _last in st["by_category"]:
            lines.append(f"| {CATEGORY_LABELS.get(cat, cat)} | {n} | {sz} |")
    else:
        lines.append("（尚无完成的扫描快照）")
    lines.append("")

    lines.append("## 二、快照历史")
    if d["snapshots"]:
        lines.append("| 快照 | 扫描时间(UTC) | 文件数 | 体积(字节) | 事件数 |")
        lines.append("|---|---|---|---|---|")
        for s in d["snapshots"]:
            lines.append(f"| #{s['id']} | {s['created_at']} | {s['file_count']} | {s['total_size']} | {s['event_count']} |")
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## 三、最近两次快照对比")
    if d["diff"]:
        added, removed, modified = d["diff"]["counts"]
        lines.append(f"新增 {added}，删除 {removed}，修改 {modified}。")
        if d["diff"]["added"]:
            lines.append("")
            lines.append("**新增文件（前 50）**：")
            lines.extend(f"- `{f['rel_path']}` ({f['size']} B)" for f in d["diff"]["added"][:50])
        if d["diff"]["removed"]:
            lines.append("")
            lines.append("**删除文件（前 50）**：")
            lines.extend(f"- `{f['rel_path']}`" for f in d["diff"]["removed"][:50])
        if d["diff"]["modified"]:
            lines.append("")
            lines.append("**修改文件（前 50）**：")
            lines.extend(
                f"- `{f['rel_path']}`：{f['old_size']}B → {f['new_size']}B"
                for f in d["diff"]["modified"][:50])
    else:
        lines.append("（不足两次快照，无法对比）")
    lines.append("")

    lines.append("## 四、活动时间线（最近 200 条）")
    if d["events"]:
        lines.append("| 观察时间(UTC) | 事件 | 确定性 | 文件 |")
        lines.append("|---|---|---|---|")
        for e in d["events"][:200]:
            label = EVENT_LABELS.get(e["event_type"], e["event_type"])
            cert = CERTAINTY_LABELS.get(e["certainty"], e["certainty"])
            lines.append(f"| {e['observed_at']} | {label} | {cert} | `{_norm_path(e['rel_path'])}` |")
    else:
        lines.append("（无事件）")
    lines.append("")

    lines.append("## 五、成果考古（启发式判断 + 人工确认状态）")
    if d["findings"]:
        status_labels = {"auto": "自动判断", "confirmed": "人工确认", "rejected": "人工否决"}
        lines.append("| 状态 | 类型 | 置信度 | 文件 | 依据 |")
        lines.append("|---|---|---|---|---|")
        for f in d["findings"]:
            kind = KIND_LABELS.get(f["kind"], f["kind"])
            st = status_labels.get(f["status"], f["status"])
            lines.append(f"| {st} | {kind} | {f['confidence']} | `{_norm_path(f['rel_path'])}` | {f['basis']} |")
    else:
        lines.append("（证据不足：未识别到潜在成果，或尚未扫描）")
    lines.append("")
    return "\n".join(lines) + "\n"


def build_json(db: Database, project_id: int) -> str:
    d = _collect(db, project_id)
    for f in d["findings"]:
        try:
            f["evidence"] = json.loads(f["evidence"])
        except (TypeError, ValueError):
            pass
    for e in d["events"]:
        try:
            e["evidence"] = json.loads(e["evidence"])
        except (TypeError, ValueError):
            pass
    d["meta"] = {"app": "Time Archaeologist", "disclaimer": DISCLAIMER}
    return json.dumps(d, ensure_ascii=False, indent=2, default=str)


def export_report(db: Database, project_id: int, fmt: str, out_path: str) -> str:
    """fmt: 'markdown' | 'json'。写入指定路径并返回该路径。"""
    content = build_markdown(db, project_id) if fmt == "markdown" else build_json(db, project_id)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path
