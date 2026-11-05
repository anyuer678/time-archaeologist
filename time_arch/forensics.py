"""2.0 完整取证报告：把成果档案、调查线索、任务、复现检查、阶段与里程碑
汇总为一份可提交的 Markdown。

内容全部来自库内记录与既有纯函数推导，不新增推断；明确区分
观察事实 / 启发式判断 / 人工确认，并声明 mtime ≠ 真实工作时长。
"""
from __future__ import annotations

from .classify import CATEGORY_LABELS
from .db import Database
from .dossier import ROLE_LABELS, dossier_view
from .investigate import lead_view
from .phases import suggest_phases
from .repro import ITEM_WEIGHTS, LEVEL_LABELS, LEVEL_POINTS, load_repro_check, scoring_rules_text
from .tasks import TASK_STATUS, task_view
from .util import fmt_size, fmt_time_local, utcnow_iso

ARTIFACT_STATUS = {"auto": "自动判断", "confirmed": "人工确认", "rejected": "人工否决"}
LEAD_STATUS = {"open": "待查看", "acknowledged": "已知悉", "dismissed": "已忽略"}
COMPLETENESS_STATE = {"present": "有直接证据", "inferred": "推测存在",
                      "missing": "未发现（待调查）"}

DISCLAIMER = (
    "> 诚实边界：文件修改时间(mtime)是文件系统时间戳，不代表真实工作时长；"
    "自动判断均为启发式，标注「推测/建议」的内容需人工核实；缺失证据只表示"
    "未在快照中发现，不判定项目失败；复现检查的分数只衡量证据覆盖度，"
    "不代表实验成功或失败。"
)


def _bullet(items, empty: str = "（无）") -> list[str]:
    return [f"- {x}" for x in items] if items else [f"- {empty}"]


