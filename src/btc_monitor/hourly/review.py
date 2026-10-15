"""Read-only derived chart repair. Stored decisions and signal snapshots never change."""

from datetime import datetime, timedelta

from btc_monitor.hourly.features import build_hour


async def review_bars(repo, source, start, end):
    bars = await repo.bars(source, start, end)
    if source != "coinalyze":
        return bars
    result = []
    for original in bars:
        if original["complete"]:
            result.append(original)
            continue
        at = datetime.fromisoformat(original["start"])
        decision = datetime.fromisoformat(original["decision_at"])
        rows = await repo.observations(at - timedelta(minutes=10), decision, source)
        metadata = await repo.metadata_history(decision)
        rows = list({r["id"]: r for r in rows + metadata}.values())
        rebuilt = build_hour(rows, at, decision, source)
        # Never substitute late-fetched values or repairs without a prior unit proof.
        if not rebuilt["unit_recovery"]:
            result.append(
                {
                    **original,
                    "flows": {
                        key: {**value, "quality_reasons": rebuilt["flows"][key]["quality_reasons"]}
                        for key, value in original["flows"].items()
                    },
                }
            )
            continue
        rebuilt["review_recovery"] = {
            "basis": "prior_available_unit_evidence_v1",
            "original_decision_complete": original["complete"],
            "original_lineage_sha256": original["lineage_sha256"],
            "notice": "只读复盘修正版；未修改历史决策或补发通知",
        }
        result.append(rebuilt)
    return result
