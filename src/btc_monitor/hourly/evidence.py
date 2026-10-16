"""Read-only local validation against exported raw path evidence; no DB or Telegram."""

import argparse
import asyncio
import gzip
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from btc_monitor.archive.types import Instrument, serializable
from btc_monitor.hourly.cli import save_export
from btc_monitor.hourly.outcomes import track
from btc_monitor.hourly.scoring import confidence


def load(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as stream:
        data = stream.read(50 * 1024 * 1024 + 1)
    if len(data) > 50 * 1024 * 1024:
        raise ValueError("Evidence exceeds 50 MiB decoded limit")
    return json.loads(data)


async def repair_prices(alert, rows, now):
    """Explicit local public-price check; never uploads or writes a database."""
    from btc_monitor.archive.exchanges import PublicRest
    from btc_monitor.archive.history import HistoryLoader
    from btc_monitor.config import get_settings

    collected = []

    class EvidenceStore:
        async def append(self, observations):
            for observation in observations:
                value = observation.values()
                value.update(
                    id=-int(value["fingerprint"][:15], 16), available_at=value["received_at"]
                )
                collected.append(value)
            return len(observations)

    result = track(alert, rows, now)
    times = [datetime.fromisoformat(m["start"]) for m in result["windows"]["24"]["missing_minutes"]]
    ranges = []
    for t in times:
        if ranges and ranges[-1][1] == t and t - ranges[-1][0] < timedelta(minutes=200):
            ranges[-1] = (ranges[-1][0], t + timedelta(minutes=1))
        else:
            ranges.append((t, t + timedelta(minutes=1)))
    rest = PublicRest(get_settings())
    reports = []
    try:
        for start, end in ranges[:8]:
            try:
                report = await asyncio.wait_for(
                    HistoryLoader(rest, EvidenceStore()).prices_and_flow(
                        Instrument("bybit", "linear", "BTCUSDT"), start, end
                    ),
                    timeout=15,
                )
                reports.append({"from": start, "to": end, "result": report})
            except Exception as exc:
                reports.append({"from": start, "to": end, "error_type": type(exc).__name__})
    finally:
        await rest.close()
    return collected, reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True)
    parser.add_argument("--path-evidence", required=True, nargs="+")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--repair-prices",
        action="store_true",
        help="Read public Bybit candles locally (max 8 requests/signal), no cloud writes",
    )
    args = parser.parse_args()
    review = load(args.review)
    signals = {s["id"]: s for s in review["signals"]}
    verified = []
    local_price_evidence = []
    for path in args.path_evidence:
        evidence = load(path)
        source = evidence["signal"]
        if source["id"] not in signals:
            raise ValueError("Path signal not present in review file")
        target = signals[source["id"]]
        if any(
            target[k] != source[k]
            for k in ("snapshot", "entry", "invalidation", "direction", "generated_at", "source")
        ):
            raise ValueError("Original signal evidence does not match")
        rows = evidence.get("rows", evidence.get("raw_inputs", []))
        for r in rows:
            for field in ("event_time", "end_time", "available_at", "received_at"):
                r[field] = datetime.fromisoformat(r[field])
        alert = SimpleNamespace(**source)
        alert.generated_at = datetime.fromisoformat(alert.generated_at)
        now = datetime.fromisoformat(evidence["queried_at"])
        reports = []
        if args.repair_prices:
            extra, reports = asyncio.run(repair_prices(alert, rows, now))
            rows += extra
            local_price_evidence += extra
            now = datetime.now(UTC)
        target["outcomes"] = track(alert, rows, now)
        if reports:
            target["outcomes"]["local_public_price_checks"] = serializable(reports)
        verified.append(source["id"])
    for s in signals.values():
        s["confidence"] = s["snapshot"].get("confidence") or confidence(s["snapshot"])
        s["confidence_origin"] = (
            "at_generation" if "confidence" in s["snapshot"] else "historical_snapshot_rescore"
        )
    review.update(
        local_verification=True,
        verified_signal_ids=verified,
        verified_at=datetime.now(UTC).isoformat(),
        local_price_evidence=serializable(local_price_evidence),
    )
    print(
        json.dumps(
            {
                "file": save_export(args.output, review),
                "verified_signals": verified,
                "cloud_writes": False,
                "notifications": False,
            }
        )
    )


if __name__ == "__main__":
    main()
