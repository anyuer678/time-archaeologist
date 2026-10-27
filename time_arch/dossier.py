"""成果档案引擎：把单文件启发式线索聚合成带证据链的「成果档案」。

设计约束：
- 可扩展：识别器按成果类型注册（SEEDERS），新增类型不改核心代码。
- 确定性：同一快照输入重复分析结果一致（safe 重跑，DB 侧幂等）。
- 只读：仅使用快照 file_records，不执行、不写入被扫描目录。
- 不冒充事实：识别出的成果都带 seed rule / basis / confidence；
  一个 .exe 或 report.md 的存在不等于项目交付成功——完整性检查
  把缺失证据列为待调查项（missing），既不判成功也不判失败。
"""
from __future__ import annotations

import json
import os
import re

from .db import Database
from .util import utcnow_iso

ARTIFACT_KINDS = {
    "software_release": "软件与发布包",
    "test_result": "自动化测试结果",
    "experiment_result": "实验结果与统计报告",
    "dataset": "数据集与生成记录",
    "build_artifact": "构建产物",
    "document": "文档与技术报告",
}

ROLE_LABELS = {
    "output": "结果/输出", "params": "参数/配置", "log": "日志", "data": "数据",
    "source": "源码", "test": "测试", "report": "报告", "member": "成员",
}

_EXECUTABLE_EXTS = {".exe", ".msi", ".msix", ".jar", ".apk", ".aab", ".whl", ".egg", ".appx", ".dll", ".so", ".pyd"}
_ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z"}
_VERSION_RE = re.compile(r"v?\d+\.\d+(\.\d+)?")
_RELEASE_DIR_RE = re.compile(r"(^|/)(dist|release|publish|artifacts?)(/|$)", re.IGNORECASE)
_RESULT_ROOT_RE = re.compile(r"^(results?|runs?|outputs?|experiments?|evals?|eval_results)$", re.IGNORECASE)
_TEST_REPORT_RE = re.compile(
    r"(^|/)(junit[^/]*\.xml|test[-_]?report|report[-_]?test|coverage\.xml|coverage\.json|"
    r"pytest[-_]report|htmlcov|lcov|cobertura)", re.IGNORECASE)
_METRICS_FILE_RE = re.compile(
    r"(metrics|accuracy|precision|recall|f1|eval|result|loss|reward|score|summary)s?\.\w+$", re.IGNORECASE)
_DATASET_EXTS = {".csv", ".tsv", ".parquet", ".npz", ".npy", ".pkl", ".pickle", ".h5",
                 ".hdf5", ".xlsx", ".xls", ".feather", ".arrow", ".jsonl", ".ndjson", ".sqlite", ".db"}
_BUILD_ARTIFACT_EXTS = {".so", ".dll", ".pyd", ".o", ".obj", ".a", ".lib", ".class", ".wasm", ".bin", ".out"}
_DOC_HINT_RE = re.compile(
    r"(design|spec|report|plan|survey|summary|方案|设计|报告|总结|调研)", re.IGNORECASE)
_DOC_DIR_RE = re.compile(r"(^|/)(docs?|reports?|papers?)(/|$)", re.IGNORECASE)

_CHECK_SIZE = 512          # 数据集种子最小体积（字节）
_MAX_MEMBERS_PER_ART = 60  # 单档案关联文件上限（防巨型目录刷屏）


def _norm(p: str) -> str:
    return str(p).replace("\\", "/")


def _stem(path: str) -> str:
    name = _norm(path).rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _tokens(path: str) -> set[str]:
    """文件名词元：按 _-. 与大小写切分，长度≥3 才保留（降低误配）。"""
    raw = re.split(r"[_\-.]+", _stem(_norm(path).rsplit("/", 1)[-1]))
    return {t.lower() for t in raw if len(t) >= 3}


def run_dir_of(rel_path: str) -> str | None:
    """实验运行目录：路径中含 results/runs/outputs/... 根时，返回根+一级子目录。
    例：runs/exp03/metrics.json → runs/exp03；results/a.png → results。"""
    parts = _norm(rel_path).split("/")
    for i, seg in enumerate(parts[:-1]):
        if _RESULT_ROOT_RE.match(seg):
            depth = 2 if len(parts) - 1 > i + 1 else 1
            return "/".join(parts[:i + depth])
    return None


