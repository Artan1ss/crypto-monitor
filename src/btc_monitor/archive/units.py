"""Bounded, identity-bound unit evidence; never infer units from missing data."""

from datetime import datetime, timedelta

from btc_monitor.archive.types import decimal, utc

UNIT_PROOF_TTL = timedelta(hours=48)
UNIT_RECHECK_AFTER = timedelta(hours=24)
UNIT_TOLERANCE = decimal("0.000001")


def proof_valid(proof, at):
    try:
        verified = utc(datetime.fromisoformat(proof["verified_at"]))
        evidence = proof["evidence"]
        return (
            timedelta(0) <= utc(at) - verified <= UNIT_PROOF_TTL
            and evidence.get("verified") is True
            and evidence["matched_minutes"] >= 3
            and 0 <= decimal(evidence["max_relative_error"]) <= UNIT_TOLERANCE
        )
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return False


def unit_proofs(rows, at):
    """Accept legacy exact-match evidence too; use only evidence available by `at`."""
    result = {}
    for row in sorted(rows, key=lambda r: (r["available_at"], r["id"])):
        if (
            row["dataset"] != "provider_metadata"
            or row["quality"] != "complete"
            or row["available_at"] > at
            or row["end_time"] > at
            or row["payload"].get("provider") != "coinalyze"
        ):
            continue
        payload = row["payload"]
        for market in payload.get("selected", []):
            feed, symbol = market.get("feed"), market.get("provider_symbol")
            if not feed or ":spot:" not in feed:
                continue
            evidence = payload.get("calibration", {}).get(feed, {})
            if evidence.get("reason") == "contradictory_unit_scale":
                result.pop((feed, symbol), None)
                continue
            if market.get("volume_unit") != "base":
                continue
            saved = payload.get("unit_verifications", {}).get(feed)
            proof = (
                dict(saved)
                if saved and saved.get("provider_symbol") == symbol and saved.get("feed") == feed
                else {
                    "feed": feed,
                    "provider_symbol": symbol,
                    "verified_at": evidence.get("end"),
                    "evidence": evidence,
                }
            )
            if not proof_valid(proof, at):
                continue
            verified = datetime.fromisoformat(proof["verified_at"])
            if verified > row["available_at"]:
                continue
            proof["metadata_id"] = row["id"]
            key = (feed, symbol)
            if key not in result or verified > datetime.fromisoformat(result[key]["verified_at"]):
                result[key] = proof
    return result


def recover_flow_units(rows, decision):
    """Derived copies only. Unknown units may use a prior still-valid exact proof.

    No raw record is changed, no explicit quote/contract unit is overridden, and
    evidence received after the observation itself cannot repair that observation.
    """
    recovered, result = [], []
    metadata = {r["id"]: r for r in rows if r["dataset"] == "provider_metadata"}
    for row in rows:
        if row["dataset"] != "provider_flow":
            result.append(row)
            continue
        p = row["payload"]
        feed = f"{row['venue']}:{row['market']}:{row['instrument']}"
        proof = None
        if row["dataset"] == "provider_flow" and p.get("volume_unit") == "unknown":
            proof = unit_proofs(rows, min(decision, row["available_at"])).get(
                (feed, p.get("provider_symbol"))
            )
        if (
            row["dataset"] == "provider_flow"
            and row["quality"] == "complete"
            and row["available_at"] <= decision
            and p.get("volume_unit") == "unknown"
            and p.get("delta_native") is not None
            and proof
            and metadata[proof["metadata_id"]]["available_at"] <= row["available_at"]
            and proof_valid(proof, row["available_at"])
        ):
            total, delta = decimal(p["total_native"]), decimal(p["delta_native"])
            if total >= 0 and abs(delta) <= total:
                result.append(
                    {
                        **row,
                        "payload": {
                            **p,
                            "volume_unit": "base",
                            "delta_btc": str(delta),
                            "normalization_complete": True,
                        },
                    }
                )
                recovered.append(
                    {
                        "id": row["id"],
                        "proof_metadata_id": proof["metadata_id"],
                        "verified_at": proof["verified_at"],
                    }
                )
                continue
        result.append(row)
    return result, recovered
