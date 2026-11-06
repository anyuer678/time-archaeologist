"""跨项目摘要：每个项目的最新快照、待查看线索、待处理任务与成果档案数量。

只读聚合库内记录，不触发任何分析；数量仅反映当前库状态。
"""
from __future__ import annotations

from .db import Database


def project_summaries(db: Database) -> list[dict]:
    out: list[dict] = []
    for p in db.list_projects():
        latest = db.latest_snapshot(p["id"])
        stats = (db.snapshot_stats(latest["id"]) if latest
                 else {"files": 0, "total_size": 0, "errors": 0, "by_category": []})
        leads = (db.list_leads(p["id"], snapshot_id=latest["id"], limit=10000)
                 if latest else [])
        tasks = db.list_tasks(p["id"], limit=10000)
        artifacts = db.list_artifacts(p["id"])
        out.append({
            "id": p["id"], "name": p["name"], "path": p["path"],
            "last_scan_at": p["last_scan_at"],
            "latest_snapshot_id": latest["id"] if latest else None,
            "files": stats["files"], "total_size": stats["total_size"],
            "open_leads": sum(1 for r in leads if r["status"] == "open"),
            "open_tasks": sum(1 for r in tasks if r["status"] == "open"),
            "artifacts": len(artifacts),
        })
    return out