class _Files:
    """快照文件索引：按路径、目录、类别查询。"""

    def __init__(self, rows: list):
        self.by_path: dict[str, dict] = {}
        self.by_dir: dict[str, list[dict]] = {}
        self.by_cat: dict[str, list[dict]] = {}
        for r in rows:
            f = {"rel_path": _norm(r["rel_path"]), "size": r["size"] or 0,
                 "sha256": r["sha256"], "category": r["category"],
                 "mtime": r["mtime"], "read_error": r["read_error"]}
            if f["read_error"]:
                continue
            self.by_path[f["rel_path"]] = f
            d = f["rel_path"].rsplit("/", 1)[0] if "/" in f["rel_path"] else ""
            self.by_dir.setdefault(d, []).append(f)
            self.by_cat.setdefault(f["category"], []).append(f)

    def under_dir(self, d: str) -> list[dict]:
        prefix = d + "/"
        return [f for p, f in self.by_path.items() if p == d or p.startswith(prefix)]


def _seeds_software(files: _Files) -> list[dict]:
    out = []
    for f in files.by_path.values():
        ext = os.path.splitext(f["rel_path"])[1].lower()
        in_release = bool(_RELEASE_DIR_RE.search(f["rel_path"]))
        if ext in _EXECUTABLE_EXTS:
            conf = "high" if in_release or ext in {".exe", ".msi", ".jar", ".apk", ".whl"} else "medium"
            out.append({"file": f, "confidence": conf, "rule": "exec-ext",
                        "basis": f"扩展名 {ext} 是可执行/分发格式" + ("，且位于发布目录" if in_release else "")})
        elif ext in _ARCHIVE_EXTS and (in_release or _VERSION_RE.search(_stem(f["rel_path"]))):
            conf = "high" if in_release else "medium"
            out.append({"file": f, "confidence": conf, "rule": "archive-release",
                        "basis": "发布目录压缩包或文件名含版本号，可能是发布包（未解包验证内容）"})
    return out


def _seeds_test_result(files: _Files) -> list[dict]:
    return [{"file": f, "confidence": "high", "rule": "test-report-name",
             "basis": f"路径匹配测试报告特征：{_norm(f['rel_path']).rsplit('/', 1)[-1]}"}
            for f in files.by_path.values() if _TEST_REPORT_RE.search(f["rel_path"])]


def _seeds_experiment(files: _Files) -> list[dict]:
    """每个运行目录一个档案；主文件优先 metrics 类命名，否则该目录下最大数据文件。"""
    runs: dict[str, list[dict]] = {}
    for f in files.by_path.values():
        rd = run_dir_of(f["rel_path"])
        if rd:
            runs.setdefault(rd, []).append(f)
    out = []
    for rd, members in sorted(runs.items()):
        primary = None
        for f in sorted(members, key=lambda x: (-x["size"], x["rel_path"])):
            if _METRICS_FILE_RE.search(f["rel_path"]) or f["category"] == "data":
                primary = f
                break
        if primary is None:
            continue
        out.append({"file": primary, "confidence": "high", "rule": "run-directory",
                    "basis": f"位于实验运行目录 {rd}/，且有结果/指标类文件",
                    "group": rd, "members": members})
    return out


def _seeds_dataset(files: _Files) -> list[dict]:
    """非运行目录的数据文件按父目录聚合，主文件取最大。"""
    dirs: dict[str, list[dict]] = {}
    for f in files.by_path.values():
        ext = os.path.splitext(f["rel_path"])[1].lower()
        if f["category"] != "data" or ext not in _DATASET_EXTS or f["size"] < _CHECK_SIZE:
            continue
        if run_dir_of(f["rel_path"]):
            continue
        d = f["rel_path"].rsplit("/", 1)[0] if "/" in f["rel_path"] else ""
        dirs.setdefault(d, []).append(f)
    out = []
    for d, members in sorted(dirs.items()):
        primary = max(members, key=lambda x: (x["size"], x["rel_path"]))
        out.append({"file": primary, "confidence": "medium", "rule": "dataset-ext",
                    "basis": f"数据格式文件（{'/'.join(sorted({os.path.splitext(m['rel_path'])[1] for m in members}))}，≥{_CHECK_SIZE}B）",
                    "group": d or ".", "members": members})
    return out


