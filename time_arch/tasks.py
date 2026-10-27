"""调查任务生成（2.0 第六步）：把真实调查线索转成可执行的调查清单。

铁律：
- 任务必须来自真实线索（leads 表），并保留与线索/证据的关联（lead_id、证据列表）。
- 每个任务包含：为什么需要调查（why）、当前已有的证据（evidence）、
  需要人工执行的操作（manual_steps）、完成条件（done_criteria）、
  可选的只读验证命令（verify_command + verify_desc）。
- 验证命令只是给用户的建议文本，应用自身永不执行任何命令；
  命令只允许只读操作（查看/哈希/查询），不允许安装、运行项目程序或修改文件。
- 只有用户确认才把任务标记完成：set_task_status('done') 仅由 UI 用户操作调用，
  生成/更新任务永远不会写 done，也不伪造任何执行结果。
- 幂等：同一 (task_key, target_key) 重跑只更新内容，保留人工状态；
  已忽略（dismissed）的线索不再生成任务，已存在的任务不会因线索消失被自动删除。
- 措辞只陈述观察与缺失，不判定项目失败或停滞（尤其 doc_config_only_changes）。

扩展方式：新增规则的任务生成器只需在 TASK_GENERATORS 注册一个
fn(lead) -> task dict；未注册的规则走通用生成器（从线索字段直接派生），
保证新规则无需修改本模块即可产出任务。
"""
from __future__ import annotations

import json

from .db import Database
from .investigate import RULE_LABELS, lead_view
from .util import utcnow_iso

TASK_STATUS = {"open": "待处理", "done": "已完成（人工确认）", "dismissed": "已忽略"}


def _task(task_key: str, target_key: str, title: str, why: str,
          evidence: list, manual_steps: list, done_criteria: str,
          verify_command: str = "", verify_desc: str = "") -> dict:
    return {
        "task_key": task_key, "target_key": target_key, "title": title,
        "why": why, "evidence": evidence, "manual_steps": manual_steps,
        "done_criteria": done_criteria,
        "verify_command": verify_command, "verify_desc": verify_desc,
    }


def _task_result_without_input(lead: dict) -> dict:
    rd = lead["target_path"]
    return _task(
        "result_without_input", rd,
        f"调查 {rd}/ 结果文件的生成方式并补齐参数/输入记录",
        lead["basis"] + " 缺少参数与输入记录时，无法确认结果由哪次运行产生、是否可复现。",
        lead["evidence_list"],
        [
            f"回忆或检查 {rd}/ 中结果文件的生成方式（脚本、命令行参数、外部工具）",
            "若为程序生成：把实际使用的参数保存为配置文件，把输入数据放入运行目录或记录其来源路径",
            "若为手工放置/复制：在运行目录补一个来源说明文件，注明来源与用途",
        ],
        f"{rd}/ 内出现参数记录与输入数据（或来源说明），或人工确认结果并非程序生成并已注明",
        verify_desc="参数记录是否与实际运行一致需人工判断，没有通用只读命令可自动验证")


def _task_test_without_result(lead: dict) -> dict:
    return _task(
        "test_without_result", lead["target_path"],
        "运行一次测试并保留可识别的结果报告",
        lead["basis"] + " 测试存在但没有结果报告时，无法区分「从未运行」与「运行了但没留证据」。",
        lead["evidence_list"],
        [
            "人工运行一次测试（如 pytest），这是执行操作，由用户自行决定与执行",
            "保留标准命名的结果报告（如 pytest --junitxml=reports/pytest.xml 或 coverage 报告）",
            "确认项目中是否存在非标准命名的测试结果文件（有的话无需补生成）",
        ],
        "项目内出现可识别的测试结果报告文件（junit/coverage/pytest 报告等特征）",
        verify_desc="运行测试属于执行操作，本系统不代为执行；报告是否出现可在下次扫描后于成果证据中查看")


def _task_error_but_complete_result(lead: dict) -> dict:
    rd = lead["target_path"]
    return _task(
        "error_but_complete_result", rd,
        f"核对 {rd}/ 中结果文件与日志错误的关系",
        lead["basis"] + " 若程序在写出全部结果前异常终止，直接使用这些结果可能得出错误结论。",
        lead["evidence_list"],
        [
            "打开日志，按时间顺序确认错误发生在结果写出之前还是之后",
            "核对结果文件的规模（行数/样本量/文件大小）与日志记录是否一致",
            "若确认残缺：把残缺文件移出运行目录或重命名标注，再决定是否重新生成",
        ],
        "对「日志错误与结果文件的关系」有明确结论（完好或残缺），残缺的已标注或移出",
        verify_command=(f"Get-Content {rd}/<日志名> -Tail 50（PowerShell）或 "
                        f"tail -n 50 {rd}/<日志名>（macOS/Linux）"),
        verify_desc="示例命令只读查看日志末尾，用于人工核对；不会自动执行")


