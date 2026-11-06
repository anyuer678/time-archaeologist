"""2.0 UI 冒烟：成果档案 + 复现检查页签的真实 Tk 全链路。

运行前不执行被扫描目录中的任何程序；仅调用应用自身的只读分析 API。
样例目录 scripts/demo_lab 是开发期构建的固定实验布局样例（真实磁盘文件，
非用户科研数据）。数据库与导出报告均写入系统临时目录，不弄脏仓库。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from time_arch.db import Database
from time_arch.scanner import run_scan
from time_arch.ui.app import App
from time_arch.ui.theme import apply_theme

LAB = os.path.join(REPO, "scripts", "demo_lab")


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="ta_ui_smoke_2_")
    root = None
    try:
        db = Database(os.path.join(tmpdir, "smoke.db"))
        pid = db.add_project(LAB)[0]
        res = run_scan(db, pid, analyze=True)
        assert res.status == "completed" and res.snapshot_id, res

        root = tk.Tk()
        root.withdraw()
        apply_theme(root)
        app = App(root, db)
        app.selected_project_id = pid
        app.refresh_all_tabs()
        root.update()

        import tkinter.simpledialog as sd
        sd.askstring = lambda *a, **k: "演示快照"
        kids = app.ov_snaps.get_children()
        assert kids, "快照历史为空"
        app.ov_snaps.selection_set(kids[0])
        app.on_snapshot_note()
        assert db.list_snapshots(pid)[0]["note"] == "演示快照"
        print("PASS | 快照人工备注")

        # ---- 一键分析：关系 -> 线索 -> 档案 -> 任务 ----
        app.on_analyze_all()
        deadline = time.time() + 20
        while time.time() < deadline:
            root.update()
            if not app._full_analyzing and app.art_tree.get_children():
                break
            time.sleep(0.02)
        root.update()
        assert not app._full_analyzing
        assert app.ld_tree.get_children(), "线索页签为空"
        assert app.tk_tree.get_children(), "任务页签为空"
        print(f"PASS | 一键分析（关系/线索/档案/任务）· {app.scan_label_var.get()}")

        n_art = len(app.art_tree.get_children())
        assert n_art > 0, "成果档案列表为空"
        print(f"PASS | 成果档案列出 · {n_art} 份")

        first = app.art_tree.get_children()[0]
        app.art_tree.selection_set(first)
        app._show_artifact_detail()
        root.update()
        tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        assert tops, "成果档案详情对话框未创建"
        for w in tops:
            w.destroy()
        root.update()
        print("PASS | 成果档案详情对话框（已验证事实/未验证假设/完整性检查）")

        for status in ("confirmed", "rejected", "auto"):
            app.art_tree.selection_set(first)
            app._set_artifact(status)
            assert db.get_artifact(int(first))["status"] == status
        print("PASS | 成果档案人工确认/否决/恢复自动")

        # ---- 复现检查：候选 -> 运行 -> 规则 -> 历史 -> 导出 -> 校验 ----
        cands = app.rp_target.cget("values")
        assert "runs/exp03/metrics.json" in cands, cands[:5]
        app.rp_target.set("runs/exp03/metrics.json")
        app.on_run_repro()
        root.update()
        result = app._repro_result
        assert result is not None and result["level"] in ("A", "B", "C", "D")
        assert len(app.rp_tree.get_children()) == 9, len(app.rp_tree.get_children())
        rules = app.rp_rules.get("1.0", "end")
        assert "权重" in rules and "等级分" in rules
        print(f"PASS | 复现检查运行 · 级别 {result['level']} · "
              f"{len(app.rp_tree.get_children())} 项 · 规则公开")

        assert app._repro_history, "历史记录为空"
        app.rp_history.current(0)
        app.on_load_repro()
        root.update()
        assert "已载入历史记录" in app.rp_info.get()
        print("PASS | 历史复现检查载入")

        import tkinter.filedialog as fd
        import tkinter.messagebox as mb
        out = os.path.join(tmpdir, "repro_report.md")
        fd.asksaveasfilename = lambda *a, **k: out
        mb.showinfo = lambda *a, **k: None
        app.on_export_repro()
        assert os.path.exists(out)
        text = open(out, encoding="utf-8").read()
        assert ("复现就绪度报告" in text and "评分规则（完全公开）" in text
                and "逐项检查" in text)
        print(f"PASS | 复现报告导出 · {len(text)} 字符")

        fs = os.path.join(tmpdir, "forensics.md")
        fd.asksaveasfilename = lambda *a, **k: fs
        mb.askquestion = lambda *a, **k: "yes"
        app.on_export_forensics()
        assert os.path.exists(fs)
        ftext = open(fs, encoding="utf-8").read()
        assert ("# 项目取证报告" in ftext and "## 二、成果档案" in ftext
                and "## 五、复现检查" in ftext and "评分规则（完全公开）" in ftext)
        print(f"PASS | 完整取证报告导出 · {len(ftext)} 字符")

        js = os.path.join(tmpdir, "forensics.json")
        fd.asksaveasfilename = lambda *a, **k: js
        mb.askquestion = lambda *a, **k: "no"
        app.on_export_forensics()
        assert os.path.exists(js)
        jd = json.loads(open(js, encoding="utf-8").read())
        assert "artifacts" in jd and "tasks" in jd and "repro_checks" in jd
        print("PASS | 取证报告 JSON 导出")

        csv_path = os.path.join(tmpdir, "events.csv")
        fd.asksaveasfilename = lambda *a, **k: csv_path
        mb.showinfo = lambda *a, **k: None
        app.on_export_timeline_csv()
        assert os.path.exists(csv_path)
        csv_text = open(csv_path, encoding="utf-8-sig").read()
        assert "observed_at_utc" in csv_text and "rel_path" in csv_text
        print(f"PASS | 事件 CSV 导出 · {len(csv_text)} 字符")

        # ---- 项目级扫描排除（v7） ----
        import tkinter.simpledialog as sd
        sd.askstring = lambda *a, **k: ".git, node_modules"
        mb.showinfo = lambda *a, **k: None
        app.on_edit_excludes()
        assert db.get_project(pid)["excludes"] == ".git,node_modules"
        captured = {}

        def fake_start(project_id, on_done, on_progress=None, excludes=None):
            captured["excludes"] = excludes
            return False

        orig_start = app.service.start
        app.service.start = fake_start
        app.on_rescan()
        app.service.start = orig_start
        assert captured.get("excludes") == {".git", "node_modules"}, captured
        print("PASS | 扫描排除设置保存并传入扫描服务")

        sd.askstring = lambda *a, **k: "演示项目"
        app.on_rename_project()
        assert db.get_project(pid)["name"] == "演示项目"
        print("PASS | 项目重命名")

        app.on_project_overview()
        root.update()
        tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        assert tops, "项目摘要对话框未创建"
        for w in tops:
            w.destroy()
        root.update()
        print("PASS | 全部项目摘要对话框")

        # ---- 数据库维护 ----
        app.on_maintenance()
        root.update()
        assert app._maint_win.winfo_exists()
        app.on_db_integrity()
        root.update()
        assert "ok" in app._maint_info.get()
        bak = os.path.join(tmpdir, "backup.db")
        fd.asksaveasfilename = lambda *a, **k: bak
        mb.showinfo = lambda *a, **k: None
        app.on_db_backup()
        assert os.path.exists(bak)
        copy = Database(bak)
        assert copy.list_projects() and copy.integrity_check() == "ok"
        mb.askyesno = lambda *a, **k: True
        app.on_db_vacuum()
        root.update()
        assert "VACUUM" in app._maint_info.get()
        app._maint_win.destroy()
        print("PASS | 数据库完整性/备份/压缩")

        shown = []
        mb.showinfo = lambda title, msg, **k: shown.append(msg)
        app.rp_target.set("")
        app.on_run_repro()
        assert shown and "目标" in shown[0]
        print("PASS | 空目标输入给出提示（不崩溃）")

        print("UI SMOKE 2 PASS")
        return 0
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