def _seeds_build(files: _Files) -> list[dict]:
    out = []
    for f in files.by_path.values():
        ext = os.path.splitext(f["rel_path"])[1].lower()
        if f["category"] == "build" and ext in _BUILD_ARTIFACT_EXTS and f["size"] >= _CHECK_SIZE:
            out.append({"file": f, "confidence": "medium", "rule": "build-ext",
                        "basis": f"构建类别中的目标文件格式（{ext}），可能是编译产物"})
    return out


def _seeds_document(files: _Files) -> list[dict]:
    out = []
    for f in files.by_path.values():
        if f["category"] != "doc":
            continue
        name = _norm(f["rel_path"]).rsplit("/", 1)[-1]
        stem = _stem(name)
        if _DOC_HINT_RE.search(stem) or _DOC_DIR_RE.search(f["rel_path"]):
            out.append({"file": f, "confidence": "medium", "rule": "doc-hint",
                        "basis": "文件名/目录符合设计稿、报告、总结类文档特征"})
    return out


# 可扩展注册点：新增成果类型 = 新增一个 seeder + KINDS/COMPLETENESS 条目
SEEDERS = {
    "software_release": _seeds_software,
    "test_result": _seeds_test_result,
    "experiment_result": _seeds_experiment,
    "dataset": _seeds_dataset,
    "build_artifact": _seeds_build,
    "document": _seeds_document,
}

# 完整性检查表：每项 [展示名, 探测函数(members, files, primary) -> (state, detail)]
# state: present=有直接证据 / inferred=推测存在 / missing=未发现（待调查，不是失败）
_CHECKLISTS: dict[str, list[tuple[str, object]]] = {
    "experiment_result": [
        ("参数记录", lambda m, fs, p: _any(m, "config")),
        ("输入数据", lambda m, fs, p: _any(m, "data")),
        ("运行日志", lambda m, fs, p: _any(m, "log")),
        ("结果文件", lambda m, fs, p: ("present", "主结果文件存在于快照")),
        ("错误/异常记录", lambda m, fs, p: _any(m, "log", name_hint="err")),
    ],
    "test_result": [
        ("测试报告", lambda m, fs, p: ("present", "主报告文件存在于快照")),
        ("测试源码", lambda m, fs, p: _any(m, "test") if m else _any(fs.by_cat.get("test", []), "test")),
        ("覆盖率数据", lambda m, fs, p: _any(fs.by_path.values(), "any", name_hint="coverage")),
    ],
    "software_release": [
        ("依赖/构建配置", lambda m, fs, p: _any(fs.by_path.values(), "any",
                                               name_hint=r"requirements|pyproject|setup\.(py|cfg)|package\.json|cargo\.toml")),
        ("源码", lambda m, fs, p: _any(fs.by_cat.get("source", []), "source")),
        ("版本/变更说明", lambda m, fs, p: _any(fs.by_cat.get("doc", []), "doc",
                                               name_hint="changelog|version|release|news")),
        ("校验和/签名", lambda m, fs, p: _any(fs.by_path.values(), "any", name_hint=r"\.(sha256|sha1|md5|asc|sig)$")),
    ],
    "dataset": [
        ("数据文件", lambda m, fs, p: ("present", "主数据文件存在于快照")),
        ("生成脚本/来源", lambda m, fs, p: _any(fs.by_cat.get("source", []), "source")),
        ("处理日志", lambda m, fs, p: _any(fs.by_cat.get("log", []), "log")),
    ],
    "build_artifact": [
        ("目标文件", lambda m, fs, p: ("present", "构建产物存在于快照")),
        ("源码", lambda m, fs, p: _any(fs.by_cat.get("source", []), "source")),
        ("构建配置", lambda m, fs, p: _any(fs.by_path.values(), "any",
                                           name_hint=r"makefile|pyproject|setup\.(py|cfg)|cmake|build\.(py|gradle|xml)")),
    ],
    "document": [
        ("文档本体", lambda m, fs, p: ("present", "文档存在于快照")),
        ("引用的结果/数据", lambda m, fs, p: _any(fs.by_cat.get("data", []) + fs.by_cat.get("build", []), "data")),
    ],
}


