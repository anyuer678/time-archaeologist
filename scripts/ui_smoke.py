"""UI 冒烟测试：两阶段。

阶段一（同步）：预置已扫描项目，构建主窗口，检查五个区块刷新与分页控件。
阶段二（异步）：通过 App._start_scan 走真实后台线程扫描 → 队列 → 轮询 → 刷新全流程。

用法：python scripts/ui_smoke.py
"""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk

from time_arch.db import Database
from time_arch.scanner import run_scan
from time_arch.ui.app import App
from time_arch.ui.theme import apply_theme

OK = []


def check(name, cond, detail=""):
    OK.append((name, bool(cond)))
    mark = "PASS " if cond else "FAIL "
    suffix = f"  [{detail}]" if detail else ""
    print(mark + name + suffix)


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    root_proj = Path(tmp.name) / "demo_proj"
    (root_proj / "src").mkdir(parents=True)
    (root_proj / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (root_proj / "logs").mkdir()
    (root_proj / "logs" / "run.log").write_text("ok\n", encoding="utf-8")
    (root_proj / "dist").mkdir()
    (root_proj / "dist" / "app-0.1.zip").write_bytes(b"zip")

    async_proj = Path(tmp.name) / "async_proj"   # 阶段二用：未扫描
    async_proj.mkdir()
    (async_proj / "hello.py").write_text("y = 2\n", encoding="utf-8")

    db = Database(Path(tmp.name) / "ui.db")
    pid, _ = db.add_project(str(root_proj), name="demo_proj")
    pid2, _ = db.add_project(str(async_proj), name="async_proj")
    res = run_scan(db, pid)
    assert res.status == "completed"

    win = tk.Tk()
    apply_theme(win)
    app = App(win, db)
    app.selected_project_id = pid
    app.refresh_projects(select_first=True)

    state = {"phase": 1, "scan_done": False}

    def phase1_checks():
        try:
            check("P1 项目列表非空", app.tree_projects.get_children())
            check("P1 概览类别表非空", app.ov_cat.get_children())
            check("P1 快照历史表非空", app.ov_snaps.get_children())
            check("P1 时间线有事件", len(app.tl_tree.get_children()) > 0)
            check("P1 最近发现有事件", len(app.recent_tree.get_children()) > 0)
            check("P1 成果证据有线索", len(app.fd_tree.get_children()) > 0)
            check("P1 快照对比下拉已填充", app.cmp_old["values"])
            check("P1 概览含最近活动", "最近活动" in app.ov_info.get())
            from time_arch.scanner import ScanProgress
            app.scan_queue.put(("progress", ScanProgress(files=3, bytes_hashed=100, errors=0,
                                                         current="x.py", phase="walk")))
            app._poll_scan_queue()
            check("P1 扫描状态条可更新", "3 个文件" in app.scan_label_var.get())
            app.tl_page = 0
            app.refresh_timeline()
            check("P1 时间线分页文本", "第 1/" in app.tl_page_var.get())
        except Exception as e:  # noqa: BLE001
            check("P1 无异常", False)
            print("  异常:", e)
        # 阶段二：真实异步扫描（UI 线程发起 → worker → 队列 → _handle_scan_done）
        try:
            state["ev_before"] = db.count_events(project_id=pid2)
            app._start_scan(pid2)
            check("P2 扫描已启动", app._scan_running)
            state["phase"] = 2
        except Exception as e:  # noqa: BLE001
            check("P2 扫描已启动", False)
            print("  异常:", e)
            finish()

    def wait_scan(timeout_at=time.monotonic() + 30):
        if app.scan_label_var.get().startswith("扫描状态：完成") or \
           app.scan_label_var.get().startswith("扫描状态：出错") or \
           app.scan_label_var.get().startswith("扫描状态：已取消"):
            state["scan_done"] = True
            phase2_checks()
            return
        if time.monotonic() > timeout_at:
            check("P2 异步扫描在 30s 内完成", False)
            finish()
            return
        win.after(200, wait_scan)

    def phase2_checks():
        try:
            check("P2 异步扫描完成", app.scan_label_var.get().startswith("扫描状态：完成"))
            check("P2 扫描状态条归位", not app._scan_running)
            check("P2 项目自动选中", str(pid2) in app.tree_projects.selection())
            n_ev_after = db.count_events(project_id=pid2)
            check("P2 事件已入库", n_ev_after >= 1 and n_ev_after > state["ev_before"],
                  f"{state['ev_before']}→{n_ev_after} 条")
            check("P2 概览刷新为新项目", f"#{pid2}" in app.ov_info.get())
        except Exception as e:  # noqa: BLE001
            check("P2 无异常", False)
            print("  异常:", e)
        finally:
            finish()

    def finish():
        win.destroy()

    # 阶段二轮询在 _start_scan 之后启动
    orig_start = app._start_scan

    def start_and_watch(project_id):
        orig_start(project_id)
        win.after(200, wait_scan)

    app._start_scan = start_and_watch
    win.after(1500, phase1_checks)
    win.mainloop()
    tmp.cleanup()
    failed = [n for n, ok in OK if not ok]
    print(f"\n冒烟结果：{len(OK) - len(failed)}/{len(OK)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
