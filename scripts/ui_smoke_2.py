"""2.0 UI 冒烟：成果档案 + 复现检查页签的真实 Tk 全链路。

运行前不执行被扫描目录中的任何程序；仅调用应用自身的只读分析 API。
样例目录 scripts/demo_lab 是开发期构建的固定实验布局样例（真实磁盘文件，
非用户科研数据）。数据库与导出报告均写入系统临时目录，不弄脏仓库。
"""
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
        app.on_export_forensics()
        assert os.path.exists(fs)
        ftext = open(fs, encoding="utf-8").read()
        assert ("# 项目取证报告" in ftext and "## 二、成果档案" in ftext
                and "## 五、复现检查" in ftext and "评分规则（完全公开）" in ftext)
        print(f"PASS | 完整取证报告导出 · {len(ftext)} 字符")

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