def _any(candidates, role: str, name_hint: str | None = None):
    """探测成员中是否有符合类别/名称特征的文件。missing=未发现，不判失败。"""
    hint = re.compile(name_hint, re.IGNORECASE) if name_hint else None
    for f in candidates:
        if role != "any" and f.get("category") != role and f.get("role") != role:
            continue
        if hint and not hint.search(_norm(f["rel_path"]).rsplit("/", 1)[-1]) \
                and not hint.search(f["rel_path"]):
            continue
        detail = f"存在：{_norm(f['rel_path'])}"
        return ("present", detail)
    return ("missing", "未在快照中发现（待调查项，不代表失败）")


def _member_roles(rd: str | None, members: list[dict], primary_path: str) -> list[dict]:
    """把关联文件赋角色：运行目录成员按类别映射，主文件为 output。"""
    cat_role = {"config": "params", "log": "log", "data": "data", "source": "source",
                "test": "test", "doc": "report", "build": "output", "other": "member"}
    out: dict[str, dict] = {}
    for f in members:
        role = "output" if f["rel_path"] == primary_path else cat_role.get(f["category"], "member")
        basis = (f"同一实验运行目录（{rd}/）" if rd and f["rel_path"] != primary_path
                 else "档案主文件（识别种子）" if f["rel_path"] == primary_path
                 else "同目录关联")
        out[f["rel_path"]] = {"rel_path": f["rel_path"], "role": role, "basis": basis,
                              "confidence": "high" if f["rel_path"] == primary_path else "medium",
                              "category": f["category"]}
    return out


def _name_token_matches(files: _Files, primary_path: str, existing: set[str]) -> list[dict]:
    """词元匹配：档案主文件名与源码/测试文件共享 ≥1 个 ≥3 字符词元。"""
    toks = _tokens(primary_path) - {"metrics", "results", "output", "outputs", "final"}
    if not toks:
        return []
    out = []
    for cat, role in (("source", "source"), ("test", "test")):
        for f in files.by_cat.get(cat, []):
            if f["rel_path"] in existing:
                continue
            if _tokens(f["rel_path"]) & toks:
                out.append({"rel_path": f["rel_path"], "role": role,
                            "basis": f"文件名词元匹配（{_stem(primary_path)} ↔ {_stem(f['rel_path'])}），推测相关，未观察执行",
                            "confidence": "low"})
    return out


def analyze_artifacts(db: Database, project_id: int, snapshot_id: int) -> list[dict]:
    """对快照做成果档案分析（纯函数，不写库）。返回档案列表：
    {kind, name, primary_path, confidence, basis, files[], verified_facts[],
     unverified_assumptions[], completeness[], next_steps[], seed_rule}"""
    rows = db.query(
        "SELECT rel_path, size, sha256, category, mtime, read_error "
        "FROM file_records WHERE snapshot_id=?", (snapshot_id,))
    files = _Files(rows)
    artifacts: list[dict] = []

    for kind, seeder in SEEDERS.items():
        for seed in seeder(files):
            primary = seed["file"]
            members = seed.get("members", [primary])
            linked = _member_roles(seed.get("group"), members, primary["rel_path"])
            for extra in _name_token_matches(files, primary["rel_path"], set(linked)):
                if len(linked) >= _MAX_MEMBERS_PER_ART:
                    break
                linked[extra["rel_path"]] = extra

            present_roles = {v["role"] for v in linked.values()}
            checklist = []
            for label, probe in _CHECKLISTS[kind]:
                state, detail = probe(linked.values(), files, primary)
                checklist.append({"item": label, "state": state, "detail": detail})

            verified = [
                f"文件存在于快照 #{snapshot_id}：{_norm(primary['rel_path'])}"
                f"（{primary['size']} 字节，SHA-256 {'已计算' if primary['sha256'] else '未计算（读取失败）'}）",
                f"关联文件 {len(linked)} 个，均有快照文件记录可溯源",
            ]
            assumptions = [f"识别依据（启发式 {seed['rule']}）：{seed['basis']}。"
                           "成果的实际用途/质量未经执行验证，也不应执行来验证。"]
            src_files = [v["rel_path"] for v in linked.values() if v["role"] == "source"]
            if src_files:
                assumptions.append(f"推测与源码 {', '.join(src_files[:3])} 相关（基于词元/目录匹配，未观察生成过程）。")
            missing = [c for c in checklist if c["state"] == "missing"]
            next_steps = [f"补查「{c['item']}」：{c['detail']}" for c in missing]
            if kind == "experiment_result" and not src_files:
                next_steps.append("未发现可能的生成源码：检查实验脚本是否保存在项目内。")

            name = _norm(primary["rel_path"]).rsplit("/", 1)[-1]
            if seed.get("group") and seed["group"] not in ("", "."):
                name = f"{seed['group'].rsplit('/', 1)[-1]}/{name}"
            artifacts.append({
                "kind": kind, "name": name, "primary_path": primary["rel_path"],
                "confidence": seed["confidence"], "basis": seed["basis"],
                "files": sorted(linked.values(), key=lambda x: (x["role"], x["rel_path"])),
                "verified_facts": verified, "unverified_assumptions": assumptions,
                "completeness": checklist, "next_steps": next_steps,
                "seed_rule": seed["rule"],
            })
    return artifacts


