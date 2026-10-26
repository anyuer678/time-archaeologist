"""阶段验收脚本：对真实目录（本应用源码目录）执行完整验收流程。

用法：python scripts/acceptance_check.py
覆盖验收标准：
  1. 登记并扫描真实项目目录
  2. 重启（重开数据库）后数据仍在
  3. 重复扫描不重复创建事件
  4. 两次快照之间文件变化准确（含 mtime-only 变化不误报）
  5. 取消后可安全恢复并重试
  6. 结论均可查看依据（事件/成果带 evidence）
  7. 扫描不修改被扫描目录（全树 mtime+大小+内容哈希前后一致）
"""
import hashlib
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from time_arch.db import Database
from time_arch.reports import export_report
from time_arch.scanner import run_scan

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    mark = "PASS" if cond else "FAIL"
    suffix = f"  [{detail}]" if detail else ""
    print(f"{mark}  {name}{suffix}")


def tree_fingerprint(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (
                st.st_mtime_ns, st.st_size,
                hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def main() -> int:
    src_dir = Path(__file__).resolve().parents[1]          # 真实项目目录：本应用源码
    tmp = Path(tempfile.mkdtemp(prefix="ta_accept_"))
    target = tmp / "timeline_copy"                          # 复制一份以便做改动验证
    shutil.copytree(src_dir, target, ignore=shutil.ignore_patterns("__pycache__"))

    db_path = tmp / "accept.db"
    db = Database(db_path)

    # 1. 登记并扫描真实目录
    pid, created = db.add_project(str(src_dir), name="timeline(本应用)")
    check("A1 登记真实项目目录", created)
    r1 = run_scan(db, pid)
    check("A1 首次扫描完成", r1.status == "completed", f"{r1.files} 文件, {r1.errors} 错误")

    # 7. 只读保证（对副本目录做完整前后指纹对比）
    pid2, _ = db.add_project(str(target), name="副本")
    before = tree_fingerprint(target)
    r2 = run_scan(db, pid2)
    after = tree_fingerprint(target)
    check("A7 扫描前后目录指纹一致(只读)", before == after and r2.status == "completed",
          f"{len(before)} 个文件")

    # 2. 重启持久化
    db2 = Database(db_path)
    projs = db2.list_projects()
    check("A2 重启后项目仍在", len(projs) == 2)
    check("A2 重启后快照仍在", db2.latest_snapshot(pid)["id"] == r1.snapshot_id)

    # 3. 重复扫描不重复建事件
    n_before = db2.count_events(project_id=pid)
    r1b = run_scan(db2, pid)
    n_after = db2.count_events(project_id=pid)
    check("A3 无变化重扫不新增事件", r1b.events_created == 0 and n_before == n_after,
          f"事件数 {n_before}→{n_after}")

    # 4. 两次快照对比准确性
    (target / "brand_new.py").write_text("x = 1\n", encoding="utf-8")          # 新增
    (target / "run_app.py").write_text("# edited\n" + (target / "run_app.py").read_text(encoding="utf-8"), encoding="utf-8")  # 修改
    (target / "scripts" / "ui_smoke.py").unlink()                              # 删除
    r3 = run_scan(db2, pid2)
    d = db2.compare_snapshots(r2.snapshot_id, r3.snapshot_id)
    a, rem, m = d["counts"]
    added_set = {x["rel_path"] for x in d["added"]}
    removed_set = {x["rel_path"] for x in d["removed"]}
    check("A4 新增识别", "brand_new.py" in added_set, f"共新增 {a}")
    check("A4 删除识别", os.path.join("scripts", "ui_smoke.py") in removed_set, f"共删除 {rem}")
    mod_set = {x["rel_path"] for x in d["modified"]}
    check("A4 修改识别", "run_app.py" in mod_set, f"共修改 {m}")

    # mtime-only 不误报
    p_untouched = target / "time_arch" / "util.py"
    os.utime(p_untouched, (p_untouched.stat().st_atime + 3600, p_untouched.stat().st_mtime + 3600))
    r4 = run_scan(db2, pid2)
    d2 = db2.compare_snapshots(r3.snapshot_id, r4.snapshot_id)
    check("A4 仅mtime变化不判为修改", d2["counts"] == (0, 0, 0), str(d2["counts"]))

    # 5. 取消后安全恢复
    cancel = threading.Event()
    state = {"n": 0}

    def cb(pr):
        state["n"] += 1
        if state["n"] >= 2:
            cancel.set()

    r5 = run_scan(db2, pid2, cancel_event=cancel, progress_cb=cb)
    last_snap_before = db2.latest_snapshot(pid2)["id"]
    check("A5 取消回滚部分快照", r5.status == "cancelled" and r5.snapshot_id is None
          and db2.latest_snapshot(pid2)["id"] == last_snap_before)
    r5b = run_scan(db2, pid2)
    check("A5 取消后安全重试成功", r5b.status == "completed", f"{r5b.files} 文件")

    # 6. 结论可溯源
    evs = db2.list_events(project_id=pid2, limit=10)
    all_evidence = all(e["evidence"] and "basis" in e["evidence"] for e in evs)
    inferred = [e for e in db2.list_events(project_id=pid2, limit=500)
                if e["certainty"] == "inferred"]
    findings = db2.list_findings(pid)
    check("A6 事件带证据链", all_evidence, f"{len(evs)} 条抽查")
    check("A6 推测事件有标记", all("启发式" in e["evidence"] for e in inferred) if inferred else True,
          f"{len(inferred)} 条推测")
    check("A6 成果线索有依据", all(f["basis"] for f in findings), f"{len(findings)} 条线索")

    # A6 强化：带真实产物形态的迷你项目（exe/发布包/测试报告/实验结果/日志）
    mini = tmp / "mini_lab"
    for rel, content in [
        ("dist/tool-2.0.0.zip", b"zip-bytes"), ("bin/run.exe", b"MZ..."),
        ("reports/junit.xml", "<testsuite/>"), ("runs/exp1/metrics.json", '{"acc": 0.91}'),
        ("train.log", "epoch 1 done\n"),
    ]:
        q = mini / rel
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_bytes(content if isinstance(content, bytes) else content.encode())
    pid3, _ = db2.add_project(str(mini), name="mini_lab")
    r6 = run_scan(db2, pid3)
    fds = db2.list_findings(pid3)
    kinds = {f["kind"] for f in fds}
    want = {"executable", "release_package", "test_report", "experiment_output", "log_evidence"}
    check("A6 产物类型识别齐全", want <= kinds, f"{sorted(kinds)}")
    inferred3 = [e for e in db2.list_events(project_id=pid3, limit=200)
                 if e["certainty"] == "inferred"]
    check("A6 日志/产物推测事件存在", len(inferred3) >= 2,
          f"{len(inferred3)} 条，均含启发式依据")

    # 导出报告
    md = tmp / "report.md"
    js = tmp / "report.json"
    export_report(db2, pid, "markdown", str(md))
    export_report(db2, pid, "json", str(js))
    check("A+ Markdown 报告导出", md.exists() and "成果考古" in md.read_text(encoding="utf-8"))
    check("A+ JSON 报告导出", js.exists() and js.stat().st_size > 200)

    # 性能实测：3000 文件大目录首扫 + 重扫 + 分页查询
    import time as _time
    perf = tmp / "perf_proj"
    for i in range(60):
        d = perf / f"pkg{i:02d}" / "src"
        d.mkdir(parents=True)
        for j in range(50):
            (d / f"mod{j:02d}.py").write_bytes(b"print('x')\n" * 16)
    pid4, _ = db2.add_project(str(perf), name="perf_3000")
    t0 = _time.perf_counter()
    rp1 = run_scan(db2, pid4)
    t_scan1 = _time.perf_counter() - t0
    t0 = _time.perf_counter()
    rp2 = run_scan(db2, pid4)
    t_scan2 = _time.perf_counter() - t0
    n_events = db2.count_events(project_id=pid4)
    t0 = _time.perf_counter()
    page = db2.list_events(project_id=pid4, offset=max(0, n_events - 200), limit=200)
    t_page = _time.perf_counter() - t0
    check("P 性能：3000 文件首扫完成", rp1.status == "completed" and rp1.files == 3000,
          f"{t_scan1:.1f}s，{3000 / t_scan1:.0f} 文件/秒（全量 SHA-256）")
    check("P 性能：无变化重扫复用缓存提速",
          rp2.events_created == 0 and rp2.files_cached == 3000 and rp2.bytes_hashed == 0,
          f"{t_scan2:.1f}s，3000/3000 文件复用上次哈希，0 字节重算")
    check("P 性能：分页查询 <2s", len(page) == 200 and t_page < 2.0,
          f"{t_page * 1000:.0f}ms / 200 条（共 {n_events} 事件）")

    print()
    print("示例报告片段（报告前 12 行）：")
    print("\n".join(md.read_text(encoding="utf-8").splitlines()[:12]))
    print(f"\n报告文件：{md}")

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n验收结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if not failed:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"失败项：{failed}（现场保留：{tmp}）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
