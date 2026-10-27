"""项目关系图谱：从快照文件记录提取文件间关系（边），每条边带规则来源与置信度。

铁律：
- 关系必须带 source（规则 id 或 manual）与 confidence（0..1）；
  文件名推测的关联绝不冒充确定事实（confidence ≤ 0.7 并在 basis 说明）。
- 内容引用规则只读读取小文本文件（≤256KB，限量），跳过二进制（首 1KB 含 \0）。
- 幂等：同快照同规则重跑结果一致，可安全重放。
"""
from __future__ import annotations

import os
import re

from .db import Database
from .util import read_text_head, utcnow_iso

RELATION_LABELS = {
    "test_module": "测试↔被测模块",
    "references": "内容引用",
    "uses_config": "使用配置",
    "same_run": "同次运行",
    "builds_from": "构建自",
    "evolves_to": "演化版本",
}

TEXT_READ_LIMIT = 256 * 1024          # 单文件读取上限
MAX_TEXT_FILES = 400                  # 内容引用规则最多读取文件数
MAX_EDGES_PER_RULE = 2000

_TEST_STEM_RE = re.compile(r"^(?:tests?_|test_)(.+)$|^(.+?)_(?:tests?|test)$")
_VERSION_TOKEN_RE = re.compile(r"(?<![0-9])(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _norm(p: str) -> str:
    return str(p).replace("\\", "/")


def _stem(path: str) -> str:
    name = _norm(path).rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _read_text_head(abs_root: str, rel_path: str) -> str | None:
    return read_text_head(abs_root, rel_path, TEXT_READ_LIMIT)


# ---------------------------------------------------------------- 规则实现

def rule_test_module(files: list[dict], project_root: str) -> list[dict]:
    """tests/test_X.py ↔ X.py：文件名约定，confidence 0.6。"""
    edges = []
    sources_by_stem: dict[str, list[dict]] = {}
    for f in files:
        if f["category"] == "source":
            sources_by_stem.setdefault(_stem(f["rel_path"]).lower(), []).append(f)
    for f in files:
        if f["category"] != "test":
            continue
        stem = _stem(f["rel_path"]).lower()
        m = _TEST_STEM_RE.match(stem)
        target = (m.group(1) or m.group(2)) if m else None
        if not target:
            continue
        for src in sources_by_stem.get(target, []):
            if src["rel_path"] == f["rel_path"]:
                continue
            edges.append({
                "src_path": f["rel_path"], "dst_path": src["rel_path"],
                "relation": "test_module", "source": "test_module@1",
                "confidence": 0.6,
                "basis": f"测试文件名约定匹配（{stem} → {target}）；文件名推测，非 import 分析",
            })
            if len(edges) >= MAX_EDGES_PER_RULE:
                return edges
    return edges


_PATH_TOKEN_RE = re.compile(r"[\w\-./\\]{2,180}\.[A-Za-z0-9]{1,8}")
_PUNCT_EDGE = " \t\r\n:,;()[]{}<>\"'`=|*"


def rule_content_reference(files: list[dict], project_root: str) -> list[dict]:
    """小文本文件（doc/report/log/source/test）内容中出现其它文件的路径记号 → 引用边。
    实现：从文本提取路径形 token，查路径索引（全路径 0.9；同名词后缀匹配 0.7，
    basis 注明可能有同名词歧义）。只读、限量、跳过二进制。"""
    paths_by_full = {f["rel_path"]: f for f in files}
    paths_by_name: dict[str, list[dict]] = {}
    for f in files:
        name = f["rel_path"].rsplit("/", 1)[-1]
        paths_by_name.setdefault(name.lower(), []).append(f)
    edges = []
    scanned = 0
    for f in sorted(files, key=lambda x: x["rel_path"]):
        if f["category"] not in ("doc", "log", "source", "test"):
            continue
        if scanned >= MAX_TEXT_FILES:
            break
        text = _read_text_head(project_root, f["rel_path"])
        if text is None:
            continue
        scanned += 1
        rel = f["rel_path"]
        seen_targets: set[str] = set()
        for raw in _PATH_TOKEN_RE.findall(text):
            token = raw.strip(_PUNCT_EDGE).replace("\\", "/")
            if not token or token == rel:
                continue
            match = None
            strong = False
            if token in paths_by_full and token != rel:
                match, strong = token, True
            else:
                cands = paths_by_name.get(token.rsplit("/", 1)[-1].lower(), [])
                if len(cands) == 1 and cands[0]["rel_path"] != rel:
                    match, strong = cands[0]["rel_path"], False
            if match is None or match in seen_targets:
                continue
            seen_targets.add(match)
            relation = "uses_config" if paths_by_full[match]["category"] == "config" \
                and f["category"] == "source" else "references"
            if strong:
                basis = f"文件内容中出现了完整路径「{match}」（内容直接证据）"
                conf = 0.9
            else:
                basis = (f"文件内容中出现了文件名「{match.rsplit('/', 1)[-1]}」，"
                         f"项目中唯一对应 {match}；可能有同名词歧义，需人工确认")
                conf = 0.7
            edges.append({
                "src_path": rel, "dst_path": match, "relation": relation,
                "source": "content_reference@1", "confidence": conf, "basis": basis,
            })
            if len(edges) >= MAX_EDGES_PER_RULE:
                return edges
    return edges


def rule_same_run(files: list[dict], project_root: str) -> list[dict]:
    """同一次实验运行：运行目录内 params/log → data/output 边，0.7。
    指标类命名的文件（metrics/result/...）即使类别是 config 也按输出处理。"""
    from .dossier import run_dir_of, _METRICS_FILE_RE
    runs: dict[str, list[dict]] = {}
    for f in files:
        rd = run_dir_of(f["rel_path"])
        if rd:
            runs.setdefault(rd, []).append(f)
    edges = []
    for rd, members in sorted(runs.items()):
        outputs = [m for m in members if m["category"] in ("data", "doc", "build")
                   or _METRICS_FILE_RE.search(m["rel_path"])]
        out_paths = {m["rel_path"] for m in outputs}
        producers = [m for m in members if m["rel_path"] not in out_paths]
        for p in producers:
            for o in outputs:
                if p["rel_path"] == o["rel_path"]:
                    continue
                edges.append({
                    "src_path": p["rel_path"], "dst_path": o["rel_path"],
                    "relation": "same_run", "source": "same_run@1",
                    "confidence": 0.7,
                    "basis": f"同处实验运行目录 {rd}/（目录结构证据，非执行观察）",
                })
                if len(edges) >= MAX_EDGES_PER_RULE:
                    return edges
    return edges


def rule_builds_from(files: list[dict], project_root: str) -> list[dict]:
    """发布目录产物 ↔ 项目顶层同名(词元)源码目录/文件：0.4。"""
    from .dossier import _tokens
    top_dirs: dict[str, list[dict]] = {}
    for f in files:
        parts = f["rel_path"].split("/")
        if f["category"] in ("source", "config") and len(parts) >= 1:
            top_dirs.setdefault(parts[0].lower(), []).append(f)
    edges = []
    for f in files:
        if not (_RELEASE_DIR_RE_ISH(f["rel_path"]) and f["size"] >= 512):
            continue
        toks = {t for t in _tokens(f["rel_path"]) if t not in {"dist", "release", "build", "publish"}}
        for tok in toks:
            for src in top_dirs.get(tok, []):
                edges.append({
                    "src_path": f["rel_path"], "dst_path": src["rel_path"],
                    "relation": "builds_from", "source": "builds_from@1",
                    "confidence": 0.4,
                    "basis": f"发布目录产物名与源码目录「{src['rel_path'].split('/')[0]}」词元匹配；命名推测，未验证构建过程",
                })
                if len(edges) >= MAX_EDGES_PER_RULE:
                    return edges
    return edges


def _RELEASE_DIR_RE_ISH(path: str) -> bool:
    return bool(re.search(r"(^|/)(dist|release|publish|artifacts?)(/|$)", path, re.IGNORECASE))


def rule_evolves_to(files: list[dict], project_root: str) -> list[dict]:
    """同目录、同名干、版本号递增 → 演化边（旧→新），0.5。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for f in files:
        d = f["rel_path"].rsplit("/", 1)[0] if "/" in f["rel_path"] else ""
        stem = _stem(f["rel_path"])
        base = _VERSION_TOKEN_RE.sub("", stem).strip("_-. ")
        if base and base != stem:
            groups.setdefault((d, base.lower()), []).append(f)
    edges = []
    for (d, base), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        def vkey(f):
            m = _VERSION_TOKEN_RE.search(_stem(f["rel_path"]))
            if not m:
                return (0, 0, 0, f["rel_path"])
            g = m.groups()
            return (int(g[0]), int(g[1] or 0), int(g[2] or 0), f["rel_path"])
        ordered = sorted(members, key=vkey)
        for older, newer in zip(ordered, ordered[1:]):
            if older["rel_path"] == newer["rel_path"]:
                continue
            edges.append({
                "src_path": older["rel_path"], "dst_path": newer["rel_path"],
                "relation": "evolves_to", "source": "evolves_to@1",
                "confidence": 0.5,
                "basis": f"同目录同名干、版本号递增（{older['rel_path']} → {newer['rel_path']}）；命名推测",
            })
            if len(edges) >= MAX_EDGES_PER_RULE:
                return edges
    return edges


# 可扩展注册点：每条规则 (id, version, fn, 说明)。规则可单独启停（investigate 模块复用同一机制）。
RULES = [
    ("test_module", 1, rule_test_module, "测试文件名约定匹配被测模块"),
    ("content_reference", 1, rule_content_reference, "文本内容中出现其它文件路径（内容证据）"),
    ("same_run", 1, rule_same_run, "运行目录内参数/日志 → 输出"),
    ("builds_from", 1, rule_builds_from, "发布产物与源码目录词元匹配"),
    ("evolves_to", 1, rule_evolves_to, "同目录版本号递增的演化对"),
]

RULE_LABELS = {rid: RELATION_LABELS[{"test_module": "test_module", "content_reference": "references",
                                     "same_run": "same_run", "builds_from": "builds_from",
                                     "evolves_to": "evolves_to"}[rid]] for rid, *_ in RULES}


def build_relations(db: Database, project_id: int, snapshot_id: int,
                    enabled: set[str] | None = None) -> list[dict]:
    """对快照执行全部（或指定）关系规则，返回边列表（不写库）。"""
    rows = db.query(
        "SELECT rel_path, size, sha256, category, read_error FROM file_records WHERE snapshot_id=?",
        (snapshot_id,))
    files = [{"rel_path": _norm(r["rel_path"]), "size": r["size"] or 0,
              "category": r["category"]} for r in rows if not r["read_error"]]
    project = db.get_project(project_id)
    root = project["path"] if project else ""
    edges: list[dict] = []
    now = utcnow_iso()
    for rid, _ver, fn, _desc in RULES:
        if enabled is not None and rid not in enabled:
            continue
        for e in fn(files, root):
            e["created_at"] = now
            edges.append(e)
    return edges


def run_relation_analysis(db: Database, project_id: int, snapshot_id: int,
                          enabled: set[str] | None = None) -> dict:
    """执行并持久化关系边（按规则来源独立替换，幂等）。"""
    enabled = enabled or {rid for rid, *_ in RULES}
    edges = build_relations(db, project_id, snapshot_id, enabled)
    n = db.replace_relations(project_id, snapshot_id, edges, sources=sorted(enabled))
    return {"snapshot_id": snapshot_id, "edges": n,
            "rules": sorted(enabled)}


def trace_relations(db: Database, project_id: int, snapshot_id: int, rel_path: str,
                    direction: str = "both", max_depth: int = 3,
                    max_nodes: int = 80) -> dict:
    """从某文件出发沿关系边 BFS 反向/正向追踪，返回子图（节点+边）。
    direction: upstream（谁产生了它）/ downstream（它影响了谁）/ both。"""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    frontier = [(rel_path, 0)]
    seen_paths = {rel_path}
    nodes[rel_path] = {"rel_path": rel_path, "depth": 0}
    rows = db.query(
        "SELECT src_path, dst_path, relation, source, basis, confidence FROM relations "
        "WHERE project_id=? AND snapshot_id=?", (project_id, snapshot_id))
    adj: dict[str, list] = {}
    for r in rows:
        adj.setdefault(r["src_path"], []).append(dict(r))
        adj.setdefault(r["dst_path"], []).append(dict(r))
    while frontier:
        cur, depth = frontier.pop(0)
        if depth >= max_depth or len(nodes) >= max_nodes:
            continue
        for e in adj.get(cur, []):
            other = e["dst_path"] if e["src_path"] == cur else e["src_path"]
            if direction == "upstream" and e["dst_path"] != cur:
                continue
            if direction == "downstream" and e["src_path"] != cur:
                continue
            if e not in edges:
                edges.append(e)
            if other not in seen_paths and len(nodes) < max_nodes:
                seen_paths.add(other)
                nodes[other] = {"rel_path": other, "depth": depth + 1}
                frontier.append((other, depth + 1))
    from .dossier import lookup_file_record
    fr = lookup_file_record(db, snapshot_id, rel_path)
    return {"root": rel_path, "nodes": sorted(nodes.values(), key=lambda x: (x["depth"], x["rel_path"])),
            "edges": edges, "root_record": dict(fr) if fr else None}
