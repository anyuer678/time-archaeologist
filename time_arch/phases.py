"""演化阶段与里程碑建议（2.0 第五步）。

铁律：
- 「需求→设计→…→发布」只是可选分析模型，不强行套用到所有项目；
  证据不足的阶段不生成建议。
- 阶段边界 = 扫描快照（观察时点），不是日历时间；不把文件修改时间解释为工作时长。
- 机器建议即时计算不入库；人工确认/修正写入 phases 表，二者严格分开。
- 发布物/构建产物的出现是观察事实，不构成「发布成功」或「项目完成」的判断。

公开置信度规则（每段阶段的依据可在 evidence 中逐项查看）：
- high  = 结构性证据：运行目录同时含参数与输出、发布目录中的产物。
- medium = 单文件类别/命名启发（类别本身来自扩展名与路径启发式）。
"""
from __future__ import annotations

import json
import re

from .db import Database
from .dossier import _METRICS_FILE_RE, _TEST_REPORT_RE, run_dir_of
from .graph import _RELEASE_DIR_RE_ISH
from .util import fmt_time_local, utcnow_iso

MAX_EVIDENCE_SAMPLES = 5

PHASE_DEFS = [
    ("requirements", "需求形成", "需求/提案/规格类文档出现"),
    ("design", "设计", "设计方案/架构类文档出现"),
    ("implementation", "实现", "源码文件出现"),
    ("testing", "测试", "测试代码或测试结果出现"),
    ("experiment", "实验", "实验运行目录出现"),
    ("output", "产出", "构建产物或指标结果出现"),
    ("release", "发布", "发布目录/发布包出现（观察事实，不代表发布成功）"),
]

_REQUIREMENT_RE = re.compile(r"(需求|requirement|proposal|rfc)", re.IGNORECASE)
_DESIGN_RE = re.compile(r"(设计|design|方案|架构|architecture|spec)", re.IGNORECASE)
_ARCHIVE_EXTS = (".zip", ".whl", ".tar.gz", ".tgz", ".exe", ".msi", ".apk", ".dmg")


def _norm(p: str) -> str:
    return str(p).replace("\\", "/")


def _snapshot_files(db: Database, snapshot_id: int) -> list[dict]:
    rows = db.query(
        "SELECT rel_path, category, read_error FROM file_records WHERE snapshot_id=?",
        (snapshot_id,))
    return [{"rel_path": _norm(r["rel_path"]), "category": r["category"]}
            for r in rows
            if not r["read_error"] and ".git" not in _norm(r["rel_path"]).split("/")]


def _run_dirs(files: list[dict]) -> dict[str, list[dict]]:
    runs: dict[str, list[dict]] = {}
    for f in files:
        rd = run_dir_of(f["rel_path"])
        if rd:
            runs.setdefault(rd, []).append(f)
    return runs


def _phase_evidence(files: list[dict], key: str) -> tuple[list[str], bool]:
    """返回 (证据路径样本, 是否结构性证据 direct)。路径数 ≤ MAX_EVIDENCE_SAMPLES。"""
    paths: list[str] = []
    direct = False

    def add(p: str) -> None:
        if len(paths) < MAX_EVIDENCE_SAMPLES and p not in paths:
            paths.append(p)

    if key == "requirements":
        for f in files:
            if f["category"] == "doc" and _REQUIREMENT_RE.search(f["rel_path"]):
                add(f["rel_path"])
    elif key == "design":
        for f in files:
            if f["category"] == "doc" and _DESIGN_RE.search(f["rel_path"]):
                add(f["rel_path"])
    elif key == "implementation":
        for f in files:
            if f["category"] == "source":
                add(f["rel_path"])
    elif key == "testing":
        for f in files:
            if _TEST_REPORT_RE.search(f["rel_path"]):
                add(f["rel_path"])
        for f in files:
            if f["category"] == "test":
                add(f["rel_path"])
    elif key == "experiment":
        for rd, members in sorted(_run_dirs(files).items()):
            outputs = [m for m in members if m["category"] in ("data", "build")
                       or _METRICS_FILE_RE.search(m["rel_path"])]
            # 指标类命名的文件类别是 config（json→config 怪癖），不能算参数记录
            params = [m for m in members if m["category"] == "config"
                      and not _METRICS_FILE_RE.search(m["rel_path"])]
            if outputs:
                add(f"{rd}/（{len(outputs)} 个输出文件）")
            if outputs and params:
                direct = True
    elif key == "output":
        for f in files:
            if f["category"] == "build" or _METRICS_FILE_RE.search(f["rel_path"]):
                add(f["rel_path"])
    elif key == "release":
        for f in files:
            name = f["rel_path"].rsplit("/", 1)[-1].lower()
            if _RELEASE_DIR_RE_ISH(f["rel_path"]):
                add(f["rel_path"])
                direct = True
            elif name.endswith(_ARCHIVE_EXTS):
                add(f["rel_path"])
    return paths, direct


