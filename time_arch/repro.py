"""实验复现检查器（只读）。

对一项成果（通常是实验运行目录）从快照与项目文件中收集复现所需证据：
实验参数、输入数据、随机种子、源码快照、运行环境与依赖、开始/结束记录、
输出结果、错误与警告、重复运行差异。

每项证据分四级：
  A 已有直接证据 / B 合理推测，需人工确认 / C 缺少必要证据 / D 相互矛盾的证据。

评分规则完全公开（见 scoring_rules_text，随每份报告输出）：
  总分 = Σ(权重 × 等级分)，等级分 A=1.0 B=0.6 C=0 D=0；
  总级别：任一项 D → D；关键项全部 A 且总分 ≥90 → A；
  任一关键项 C/D 或总分 <50 → C；其余 → B。
  总分只衡量证据覆盖度，不代表实验成功率。

铁律：只读（stat/scandir/open(rb)），不执行被扫描目录中的任何程序，
不联网、不安装依赖、不下载文件。
"""
from __future__ import annotations

import json
import os
import re

from .db import Database
from .dossier import _METRICS_FILE_RE, _Files, _norm, run_dir_of
from .util import read_text_head, utcnow_iso

ITEM_WEIGHTS = {
    "实验参数": 15, "输入数据": 10, "随机种子": 10, "源码快照": 15,
    "运行环境与依赖": 15, "开始与结束记录": 10, "输出结果": 10,
    "错误与警告": 5, "重复实验差异": 10,
}
LEVEL_POINTS = {"A": 1.0, "B": 0.6, "C": 0.0, "D": 0.0}
CRITICAL_ITEMS = ("实验参数", "输入数据", "源码快照", "输出结果")
LEVEL_LABELS = {"A": "A 已有直接证据", "B": "B 合理推测，需人工确认",
                "C": "C 缺少必要证据", "D": "D 相互矛盾的证据"}

TEXT_LIMIT = 256 * 1024

_SEED_RE = re.compile(r"(?:random_)?seed\s*[=:]\s*(\d{1,10})|manual_seed\((\d+)\)",
                      re.IGNORECASE)
_START_RE = re.compile(r"\bstart(ed)?\b|\bbegin\b|\brunning\b", re.IGNORECASE)
_END_RE = re.compile(r"\bfinish(ed)?\b|\bcomplete(d)?\b|\bdone\b|\bsucceed(ed)?\b",
                     re.IGNORECASE)
_ERROR_RE = re.compile(r"Traceback|Exception|AssertionError|(?<![A-Za-z])(?:ERROR|Error"
                       r"|FAILED|FATAL)(?![A-Za-z])|exit code [1-9]")
_PARAM_HINT_RE = re.compile(r"param|config|setting|arg|hyper|option", re.IGNORECASE)
_PARAM_EXTS = {".yaml", ".yml", ".toml", ".ini", ".cfg"}
_ENV_NAME_RE = re.compile(
    r"requirements.*|environment[^/]*\.ya?ml|pyproject\.toml|setup\.(?:py|cfg)|"
    r"pipfile|conda\.yaml|pip[-_]freeze[^/]*", re.IGNORECASE)
_ROW_COUNT_EXTS = {".csv", ".tsv", ".jsonl", ".ndjson"}


def scoring_rules_text() -> list[str]:
    """公开评分规则（每份报告都原样输出，用户可逐项核对扣分/加分依据）。"""
    return [
        "权重：" + "，".join(f"{k} {v}" for k, v in ITEM_WEIGHTS.items()) + "（合计 100）",
        "等级分：A=1.0，B=0.6，C=0，D=0；总分 = Σ(权重 × 等级分)，四舍五入",
        "总分只衡量证据覆盖度，不代表实验成功率，也不是复现概率",
        "总级别：任一项为 D → D；否则关键项（" + "/".join(CRITICAL_ITEMS) +
        "）全部 A 且总分 ≥90 → A；否则任一关键项 C/D 或总分 <50 → C；其余 → B",
        "关键项 = " + "、".join(CRITICAL_ITEMS),
    ]


def _stem(p: str) -> str:
    name = _norm(p).rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _name(p: str) -> str:
    return _norm(p).rsplit("/", 1)[-1]


def _quotes(text: str, regex: re.Pattern, limit: int = 3) -> list[str]:
    """引用匹配行（带行号、截断），作为可核对的证据。"""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if regex.search(line):
            out.append(f"第{i}行：{line.strip()[:120]}")
            if len(out) >= limit:
                break
    return out


