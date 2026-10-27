"""调查规则引擎（2.0 第四步）：从快照证据主动发现值得人工查看的问题。

铁律：
- 每条线索必须给出：证据文件（快照可溯源）、触发原因、可能的解释、
  下一步调查建议、误报可能性。线索只是「值得查看的问题」，不是结论。
- 规则可扩展：LEADS_RULES 注册表 (rule_id, version, fn, 说明)，新增规则
  不改核心代码；每条规则可单独启停（rule_config 表，默认启用）。
- 幂等：同快照同规则重跑结果一致；重跑只更新分析内容，保留人工状态。
- 只读：只使用快照 file_records / relations 与文本头部只读读取，不执行任何程序。
- 不做时间推断：不以「长期未修改」判定项目失败或停滞。
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict

from .db import Database
from .dossier import _METRICS_FILE_RE, _Files, _norm, _TEST_REPORT_RE, run_dir_of
from .repro import _ERROR_RE
from .util import read_text_head, utcnow_iso

MAX_LEADS_PER_RULE = 100
MAX_TEXT_DOCS = 200          # 内容类规则最多读取的文档数

_CLAIM_RE = re.compile(
    r"(已完成|已实现|已通过|全部通过|已修复|implemented|tests?\s+pass(?:ed)?|all\s+tests\s+pass)",
    re.IGNORECASE)
_PIN_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*?)\s*[~=]=\s*([^\s;#,\]]+)", re.MULTILINE)
_LOG_EXTS = {".log", ".out", ".err"}


def _name(rel_path: str) -> str:
    return _norm(rel_path).rsplit("/", 1)[-1]


def _stem(rel_path: str) -> str:
    n = _name(rel_path)
    return n.rsplit(".", 1)[0] if "." in n else n


def _short(sha: str | None) -> str:
    return (sha or "无哈希")[:12]


class _Ctx:
    """一次线索分析的共享上下文（构建一次，全部规则复用）。"""

    def __init__(self, db: Database, project_id: int, snapshot_id: int):
        rows = db.query(
            "SELECT rel_path, size, sha256, category, mtime, read_error FROM file_records "
            "WHERE snapshot_id=?", (snapshot_id,))
        # .git 内部文件（refs/logs/objects）不是项目成果，全部规则忽略
        rows = [r for r in rows if ".git" not in _norm(r["rel_path"]).split("/")]
        self.files = _Files(rows)
        self.runs: dict[str, list[dict]] = defaultdict(list)
        for f in self.files.by_path.values():
            rd = run_dir_of(f["rel_path"])
            if rd:
                self.runs[rd].append(f)
        edge_rows = db.query(
            "SELECT src_path, dst_path FROM relations WHERE project_id=? AND snapshot_id=?",
            (project_id, snapshot_id))
        self.edge_pairs: set[tuple[str, str]] = set()
        for r in edge_rows:
            self.edge_pairs.add((_norm(r["src_path"]), _norm(r["dst_path"])))
            self.edge_pairs.add((_norm(r["dst_path"]), _norm(r["src_path"])))
        self.prev_files: dict[str, dict] | None = self._load_prev(db, project_id, snapshot_id)

    @staticmethod
    def _load_prev(db: Database, project_id: int, snapshot_id: int) -> dict[str, dict] | None:
        snaps = db.list_snapshots(project_id)
        prev_id = None
        for s in snaps:  # list_snapshots 按新→旧排列
            if s["id"] < snapshot_id:
                prev_id = s["id"]
                break
        if prev_id is None:
            return None
        rows = db.query(
            "SELECT rel_path, sha256, category, read_error FROM file_records WHERE snapshot_id=?",
            (prev_id,))
        return {_norm(r["rel_path"]): {"sha256": r["sha256"], "category": r["category"]}
                for r in rows if not r["read_error"]}

    def is_output(self, f: dict) -> bool:
        """结果文件：构建产物 / 指标命名 / 运行目录内报告文档。"""
        if f["category"] == "build" or _METRICS_FILE_RE.search(f["rel_path"]):
            return True
        return f["category"] == "doc" and bool(run_dir_of(f["rel_path"]))

    def has_log_in(self, directory: str) -> bool:
        for f in self.files.by_dir.get(directory, []):
            ext = os.path.splitext(_name(f["rel_path"]))[1].lower()
            if f["category"] == "log" or ext in _LOG_EXTS:
                return True
        return False


# ---------------------------------------------------------------- 规则实现
# 每条规则 fn(ctx, project_root) -> list[lead]；
# lead = {title, target_path, basis, evidence[], possible_explanations[], next_steps[], false_positive}

def lead_result_without_input(ctx: _Ctx, root: str) -> list[dict]:
    """规则1：运行目录有结果文件，但找不到参数记录与输入数据。"""
    out = []
    for rd in sorted(ctx.runs):
        members = ctx.runs[rd]
        outputs = [m for m in members if ctx.is_output(m)]
        if not outputs:
            continue
        # 指标命名的文件本身是结果（即使 json/csv 被归为 config/data），不计入参数或输入
        params = [m for m in members if m["category"] == "config"
                  and not _METRICS_FILE_RE.search(m["rel_path"])]
        inputs = [m for m in members if m["category"] == "data"
                  and not _METRICS_FILE_RE.search(m["rel_path"])]
        if params or inputs:
            continue
        names = ", ".join(_name(m["rel_path"]) for m in sorted(outputs, key=lambda x: x["rel_path"])[:3])
        out.append({
            "title": f"运行目录 {rd}/ 有结果文件但缺少参数与输入记录",
            "target_path": rd,
            "basis": (f"运行目录 {rd}/ 中发现结果文件（{names}），但目录内没有任何"
                      "配置类文件（参数记录）或数据类文件（输入数据）"),
            "evidence": [m["rel_path"] for m in sorted(outputs, key=lambda x: x["rel_path"])[:5]],
            "possible_explanations": [
                "参数和输入可能硬编码在脚本中，未落盘为参数文件",
                "参数/输入文件可能位于运行目录之外，或未被扫描捕获",
                "结果文件可能是手工放置或从别处复制的",
            ],
            "next_steps": [
                f"人工确认 {rd}/ 中结果文件的生成方式",
                "若确为实验输出，建议补记参数文件与输入数据清单（可参照复现检查报告）",
            ],
            "false_positive": "脚本自身即参数来源（命令行驱动）且输入来自外部路径时，属正常情况",
        })
        if len(out) >= MAX_LEADS_PER_RULE:
            break
    return out


def lead_test_without_result(ctx: _Ctx, root: str) -> list[dict]:
    """规则2：存在测试源码，但全项目未发现测试结果报告。"""
    tests = ctx.files.by_cat.get("test", [])
    if not tests:
        return []
    if any(_TEST_REPORT_RE.search(f["rel_path"]) for f in ctx.files.by_path.values()):
        return []
    names = ", ".join(_name(f["rel_path"]) for f in sorted(tests, key=lambda x: x["rel_path"])[:3])
    return [{
        "title": "存在测试源码，但未发现测试结果报告",
        "target_path": "tests" if any(f["rel_path"].startswith("tests/") for f in tests) else _name(tests[0]["rel_path"]),
        "basis": (f"快照中有 {len(tests)} 个测试类文件（{names} 等），但未发现任何测试报告"
                  "（junit/coverage/pytest report 等特征文件）"),
        "evidence": [f["rel_path"] for f in sorted(tests, key=lambda x: x["rel_path"])[:5]],
        "possible_explanations": [
            "测试从未运行过，或运行后未保留报告文件",
            "测试结果以其它格式存在（如控制台输出、自定义日志），未被特征识别",
        ],
        "next_steps": [
            "人工运行一次测试并保留报告（如 pytest --junitxml）",
            "确认项目中是否存在非标准命名的测试结果文件",
        ],
        "false_positive": "测试结果保存在项目外或以非标准格式存在时，属漏识别而非真缺失",
    }]


def lead_error_but_complete_result(ctx: _Ctx, root: str) -> list[dict]:
    """规则3：日志显示异常/错误，但运行目录仍存在看似完整的结果文件。"""
    out = []
    for rd in sorted(ctx.runs):
        members = ctx.runs[rd]
        outputs = [m for m in members if ctx.is_output(m)]
        if not outputs:
            continue
        hits: list[str] = []
        for m in members:
            if m["category"] != "log":
                continue
            text = read_text_head(root, m["rel_path"])
            if text is None:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if _ERROR_RE.search(line):
                    hits.append(f"{m['rel_path']}:{i}: {line.strip()[:120]}")
        if not hits:
            continue
        out.append({
            "title": f"运行目录 {rd}/ 日志有错误记录，但结果文件仍存在",
            "target_path": rd,
            "basis": f"运行目录 {rd}/ 的日志中出现错误/异常特征，同时目录内仍有结果文件（可能不完整）",
            "evidence": hits[:5] + [m["rel_path"] for m in outputs[:2]],
            "possible_explanations": [
                "程序在写出全部结果前异常终止，结果文件是残缺的",
                "错误发生在非关键步骤（如可选的后处理），结果本身有效",
                "上次失败的残留文件与后续成功运行混在同一目录",
            ],
            "next_steps": [
                f"人工核对 {rd}/ 中结果文件的完整性（行数/样本量是否与日志一致）",
                "对照日志时间顺序，确认结果文件产生于错误之前还是之后",
            ],
            "false_positive": "日志中的 Error 只是警告性输出（如依赖弃用提示）时，结果可能完好",
        })
        if len(out) >= MAX_LEADS_PER_RULE:
            break
    return out


def lead_duplicate_results(ctx: _Ctx, root: str) -> list[dict]:
    """规则4：多份结果文件相同内容或同名，但无法确定其关系。"""
    out = []
    covered: set[frozenset] = set()
    by_sha: dict[str, list[dict]] = defaultdict(list)
    for f in ctx.files.by_path.values():
        if ctx.is_output(f) or f["category"] == "data":
            if f["sha256"]:
                by_sha[f["sha256"]].append(f)
    for sha, group in sorted(by_sha.items()):
        dirs = {_norm(f["rel_path"]).rsplit("/", 1)[0] for f in group}
        if len(group) < 2 or len(dirs) < 2:
            continue
        paths = sorted(f["rel_path"] for f in group)
        covered.add(frozenset(paths))
        out.append({
            "title": f"发现内容完全相同的结果文件（{len(group)} 份）",
            "target_path": paths[0],
            "basis": (f"{len(group)} 份结果文件 SHA-256 完全相同（{_short(sha)}），"
                      "且位于不同目录，无法从快照判断它们的关系"),
            "evidence": paths[:6],
            "possible_explanations": [
                "同一次运行的结果被复制/同步到多个目录",
                "重复运行产生了完全一致的结果（固定随机种子下的确定性输出）",
                "某份是另一份的备份副本",
            ],
            "next_steps": [
                "人工确认这些副本中哪一份是「权威版本」",
                "如为重复实验产物，可查看复现检查中的「重复实验差异」项",
            ],
            "false_positive": "同名模板文件（内容本就固定）被多目录引用时，属正常复用",
        })
        if len(out) >= MAX_LEADS_PER_RULE:
            return out
    # 相似名称（去版本号后同词干、不同目录、无任何关系边连接）
    from .graph import _VERSION_TOKEN_RE
    groups: dict[str, list[dict]] = defaultdict(list)
    for f in ctx.files.by_path.values():
        if f["category"] in ("data", "doc") and not ctx.is_output(f):
            base = _VERSION_TOKEN_RE.sub("", _stem(f["rel_path"])).strip("_-. ").lower()
            if base:
                groups[base].append(f)
    for base, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        pairs = [(a, b) for i, a in enumerate(members) for b in members[i + 1:]
                 if _norm(a["rel_path"]).rsplit("/", 1)[0] != _norm(b["rel_path"]).rsplit("/", 1)[0]]
        for a, b in pairs:
            pa, pb = _norm(a["rel_path"]), _norm(b["rel_path"])
            if frozenset((pa, pb)) in covered or (pa, pb) in ctx.edge_pairs:
                continue
            out.append({
                "title": f"两份同名结果文件缺少可确认的关系",
                "target_path": pa,
                "basis": (f"「{_name(pa)}」与「{_name(pb)}」词干相同、位于不同目录，"
                          "且关系图谱中没有可确认二者关系的边（内容也不同）"),
                "evidence": [f"{pa}（{_short(a['sha256'])}）", f"{pb}（{_short(b['sha256'])}）"],
                "possible_explanations": [
                    "一份是另一份的历史版本（版本演化未被命名规则捕获）",
                    "两次独立运行各自产生的相似结果",
                ],
                "next_steps": ["人工比对两份文件内容，确认其演化/重复关系"],
                "false_positive": "同名文件本就是不同用途的独立文件时（如各目录的 summary.md），属正常并列",
            })
            if len(out) >= MAX_LEADS_PER_RULE:
                return out
    return out


def lead_doc_claims_without_evidence(ctx: _Ctx, root: str) -> list[dict]:
    """规则5：文档声称功能已完成/测试已通过，但项目缺少对应验证证据。"""
    if any(_TEST_REPORT_RE.search(f["rel_path"]) for f in ctx.files.by_path.values()):
        has_test_report = True
    else:
        has_test_report = False
    has_artifact = any(ctx.is_output(f) and f["category"] == "build"
                       for f in ctx.files.by_path.values())
    if has_test_report and has_artifact:
        return []
    out = []
    scanned = 0
    for f in sorted(ctx.files.by_cat.get("doc", []), key=lambda x: x["rel_path"]):
        if scanned >= MAX_TEXT_DOCS:
            break
        text = read_text_head(root, f["rel_path"])
        if text is None:
            continue
        scanned += 1
        hits = []
        for i, line in enumerate(text.splitlines(), 1):
            if _CLAIM_RE.search(line):
                hits.append(f"{f['rel_path']}:{i}: {line.strip()[:120]}")
        if not hits:
            continue
        missing = []
        if not has_test_report:
            missing.append("测试结果报告")
        if not has_artifact:
            missing.append("可验证的构建产物/输出")
        out.append({
            "title": f"文档声称已完成的工作缺少对应验证证据（{_name(f['rel_path'])}）",
            "target_path": f["rel_path"],
            "basis": (f"文档中包含「已完成/已实现/已通过」类表述，但项目中未发现"
                      f"{'、'.join(missing)}"),
            "evidence": hits[:5],
            "possible_explanations": [
                "验证确实发生过，但证据文件未被提交/未被扫描捕获",
                "文档表述超前于实际进度（计划性描述）",
                "验证证据以非标准形式存在，未被识别",
            ],
            "next_steps": [
                f"人工核实文档中声称内容的真实完成情况",
                "如已完成，建议补充可验证的证据文件（测试报告/构建产物）",
            ],
            "false_positive": "文档描述的是计划或愿景（如 TODO/路线图）时，不构成虚假声明",
        })
        if len(out) >= MAX_LEADS_PER_RULE:
            break
    return out


def lead_dependency_conflict(ctx: _Ctx, root: str) -> list[dict]:
    """规则6：不同依赖声明文件中，同一包被固定为不同版本。"""
    env_files = []
    for f in ctx.files.by_path.values():
        n = _name(f["rel_path"]).lower()
        if (n.startswith("requirements") or n in {"pyproject.toml", "pipfile", "conda.yaml"}
                or n.startswith("pip-freeze") or n.startswith("pip_freeze")) \
                and f["category"] in ("config", "doc", "other"):
            env_files.append(f)
    if len(env_files) < 1:
        return []
    pins: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    norm_name = lambda s: re.sub(r"[-_.]+", "-", s).lower()
    for f in sorted(env_files, key=lambda x: x["rel_path"]):
        text = read_text_head(root, f["rel_path"])
        if text is None:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for m in _PIN_RE.finditer(line):
                pkg, ver = norm_name(m.group(1)), m.group(2)
                pins[pkg][ver].append(f"{f['rel_path']}:{i}: {line.strip()[:120]}")
    out = []
    for pkg in sorted(pins):
        versions = pins[pkg]
        if len(versions) < 2:
            continue
        desc = ", ".join(f"{v}（{locs[0].split(':')[0]}）" for v, locs in sorted(versions.items()))
        out.append({
            "title": f"依赖 {pkg} 在不同声明文件中被固定为不同版本",
            "target_path": pkg,
            "basis": f"同一依赖包 {pkg} 出现多个固定版本：{desc}",
            "evidence": [loc for locs in versions.values() for loc in locs][:6],
            "possible_explanations": [
                "两份声明服务于不同环境（开发/生产、不同平台），差异是有意的",
                "其中一份未及时更新，环境实际安装的版本不明",
            ],
            "next_steps": [
                f"人工确认 {pkg} 的实际使用版本，统一各声明文件",
                "可用只读命令 pip show " + pkg + " 查看当前环境安装的版本",
            ],
            "false_positive": "多环境声明文件（如 requirements-dev/prod）本就允许版本不同时，属正常配置",
        })
        if len(out) >= MAX_LEADS_PER_RULE:
            break
    return out


def lead_result_changed_without_record(ctx: _Ctx, root: str) -> list[dict]:
    """规则7：结果文件在两次快照间内容变化，但其目录缺少可解释的生成记录（日志）。"""
    if ctx.prev_files is None:
        return []
    out = []
    for path, f in sorted(ctx.files.by_path.items()):
        if not (ctx.is_output(f) or f["category"] == "data"):
            continue
        prev = ctx.prev_files.get(path)
        if not prev or prev["sha256"] == f["sha256"] or not f["sha256"]:
            continue
        d = path.rsplit("/", 1)[0] if "/" in path else ""
        if ctx.has_log_in(d):
            continue
        out.append({
            "title": f"结果文件 {_name(path)} 在两次扫描间发生变化，但缺少生成记录",
            "target_path": path,
            "basis": (f"文件内容在快照间变化（{_short(prev['sha256'])} → {_short(f['sha256'])}），"
                      f"但其所在目录 {d or '(项目根)'} 中没有日志类文件可解释这次变化"),
            "evidence": [path, f"前次 SHA-256: {prev['sha256'] or '无'}",
                         f"本次 SHA-256: {f['sha256']}"],
            "possible_explanations": [
                "程序重新运行生成了新结果，但日志未保存或已被删除",
                "文件被人工编辑或外部工具修改",
                "生成记录保存在其它目录，未被关联",
            ],
            "next_steps": [
                f"人工确认 {_name(path)} 的变化来源；如为实验输出，建议在运行时保留日志",
            ],
            "false_positive": "日志文件使用非常规扩展名或位于上级目录时，属漏识别而非真缺失",
        })
        if len(out) >= MAX_LEADS_PER_RULE:
            break
    return out


def lead_doc_config_only_changes(ctx: _Ctx, root: str) -> list[dict]:
    """规则8：两次快照间的可见变化全部为文档/配置类，无新数据或构建产物。
    仅陈述观察，不代表项目停滞或停止工作。"""
    if ctx.prev_files is None:
        return []
    changed = []
    for path, f in ctx.files.by_path.items():
        if f["read_error"]:
            continue
        prev = ctx.prev_files.get(path)
        if prev is None:
            changed.append(("added", f))
        elif (prev["sha256"] or f["sha256"]) and prev["sha256"] != f["sha256"]:
            changed.append(("modified", f))
    for path in ctx.prev_files:
        if path not in ctx.files.by_path:
            changed.append(("removed", {"rel_path": path, "category": ctx.prev_files[path]["category"]}))
    if len(changed) < 3:
        return []
    if any(f["category"] in ("data", "build", "source", "test") for _, f in changed):
        return []
    doc_cfg = [f["rel_path"] for _, f in changed if f["category"] in ("doc", "config")]
    if not doc_cfg:
        return []
    return [{
        "title": "最近两次快照间的可见变化均为文档/配置类",
        "target_path": "",
        "basis": (f"两次快照间共观察到 {len(changed)} 处文件变化，全部为文档或配置类，"
                  "未发现新的数据、构建产物、源码或测试变化"),
        "evidence": [f"{'新增' if k == 'added' else '修改' if k == 'modified' else '移除'}：{f['rel_path']}"
                     for k, f in changed[:8]],
        "possible_explanations": [
            "项目正处于文档整理、方案设计或配置调整阶段",
            "代码变化发生在未被扫描捕获的位置（如忽略规则覆盖的目录）",
            "该期间工作以讨论、规划等不落盘形式进行",
        ],
        "next_steps": [
            "人工确认该阶段的工作性质；如确有代码工作，检查扫描范围是否遗漏",
        ],
        "false_positive": ("本线索仅陈述「可见文件变化」的观察。文档/配置阶段是正常的工作状态，"
                           "不构成任何关于项目进展或停滞的判断"),
    }]


# 可扩展注册点：(rule_id, version, fn, 说明)
LEADS_RULES = [
    ("result_without_input", 1, lead_result_without_input, "结果文件缺少参数与输入记录"),
    ("test_without_result", 1, lead_test_without_result, "有测试源码但无测试结果报告"),
    ("error_but_complete_result", 1, lead_error_but_complete_result, "日志报错但结果文件仍存在"),
    ("duplicate_results", 1, lead_duplicate_results, "结果文件相同/相似且关系不明"),
    ("doc_claims_without_evidence", 1, lead_doc_claims_without_evidence, "文档声明缺少验证证据"),
    ("dependency_conflict", 1, lead_dependency_conflict, "依赖声明版本冲突"),
    ("result_changed_without_record", 1, lead_result_changed_without_record, "结果变化缺少生成记录"),
    ("doc_config_only_changes", 1, lead_doc_config_only_changes, "快照间仅文档/配置变化（观察性）"),
]

RULE_LABELS = {
    "result_without_input": "结果无输入/参数",
    "test_without_result": "有测试无结果",
    "error_but_complete_result": "报错与完整结果并存",
    "duplicate_results": "疑似重复结果",
    "doc_claims_without_evidence": "文档声明无证据",
    "dependency_conflict": "依赖版本冲突",
    "result_changed_without_record": "结果变化无记录",
    "doc_config_only_changes": "仅文档/配置变化",
}


def build_leads(db: Database, project_id: int, snapshot_id: int,
                enabled: set[str] | None = None) -> list[dict]:
    """对快照执行全部（或指定）线索规则，返回线索列表（不写库）。"""
    if enabled is None:
        enabled = {rid for rid, *_ in LEADS_RULES if db.get_rule_enabled(rid)}
    project = db.get_project(project_id)
    root = project["path"] if project else ""
    ctx = _Ctx(db, project_id, snapshot_id)
    leads: list[dict] = []
    now = utcnow_iso()
    for rid, ver, fn, _desc in LEADS_RULES:
        if rid not in enabled:
            continue
        for lead in fn(ctx, root):
            lead["rule_id"] = rid
            lead["rule_version"] = ver
            lead["created_at"] = now
            leads.append(lead)
    return leads


def run_lead_analysis(db: Database, project_id: int, snapshot_id: int,
                      enabled: set[str] | None = None) -> dict:
    """执行并持久化调查线索（幂等：重跑更新分析内容，保留人工状态）。"""
    leads = build_leads(db, project_id, snapshot_id, enabled)
    by_rule: dict[str, int] = defaultdict(int)
    for lead in leads:
        db.upsert_lead(
            project_id, snapshot_id, lead["rule_id"], lead["rule_version"], lead["title"],
            lead["target_path"], lead["basis"],
            json.dumps(lead["evidence"], ensure_ascii=False),
            json.dumps({"possible_explanations": lead["possible_explanations"],
                        "next_steps": lead["next_steps"],
                        "false_positive": lead["false_positive"]}, ensure_ascii=False),
            lead["created_at"])
        by_rule[lead["rule_id"]] += 1
    return {"snapshot_id": snapshot_id, "leads": len(leads),
            "by_rule": dict(by_rule), "rules": sorted(enabled) if enabled else
            [rid for rid, *_ in LEADS_RULES if db.get_rule_enabled(rid)]}


def lead_view(db: Database, lead_id: int) -> dict | None:
    """线索全量视图（JSON 已解析，供 UI/报告）。"""
    rows = db.query("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not rows:
        return None
    r = dict(rows[0])
    try:
        r["evidence_list"] = json.loads(r.get("evidence") or "[]")
    except (TypeError, ValueError):
        r["evidence_list"] = []
    try:
        details = json.loads(r.get("details") or "{}")
    except (TypeError, ValueError):
        details = {}
    r["possible_explanations"] = details.get("possible_explanations", [])
    r["next_steps"] = details.get("next_steps", [])
    r["false_positive"] = details.get("false_positive", "")
    r["rule_label"] = RULE_LABELS.get(r["rule_id"], r["rule_id"])
    return r
