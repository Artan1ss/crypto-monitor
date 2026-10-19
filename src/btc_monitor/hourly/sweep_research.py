"""Read a local market snapshot, walk forward, export research (never DB/TG).

Prices below are bar-close reference scenarios, NOT fills at the actual delayed
decision time. No execution return, commission-adjusted win rate or causal
historical alert claim is made from retrospective provider data.
"""

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from urllib.request import urlopen

from btc_monitor.archive.types import serializable
from btc_monitor.config import Settings
from btc_monitor.hourly.cli import save_export
from btc_monitor.hourly.engine import evaluate, parameters
from btc_monitor.hourly.sweep import PARAMETERS, D, evaluate_sweep


def replay(payload):
    bars = [b for b in payload["bars"] if b.get("closed") is True]
    signals, blocked = [], Counter()
    for i in range(24, len(bars)):
        history = bars[max(0, i - 167) : i + 1]
        analysis = evaluate_sweep(history, research=True)
        if analysis["blocked"]:
            blocked[analysis["blocked"]] += 1
        for candidate in analysis["candidates"]:
            entry = D(candidate["bar_close"])
            risk = abs(entry - D(candidate["invalidation"]))
            sign = 1 if candidate["direction"] == "LONG" else -1
            reviews = {}
            for hours in (1, 4, 12, 24):
                future = bars[i + 1 : i + 1 + hours]
                complete = len(future) == hours and all(
                    datetime.fromisoformat(a["end"]) == datetime.fromisoformat(b["start"])
                    for a, b in zip([bars[i]] + future, future, strict=False)
                )
                endpoint_r = (
                    (D(future[-1]["price"]["close"]) - entry) * sign / risk if complete else None
                )
                order = "neither" if complete else "pending_or_gap"
                previous = bars[i]
                for b in future:
                    if datetime.fromisoformat(previous["end"]) != datetime.fromisoformat(
                        b["start"]
                    ):
                        order = "unknown_due_to_gaps"
                        break
                    previous = b
                    high, low = D(b["price"]["high"]), D(b["price"]["low"])
                    target = high >= entry + risk if sign == 1 else low <= entry - risk
                    stop = low <= entry - risk if sign == 1 else high >= entry + risk
                    if target or stop:
                        order = (
                            "same_hour_ambiguous"
                            if target and stop
                            else "target_first"
                            if target
                            else "invalidation_first"
                        )
                        break
                reviews[str(hours)] = {
                    "complete": complete,
                    "observed_hours": len(future),
                    "endpoint_reference_r": endpoint_r,
                    "direction_score": round(max(D(0), min(D(100), 50 + 25 * endpoint_r)), 1)
                    if endpoint_r is not None
                    else None,
                    "touch_order": order,
                    "structure_score": 100
                    if order == "target_first"
                    else 0
                    if order == "invalidation_first"
                    else 50
                    if order == "neither" and complete
                    else None,
                    "execution_return": None,
                }
            signals.append(
                {
                    "bar_start": bars[i]["start"],
                    "theoretical_decision_at": (
                        datetime.fromisoformat(bars[i]["end"]) + timedelta(seconds=180)
                    ).isoformat(),
                    "candidate": candidate,
                    "reviews": reviews,
                    "snapshot": {
                        "parameters": PARAMETERS,
                        "history": history,
                        "analysis": analysis,
                        "causal_inputs": False,
                        "origin": "retrospective_research_no_notification",
                    },
                }
            )
    encoded = json.dumps(payload, sort_keys=True).encode()
    return serializable(
        {
            "schema": "btc-hourly-sweep-research-v1",
            "exported_at": datetime.now(UTC),
            "parameters": PARAMETERS,
            "input_sha256": hashlib.sha256(encoded).hexdigest(),
            "input_snapshot": payload,
            "summary": {
                "closed_bars": len(bars),
                "evaluated_prefixes": max(0, len(bars) - 24),
                "signals": len(signals),
                "blocked": dict(blocked),
            },
            "signals": signals,
            "notice": (
                "Retrospective fixed-cohort data, not original historical availability. "
                "Close-reference scenarios include the first post-close hour; actual decision "
                "is delayed 3 min, so these are NOT executable returns. Same-hour target/stop "
                "order is unknown. No Telegram or DB writes; scores are not probabilities."
            ),
        }
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="Saved btc-hourly-market-v1 JSON")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.input:
        with open(args.input, encoding="utf-8") as stream:
            payload = json.load(stream)
    else:
        # Fixed loopback-only URL, not an arbitrary credential-bearing URL.
        with urlopen("http://127.0.0.1:8011/hourly/market?hours=168", timeout=30) as response:
            payload = json.load(response)
    if payload.get("schema") != "btc-hourly-market-v1" or len(payload["bars"]) > 168:
        raise ValueError("Expected bounded local market snapshot")
    report = replay(payload)
    print(json.dumps({"file": save_export(args.output, report), **report["summary"]}))


def dashboard_payload(report):
    """Adapt read-only research to the existing chart, not the live alert table."""
    signals = []
    settings = Settings(_env_file=None, telegram_enabled=False)
    for s in report["signals"]:
        c = s["candidate"]
        history = s["snapshot"]["history"]
        snapshot = {
            **s["snapshot"],
            "candidate": c,
            "confidence": c["evidence_score"],
            "parameters": parameters(settings),
            "diagnostics": evaluate(history, settings)["diagnostics"],
        }
        signals.append(
            {
                "id": c["event_key"],
                "bar_start": s["bar_start"],
                "generated_at": s["theoretical_decision_at"],
                "direction": c["direction"],
                "tier": c["tier"],
                "origin": "retrospective",
                "delivery_state": "retrospective",
                "kind": c["kind"],
                "entry": c["bar_close"],
                "invalidation": c["invalidation"],
                "reasons": c["reasons"],
                "confidence": c["evidence_score"],
                "confidence_origin": "historical_snapshot_rescore",
                "outcomes": None,
                "snapshot": snapshot,
                "research_review": s["reviews"],
            }
        )
    return {
        "schema": "btc-hourly-review-v1",
        "exported_at": report["exported_at"],
        "bars": report["input_snapshot"]["bars"],
        "signals": signals,
        "health": {"source": "coinalyze", "offline": True},
        "sweep_research": True,
        "notice": report["notice"],
        "research_summary": report["summary"],
    }


if __name__ == "__main__":
    main()
