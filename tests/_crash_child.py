"""崩溃恢复测试的子进程：扫描进行中 os._exit 硬杀自己（模拟断电/进程被杀）。

用法：python _crash_child.py <db_path> <project_id> <kill_at_files>
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from time_arch.db import Database
from time_arch.scanner import run_scan


def main() -> None:
    db_path, pid, kill_at = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    db = Database(db_path)

    def cb(pr):
        if pr.files >= kill_at:
            print(f"kill at files={pr.files}", flush=True)
            os._exit(137)  # 不给任何清理机会：WAL 落盘状态即真实崩溃现场

    run_scan(db, pid, progress_cb=cb)
    print("child finished scan (should not happen)", flush=True)


if __name__ == "__main__":
    main()