def build_forensics_markdown(db: Database, project_id: int,
                             snapshot_id: int | None = None) -> str:
    project = db.get_project(project_id)
    if project is None:
        raise ValueError(f"项目不存在: {project_id}")
    latest = None
    if snapshot_id is not None:
        for s in db.list_snapshots(project_id):
            if s["id"] == snapshot_id:
                latest = s
                break
        if latest is None:
            raise ValueError(f"快照 #{snapshot_id} 不存在或不属于该项目")
    else:
        latest = db.latest_snapshot(project_id)

    lines: list[str] = [f"# 项目取证报告：{project['name']}", ""]
    lines.append(f"- 项目路径：`{project['path']}`")
    lines.append(f"- 报告生成：{utcnow_iso()} (UTC)")
    lines.append(f"- 完成扫描快照数：{len(db.list_snapshots(project_id))}")
    if latest is not None:
        lines.append(f"- 报告快照：#{latest['id']}（{fmt_time_local(latest['created_at'])}）")
    else:
        lines.append("- 报告快照：尚无完成的扫描快照")
    lines.append("")
    lines.append(DISCLAIMER)
    lines.append("")

    lines += ["## 一、项目画像（报告快照）", ""]
    if latest is not None:
        st = db.snapshot_stats(latest["id"])
        lines.append(f"- 文件总数：{st['files']}；总体积：{fmt_size(st['total_size'])}；"
                     f"读取错误：{st['errors']}")
        lines += ["", "| 类别 | 文件数 | 体积 |", "|---|---:|---:|"]
        from .classify import CATEGORY_LABELS
        for cat, n, sz, _last in st["by_category"]:
            lines.append(f"| {CATEGORY_LABELS.get(cat, cat)} | {n} | {fmt_size(sz)} |")
    else:
        lines.append("（尚无完成的扫描快照，请先执行扫描。）")
    lines.append("")

    # ---- 成果档案 ----
    artifacts = [v for v in (dossier_view(db, r["id"]) for r in db.list_artifacts(project_id))
                 if v is not None]
    lines += [f"## 二、成果档案（{len(artifacts)} 份）", ""]
    if artifacts:
        lines.append("档案由单文件启发式线索聚合而成；存在 .exe/.zip/report.md "
                     "不等于交付成功，缺失证据列为待调查项。")
        for v in artifacts:
            lines += ["", f"### [{v['kind_label']}] {v['name']}",
                      f"- 状态：{ARTIFACT_STATUS.get(v['status'], v['status'])}"
                      f" · 置信度：{v['confidence']}（启发式）",
                      f"- 主文件：`{v['primary_path']}`",
                      f"- 首次发现 {fmt_time_local(v['first_seen_at'])}"
                      f" · 最近出现 {fmt_time_local(v['last_seen_at'])}"
                      f" · 快照 #{v['snapshot_id']}",
                      f"- 识别依据：{v['basis']}", "", "**已验证事实（快照记录）**", ""]
            lines += _bullet(v["verified_facts"])
            lines += ["", "**尚未验证的假设**", ""]
            lines += _bullet(v["unverified_assumptions"])
            lines += ["", "**完整性检查**", "", "| 项目 | 状态 | 说明 |", "|---|---|---|"]
            for c in v["completeness"]:
                lines.append(f"| {c['item']} | {COMPLETENESS_STATE.get(c['state'], c['state'])} "
                             f"| {c['detail']} |")
            if not v["completeness"]:
                lines.append("| （无检查项） | - | - |")
            lines += ["", "**后续工作**", ""]
            lines += _bullet(v["next_steps"])
            lines += ["", "**关联文件**", ""]
            for f in v["files"]:
                size = fmt_size(f["size"]) if f["size"] is not None else "-"
                mark = "" if f["in_snapshot"] else "（快照中无记录）"
                lines.append(f"- [{ROLE_LABELS.get(f['role'], f['role'])}] "
                             f"`{f['rel_path']}`（{size}）{mark}")
            if not v["files"]:
                lines.append("- （无关联文件）")
    else:
        lines.append("（尚未生成成果档案：可在 GUI「成果证据」页或 CLI `analyze` 中生成。）")
    lines.append("")

    # ---- 调查线索 ----
    leads = db.list_leads(project_id, snapshot_id=latest["id"] if latest else None,
                          limit=10000) if latest else []
    open_n = sum(1 for r in leads if r["status"] == "open")
    lines += [f"## 三、调查线索（报告快照 {len(leads)} 条，待查看 {open_n} 条）", ""]
    if leads:
        lines.append("线索是「值得查看的问题」，不是结论；每条附证据与误报可能性。")
        for r in leads:
            v = lead_view(db, r["id"])
            if v is None:
                continue
            lines += ["", f"### [{LEAD_STATUS.get(v['status'], v['status'])}] "
                          f"{v['rule_label']}（v{v['rule_version']}）— {v['title']}",
                      f"- 目标：`{v['target_path'] or '—'}` · 快照 #{v['snapshot_id']}"
                      f" · 生成于 {fmt_time_local(v['created_at'])}",
                      f"- 触发原因：{v['basis']}", "", "**证据文件**", ""]
            lines += _bullet(v["evidence_list"])
            lines += ["", "**可能的解释**", ""]
            lines += _bullet(v["possible_explanations"])
            lines += ["", "**下一步建议**", ""]
            lines += _bullet(v["next_steps"])
            lines += ["", f"- 误报可能性：{v['false_positive'] or '（未提供）'}"]
    else:
        lines.append("（最新快照没有线索。线索需要先运行分析：GUI「调查工作台」或 CLI `analyze`。）")
    lines.append("")

    # ---- 调查任务 ----
    tasks = db.list_tasks(project_id, limit=10000)
    open_tasks = sum(1 for r in tasks if r["status"] == "open")
    lines += [f"## 四、调查任务（{len(tasks)} 项，待处理 {open_tasks} 项）", ""]
    if tasks:
        lines.append("「已完成」仅表示用户本人完成调查并确认结论；系统不执行任务、不伪造结果。")
        for r in tasks:
            v = task_view(db, r["id"])
            if v is None:
                continue
            origin = "人工添加" if v["origin"] == "user" else \
                (v["lead_rule_label"] or "由线索生成")
            lines += ["", f"### [{TASK_STATUS.get(v['status'], v['status'])}] {v['title']}"
                          f"（来源：{origin}）",
                      f"- 为什么调查：{v['why']}", "", "**已有证据**", ""]
            lines += _bullet(v["evidence_list"])
            lines += ["", "**人工操作**", ""]
            lines += _bullet(v["manual_steps_list"])
            lines += ["", f"- 完成条件：{v['done_criteria'] or '（未提供）'}"]
            if v["verify_command"]:
                lines.append(f"- 只读验证建议（不会自动执行）：`{v['verify_command']}`"
                             + (f" — {v['verify_desc']}" if v["verify_desc"] else ""))
            if v["completed_at"]:
                lines.append(f"- 人工确认完成于：{fmt_time_local(v['completed_at'])}")
    else:
        lines.append("（尚无任务。可在 GUI「调查任务」页生成，或 CLI `analyze`。）")
    lines.append("")

    # ---- 复现检查 ----
    checks = db.list_repro_checks(project_id, latest["id"], limit=200) if latest else []
    lines += [f"## 五、复现检查（报告快照 {len(checks)} 条）", ""]
    if checks:
        for row in checks:
            c = load_repro_check(db, row["id"])
            if c is None:
                continue
            lines += ["", f"### `{c['target_path']}` — "
                          f"{LEVEL_LABELS.get(c['level'], c['level'])} · 总分 {c['score']}/100",
                      f"- 生成于 {fmt_time_local(c['generated_at'])}", "",
                      "| 项目 | 等级 | 权重 | 得分 | 证据/说明 |",
                      "|---|---|---:|---:|---|"]
            for item in c["checks"]:
                pts = round(ITEM_WEIGHTS.get(item["item"], 0)
                            * LEVEL_POINTS.get(item["level"], 0), 1)
                ev = "；".join(item.get("evidence") or [])
                detail = item["detail"] + (f"（{ev}）" if ev else "")
                lines.append(f"| {item['item']} | {item['level']} | "
                             f"{ITEM_WEIGHTS.get(item['item'], '-')} | {pts} | {detail} |")
        lines += ["", "### 评分规则（完全公开）", ""]
        lines += [f"- {t}" for t in scoring_rules_text()]
    else:
        lines.append("（尚未运行复现检查。可在 GUI「复现检查」页或 CLI `repro` 中运行。）")
    lines.append("")

    # ---- 阶段与里程碑 ----
    phases = suggest_phases(db, project_id)
    milestones = db.list_milestones(project_id)
    lines += ["## 六、演化阶段建议与里程碑", "",
              "阶段模型（需求→设计→…→发布）只是可选分析框架；以下为机器建议，不等于事实。", ""]
    seg_lines = []
    for ph in phases:
        for seg in ph["segments"]:
            seg_lines.append(f"- **{ph['label']}**：快照 #{seg['start_snapshot']} → "
                             f"#{seg['end_snapshot']}（证据 {seg['evidence_count']} 项，"
                             f"置信度 {seg['confidence']}）— {ph['description']}")
    lines += seg_lines or ["（尚无阶段证据。阶段建议至少需要一次完成的扫描。）"]
    lines += ["", "### 里程碑", ""]
    if milestones:
        origin_labels = {"machine": "机器建议", "user": "人工添加"}
        status_labels = {"suggested": "待确认", "confirmed": "已确认"}
        for m in milestones:
            origin = origin_labels.get(m["origin"], m["origin"])
            status = status_labels.get(m["status"], m["status"])
            snap = f"快照 #{m['snapshot_id']}" if m["snapshot_id"] else "未锚定快照"
            lines.append(f"- [{origin}·{status}] {m['title']}（{snap}）"
                         + (f" — {m['note']}" if m["note"] else ""))
    else:
        lines.append("（尚无已保存的里程碑；可在 GUI「演化阶段」页保存机器建议。）")
    lines.append("")
    return "\n".join(lines) + "\n"


def export_forensics_markdown(db: Database, project_id: int, path: str,
                              snapshot_id: int | None = None) -> str:
    """写入 Markdown 取证报告并返回路径。"""
    content = build_forensics_markdown(db, project_id, snapshot_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