def suggest_phases(db: Database, project_id: int) -> list[dict]:
    """按快照序列推导各阶段的建议区间（纯函数，不写库）。

    区间 = 连续「有该阶段证据」的快照段；边界均为快照号（观察时点），
    不是日历时间，也不代表工作时长。
    """
    snaps = list(reversed(db.list_snapshots(project_id)))  # 旧 → 新
    if not snaps:
        return []
    files_by_snap = {s["id"]: _snapshot_files(db, s["id"]) for s in snaps}
    result = []
    for key, label, desc in PHASE_DEFS:
        segments: list[dict] = []
        start: int | None = None
        last: int | None = None
        count = 0
        direct_any = False
        samples: list[str] = []

        def close(end_id: int | None) -> None:
            nonlocal start, last, count, direct_any, samples
            if start is not None:
                segments.append({
                    "start_snapshot": start, "end_snapshot": end_id or last,
                    "evidence_count": count,
                    "evidence_samples": samples[:MAX_EVIDENCE_SAMPLES],
                    "confidence": "high" if direct_any else "medium",
                })
            start = last = None
            count = 0
            direct_any = False
            samples = []

        for s in snaps:
            paths, direct = _phase_evidence(files_by_snap[s["id"]], key)
            if paths:
                if start is None:
                    start = s["id"]
                    samples = []
                last = s["id"]
                count += len(paths)
                direct_any = direct_any or direct
                for p in paths:
                    if p not in samples and len(samples) < MAX_EVIDENCE_SAMPLES:
                        samples.append(p)
            else:
                close(last)
        close(last)
        result.append({"key": key, "label": label, "description": desc,
                       "segments": segments})
    return result


def suggest_milestones(db: Database, project_id: int) -> list[dict]:
    """按「首次出现」推导里程碑候选（机器建议，不写库）。"""
    snaps = list(reversed(db.list_snapshots(project_id)))
    found: dict[str, dict | None] = {
        "首个测试结果报告出现": None,
        "首个含参数与输出的完整实验运行": None,
        "首个构建产物出现": None,
        "首个发布物出现（观察事实，不代表发布成功）": None,
    }
    for s in snaps:
        files = _snapshot_files(db, s["id"])
        if found["首个测试结果报告出现"] is None:
            hits = [f["rel_path"] for f in files if _TEST_REPORT_RE.search(f["rel_path"])]
            if hits:
                found["首个测试结果报告出现"] = {
                    "snapshot_id": s["id"], "evidence": hits,
                    "note": "存在测试结果报告文件（命名启发，是否真的通过需人工查看）"}
        if found["首个含参数与输出的完整实验运行"] is None:
            for rd, members in sorted(_run_dirs(files).items()):
                outputs = [m for m in members if m["category"] in ("data", "build")
                           or _METRICS_FILE_RE.search(m["rel_path"])]
                # 指标类命名的文件类别是 config（json→config 怪癖），不能算参数记录
                params = [m for m in members if m["category"] == "config"
                          and not _METRICS_FILE_RE.search(m["rel_path"])]
                if outputs and params:
                    found["首个含参数与输出的完整实验运行"] = {
                        "snapshot_id": s["id"],
                        "evidence": [m["rel_path"] for m in (params + outputs)],
                        "note": f"运行目录 {rd}/ 同时含参数记录与输出文件（结构证据）"}
                    break
        if found["首个构建产物出现"] is None:
            hits = [f["rel_path"] for f in files if f["category"] == "build"]
            if hits:
                found["首个构建产物出现"] = {
                    "snapshot_id": s["id"], "evidence": hits,
                    "note": "构建/依赖产物目录中的文件（类别启发，未验证可用性）"}
        if found["首个发布物出现（观察事实，不代表发布成功）"] is None:
            hits = [f["rel_path"] for f in files
                    if _RELEASE_DIR_RE_ISH(f["rel_path"])
                    or f["rel_path"].rsplit("/", 1)[-1].lower().endswith(_ARCHIVE_EXTS)]
            if hits:
                found["首个发布物出现（观察事实，不代表发布成功）"] = {
                    "snapshot_id": s["id"], "evidence": hits,
                    "note": "发布目录或压缩包出现，仅为观察事实"}
    return [dict(title=t, **v) for t, v in found.items() if v]


def persist_milestone_suggestions(db: Database, project_id: int) -> int:
    """把机器建议幂等写入 milestones 表，返回新建条数。已确认条目不受影响。"""
    now = utcnow_iso()
    created = 0
    for m in suggest_milestones(db, project_id):
        if db.upsert_milestone(project_id, m["snapshot_id"], m["title"], m["note"],
                               json.dumps(m["evidence"], ensure_ascii=False), now,
                               origin="machine"):
            created += 1
    return created


MILESTONE_STATUS = {"suggested": "机器建议", "confirmed": "已确认"}


