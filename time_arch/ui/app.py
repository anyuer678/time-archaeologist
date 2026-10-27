"""时光考古机主界面（Tkinter）。

区块：项目概览 / 最近发现 / 活动时间线 / 成果证据 / 调查工作台 / 扫描状态 + 快照对比。
任何发现可双击查看证据来源并定位文件记录；时间线分页加载。
"""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import APP_NAME_CN, __version__
from ..analyze import KIND_LABELS
from ..classify import CATEGORY_LABELS
from ..db import Database
from ..investigate import LEADS_RULES, RULE_LABELS, lead_view, run_lead_analysis
from ..reports import export_report
from ..scanner import ScanProgress, ScanResult, ScanService
from ..util import fmt_size, fmt_time_local, utcnow_iso
from .theme import ACCENT, BG, FG, FG_DIM, PANEL, apply_theme

EVENT_LABELS = {
    "file_added": "新增", "file_modified": "修改", "file_removed": "删除",
    "log_generated": "日志生成※", "artifact_appeared": "产物出现※",
}
CERTAINTY_LABELS = {"observed": "观察事实", "inferred": "推测"}
FINDING_STATUS = {"auto": "自动判断", "confirmed": "人工确认", "rejected": "人工否决"}
LEAD_STATUS = {"open": "待查看", "acknowledged": "已知悉", "dismissed": "已忽略"}
PAGE_SIZE = 200


