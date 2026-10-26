"""成果考古：从快照文件记录中启发式识别潜在成果，并给出可追溯证据。

所有结论都是启发式判断（heuristic），在界面上必须以“置信度 + 依据 + 证据”呈现；
用户可人工确认（confirmed）或否决（rejected），三种状态严格区分。
"""
from __future__ import annotations

import os
import re

from .db import Database
from .util import utcnow_iso

KIND_LABELS = {
    "executable": "可执行文件",
    "release_package": "发布包",
    "test_report": "测试报告",
    "experiment_output": "实验产物",
    "log_evidence": "日志证据",
}

_EXECUTABLE_EXTS = {".exe", ".msi", ".msix", ".jar", ".apk", ".aab", ".whl", ".egg", ".appx"}
_RELEASE_ARCHIVE = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z"}
_VERSION_RE = re.compile(r"v?\d+\.\d+(\.\d+)?")
_RELEASE_DIR_RE = re.compile(r"(^|/)(dist|release|publish|artifacts?)(/|$)", re.IGNORECASE)
_RESULT_DIR_RE = re.compile(
    r"(^|/)(results?|runs?|outputs?|experiments?|evals?|eval_results|checkpoints?|figures?|plots?)(/|$)",
    re.IGNORECASE)
_TEST_REPORT_RE = re.compile(
    r"(^|/)(junit[^/]*\.xml|test[-_]?report|report[-_]?test|coverage\.xml|coverage\.json|"
    r"pytest[-_]report|htmlcov|lcov|cobertura)", re.IGNORECASE)
_EXPERIMENT_FILE_RE = re.compile(
    r"(metrics|accuracy|precision|recall|f1_score|eval|result|loss|reward|score)s?\.(json|jsonl|csv|txt|yaml|yml)$",
    re.IGNORECASE)


def _norm(rel_path: str) -> str:
    return rel_path.replace("\\", "/")


def detect_findings(rel_path: str, size: int, sha256: str | None,
                    category: str) -> list[dict]:
    """对单个文件记录做启发式判断，返回 0..n 条发现（含 kind/confidence/basis/evidence）。"""
    p = _norm(rel_path)
    lower = p.lower()
    name = lower.rsplit("/", 1)[-1]
    ext = os.path.splitext(name)[1]
    found: list[dict] = []

    def add(kind: str, confidence: str, basis: str, extra: dict | None = None):
        ev = {"rule": kind, "rel_path": p, "size": size, "sha256": sha256,
              "note": "启发式判断，非事实；可在界面人工确认或否决。"}
        if extra:
            ev.update(extra)
        found.append({"kind": kind, "confidence": confidence, "basis": basis, "evidence": ev})

    if ext in _EXECUTABLE_EXTS:
        add("executable", "high",
            f"扩展名 {ext} 是常见可分发执行/安装格式" + ("，且位于构建目录" if _RELEASE_DIR_RE.search(p) else ""))
    if _TEST_REPORT_RE.search(p):
        add("test_report", "high", f"路径匹配测试报告特征（junit/coverage/test-report 命名）")
    if _RELEASE_DIR_RE.search(p) and (ext in _RELEASE_ARCHIVE or _VERSION_RE.search(name)):
        add("release_package", "high",
            f"位于发布目录（dist/release 等）且文件名含版本号或为压缩包格式")
    elif ext in _RELEASE_ARCHIVE and _VERSION_RE.search(name):
        add("release_package", "medium", f"压缩包文件名含版本号（{name}），但不在标准发布目录")
    if _RESULT_DIR_RE.search(p):
        add("experiment_output", "high", f"位于实验/结果目录（results/runs/outputs 等）")
    if _EXPERIMENT_FILE_RE.search(p):
        add("experiment_output", "medium" if not _RESULT_DIR_RE.search(p) else "high",
            f"文件名匹配指标/结果文件特征（metrics、accuracy、eval 等）")
    if category == "log" and size > 0:
        add("log_evidence", "medium",
            f"日志类别文件（扩展名/路径特征），可能包含运行记录片段，可作为时间线佐证")
    return found


def analyze_snapshot(db: Database, project_id: int, snapshot_id: int) -> list[dict]:
    """对一个快照做成果考古，返回待写入 findings 的记录列表。"""
    rows = db.query(
        "SELECT rel_path, size, sha256, category FROM file_records WHERE snapshot_id=?",
        (snapshot_id,))
    files = [dict(r) for r in rows]

    now = utcnow_iso()
    findings: list[dict] = []
    for f in files:
        if f.get("read_error_marker"):
            continue
        for one in detect_findings(f["rel_path"], f["size"], f.get("sha256"), f["category"]):
            findings.append({
                "project_id": project_id, "snapshot_id": snapshot_id,
                "rel_path": f["rel_path"], "kind": one["kind"],
                "confidence": one["confidence"], "basis": one["basis"],
                "evidence": one["evidence"], "detected_at": now,
            })
    return findings
