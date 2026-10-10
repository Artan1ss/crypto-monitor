from __future__ import annotations

import html
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from btc_monitor.domain import Signal

TORONTO = ZoneInfo("America/Toronto")


def _money(value: Decimal | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _pct(value: Decimal | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:+.{digits}f}%"


def render_signal(signal: Signal) -> str:
    direction_icon = "🟢" if signal.direction.value == "LONG" else "🔴"
    spot = "Bullish" if signal.direction.value == "LONG" else "Bearish"
    from datetime import timedelta

    generated = signal.generated_at or signal.timestamp + timedelta(minutes=5)
    time = generated.astimezone(TORONTO).strftime("%Y-%m-%d %H:%M:%S %Z")
    candle_time = signal.timestamp.astimezone(TORONTO).strftime("%H:%M")
    reasons = "\n".join(f"• {html.escape(reason)}" for reason in signal.reasons)
    warnings = "\n".join(f"⚠️ {html.escape(item)}" for item in signal.warnings)
    warning_section = f"\n\n{warnings}" if warnings else ""
    setup_side = "Sell-side" if signal.direction.value == "LONG" else "Buy-side"
    context_text = html.escape(signal.oi_context or "Neutral")
    funding = signal.funding_rate * 100 if signal.funding_rate is not None else None
    volume = f"{signal.volume_ratio:.2f}x median" if signal.volume_ratio is not None else "n/a"
    orderbook = (
        f"\nOrderbook 0.25% imbalance: {signal.orderbook_imbalance * 100:+.1f}%"
        if signal.orderbook_imbalance is not None
        else ""
    )
    return (
        f"{direction_icon} <b>{signal.symbol} {signal.direction.value} — "
        f"{signal.strength.value}</b>\n\n"
        f"<b>Score:</b> {signal.score} / 8\n<b>Price:</b> {_money(signal.entry_reference)}\n"
        f"<b>Generated:</b> {time}\n<b>5m candle start:</b> {candle_time}\n\n<b>SETUP</b>\n"
        f"{setup_side} liquidity sweep + reclaim\n\n"
        f"<b>CVD</b>\nSpot: {spot} divergence {'✅' if signal.spot_cvd_divergence else '—'} "
        f"({signal.spot_cvd_strength:+.2f})\nFutures: {spot} divergence "
        f"{'✅' if signal.futures_cvd_divergence else '—'} ({signal.futures_cvd_strength:+.2f})\n\n"
        f"<b>DERIVATIVES</b>\nOI 5m: {_pct(signal.oi_change_5m)}\n"
        f"OI 15m: {_pct(signal.oi_change_15m)}\nContext: {context_text}\n"
        f"Funding: {_pct(funding, 4)}\n\n"
        f"<b>VOLUME</b>\n5m turnover: "
        f"{volume}\n\n"
        f"<b>TREND</b>\n1H: {signal.htf_trend}{orderbook}\n\n"
        f"<b>LEVELS</b>\nSwept level: {_money(signal.reference_swing_price)}\n"
        f"Sweep extreme: {_money(signal.sweep_extreme)}\n"
        f"Entry reference: {_money(signal.entry_reference)}\n"
        f"Invalidation: {_money(signal.stop_reference)}\nTP1 1R: {_money(signal.tp1)}\n"
        f"TP2 2R: {_money(signal.tp2)}\nTP3 3R: {_money(signal.tp3)}\n"
        f"Nearest structure target: {_money(signal.structure_target)}\n\n<b>WHY</b>\n{reasons}"
        f"{warning_section}\n\n<i>Informational signal only — no order was placed.</i>"
    )


class TelegramClient:
    def __init__(self, enabled: bool, token: str | None, chat_id: str | None) -> None:
        self.enabled = enabled
        self.token = token
        self.chat_id = chat_id
        self.client = httpx.AsyncClient(timeout=15)

    async def send(self, text: str) -> str | None:
        if not self.enabled:
            return None
        response = await self.client.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram rejected message: {payload}")
        return str(payload["result"]["message_id"])

    async def close(self) -> None:
        await self.client.aclose()