def run_dossier_analysis(db: Database, project_id: int, snapshot_id: int) -> dict:
    """执行并持久化成果档案（幂等：同快照重跑结果一致）。"""
    now = utcnow_iso()
    artifacts = analyze_artifacts(db, project_id, snapshot_id)
    created = updated = 0
    for a in artifacts:
        details = json.dumps({
            "verified_facts": a["verified_facts"],
            "unverified_assumptions": a["unverified_assumptions"],
            "completeness": a["completeness"],
            "next_steps": a["next_steps"],
            "seed_rule": a["seed_rule"],
        }, ensure_ascii=False)
        art_id, is_new = db.upsert_artifact(
            project_id, a["kind"], a["name"], a["primary_path"], a["confidence"],
            a["basis"], details, snapshot_id, now)
        created += int(is_new)
        updated += int(not is_new)
        db.replace_artifact_files(art_id, snapshot_id, a["files"])
    return {"snapshot_id": snapshot_id, "artifacts_created": created,
            "artifacts_updated": updated, "total": len(artifacts), "analyzed_at": now}


def dossier_view(db: Database, artifact_id: int, snapshot_id: int | None = None) -> dict | None:
    """成果档案全量视图：档案 + 关联文件 + 文件记录证据（供 UI/报告）。"""
    art = db.get_artifact(artifact_id)
    if art is None:
        return None
    sid = snapshot_id or art["last_snapshot_id"]
    files = db.list_artifact_files(artifact_id, sid)
    enriched = []
    for f in files:
        fr = lookup_file_record(db, sid, f["rel_path"])
        enriched.append({
            "rel_path": f["rel_path"], "role": f["role"], "basis": f["basis"],
            "confidence": f["confidence"],
            "size": fr["size"] if fr else None,
            "sha256": fr["sha256"] if fr else None,
            "category": fr["category"] if fr else None,
            "in_snapshot": fr is not None,
        })
    try:
        details = json.loads(art["details"])
    except (TypeError, ValueError):
        details = {}
    return {
        "id": art["id"], "project_id": art["project_id"], "kind": art["kind"],
        "kind_label": ARTIFACT_KINDS.get(art["kind"], art["kind"]),
        "name": art["name"], "primary_path": art["primary_path"],
        "confidence": art["confidence"], "status": art["status"],
        "first_seen_at": art["first_seen_at"], "last_seen_at": art["last_seen_at"],
        "snapshot_id": sid, "basis": art["basis"], "files": enriched,
        "verified_facts": details.get("verified_facts", []),
        "unverified_assumptions": details.get("unverified_assumptions", []),
        "completeness": details.get("completeness", []),
        "next_steps": details.get("next_steps", []),
        "seed_rule": details.get("seed_rule", ""),
    }


def lookup_file_record(db: Database, snapshot_id: int, rel_path: str):
    """按规范化路径查文件记录，兼容原生分隔符存储（Windows 为 \\）。"""
    fr = db.find_file_in_snapshot(snapshot_id, rel_path)
    if fr is None and "/" in rel_path:
        fr = db.find_file_in_snapshot(snapshot_id, rel_path.replace("/", os.sep))
    return fr
