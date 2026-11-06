"""python -m time_arch 入口：无参数（或仅 --db）启动 GUI；带子命令进入无头 CLI。"""
import sys

_CLI_COMMANDS = {"projects", "overview", "search", "rules", "config", "scan",
                 "analyze", "report", "leads", "dossiers", "repro", "tasks",
                 "timeline", "forensics", "milestones", "phases", "diff",
                 "maintain"}
_CLI_FLAGS = {"-h", "--help", "--version"}


def main() -> None:
    args = sys.argv[1:]
    if any(a in _CLI_COMMANDS for a in args) or any(a in _CLI_FLAGS for a in args):
        from .cli import main as cli_main
        sys.exit(cli_main(args))
    from .ui.app import main as ui_main
    ui_main()


if __name__ == "__main__":
    main()
