from __future__ import annotations

from decimal import Decimal

from btc_monitor.indicators import depth_imbalance


class LocalOrderBook:
    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.update_id: int | None = None

    def apply(self, message_type: str, data: dict) -> None:
        if message_type == "snapshot":
            self.bids.clear()
            self.asks.clear()
        for price_text, size_text in data.get("b", []):
            self._update(self.bids, Decimal(price_text), Decimal(size_text))
        for price_text, size_text in data.get("a", []):
            self._update(self.asks, Decimal(price_text), Decimal(size_text))
        self.update_id = data.get("u", self.update_id)

    @staticmethod
    def _update(side: dict[Decimal, Decimal], price: Decimal, size: Decimal) -> None:
        if size == 0:
            side.pop(price, None)
        else:
            side[price] = size

    def metrics(self) -> dict[str, Decimal] | None:
        if not self.bids or not self.asks:
            return None
        best_bid, best_ask = max(self.bids), min(self.asks)
        mid = (best_bid + best_ask) / Decimal(2)
        output: dict[str, Decimal] = {"mid": mid}
        for label, distance in (
            ("010", Decimal("0.10")),
            ("025", Decimal("0.25")),
            ("050", Decimal("0.50")),
        ):
            bids, asks, imbalance = depth_imbalance(self.bids, self.asks, mid, distance)
            output[f"bid_{label}"] = bids
            output[f"ask_{label}"] = asks
            output[f"imbalance_{label}"] = imbalance
        return output