def _task_duplicate_results(lead: dict) -> dict:
    return _task(
        "duplicate_results", lead["target_path"],
        "确认重复结果文件的权威版本",
        lead["basis"] + " 关系不明时，后续分析可能把副本当成独立成果，或漏掉真正的权威版本。",
        lead["evidence_list"],
        [
            "逐份比对内容与修改来源（同一次运行的副本，还是不同时间重复产生）",
            "指定权威版本，并在项目清单/说明文件中记录",
            "其余副本按需归档；如需删除请先自行备份，本系统不做任何删除",
        ],
        "每组重复文件都有明确的权威版本记录，或已说明各副本的用途",
        verify_command="certutil -hashfile <文件> SHA256（Windows）或 shasum -a 256 <文件>（macOS/Linux）",
        verify_desc="示例命令只读计算哈希，用于人工确认内容是否一致")


def _task_doc_claims_without_evidence(lead: dict) -> dict:
    return _task(
        "doc_claims_without_evidence", lead["target_path"],
        "逐条核实文档中的完成性声称",
        lead["basis"] + " 声称与实际不符时，会误导后续使用（包括未来的自己）。",
        lead["evidence_list"],
        [
            "打开文档，列出所有「已完成/已实现/已通过」类表述及其所在行",
            "对每条表述人工核实：是否有对应实现与可验证证据",
            "已属实的：补齐证据文件（测试报告/产物）；未属实的：把表述改为计划/TODO 语气",
        ],
        "文档中每条完成性声称都有对应证据文件，或表述已修正为非完成语气",
        verify_desc="声称是否属实需要对照实现逐条人工核实，无法用命令自动判断")


def _task_dependency_conflict(lead: dict) -> dict:
    pkg = lead["target_path"]
    return _task(
        "dependency_conflict", pkg,
        f"统一依赖 {pkg} 的版本声明",
        lead["basis"] + " 版本不明时，复现环境与原始运行环境可能不一致，结果不可比。",
        lead["evidence_list"],
        [
            f"用 pip show {pkg} 查看当前环境实际安装的版本",
            "决定权威版本（通常以实际运行过的环境为准）",
            "统一所有声明文件中的版本；如差异是有意的（开发/生产），在文件中注明环境用途",
        ],
        f"同一包 {pkg} 在所有声明文件中的版本一致，或差异已有明确的环境说明",
        verify_command=f"pip show {pkg}",
        verify_desc="只读查看当前安装版本，不安装、不升级任何包")


def _task_result_changed_without_record(lead: dict) -> dict:
    p = lead["target_path"]
    return _task(
        "result_changed_without_record", p,
        f"解释结果文件 {_name_of(p)} 的变化来源",
        lead["basis"] + " 无法解释的结果变化会让该文件的可信度存疑。",
        lead["evidence_list"],
        [
            "确认这次变化来自重新运行、人工编辑还是外部工具",
            "若为运行产生：之后运行时保留日志或命令记录",
            "若为人工编辑：在附近补一个说明，或把人工版本另存并注明",
        ],
        "变化来源已有结论，且相应目录内出现可解释该次变化的记录（日志/说明）",
        verify_desc="变化来源只能由使用者回忆确认，命令无法判断")


def _task_doc_config_only_changes(lead: dict) -> dict:
    return _task(
        "doc_config_only_changes", "",
        "确认最近两次扫描间的工作性质与扫描覆盖",
        lead["basis"] + " 本任务只用于确认工作性质与扫描覆盖范围，"
                        "不构成对项目进展或停滞的任何判断。",
        lead["evidence_list"],
        [
            "回顾两次快照之间实际做过的工作",
            "检查扫描忽略规则是否把代码/数据目录排除在外",
            "若存在未落盘的工作（讨论/规划等），无需补救，可选择性补记结论",
        ],
        "对该阶段的工作性质有明确认识；如发现扫描范围遗漏，已调整扫描配置",
        verify_desc="观察性任务：是否需要行动由人工判断")


def _name_of(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1] if path else "(项目根)"


def _task_generic(lead: dict) -> dict:
    """未注册规则的通用生成器：直接从线索字段派生，保证新规则自动产出任务。"""
    return _task(
        lead["rule_id"] or "generic", lead["target_path"],
        f"调查：{lead['title']}",
        lead["basis"],
        lead["evidence_list"],
        lead["next_steps"] or ["人工查看该线索并给出结论"],
        "线索所述问题得到人工解释或处理，并在任务备注中记录结论",
        verify_desc="通用任务：验证方式取决于具体调查内容，由人工决定")


# 可扩展注册点：rule_id -> 生成器；未注册的规则走 _task_generic
TASK_GENERATORS = {
    "result_without_input": _task_result_without_input,
    "test_without_result": _task_test_without_result,
    "error_but_complete_result": _task_error_but_complete_result,
    "duplicate_results": _task_duplicate_results,
    "doc_claims_without_evidence": _task_doc_claims_without_evidence,
    "dependency_conflict": _task_dependency_conflict,
    "result_changed_without_record": _task_result_changed_without_record,
    "doc_config_only_changes": _task_doc_config_only_changes,
}


