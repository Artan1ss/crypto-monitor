"""Deterministic observational outcomes; never invent OHLC intrabar order or fills."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from btc_monitor.diagnostics import json_value
from btc_monitor.domain import ZERO

HORIZONS = (15, 30, 60, 120)


def track_signal(signal, candles, ticks, now, fee_bps=Decimal("0"), slippage_bps=Decimal("0")):
    start = signal.created_at
    sign = Decimal(1) if signal.direction == "LONG" else Decimal(-1)
    entry = signal.entry_reference
    risk = abs(entry - signal.stop_reference)
    levels = {
        "tp1": signal.tp1,
        "tp2": signal.tp2,
        "tp3": signal.tp3,
        "stop": signal.stop_reference,
    }
    windows = {}
    for minutes in HORIZONS:
        deadline = start + timedelta(minutes=minutes)
        end = min(deadline, now)
        # Only use OHLC candles wholly AFTER generation and wholly inside this window.
        full = [
            c for c in candles if c.timestamp >= start and c.timestamp + timedelta(minutes=5) <= end
        ]
        intervals = [(c.timestamp, c.timestamp + timedelta(minutes=5)) for c in full]
        points = [
            t
            for t in ticks
            if start <= t.timestamp <= end and not any(a <= t.timestamp < b for a, b in intervals)
        ]
        observations = [
            (c.timestamp, c.timestamp + timedelta(minutes=5), c.low, c.high, "5m_ohlc")
            for c in full
        ]
        observations += [
            (t.timestamp, t.timestamp, t.last_price, t.last_price, "ticker_sample") for t in points
        ]
        observations.sort(key=lambda x: (x[0], x[1]))
        hits = {}
        mfe, mae = ZERO, ZERO
        for a, b, low, high, source in observations:
            favorable = (high - entry) if sign > 0 else (entry - low)
            adverse = (entry - low) if sign > 0 else (high - entry)
            mfe, mae = max(mfe, favorable), max(mae, adverse)
            for name, price in levels.items():
                touched = (
                    (low <= price if sign > 0 else high >= price)
                    if name == "stop"
                    else (high >= price if sign > 0 else low <= price)
                )
                if touched and name not in hits:
                    hits[name] = {"from": a, "to": b, "source": source}
        order = {}
        for target in ("tp1", "tp2", "tp3"):
            tp, stop = hits.get(target), hits.get("stop")
            if tp and stop:
                order[target] = (
                    "target_before_stop"
                    if tp["to"] < stop["from"]
                    or (tp["to"] == stop["from"] and tp["source"] == "5m_ohlc")
                    else "stop_before_target"
                    if stop["to"] < tp["from"]
                    or (stop["to"] == tp["from"] and stop["source"] == "5m_ohlc")
                    else "ambiguous"
                )
            else:
                order[target] = "target_only" if tp else "stop_only" if stop else "neither"
        # Coverage and sampled boundary limitations remain explicit even on a completed window.
        expected = []
        bucket = start.replace(minute=(start.minute // 5) * 5, second=0, microsecond=0)
        while bucket < end:
            if bucket >= start and bucket + timedelta(minutes=5) <= end:
                expected.append(bucket)
            bucket += timedelta(minutes=5)
        available = {c.timestamp for c in full}
        missing = [t for t in expected if t not in available]
        boundary_ticks = sorted({t.timestamp for t in points})
        boundary_gaps = []
        # Assess each boundary interval separately; don't count covered full candles as gaps.
        edges = [start]
        for a, b in sorted(intervals):
            edges += [a, b]
        edges.append(end)
        for a, b in zip(edges[::2], edges[1::2], strict=True):
            if b <= a:
                continue
            times = [a] + [t for t in boundary_ticks if a <= t <= b] + [b]
            boundary_gaps += [
                {"from": x, "to": y}
                for x, y in zip(times, times[1:], strict=False)
                if (y - x).total_seconds() > 15
            ]
        closing = [t for t in ticks if start <= t.timestamp <= end]
        exits = [(t.timestamp, t.last_price, "ticker_sample") for t in closing]
        exits += [(c.timestamp + timedelta(minutes=5), c.close, "5m_close") for c in full]
        last = max(exits, key=lambda x: x[0]) if exits else None
        gross_r = sign * (last[1] - entry) / risk if last and risk else None
        # Explicit, configurable hypothesis; roundtrip fee/slippage are NOT actual fills.
        cost = (entry + last[1]) * (fee_bps + slippage_bps) / Decimal(10000) if last else None
        windows[str(minutes)] = {
            "status": "complete" if now >= deadline else "pending",
            "start": start,
            "deadline": deadline,
            "observed_until": end,
            "mfe_usdt": mfe if observations else None,
            "mae_usdt": mae if observations else None,
            "mfe_r": mfe / risk if observations and risk else None,
            "mae_r": mae / risk if observations and risk else None,
            "first_touches": hits,
            "target_stop_order": order,
            "ambiguous": "ambiguous" in order.values(),
            "missing_full_candles": missing,
            "boundary_sampling_gaps": boundary_gaps,
            "coverage": "incomplete"
            if missing or boundary_gaps or not observations
            else "sampled_boundaries",
            "extremes_are_lower_bounds": bool(missing)
            or bool(boundary_gaps)
            or any(b > a for a, b in zip(edges[::2], edges[1::2], strict=True)),
            "ordering_is_observed_not_execution": True,
            "observation_count": len(observations),
            "exit_observation": {"timestamp": last[0], "price": last[1], "source": last[2]}
            if last
            else None,
            "exit_age_seconds": (end - last[0]).total_seconds() if last else None,
            "hypothetical_gross_r": gross_r,
            "hypothetical_net_r": gross_r - cost / risk if gross_r is not None else None,
        }
    return json_value(
        {
            "schema_version": 1,
            "basis": "observed_market_path_not_execution",
            "start_basis": "signal_generated_at",
            "generated_at": start,
            "telegram_sent_at": signal.telegram_sent_at,
            "delivery_latency_seconds": (signal.telegram_sent_at - start).total_seconds()
            if signal.telegram_sent_at
            else None,
            "entry_reference": entry,
            "risk_usdt": risk,
            "cost_assumptions": {
                "fee_bps_per_side": fee_bps,
                "slippage_bps_per_side": slippage_bps,
            },
            "updated_at": now,
            "windows": windows,
            "limitations": [
                "Ticker boundary samples can miss between-sample extremes and earlier touches.",
                "5m OHLC touch times are intervals; intrabar ordering may be ambiguous.",
                "Costs and entry/exit are hypotheses, not executed fills.",
            ],
        }
    )


class OutcomeTracker:
    def __init__(self, repository, settings):
        self.repository = repository
        self.settings = settings

    async def update(self, now):
        signals = await self.repository.tracking_signals()
        for signal in signals:
            end = min(now, signal.created_at + timedelta(minutes=max(HORIZONS)))
            if end <= signal.created_at:
                continue
            candles, ticks = await self.repository.tracking_prices(
                signal.symbol, signal.created_at, end
            )
            result = track_signal(
                signal,
                candles,
                ticks,
                now,
                Decimal(str(self.settings.tracking_fee_bps_per_side)),
                Decimal(str(self.settings.tracking_slippage_bps_per_side)),
            )
            # Wait for the candle spanning the final deadline to be finalized before freezing.
            finalize_at = signal.created_at + timedelta(minutes=max(HORIZONS) + 5)
            await self.repository.save_outcomes(signal.id, result, now >= finalize_at)