def _sha_short(sha: str | None) -> str:
    return f"SHA-256 {sha[:12]}…" if sha else "SHA-256 未计算（读取失败）"


def _overall_level(checks: list[dict], score: int) -> str:
    crit = {c["level"] for c in checks if c["item"] in CRITICAL_ITEMS}
    if any(c["level"] == "D" for c in checks):
        return "D"
    if crit <= {"A"} and score >= 90:
        return "A"
    if (crit & {"C", "D"}) or score < 50:
        return "C"
    return "B"


def check_repro(db: Database, project_id: int, snapshot_id: int, target_path: str,
                artifact_id: int | None = None) -> dict:
    """对 target_path 所在运行目录做复现检查（纯分析，不写库）。返回：
    {target_path, run_dir, level, score, checks[], scoring_rules, generated_at}"""
    project = db.get_project(project_id)
    root = project["path"] if project else ""
    rows = db.query(
        "SELECT rel_path, size, sha256, category, mtime, read_error "
        "FROM file_records WHERE snapshot_id=?", (snapshot_id,))
    files = _Files(rows)
    target = _norm(target_path)
    rd = run_dir_of(target) or (_norm(target).rsplit("/", 1)[0]
                                if "/" in _norm(target) else "")
    members = files.under_dir(rd)
    checks: list[dict] = []

    def add(item: str, level: str, detail: str, evidence: list[str] | None = None):
        checks.append({"item": item, "level": level, "detail": detail,
                       "evidence": evidence or []})

    def texts(cands: list[dict]) -> list[tuple[str, str]]:
        out = []
        for f in sorted(cands, key=lambda x: x["rel_path"]):
            t = read_text_head(root, f["rel_path"], TEXT_LIMIT)
            if t:
                out.append((f["rel_path"], t))
        return out

    # 1 实验参数：运行目录内的参数/配置文件（排除指标类输出）
    param_files = []
    for m in members:
        ext = os.path.splitext(_name(m["rel_path"]))[1].lower()
        if (m["category"] == "config" and not _METRICS_FILE_RE.search(m["rel_path"])
                and (_PARAM_HINT_RE.search(_name(m["rel_path"])) or ext in _PARAM_EXTS)):
            param_files.append(m)
    if param_files:
        ev = [f"{f['rel_path']}（{f['size']}B，{_sha_short(f['sha256'])}）"
              for f in param_files[:4]]
        add("实验参数", "A", f"运行目录内发现 {len(param_files)} 个参数/配置文件", ev)
    else:
        proj_cfg = [f for f in files.by_cat.get("config", [])
                    if not _METRICS_FILE_RE.search(f["rel_path"])][:3]
        if proj_cfg:
            add("实验参数", "B",
                "运行目录内未见参数文件；项目内存在配置文件，是否属于本次运行需人工确认",
                [f["rel_path"] for f in proj_cfg])
        else:
            add("实验参数", "C", "未发现任何参数/配置文件（快照范围内）")

    # 2 输入数据：运行目录内的数据文件
    data_files = [m for m in members if m["category"] == "data"]
    if data_files:
        ev = []
        for f in data_files[:4]:
            detail = f"{f['rel_path']}（{f['size']}B，{_sha_short(f['sha256'])}）"
            if _name(f["rel_path"]).rsplit(".", 1)[-1].lower() in _ROW_COUNT_EXTS:
                t = read_text_head(root, f["rel_path"], TEXT_LIMIT)
                if t is not None:
                    n = len([l for l in t.splitlines() if l.strip()])
                    detail += f"，文本头部可见 {n} 行"
            ev.append(detail)
        add("输入数据", "A", f"运行目录内发现 {len(data_files)} 个数据文件", ev)
    else:
        add("输入数据", "C", "运行目录内未见输入数据文件（其标识需人工核对参数中的路径）")

    # 3 随机种子：参数/源码/日志文本中的种子设定
    seed_hits: list[str] = []
    seed_srcs = param_files + [m for m in members if m["category"] in ("source", "log")]
    for rel, t in texts(seed_srcs):
        for q in _quotes(t, _SEED_RE):
            seed_hits.append(f"{rel} {q}")
    if seed_hits:
        add("随机种子", "A", "在参数/源码/日志中发现随机种子设定", seed_hits[:4])
    elif any(m["category"] == "source" for m in members):
        add("随机种子", "B", "运行目录内有源码但未发现种子设定（可能在别处或未固定）")
    else:
        add("随机种子", "C", "无可检查的参数/源码文本，无法确认种子是否固定")

    # 4 源码快照：快照记录的源码文件及哈希（项目级）
    srcs = files.by_cat.get("source", [])
    if srcs:
        ev = [f"{f['rel_path']}（{_sha_short(f['sha256'])}）" for f in srcs[:3]]
        add("源码快照", "A", f"快照已记录 {len(srcs)} 个源码文件的内容哈希，"
                             "可验证当前源码是否与产生结果时一致", ev)
    else:
        add("源码快照", "C", "快照中未发现源码文件")

    # 5 运行环境与依赖：依赖声明文件，区分是否固定版本
    env_files = [f for f in files.by_path.values() if _ENV_NAME_RE.fullmatch(_name(f["rel_path"]))]
    if env_files:
        pinned = False
        ev = []
        for f in env_files[:3]:
            t = read_text_head(root, f["rel_path"], TEXT_LIMIT)
            has_pin = bool(t) and ("==" in t or "~=" in t)
            pinned = pinned or has_pin
            ev.append(f"{f['rel_path']}" + ("（含固定版本）" if has_pin else "（未见版本固定）"))
        add("运行环境与依赖", "A" if pinned else "B",
            "发现依赖声明文件" + ("且包含固定版本号" if pinned else
                                "，但未见版本固定（环境差异可能导致结果不同）"), ev)
    else:
        add("运行环境与依赖", "C", "未发现 requirements/environment/pyproject 等依赖声明文件")

    # 6 开始与结束记录：日志中的开始/结束迹象
    log_files = [m for m in members if m["category"] == "log"]
    log_texts = texts(log_files)
    if log_texts:
        starts: list[str] = []
        ends: list[str] = []
        for rel, t in log_texts:
            starts += [f"{rel} {q}" for q in _quotes(t, _START_RE, 2)]
            ends += [f"{rel} {q}" for q in _quotes(t, _END_RE, 2)]
        if starts and ends:
            add("开始与结束记录", "A", "日志中同时发现开始与结束迹象", (starts + ends)[:4])
        elif starts or ends:
            add("开始与结束记录", "B", "日志只发现开始或结束单侧迹象，运行是否完整需人工确认",
                (starts + ends)[:4])
        else:
            add("开始与结束记录", "B", "日志存在但未见明确的开始/结束标记")
    else:
        add("开始与结束记录", "C", "运行目录内未见日志文件")

    # 7 输出结果：指标类命名或数据类文件
    outputs = [m for m in members
               if _METRICS_FILE_RE.search(m["rel_path"]) or m["category"] == "data"]
    if outputs:
        ev = [f"{f['rel_path']}（{f['size']}B，{_sha_short(f['sha256'])}）" for f in outputs[:4]]
        add("输出结果", "A", f"运行目录内发现 {len(outputs)} 个结果文件", ev)
    else:
        add("输出结果", "C", "运行目录内未见结果/指标文件")

    # 8 错误与警告：日志错误迹象；有错但结果存在 → D（矛盾）
    err_items = [c for c in checks if c["item"] == "输出结果"]
    has_output = err_items and err_items[0]["level"] == "A"
    if log_texts:
        errs: list[str] = []
        for rel, t in log_texts:
            errs += [f"{rel} {q}" for q in _quotes(t, _ERROR_RE)]
        if errs and has_output:
            add("错误与警告", "D",
                "日志显示异常/错误，但运行目录中同时存在看似完整的结果文件——"
                "相互矛盾的证据，需人工核实结果是否产生于出错之前", errs[:4])
        elif errs:
            add("错误与警告", "C", "日志显示异常/错误，且未见成功输出（记录一致地失败）", errs[:4])
        else:
            add("错误与警告", "A", "已检查日志文本，未见错误/异常记录")
    else:
        add("错误与警告", "C", "无日志可检查，无法确认运行是否报错")

    # 9 重复实验差异：同级运行目录中同名结果文件的哈希对比（逐个输出找对比样本）
    if rd and "/" in rd and outputs:
        parent = rd.rsplit("/", 1)[0]
        chosen = None
        for primary in sorted(outputs, key=lambda x: x["rel_path"]):
            out_name = _name(primary["rel_path"])
            sibs = [f for f in files.by_path.values()
                    if f["rel_path"].startswith(parent + "/")
                    and (srd := run_dir_of(f["rel_path"])) and srd != rd
                    and _name(f["rel_path"]) == out_name]
            if sibs:
                chosen = (primary, sibs)
                break
        if chosen:
            primary, sibs = chosen
            same = [f for f in sibs if primary["sha256"] and f["sha256"] == primary["sha256"]]
            if same:
                add("重复实验差异", "A",
                    f"以 {primary['rel_path']} 对比 {len(sibs)} 份同名结果："
                    f"{len(same)} 份内容完全一致（SHA-256 相同），支持确定性复现",
                    [f"{primary['rel_path']} ≡ {f['rel_path']}" for f in same[:3]])
            elif primary["sha256"]:
                add("重复实验差异", "B",
                    f"{len(sibs)} 份同名结果内容不同（可能是参数不同或非确定性），"
                    "需人工对比对应参数差异",
                    [f"{primary['rel_path']} ≢ {f['rel_path']}" for f in sibs[:3]])
            else:
                add("重复实验差异", "B", "存在同名结果但主文件哈希缺失（读取失败），无法对比")
        else:
            add("重复实验差异", "C", "同级运行目录中未发现同名结果文件，无对比样本")
    else:
        add("重复实验差异", "C", "目标不在实验运行目录或无结果文件，未做重复运行对比")

    # 校验完整性：九项齐全
    missing = set(ITEM_WEIGHTS) - {c["item"] for c in checks}
    for item in missing:  # 防御：探测逻辑漏项时显式标注，而非静默
        add(item, "C", "检查器未覆盖该项（实现缺口，请报告）")

    score = round(sum(ITEM_WEIGHTS[c["item"]] * LEVEL_POINTS[c["level"]] for c in checks))
    level = _overall_level(checks, score)
    return {
        "project_id": project_id, "snapshot_id": snapshot_id, "artifact_id": artifact_id,
        "target_path": target, "run_dir": rd or "(项目根)", "level": level,
        "score": score, "checks": checks, "scoring_rules": scoring_rules_text(),
        "generated_at": utcnow_iso(),
    }


