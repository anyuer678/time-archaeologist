"""文件类别启发式分类。

这是启发式判断（heuristic），不是文件系统事实：同一文件在不同项目里
可能属于不同类别。类别结果仅用于统计与线索提示，不作为结论依据。
"""
from __future__ import annotations

import os

# 类别顺序即优先级：build > test > doc > log > source > config > data > other
BUILD_DIRS = {"build", "dist", "target", "out", "node_modules", "__pycache__",
              ".git", "bin", "obj", "venv", ".venv", "site-packages"}
BUILD_EXTS = {".pyc", ".pyo", ".class", ".o", ".obj", ".so", ".dylib", ".lib", ".a"}
TEST_NAME_HINTS = ("test", "spec")
TEST_EXTS = {".py", ".js", ".ts", ".java", ".go", ".rs", ".cs", ".rb"}
DOC_EXTS = {".md", ".rst", ".txt", ".pdf", ".doc", ".docx", ".tex", ".adoc", ".org"}
DOC_NAMES = {"readme", "license", "changelog", "authors", "contributing", "notice"}
LOG_EXTS = {".log", ".out", ".err"}
LOG_DIR_HINTS = {"logs", "log"}
SOURCE_EXTS = {".py", ".pyw", ".c", ".h", ".cpp", ".hpp", ".cc", ".java", ".js", ".mjs",
               ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".cs", ".php", ".swift",
               ".kt", ".scala", ".sh", ".ps1", ".bat", ".cmd", ".m", ".r", ".lua",
               ".vue", ".svelte", ".html", ".htm", ".css", ".scss", ".sql", ".ipynb"}
CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
               ".properties", ".xml", ".editorconfig", ".gitignore", ".lock",
               ".cfg.toml", "dockerfile", ".gradle", ".cmake"}
DATA_EXTS = {".csv", ".tsv", ".parquet", ".npz", ".npy", ".pkl", ".pickle", ".h5",
             ".hdf5", ".xlsx", ".xls", ".sqlite", ".sqlite3", ".db", ".feather",
             ".arrow", ".jsonl", ".ndjson", ".zip", ".tar", ".gz", ".7z", ".rar"}

CATEGORY_LABELS = {
    "source": "源码",
    "config": "配置",
    "test": "测试",
    "doc": "文档",
    "log": "日志",
    "data": "数据",
    "build": "构建产物",
    "other": "其他",
}


def classify_file(rel_path: str) -> str:
    """按相对路径启发式分类，返回类别代码（source/config/test/doc/log/data/build/other）。"""
    norm = rel_path.replace("\\", "/")
    lower = norm.lower()
    name = lower.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    ext = os.path.splitext(name)[1]
    parts = set(lower.split("/"))

    if parts & BUILD_DIRS:
        return "build"
    if ext in BUILD_EXTS:
        return "build"

    if any(h in stem for h in TEST_NAME_HINTS) and (ext in TEST_EXTS or ext == ""):
        return "test"
    if "tests" in parts or "test" in parts or "__tests__" in parts:
        return "test"

    if ext in DOC_EXTS or stem in DOC_NAMES or "docs" in parts or "doc" in parts:
        return "doc"

    if ext in LOG_EXTS or parts & LOG_DIR_HINTS:
        return "log"

    if ext in SOURCE_EXTS:
        return "source"

    if ext in CONFIG_EXTS or name in {"dockerfile", "makefile", "cmakelists.txt"}:
        return "config"

    if ext in DATA_EXTS:
        return "data"

    return "other"