def get_generator(rule_id: str):
    return TASK_GENERATORS.get(rule_id, _task_generic)


def generate_tasks(db: Database, project_id: int, snapshot_id: int | None = None) -> dict:
    """从指定（默认最新）快照的线索生成任务，幂等。

    跳过已忽略（dismissed）的线索；已存在的任务只更新内容并保留人工状态。
    返回 {snapshot_id, created, updated, total}。
    """
    if snapshot_id is None:
        snap = db.latest_snapshot(project_id)
        if snap is None:
            return {"snapshot_id": None, "created": 0, "updated": 0,
                    "total": len(db.list_tasks(project_id))}
        snapshot_id = snap["id"]
    now = utcnow_iso()
    # status 恒非 NULL（默认 'open'），!= dismissed 安全
    rows = db.query(
        "SELECT id FROM leads WHERE project_id=? AND snapshot_id=? AND status!='dismissed' "
        "ORDER BY id", (project_id, snapshot_id))
    created = updated = 0
    for row in rows:
        lead = lead_view(db, row["id"])
        if not lead:
            continue
        t = get_generator(lead["rule_id"])(lead)
        _, is_new = db.upsert_task(
            project_id, lead["id"], t["task_key"], t["target_key"], t["title"],
            t["why"], json.dumps(t["evidence"], ensure_ascii=False),
            json.dumps(t["manual_steps"], ensure_ascii=False), t["done_criteria"],
            t["verify_command"], t["verify_desc"], now)
        created += 1 if is_new else 0
        updated += 0 if is_new else 1
    return {"snapshot_id": snapshot_id, "created": created, "updated": updated,
            "total": len(db.list_tasks(project_id))}


def task_view(db: Database, task_id: int) -> dict | None:
    """任务全量视图（JSON 已解析 + 关联线索摘要 + 线索是否仍来自最新快照）。"""
    rows = db.query("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        return None
    r = dict(rows[0])

    def _loads(text: str, default):
        try:
            return json.loads(text or "") if text else default
        except (TypeError, ValueError):
            return default

    r["evidence_list"] = _loads(r.get("evidence"), [])
    r["manual_steps_list"] = _loads(r.get("manual_steps"), [])
    r["status_label"] = TASK_STATUS.get(r["status"], r["status"])
    r["lead_title"] = ""
    r["lead_rule_label"] = ""
    r["lead_current"] = None  # None=无关联线索；True/False=关联线索是否来自最新有线索的快照
    if r["lead_id"]:
        lrows = db.query(
            "SELECT title, rule_id, snapshot_id FROM leads WHERE id=?", (r["lead_id"],))
        if lrows:
            l = lrows[0]
            r["lead_title"] = l["title"]
            r["lead_rule_label"] = RULE_LABELS.get(l["rule_id"], l["rule_id"])
            m = db.query("SELECT MAX(snapshot_id) AS m FROM leads WHERE project_id=?",
                         (r["project_id"],))[0]["m"]
            r["lead_current"] = (l["snapshot_id"] == m) if m is not None else None
    return r


def export_tasks_markdown(db: Database, project_id: int, path: str) -> dict:
    """导出调查任务清单 Markdown（内容全部来自库内记录）。"""
    rows = db.list_tasks(project_id, limit=10000)
    lines = ["# 调查任务清单", "",
             "> 任务由真实调查线索生成，仅描述观察与缺失；完成状态由人工确认，"
             "系统不执行、不伪造任何结果。验证命令仅供参考，使用前请自行确认其只读性。", ""]
    open_n = sum(1 for r in rows if r["status"] == "open")
    lines.append(f"共 {len(rows)} 项任务，待处理 {open_n} 项。")
    lines.append("")
    for r in rows:
        t = task_view(db, r["id"]) or dict(r)
        lines.append(f"## {r['title']}")
        lines.append(f"- 状态：{t.get('status_label', r['status'])}"
                     f"{'（' + t['lead_rule_label'] + '）' if t.get('lead_rule_label') else ''}")
        if t.get("lead_current") is False:
            lines.append("- 注：关联线索在最新扫描中未再触发（问题可能已变化，结论以人工调查为准）")
        lines.append(f"- 为什么调查：{t['why']}")
        ev = t.get("evidence_list") or []
        if ev:
            lines.append("- 已有证据：")
            lines += [f"  - {e}" for e in ev]
        steps = t.get("manual_steps_list") or []
        if steps:
            lines.append("- 人工操作：")
            lines += [f"  {i}. {s}" for i, s in enumerate(steps, 1)]
        if r["done_criteria"]:
            lines.append(f"- 完成条件：{r['done_criteria']}")
        if r["verify_command"]:
            lines.append(f"- 验证命令（只读建议）：`{r['verify_command']}`")
            if r["verify_desc"]:
                lines.append(f"  - {r['verify_desc']}")
        if r["status"] == "done" and r["completed_at"]:
            lines.append(f"- 人工确认完成于：{r['completed_at']}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return {"path": path, "tasks": len(rows), "open": open_n}