class App:
    def __init__(self, root: tk.Tk, db: Database):
        self.root = root
        self.db = db
        self.service = ScanService(db)
        self.scan_queue: queue.Queue = queue.Queue()
        self.project_rows: list = []
        self.selected_project_id: int | None = None
        self.tl_page = 0
        self._snap_map: dict[int, dict] = {}
        self._leads_running = False

        root.title(f"{APP_NAME_CN} Time Archaeologist v{__version__}")
        root.geometry("1220x780")
        root.minsize(980, 620)

        self._build_layout()
        self.refresh_projects(select_first=True)
        self.root.after(120, self._poll_scan_queue)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- 布局
    def _build_layout(self) -> None:
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(top, text="时光考古机", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="  只读扫描 · 证据可溯 · 推测与事实分明",
                  style="Dim.TLabel").pack(side="left", padx=8)
        ttk.Button(top, text="导出报告…", command=self.on_export).pack(side="right")

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=10, pady=8)

        # ---- 左侧：项目列表 ----
        side = ttk.Frame(body, style="Panel.TFrame", padding=8)
        side.pack(side="left", fill="y", padx=(0, 8))
        ttk.Label(side, text="已登记项目", style="Panel.TLabel").pack(anchor="w")
        self.tree_projects = ttk.Treeview(
            side, columns=("id", "name", "last"), show="headings", selectmode="browse", height=14)
        for col, text, w, anchor in (("id", "#", 30, "e"), ("name", "名称", 120, "w"),
                                     ("last", "最近扫描", 130, "w")):
            self.tree_projects.heading(col, text=text)
            self.tree_projects.column(col, width=w, anchor=anchor, stretch=(col == "name"))
        self.tree_projects.pack(fill="both", expand=True, pady=(6, 6))
        self.tree_projects.bind("<<TreeviewSelect>>", self._on_select_project)
        btns = ttk.Frame(side, style="Panel.TFrame")
        btns.pack(fill="x")
        ttk.Button(btns, text="登记项目", style="Accent.TButton",
                   command=self.on_register).pack(fill="x", pady=1)
        ttk.Button(btns, text="重新扫描", command=self.on_rescan).pack(fill="x", pady=1)
        ttk.Button(btns, text="移除项目", style="Danger.TButton",
                   command=self.on_remove).pack(fill="x", pady=1)

        # ---- 右侧主区 ----
        main = ttk.Frame(body)
        main.pack(side="left", fill="both", expand=True)

        # 扫描状态条
        self.scan_bar = ttk.Frame(main, style="Panel.TFrame", padding=(8, 4))
        self.scan_bar.pack(fill="x", pady=(0, 6))
        self.scan_progress = ttk.Progressbar(self.scan_bar, mode="indeterminate", length=180)
        self.scan_progress.pack(side="left")
        self.scan_label_var = tk.StringVar(value="扫描状态：空闲（未在扫描）")
        ttk.Label(self.scan_bar, textvariable=self.scan_label_var,
                  style="PanelDim.TLabel").pack(side="left", padx=10)
        self.btn_cancel = ttk.Button(self.scan_bar, text="取消扫描", style="Danger.TButton",
                                     command=self.on_cancel_scan, state="disabled")
        self.btn_cancel.pack(side="right")
        self._scan_running = False

        self.tabs = ttk.Notebook(main)
        self.tabs.pack(fill="both", expand=True)
        self._build_overview_tab()
        self._build_timeline_tab()
        self._build_recent_tab()
        self._build_findings_tab()
        self._build_leads_tab()
        self._build_compare_tab()

        status = ttk.Frame(self.root)
        status.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(status, text=f"数据库：{self.db.path}    只读扫描不会修改项目文件",
                  style="Dim.TLabel").pack(side="left")

    def _build_overview_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="  项目概览  ")
        self.ov_info = tk.StringVar(value="（左侧选择或登记一个项目）")
        ttk.Label(tab, textvariable=self.ov_info, wraplength=900,
                  justify="left").pack(anchor="w", pady=(0, 6))

        mid = ttk.Frame(tab)
        mid.pack(fill="both", expand=True)
        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ttk.Label(left, text="文件画像（最近快照）", style="Accent.TLabel").pack(anchor="w")
        cols = ("cat", "count", "size", "last", "note")
        self.ov_cat = ttk.Treeview(left, columns=cols, show="headings", height=9)
        for col, text, w, anchor in (("cat", "类别", 80, "w"), ("count", "文件数", 60, "e"),
                                     ("size", "体积", 80, "e"),
                                     ("last", "最近文件时间*", 130, "w"),
                                     ("note", "说明", 190, "w")):
            self.ov_cat.heading(col, text=text)
            self.ov_cat.column(col, width=w, anchor=anchor)

        def cat_note(c):
            return {"source": "源码文件（按扩展名启发式）", "build": "构建/依赖产物目录",
                    "test": "测试代码", "log": "日志文件", "doc": "文档",
                    "config": "配置文件", "data": "数据/归档文件",
                    "other": "未归类"}.get(c, "")
        self._cat_note = cat_note
        self.ov_cat.pack(fill="x", pady=(4, 2))
        ttk.Label(left, text="* 最近文件时间 = 该类别最新 mtime，仅为文件系统时间戳参考，"
                             "不代表真实工作时间。", style="Dim.TLabel").pack(anchor="w",
                                                                               pady=(0, 8))

        right = ttk.Frame(mid)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="扫描快照历史（完成的扫描）", style="Accent.TLabel").pack(anchor="w")
        cols = ("id", "time", "files", "size", "events")
        self.ov_snaps = ttk.Treeview(right, columns=cols, show="headings", height=9)
        for col, text, w, anchor in (("id", "快照", 50, "e"), ("time", "扫描时间", 150, "w"),
                                     ("files", "文件数", 70, "e"), ("size", "体积", 90, "e"),
                                     ("events", "事件数", 60, "e")):
            self.ov_snaps.heading(col, text=text)
            self.ov_snaps.column(col, width=w, anchor=anchor)
        self.ov_snaps.pack(fill="both", expand=True, pady=(4, 0))

    def _build_timeline_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="  活动时间线  ")

        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="项目").pack(side="left")
        self.tl_project = ttk.Combobox(bar, width=18, state="readonly", values=["全部项目"])
        self.tl_project.current(0)
        self.tl_project.pack(side="left", padx=(4, 12))
        ttk.Label(bar, text="类型").pack(side="left")
        self.tl_type = ttk.Combobox(
            bar, width=12, state="readonly",
            values=["全部"] + [EVENT_LABELS[k] for k in
                               ("file_added", "file_modified", "file_removed",
                                "log_generated", "artifact_appeared")])
        self.tl_type.current(0)
        self.tl_type.pack(side="left", padx=(4, 12))
        ttk.Label(bar, text="文件类别").pack(side="left")
        self._cat_values = ["全部类别"] + [CATEGORY_LABELS[k] for k in
                                           ("source", "config", "test", "doc", "log",
                                            "data", "build", "other")]
        self.tl_category = ttk.Combobox(bar, width=10, state="readonly",
                                        values=self._cat_values)
        self.tl_category.current(0)
        self.tl_category.pack(side="left", padx=(4, 12))
        ttk.Label(bar, text="关键词").pack(side="left")
        self.tl_keyword = ttk.Entry(bar, width=20)
        self.tl_keyword.pack(side="left", padx=(4, 12))
        ttk.Label(bar, text="日期从").pack(side="left")
        self.tl_since = ttk.Entry(bar, width=11)
        self.tl_since.pack(side="left", padx=(4, 2))
        ttk.Label(bar, text="至").pack(side="left")
        self.tl_until = ttk.Entry(bar, width=11)
        self.tl_until.pack(side="left", padx=(4, 12))
        ttk.Button(bar, text="查询", style="Accent.TButton", command=self.refresh_timeline
                   ).pack(side="left")
        for w in (self.tl_keyword, self.tl_since, self.tl_until):
            w.bind("<Return>", lambda e: self.refresh_timeline())
        self.tl_hint = tk.StringVar(value="日期格式 YYYY-MM-DD（按观察时间过滤）。※标记为推测事件。")
        ttk.Label(bar, textvariable=self.tl_hint, style="Dim.TLabel").pack(side="left", padx=8)

        cols = ("time", "event", "file", "category", "certainty", "project")
        self.tl_tree = ttk.Treeview(tab, columns=cols, show="headings")
        for col, text, w, anchor in (
                ("time", "观察时间(本地)", 150, "w"), ("event", "事件", 90, "w"),
                ("file", "文件", 420, "w"), ("category", "类别", 70, "w"),
                ("certainty", "确定性", 80, "w"), ("project", "项目", 110, "w")):
            self.tl_tree.heading(col, text=text)
            self.tl_tree.column(col, width=w, anchor=anchor, stretch=(col == "file"))
        self.tl_tree.pack(fill="both", expand=True, pady=(0, 4))
        self.tl_tree.bind("<Double-1>", lambda e: self._show_event_detail())
        self.tl_tree.tag_configure("inferred", foreground="#b08d57")

        pager = ttk.Frame(tab)
        pager.pack(fill="x")
        self.tl_count = tk.StringVar(value="0 条")
        ttk.Label(pager, textvariable=self.tl_count, style="Dim.TLabel").pack(side="left")
        ttk.Button(pager, text="◀ 上一页", command=lambda: self._tl_turn(-1)).pack(side="right")
        self.tl_page_var = tk.StringVar(value="第 1 页")
        ttk.Label(pager, textvariable=self.tl_page_var).pack(side="right", padx=6)
        ttk.Button(pager, text="下一页 ▶", command=lambda: self._tl_turn(1)).pack(side="right")

    def _build_recent_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="  最近发现  ")
        ttk.Label(tab, text="最近 30 条事件（全部项目，双击查看证据）",
                  style="Dim.TLabel").pack(anchor="w", pady=(0, 4))
        cols = ("time", "event", "file", "project", "certainty")
        self.recent_tree = ttk.Treeview(tab, columns=cols, show="headings")
        for col, text, w, anchor in (
                ("time", "观察时间(本地)", 150, "w"), ("event", "事件", 90, "w"),
                ("file", "文件", 480, "w"), ("project", "项目", 120, "w"),
                ("certainty", "确定性", 80, "w")):
            self.recent_tree.heading(col, text=text)
            self.recent_tree.column(col, width=w, anchor=anchor, stretch=(col == "file"))
        self.recent_tree.pack(fill="both", expand=True)
        self.recent_tree.bind("<Double-1>", lambda e: self._show_event_detail(tree=self.recent_tree))
        self.recent_tree.tag_configure("inferred", foreground="#b08d57")

    def _build_findings_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="  成果证据  ")
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 4))
        self.fd_info = tk.StringVar(value="（选择项目后显示成果考古结果）")
        ttk.Label(top, textvariable=self.fd_info, style="Dim.TLabel",
                  wraplength=900, justify="left").pack(side="left")
        ttk.Button(top, text="确认(人工)", command=lambda: self._set_finding("confirmed")
                   ).pack(side="right", padx=2)
        ttk.Button(top, text="否决(人工)", command=lambda: self._set_finding("rejected")
                   ).pack(side="right", padx=2)
        cols = ("status", "kind", "confidence", "file", "basis", "snapshot")
        self.fd_tree = ttk.Treeview(tab, columns=cols, show="headings")
        for col, text, w, anchor in (
                ("status", "状态", 80, "w"), ("kind", "类型", 90, "w"),
                ("confidence", "置信度", 60, "w"), ("file", "文件", 380, "w"),
                ("basis", "判断依据", 300, "w"), ("snapshot", "快照", 50, "e")):
            self.fd_tree.heading(col, text=text)
            self.fd_tree.column(col, width=w, anchor=anchor, stretch=(col in ("file", "basis")))
        self.fd_tree.pack(fill="both", expand=True)
        self.fd_tree.bind("<Double-1>", lambda e: self._show_finding_detail())
        for conf, color in (("high", "#9fbf7f"), ("medium", "#d8b35a"), ("low", "#a08a6a")):
            self.fd_tree.tag_configure(conf, foreground=color)
        self.fd_tree.tag_configure("rejected", foreground=FG_DIM)

    def _build_leads_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="  调查工作台  ")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Button(bar, text="分析线索（最新快照）", style="Accent.TButton",
                   command=self.on_analyze_leads).pack(side="left")
        ttk.Button(bar, text="规则启停…", command=self._show_rules_dialog
                   ).pack(side="left", padx=(6, 12))
        ttk.Label(bar, text="规则").pack(side="left")
        self.ld_rule = ttk.Combobox(
            bar, width=16, state="readonly",
            values=["全部规则"] + [RULE_LABELS[rid] for rid, *_ in LEADS_RULES])
        self.ld_rule.current(0)
        self.ld_rule.pack(side="left", padx=(4, 10))
        ttk.Label(bar, text="状态").pack(side="left")
        self.ld_status = ttk.Combobox(
            bar, width=8, state="readonly",
            values=["全部状态"] + [LEAD_STATUS[s] for s in
                                   ("open", "acknowledged", "dismissed")])
        self.ld_status.current(0)
        self.ld_status.pack(side="left", padx=(4, 10))
        ttk.Button(bar, text="筛选", command=self.refresh_leads).pack(side="left")

        self.ld_info = tk.StringVar(value="（左侧选择项目后，可对最新完成快照运行线索分析）")
        ttk.Label(tab, textvariable=self.ld_info, style="Dim.TLabel", wraplength=900,
                  justify="left").pack(anchor="w", pady=(0, 4))
        cols = ("status", "rule", "title", "target", "snapshot")
        self.ld_tree = ttk.Treeview(tab, columns=cols, show="headings")
        for col, text, w, anchor in (
                ("status", "状态", 70, "w"), ("rule", "规则", 140, "w"),
                ("title", "问题描述", 400, "w"), ("target", "目标", 230, "w"),
                ("snapshot", "快照", 50, "e")):
            self.ld_tree.heading(col, text=text)
            self.ld_tree.column(col, width=w, anchor=anchor,
                                stretch=(col in ("title", "target")))
        self.ld_tree.pack(fill="both", expand=True)
        self.ld_tree.bind("<Double-1>", lambda e: self._show_lead_detail())
        self.ld_tree.tag_configure("acknowledged", foreground="#9fbf7f")
        self.ld_tree.tag_configure("dismissed", foreground=FG_DIM)
        ttk.Label(tab, text="线索是「值得查看的问题」，不是结论。双击查看证据、可能的解释与"
                            "下一步建议；重跑分析会更新线索内容但保留人工状态。",
                  style="Dim.TLabel").pack(anchor="w", pady=(2, 0))

    def _build_compare_tab(self) -> None:
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="  快照对比  ")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="旧快照").pack(side="left")
        self.cmp_old = ttk.Combobox(bar, width=30, state="readonly")
        self.cmp_old.pack(side="left", padx=(4, 10))
        ttk.Label(bar, text="新快照").pack(side="left")
        self.cmp_new = ttk.Combobox(bar, width=30, state="readonly")
        self.cmp_new.pack(side="left", padx=(4, 10))
        ttk.Button(bar, text="对比", style="Accent.TButton",
                   command=self.on_compare).pack(side="left")
        self.cmp_result = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.cmp_result, style="Dim.TLabel").pack(side="left", padx=10)

        cols = ("type", "file", "detail")
        self.cmp_tree = ttk.Treeview(tab, columns=cols, show="headings")
        for col, text, w, anchor in (("type", "变化", 60, "w"), ("file", "文件", 560, "w"),
                                     ("detail", "详情", 380, "w")):
            self.cmp_tree.heading(col, text=text)
            self.cmp_tree.column(col, width=w, anchor=anchor, stretch=(col == "file"))
        self.cmp_tree.pack(fill="both", expand=True)
        for t, color in (("新增", "#9fbf7f"), ("删除", "#c98a7f"), ("修改", "#d8b35a")):
            self.cmp_tree.tag_configure(t, foreground=color)

    # ---------------------------------------------------------------- 项目
    def refresh_projects(self, select_first: bool = False) -> None:
        self.project_rows = self.db.list_projects()
        self.tree_projects.delete(*self.tree_projects.get_children())
        for p in self.project_rows:
            last = fmt_time_local(p["last_scan_at"])
            status = {"completed": "", "cancelled": " [已取消]",
                      "error": " [出错]", "interrupted": " [中断]"}.get(p["last_run_status"] or "", "")
            self.tree_projects.insert("", "end", iid=str(p["id"]),
                                      values=(p["id"], p["name"], f"{last}{status}"))
        names = ["全部项目"] + [f"{p['name']} (#{p['id']})" for p in self.project_rows]
        self.tl_project.configure(values=names)
        if select_first and self.project_rows and self.selected_project_id is None:
            first = self.project_rows[0]["id"]
            self.tree_projects.selection_set(str(first))
            self.selected_project_id = first
        self.refresh_all_tabs()

    def _selected_row(self, tree: ttk.Treeview) -> dict | None:
        sel = tree.selection()
        if not sel:
            return None
        return tree.item(sel[0], "values")

    def _on_select_project(self, _e=None) -> None:
        vals = self._selected_row(self.tree_projects)
        if vals:
            self.selected_project_id = int(vals[0])
            self.tl_page = 0
            self.refresh_all_tabs()

    def on_register(self) -> None:
        path = filedialog.askdirectory(title="选择要考古的项目目录（只读扫描）", parent=self.root)
        if not path:
            return
        pid, created = self.db.add_project(path)
        self.refresh_projects(select_first=True)
        self.tree_projects.selection_set(str(pid))
        if not created:
            messagebox.showinfo("已存在", f"该目录已登记过（项目 #{pid}），不会重复创建。",
                                parent=self.root)
        else:
            if messagebox.askyesno("登记成功", f"已登记：{path}\n\n是否立即开始只读扫描？",
                                   parent=self.root):
                self._start_scan(pid)

    def on_remove(self) -> None:
        if self.selected_project_id is None:
            return
        p = self.db.get_project(self.selected_project_id)
        if not messagebox.askyesno(
                "移除项目",
                f"将移除项目「{p['name']}」及其全部快照与事件记录。\n"
                "（仅移除考古数据库中的记录，不会动磁盘上的任何文件）\n\n确定？",
                parent=self.root):
            return
        self.db.remove_project(self.selected_project_id)
        self.selected_project_id = None
        self.refresh_projects(select_first=True)

    # ---------------------------------------------------------------- 扫描
    def on_rescan(self) -> None:
        if self.selected_project_id is None:
            messagebox.showinfo("提示", "请先在左侧选择一个项目。", parent=self.root)
            return
        self._start_scan(self.selected_project_id)

    def _start_scan(self, project_id: int) -> None:
        if self.service.busy or self._scan_running:
            messagebox.showinfo("提示", "已有扫描正在进行。", parent=self.root)
            return
        p = self.db.get_project(project_id)
        self._scan_running = True
        self.btn_cancel.configure(state="normal")
        self.scan_progress.start(24)
        self.scan_label_var.set(f"扫描状态：正在扫描「{p['name']}」… 准备中")
        ok = self.service.start(
            project_id,
            on_done=lambda r: self.scan_queue.put(("done", r)),
            on_progress=lambda pr: self.scan_queue.put(("progress", pr)),
        )
        if not ok:
            self._scan_idle()

    def on_cancel_scan(self) -> None:
        self.service.cancel()
        self.scan_label_var.set("扫描状态：正在取消（回滚未完成快照）…")
        self.btn_cancel.configure(state="disabled")

    def _scan_idle(self) -> None:
        self._scan_running = False
        self.scan_progress.stop()
        self.btn_cancel.configure(state="disabled")

    def _poll_scan_queue(self) -> None:
        try:
            while True:
                kind, payload = self.scan_queue.get_nowait()
                if kind == "progress":
                    pr: ScanProgress = payload
                    cached = f" · 复用缓存 {pr.files_cached} 个" if pr.files_cached else ""
                    self.scan_label_var.set(
                        f"扫描状态：{pr.files} 个文件 · 新哈希 {fmt_size(pr.bytes_hashed)}{cached} · "
                        f"{pr.errors} 个错误 · 当前 {pr.current[:60]}")
                elif kind == "done":
                    r: ScanResult = payload
                    self._handle_scan_done(r)
                elif kind == "leads_done":
                    self._handle_leads_done(payload)
                elif kind == "leads_error":
                    self._handle_leads_error(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_scan_queue)

    def _handle_scan_done(self, r: ScanResult) -> None:
        self._scan_idle()
        if r.status == "completed":
            extra = f"，产生 {r.events_created} 条事件、{r.findings_created} 条成果线索"
            if r.errors:
                extra += f"，{r.errors} 个文件/目录读取失败（已记录）"
            self.scan_label_var.set(
                f"扫描状态：完成 ✔ 项目#{r.project_id} {r.files} 个文件{extra}")
        elif r.status == "cancelled":
            self.scan_label_var.set(
                f"扫描状态：已取消（未产生部分快照，可随时安全重扫）· 已处理 {r.files} 个文件")
        else:
            msg = (r.error_messages or ["未知错误"])[0][:200]
            self.scan_label_var.set(f"扫描状态：出错 — {msg}")
            messagebox.showerror("扫描失败", msg, parent=self.root)
        self.refresh_projects()
        if r.project_id in [p["id"] for p in self.project_rows]:
            self.tree_projects.selection_set(str(r.project_id))

    # ---------------------------------------------------------------- 各页刷新
    def refresh_all_tabs(self) -> None:
        self.refresh_overview()
        self.refresh_timeline()
        self.refresh_recent()
        self.refresh_findings()
        self.refresh_leads()
        self.refresh_compare_choices()

    def refresh_overview(self) -> None:
        for t in self.ov_cat.get_children():
            self.ov_cat.delete(t)
        for t in self.ov_snaps.get_children():
            self.ov_snaps.delete(t)
        pid = self.selected_project_id
        if pid is None:
            self.ov_info.set("（左侧选择或登记一个项目）")
            return
        p = self.db.get_project(pid)
        if p is None:
            self.ov_info.set("（项目已移除）")
            return
        snaps = self.db.list_snapshots(pid)
        latest = self.db.latest_snapshot(pid)
        files_txt = "—"
        if latest:
            st = self.db.snapshot_stats(latest["id"])
            files_txt = (f"{st['files']} 个文件 · {fmt_size(st['total_size'])} · "
                         f"读取错误 {st['errors']} 个（快照 #{latest['id']}，"
                         f"{fmt_time_local(latest['created_at'])}）")
            for cat, n, sz, last_mtime in st["by_category"]:
                self.ov_cat.insert("", "end", values=(
                    CATEGORY_LABELS.get(cat, cat), n, fmt_size(sz),
                    fmt_time_local(last_mtime), self._cat_note(cat)))
        recent_ev = self.db.list_events(project_id=pid, limit=1)
        recent_txt = (f"最近活动（观察事实）：{EVENT_LABELS.get(recent_ev[0]['event_type'], recent_ev[0]['event_type'])} "
                      f"`{recent_ev[0]['rel_path']}`，观察于 {fmt_time_local(recent_ev[0]['observed_at'])}"
                      if recent_ev else "最近活动：尚无事件（完成两次扫描后开始记录变化）")
        self.ov_info.set(
            f"项目 #{p['id']}「{p['name']}」\n路径：{p['path']}\n"
            f"登记于 {fmt_time_local(p['created_at'])} · 最近扫描 {fmt_time_local(p['last_scan_at'])}\n"
            f"最近快照：{files_txt} · 完成的扫描共 {len(snaps)} 次\n{recent_txt}")
        for s in snaps[:50]:
            self.ov_snaps.insert("", "end", values=(
                f"#{s['id']}", fmt_time_local(s["created_at"]), s["file_count"],
                fmt_size(s["total_size"]), s["event_count"]))

    def _tl_filter_values(self) -> dict:
        proj = self.tl_project.get()
        pid = None
        if proj != "全部项目":
            for p in self.project_rows:
                if f"(#{p['id']})" in proj:
                    pid = p["id"]
                    break
        label = self.tl_type.get()
        type_map = {v: k for k, v in EVENT_LABELS.items()}
        cat_label = self.tl_category.get()
        cat_map = {v: k for k, v in CATEGORY_LABELS.items()}
        return {"project_id": pid, "event_type": type_map.get(label),
                "category": cat_map.get(cat_label),
                "keyword": self.tl_keyword.get().strip() or None,
                "since": self.tl_since.get().strip() or None,
                "until": self.tl_until.get().strip() or None}

    def refresh_timeline(self) -> None:
        for t in self.tl_tree.get_children():
            self.tl_tree.delete(t)
        f = self._tl_filter_values()
        total = self.db.count_events(**f)
        rows = self.db.list_events(offset=self.tl_page * PAGE_SIZE, limit=PAGE_SIZE, **f)
        self._tl_rows = rows
        for e in rows:
            label = EVENT_LABELS.get(e["event_type"], e["event_type"])
            cert = CERTAINTY_LABELS.get(e["certainty"], e["certainty"])
            tags = ("inferred",) if e["certainty"] == "inferred" else ()
            self.tl_tree.insert("", "end", tags=tags, values=(
                fmt_time_local(e["observed_at"]), label, e["rel_path"],
                CATEGORY_LABELS.get(e["category"], e["category"]), cert,
                e["project_name"]))
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.tl_page = min(self.tl_page, pages - 1)
        self.tl_count.set(f"{total} 条（每页 {PAGE_SIZE}）")
        self.tl_page_var.set(f"第 {self.tl_page + 1}/{pages} 页")

    def _tl_turn(self, delta: int) -> None:
        self.tl_page = max(0, self.tl_page + delta)
        self.refresh_timeline()

    def refresh_recent(self) -> None:
        for t in self.recent_tree.get_children():
            self.recent_tree.delete(t)
        self._recent_rows = self.db.list_events(limit=30)
        for e in self._recent_rows:
            label = EVENT_LABELS.get(e["event_type"], e["event_type"])
            tags = ("inferred",) if e["certainty"] == "inferred" else ()
            self.recent_tree.insert("", "end", tags=tags, values=(
                fmt_time_local(e["observed_at"]), label, e["rel_path"],
                e["project_name"], CERTAINTY_LABELS.get(e["certainty"], e["certainty"])))

    # ---------------------------------------------------------------- 成果
    def refresh_findings(self) -> None:
        for t in self.fd_tree.get_children():
            self.fd_tree.delete(t)
        pid = self.selected_project_id
        if pid is None:
            self.fd_info.set("（左侧选择项目后显示成果考古结果；结论均为启发式判断，可人工确认/否决）")
            return
        rows = self.db.list_findings(pid, limit=1000)
        self._fd_rows = rows
        confirmed = sum(1 for r in rows if r["status"] == "confirmed")
        self.fd_info.set(
            f"共 {len(rows)} 条成果线索（最近快照），其中人工确认 {confirmed} 条。"
            "双击查看证据来源；启发式结论 ≠ 事实。")
        for r in rows:
            tags = [r["confidence"]]
            if r["status"] == "rejected":
                tags.append("rejected")
            self.fd_tree.insert("", "end", tags=tags, values=(
                FINDING_STATUS.get(r["status"], r["status"]),
                KIND_LABELS.get(r["kind"], r["kind"]), r["confidence"],
                r["rel_path"], r["basis"], f"#{r['snapshot_id']}"))

    def _selected_finding(self):
        vals = self._selected_row(self.fd_tree)
        if not vals:
            return None
        for r in self._fd_rows:
            if r["rel_path"] == vals[3] and r["kind"] == vals[1]:
                return r
        return None

    def _set_finding(self, status: str) -> None:
        r = self._selected_finding()
        if not r:
            messagebox.showinfo("提示", "请先在列表中选择一条成果线索。", parent=self.root)
            return
        self.db.set_finding_status(r["id"], status)
        self.refresh_findings()

    def _show_finding_detail(self) -> None:
        r = self._selected_finding()
        if not r:
            return
        lines = [
            f"类型：{KIND_LABELS.get(r['kind'], r['kind'])}",
            f"文件：{r['rel_path']}",
            f"置信度：{r['confidence']}（启发式）  状态：{FINDING_STATUS.get(r['status'], r['status'])}",
            f"判断依据：{r['basis']}",
            f"检测时间：{fmt_time_local(r['detected_at'])} · 快照 #{r['snapshot_id']}",
            "", "证据链：",
        ]
        try:
            ev = json.loads(r["evidence"])
            lines += [f"  {k}: {v}" for k, v in ev.items()]
        except (TypeError, ValueError):
            lines.append("  （证据数据损坏）")
        lines += ["", "关联文件记录（该快照中）："]
        fr = self.db.find_file_in_snapshot(r["snapshot_id"], r["rel_path"])
        if fr:
            lines += [
                f"  大小 {fmt_size(fr['size'])} · mtime {fmt_time_local(fr['mtime'])}"
                f"（文件系统时间戳，非工作时间）",
                f"  SHA-256: {fr['sha256'] or '（未计算：读取失败或为符号链接）'}",
                f"  类别: {CATEGORY_LABELS.get(fr['category'], fr['category'])}"
                f"{' · 符号链接 → ' + str(fr['link_target']) if fr['is_symlink'] else ''}",
            ]
        else:
            lines.append("  证据不足：该文件在此快照中无记录。")
        self._detail_dialog(f"成果证据 #{r['id']}", "\n".join(lines))

    # ---------------------------------------------------------------- 调查线索
    def refresh_leads(self) -> None:
        for t in self.ld_tree.get_children():
            self.ld_tree.delete(t)
        pid = self.selected_project_id
        if pid is None:
            self.ld_info.set("（左侧选择项目后，可对最新完成快照运行线索分析）")
            return
        rule_map = {v: k for k, v in RULE_LABELS.items()}
        status_map = {v: k for k, v in LEAD_STATUS.items()}
        rows = self.db.list_leads(pid, rule_id=rule_map.get(self.ld_rule.get()),
                                  status=status_map.get(self.ld_status.get()),
                                  limit=1000)
        open_n = sum(1 for r in rows if r["status"] == "open")
        self.ld_info.set(
            f"共 {len(rows)} 条线索（当前筛选），其中待查看 {open_n} 条。"
            "线索 = 值得查看的问题，不是结论。")
        for r in rows:
            tags = (r["status"],) if r["status"] != "open" else ()
            self.ld_tree.insert("", "end", iid=str(r["id"]), tags=tags, values=(
                LEAD_STATUS.get(r["status"], r["status"]),
                RULE_LABELS.get(r["rule_id"], r["rule_id"]),
                r["title"], r["target_path"] or "—", f"#{r['snapshot_id']}"))

    def on_analyze_leads(self) -> None:
        if self._leads_running:
            messagebox.showinfo("提示", "线索分析正在进行。", parent=self.root)
            return
        pid = self.selected_project_id
        if pid is None:
            messagebox.showinfo("提示", "请先在左侧选择一个项目。", parent=self.root)
            return
        latest = self.db.latest_snapshot(pid)
        if latest is None:
            messagebox.showinfo("提示", "该项目还没有完成的扫描快照，请先执行扫描。",
                                parent=self.root)
            return
        self._leads_running = True
        self.ld_info.set("线索分析中…（只读读取已扫描记录，不修改项目文件）")

        def work():
            try:
                result = run_lead_analysis(self.db, pid, latest["id"])
            except Exception as e:  # 后台线程异常需回传主线程，否则界面状态会卡住
                self.scan_queue.put(("leads_error", str(e)))
            else:
                self.scan_queue.put(("leads_done", result))

        threading.Thread(target=work, daemon=True).start()

    def _handle_leads_done(self, result: dict) -> None:
        self._leads_running = False
        by_rule = "，".join(f"{RULE_LABELS.get(rid, rid)} {n} 条"
                            for rid, n in result["by_rule"].items())
        self.ld_info.set(
            f"分析完成（快照 #{result['snapshot_id']}）：共 {result['leads']} 条线索"
            + (f" — {by_rule}" if by_rule else "（本轮启用的规则未发现问题）"))
        self.refresh_leads()

    def _handle_leads_error(self, msg: str) -> None:
        self._leads_running = False
        self.ld_info.set("线索分析出错。")
        messagebox.showerror("线索分析失败", msg, parent=self.root)

    def _show_lead_detail(self) -> None:
        sel = self.ld_tree.selection()
        if not sel:
            return
        lv = lead_view(self.db, int(sel[0]))
        if lv is None:
            return
        lines = [
            f"规则：{lv['rule_label']}（v{lv['rule_version']}）"
            f" · 状态：{LEAD_STATUS.get(lv['status'], lv['status'])}",
            f"问题描述：{lv['title']}",
            f"目标：{lv['target_path'] or '—'} · 快照 #{lv['snapshot_id']}"
            f" · 生成于 {fmt_time_local(lv['created_at'])}",
            f"触发原因：{lv['basis']}",
            "", "证据文件：",
        ]
        ev = lv["evidence_list"]
        lines += [f"  - {e}" for e in ev] if ev else ["  （无证据记录）"]
        lines += ["", "可能的解释："]
        pe = lv["possible_explanations"]
        lines += [f"  - {e}" for e in pe] if pe else ["  （未提供）"]
        lines += ["", "下一步调查建议："]
        ns = lv["next_steps"]
        lines += [f"  - {e}" for e in ns] if ns else ["  （未提供）"]
        lines += ["", f"误报可能性：{lv['false_positive'] or '（未提供）'}"]

        win = tk.Toplevel(self.root)
        win.title(f"线索 #{lv['id']} · {lv['rule_label']}")
        win.geometry("760x540")
        win.configure(bg=PANEL)
        txt = tk.Text(win, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                      font=("Consolas", 10), wrap="word", padx=12, pady=10)
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        btns = ttk.Frame(win)
        btns.pack(pady=6)
        for label, st in (("已知悉", "acknowledged"), ("忽略", "dismissed"),
                          ("重新打开", "open")):
            ttk.Button(btns, text=label,
                       command=lambda s=st: self._set_lead_status(lv["id"], s, win)
                       ).pack(side="left", padx=3)
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="left", padx=3)

    def _set_lead_status(self, lead_id: int, status: str, win: tk.Toplevel) -> None:
        self.db.set_lead_status(lead_id, status)
        win.destroy()
        self.refresh_leads()

    def _show_rules_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("调查规则启停")
        win.geometry("820x420")
        win.configure(bg=PANEL)
        ttk.Label(win, text="规则可单独启停；停用的规则在下次「分析线索」时跳过，"
                            "已入库的线索不受影响。每条规则带版本号并有独立测试样例。",
                  style="Dim.TLabel", wraplength=790, justify="left").pack(
            anchor="w", padx=12, pady=(10, 4))
        cols = ("ver", "name", "desc", "state")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        for col, text, w, anchor in (("ver", "版本", 50, "w"), ("name", "规则", 150, "w"),
                                     ("desc", "检测内容", 400, "w"),
                                     ("state", "状态", 60, "w")):
            tree.heading(col, text=text)
            tree.column(col, width=w, anchor=anchor)
        tree.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        for rid, ver, _fn, desc in LEADS_RULES:
            tree.insert("", "end", iid=rid, values=(
                f"v{ver}", RULE_LABELS.get(rid, rid), desc,
                "启用" if self.db.get_rule_enabled(rid) else "停用"))

        def toggle():
            sel = tree.selection()
            if not sel:
                return
            rid = sel[0]
            new = not self.db.get_rule_enabled(rid)
            self.db.set_rule_enabled(rid, new, utcnow_iso())
            tree.set(rid, "state", "启用" if new else "停用")

        btns = ttk.Frame(win)
        btns.pack(pady=(0, 10))
        ttk.Button(btns, text="启用/停用所选规则", command=toggle).pack(
            side="left", padx=3)
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="left", padx=3)

    # ---------------------------------------------------------------- 事件详情
    def _show_event_detail(self, tree: ttk.Treeview | None = None) -> None:
        tree = tree or self.tl_tree
        vals = self._selected_row(tree)
        if not vals:
            return
        rows = getattr(self, "_tl_rows", []) if tree is self.tl_tree else getattr(self, "_recent_rows", [])
        ev = None
        for r in rows:
            if r["rel_path"] == vals[2]:
                ev = r
                break
        if ev is None:
            return
        lines = [
            f"事件：{EVENT_LABELS.get(ev['event_type'], ev['event_type'])}"
            f"（确定性：{CERTAINTY_LABELS.get(ev['certainty'], ev['certainty'])}）",
            f"文件：{ev['rel_path']}",
            f"观察时间：{fmt_time_local(ev['observed_at'])}（扫描观察到该变化的时间）",
            f"文件 mtime：{fmt_time_local(ev['fs_mtime'])}（仅为文件系统时间戳，不代表真实工作时间）",
            f"项目：{ev['project_name']} · 扫描运行 #{ev['run_id']}",
            "", "证据链：",
        ]
        try:
            data = json.loads(ev["evidence"])
            lines += [f"  {k}: {v}" for k, v in data.items()]
        except (TypeError, ValueError):
            data = {}
            lines.append("  （证据数据损坏）")
        lines += ["", "关联文件记录（最新完成的快照）："]
        latest = self.db.latest_snapshot(ev["project_id"])
        fr = self.db.find_file_in_snapshot(latest["id"], ev["rel_path"]) if latest else None
        if fr:
            lines += [
                f"  快照 #{latest['id']} · 大小 {fmt_size(fr['size'])} · "
                f"mtime {fmt_time_local(fr['mtime'])}",
                f"  SHA-256: {fr['sha256'] or '（未计算）'}",
                f"  类别: {CATEGORY_LABELS.get(fr['category'], fr['category'])}",
            ]
        else:
            lines.append("  证据不足：该文件不在最新快照中（可能已被删除）。")
        self._detail_dialog(f"事件 #{ev['id']} 证据", "\n".join(lines))

    def _detail_dialog(self, title: str, text: str) -> None:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("760x480")
        win.configure(bg=PANEL)
        txt = tk.Text(win, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                      font=("Consolas", 10), wrap="word", padx=12, pady=10)
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=6)

    # ---------------------------------------------------------------- 对比
    def refresh_compare_choices(self) -> None:
        pid = self.selected_project_id
        self._snap_map.clear()
        values: list[str] = []
        if pid is not None:
            for s in self.db.list_snapshots(pid):
                label = f"#{s['id']}  {fmt_time_local(s['created_at'])}  {s['file_count']}文件"
                values.append(label)
                self._snap_map[s["id"]] = label
        self.cmp_old.configure(values=values)
        self.cmp_new.configure(values=values)
        if len(values) >= 2:
            self.cmp_old.current(0)
            self.cmp_new.current(1)
        elif values:
            self.cmp_old.current(0)
            self.cmp_new.current(0)
            self.cmp_result.set("仅一次快照，需至少两次完成的扫描才能对比。")

    def on_compare(self) -> None:
        for t in self.cmp_tree.get_children():
            self.cmp_tree.delete(t)
        old_label, new_label = self.cmp_old.get(), self.cmp_new.get()
        inv = {v: k for k, v in self._snap_map.items()}
        if old_label not in inv or new_label not in inv:
            self.cmp_result.set("请选择两个快照。")
            return
        old_id, new_id = inv[old_label], inv[new_label]
        d = self.db.compare_snapshots(old_id, new_id, limit=1500)
        a, r, m = d["counts"]
        self.cmp_result.set(f"#{old_id} → #{new_id}：新增 {a} · 删除 {r} · 修改 {m}")
        for f in d["added"]:
            self.cmp_tree.insert("", "end", tags=("新增",),
                                 values=("新增", f["rel_path"], f"{fmt_size(f['size'])}"))
        for f in d["modified"]:
            self.cmp_tree.insert("", "end", tags=("修改",), values=(
                "修改", f["rel_path"],
                f"{fmt_size(f['old_size'])} → {fmt_size(f['new_size'])} · 内容签名已变化"))
        for f in d["removed"]:
            self.cmp_tree.insert("", "end", tags=("删除",),
                                 values=("删除", f["rel_path"], f"最后见于快照 #{old_id}"))

    # ---------------------------------------------------------------- 导出
    def on_export(self) -> None:
        if self.selected_project_id is None:
            messagebox.showinfo("提示", "请先选择一个项目。", parent=self.root)
            return
        fmt = messagebox.askquestion(
            "导出格式", "导出 Markdown 选择“是”，导出 JSON 选择“否”。", parent=self.root)
        fmt_code = "markdown" if fmt == "yes" else "json"
        ext = ".md" if fmt_code == "markdown" else ".json"
        p = self.db.get_project(self.selected_project_id)
        path = filedialog.asksaveasfilename(
            title="保存考古报告", defaultextension=ext,
            initialfile=f"{p['name']}_考古报告{ext}",
            filetypes=[("Markdown", "*.md"), ("JSON", "*.json"), ("所有文件", "*.*")],
            parent=self.root)
        if not path:
            return
        try:
            export_report(self.db, self.selected_project_id, fmt_code, path)
        except OSError as e:
            messagebox.showerror("导出失败", str(e), parent=self.root)
            return
        messagebox.showinfo("导出完成", f"报告已保存：\n{path}", parent=self.root)

    # ---------------------------------------------------------------- 关闭
    def _on_close(self) -> None:
        if self.service.busy:
            if not messagebox.askyesno(
                    "扫描进行中", "扫描尚未完成，退出将取消本次扫描（已保存的快照不受影响）。\n确定退出？",
                    parent=self.root):
                return
            self.service.cancel()
            self.service.join(3.0)
        self.root.destroy()


def main(db_path: str | None = None) -> None:
    import sys
    from ..util import default_db_path
    if db_path is None and len(sys.argv) > 1 and sys.argv[1] == "--db" and len(sys.argv) > 2:
        db_path = sys.argv[2]
    path = db_path or str(default_db_path())
    root = tk.Tk()
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    apply_theme(root)
    db = Database(path)
    App(root, db)
    root.mainloop()