def export_timeline_markdown(db: Database, project_id: int, path: str) -> dict:
    """导出 Markdown 演化时间线报告。内容全部来自库内记录与纯函数推导。"""
    project = db.get_project(project_id)
    if project is None:
        raise ValueError(f"项目不存在: {project_id}")
    snaps = list(reversed(db.list_snapshots(project_id)))
    phases = suggest_phases(db, project_id)
    stored_phases = db.list_phases(project_id)
    milestones = db.list_milestones(project_id)

    lines = [
        f"# 项目演化时间线报告 — {project['name']}",
        "",
        f"- 生成时间：{fmt_time_local(utcnow_iso())}",
        f"- 项目路径：{project['path']}",
        f"- 完成扫描快照数：{len(snaps)}",
        "",
        "> 声明：文件系统记录只能还原部分历史，无法完整还原真实工作过程；"
        "文件修改时间不代表工作时长，文件数量不代表项目价值。"
        "阶段模型（需求→设计→…→发布）只是可选分析框架，机器建议不等于事实。",
        "",
        "## 快照概览",
        "",
        "| 快照 | 扫描时间 | 文件数 | 事件数 |",
        "|---|---|---:|---:|",
    ]
    for s in snaps:
        lines.append(f"| #{s['id']} | {fmt_time_local(s['created_at'])} | "
                     f"{s['file_count']} | {s['event_count']} |")

    lines += ["", "## 机器阶段建议（每段依据可逐项查看）", ""]
    seg_total = 0
    for ph in phases:
        if not ph["segments"]:
            continue
        lines.append(f"### {ph['label']}（{ph['description']}）")
        for seg in ph["segments"]:
            seg_total += 1
            lines.append(
                f"- 快照 #{seg['start_snapshot']} → #{seg['end_snapshot']}"
                f" · 置信度 {seg['confidence']} · 证据 {seg['evidence_count']} 项")
            for e in seg["evidence_samples"]:
                lines.append(f"  - {e}")
        lines.append("")
    if seg_total == 0:
        lines += ["（证据不足，未生成任何阶段建议。）", ""]

    lines += ["## 人工确认 / 自定义阶段", ""]
    if stored_phases:
        for p in stored_phases:
            src = "人工确认" if p["source"] == "confirmed" else "人工添加"
            rng = f"快照 #{p['start_snapshot']} → #{p['end_snapshot']}" \
                if p["end_snapshot"] else f"自快照 #{p['start_snapshot']}"
            lines.append(f"- [{src}] {p['label']} · {rng}"
                         + (f" · {p['note']}" if p["note"] else ""))
    else:
        lines.append("（尚无人工确认或自定义阶段。）")
    lines.append("")

    lines += ["## 里程碑", ""]
    if milestones:
        for m in milestones:
            origin = "机器建议" if m["origin"] == "machine" else "人工添加"
            status = MILESTONE_STATUS.get(m["status"], m["status"])
            snap = f"快照 #{m['snapshot_id']}" if m["snapshot_id"] else "未锚定快照"
            lines.append(f"### [{origin}·{status}] {m['title']}（{snap}）")
            if m["note"]:
                lines.append(f"- 说明：{m['note']}")
            try:
                ev = json.loads(m["evidence"] or "[]")
            except (TypeError, ValueError):
                ev = []
            for e in ev[:MAX_EVIDENCE_SAMPLES]:
                lines.append(f"- 证据：{e}")
            lines.append("")
    else:
        lines += ["（尚无里程碑。）", ""]

    lines += ["## 阶段间变化（机器建议段的首→末快照对比）", ""]
    diff_total = 0
    for ph in phases:
        for seg in ph["segments"]:
            a, b = seg["start_snapshot"], seg["end_snapshot"]
            if a == b or a is None or b is None:
                continue
            d = db.compare_snapshots(a, b, limit=20)
            ca, cr, cm = d["counts"]
            diff_total += 1
            lines.append(f"### {ph['label']}：#{a} → #{b}"
                         f" — 新增 {ca} · 删除 {cr} · 修改 {cm}")
            for f in (d["added"] + d["modified"])[:20]:
                lines.append(f"- {f['rel_path']}")
            lines.append("")
    if diff_total == 0:
        lines += ["（没有可对比的多快照阶段段。）", ""]

    latest = db.latest_snapshot(project_id)
    if latest is not None:
        open_leads = [r for r in db.list_leads(project_id, snapshot_id=latest["id"])
                      if r["status"] == "open"]
        lines += ["## 遗留问题（最新快照的待查看线索）", ""]
        if open_leads:
            for r in open_leads[:20]:
                lines.append(f"- {r['title']}")
        else:
            lines.append("（最新快照没有待查看线索。）")
        lines.append("")

    text = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return {"path": path, "snapshots": len(snaps), "segments": seg_total,
            "milestones": len(milestones)}
