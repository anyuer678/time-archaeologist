"""深色考古档案主题：炭黑底、旧纸色文字、琥珀色强调。克制、无装饰动画。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

BG = "#161513"          # 窗体底
PANEL = "#1e1c19"       # 面板底
PANEL_2 = "#26231f"     # 次级面板 / 表格行悬停
FIELD = "#2b2b2b"       # 输入框
FG = "#d6cdbd"          # 旧纸色主文字
FG_DIM = "#8f8878"      # 次级文字
ACCENT = "#c9a227"      # 琥珀强调
ACCENT_DIM = "#8a7318"
BORDER = "#3a362f"
SEL_BG = "#3f3a22"
HEAD_BG = "#2a2721"

FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 14)
FONT_MONO = ("Consolas", 9)


def apply_theme(root: tk.Tk) -> ttk.Style:
    root.configure(bg=BG)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=BG, foreground=FG, font=FONT, bordercolor=BORDER,
                    troughcolor=PANEL_2, lightcolor=PANEL, darkcolor=PANEL)
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Dim.TLabel", background=BG, foreground=FG_DIM, font=FONT_SMALL)
    style.configure("Panel.TLabel", background=PANEL, foreground=FG)
    style.configure("PanelDim.TLabel", background=PANEL, foreground=FG_DIM, font=FONT_SMALL)
    style.configure("Title.TLabel", background=BG, foreground=ACCENT, font=FONT_TITLE)
    style.configure("Accent.TLabel", background=BG, foreground=ACCENT)

    style.configure("TButton", background=PANEL_2, foreground=FG, bordercolor=BORDER,
                    focusthickness=1, padding=(8, 3))
    style.map("TButton",
              background=[("active", "#3d3931"), ("disabled", PANEL)],
              foreground=[("disabled", FG_DIM)])
    style.configure("Accent.TButton", background=ACCENT_DIM, foreground="#fff8e6")
    style.map("Accent.TButton", background=[("active", ACCENT)])
    style.configure("Danger.TButton", background="#4a2323", foreground="#e8b0b0")
    style.map("Danger.TButton", background=[("active", "#6b3030")])

    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                    rowheight=24, bordercolor=BORDER, font=FONT_SMALL)
    style.configure("Treeview.Heading", background=HEAD_BG, foreground=FG_DIM, font=FONT_SMALL,
                    relief="flat")
    style.map("Treeview",
              background=[("selected", SEL_BG)],
              foreground=[("selected", "#f0e6c8")])
    style.map("Treeview.Heading", background=[("active", HEAD_BG)])

    style.configure("TNotebook", background=BG, bordercolor=BORDER)
    style.configure("TNotebook.Tab", background=PANEL, foreground=FG_DIM, padding=(14, 5))
    style.map("TNotebook.Tab",
              background=[("selected", PANEL_2)],
              foreground=[("selected", ACCENT)])

    style.configure("TCombobox", fieldbackground=FIELD, background=PANEL_2, foreground=FG,
                    arrowcolor=ACCENT)
    root.option_add("*TCombobox*Listbox.background", PANEL_2)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", SEL_BG)
    root.option_add("*TCombobox*Listbox.selectForeground", "#f0e6c8")

    style.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG,
                    bordercolor=BORDER)
    style.configure("TProgressbar", background=ACCENT, troughcolor=PANEL_2,
                    bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT_DIM)
    style.configure("Horizontal.TProgressbar", thickness=8)
    style.configure("TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BORDER,
                    arrowcolor=FG_DIM)
    style.map("TScrollbar", background=[("active", PANEL_2)])

    root.option_add("*Font", FONT)
    return style