def run_repro_check(db: Database, project_id: int, snapshot_id: int, target_path: str,
                    artifact_id: int | None = None) -> dict:
    """执行并持久化复现检查（幂等：同快照同目标整行替换）。"""
    result = check_repro(db, project_id, snapshot_id, target_path, artifact_id)
    cid = db.save_repro_check(
        project_id, snapshot_id, result["target_path"], result["level"],
        result["score"], json.dumps(result["checks"], ensure_ascii=False),
        artifact_id, result["generated_at"])
    result["id"] = cid
    return result


def load_repro_check(db: Database, check_id: int) -> dict | None:
    row = db.get_repro_check(check_id)
    if row is None:
        return None
    try:
        checks = json.loads(row["checks"])
    except (TypeError, ValueError):
        checks = []
    d = dict(row)
    d["checks"] = checks
    d["scoring_rules"] = scoring_rules_text()
    return d


def build_repro_report(check: dict) -> str:
    """可解释的 Markdown 报告：逐项等级、权重、得分、证据引用、公开评分规则。"""
    lines = [f"# 复现就绪度报告：{check['target_path']}", ""]
    lines.append(f"- 运行目录：`{check.get('run_dir', '-')}`　快照：#{check['snapshot_id']}")
    lines.append(f"- 总级别：**{LEVEL_LABELS.get(check['level'], check['level'])}**"
                 f"　总分：{check['score']}/100")
    lines.append(f"- 生成时间：{check['generated_at']} (UTC)")
    lines += ["", "## 评分规则（完全公开）", ""]
    lines += [f"- {t}" for t in check.get("scoring_rules") or scoring_rules_text()]
    lines += ["", "## 逐项检查", "",
              "| 项目 | 等级 | 权重 | 得分 | 证据/说明 |", "|---|---|---|---|---|"]
    for c in check["checks"]:
        pts = round(ITEM_WEIGHTS[c["item"]] * LEVEL_POINTS[c["level"]], 1)
        ev = "；".join(c["evidence"][:3])
        text = c["detail"] + (f"（{ev}）" if ev else "")
        lines.append(f"| {c['item']} | {c['level']} | {ITEM_WEIGHTS[c['item']]} | {pts} | {text} |")
    lines += ["", "## 建议的后续检查（需人工执行，本工具不自动执行任何程序）", ""]
    todo = [c for c in check["checks"] if c["level"] in ("C", "D")]
    if todo:
        lines += [f"- 【{c['item']}·{c['level']}】{c['detail']}" for c in todo]
    else:
        lines.append("- 无：各项均有直接证据或已由人工确认。")
    lines += ["", "> 声明：本报告只读取文件与快照记录，未执行被扫描目录中的任何程序；"
              "等级反映证据覆盖度，不代表实验成功或失败。"]
    return "\n".join(lines) + "\n"
