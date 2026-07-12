"""Append-only ledger of ACTUAL Anthropic batch spend, for estimate calibration.

Why this exists: pre-submission cost estimates ran low twice (−36% June 2026,
−50% July 2026 — see AGENTS.md incident log) because they were computed from
chars-per-token priors instead of observed history. Every ingested batch now
records its real usage here; future estimates should calibrate against these
actuals rather than re-deriving from first principles.

Each line of build/cost-history.jsonl:
    {"ts": ..., "script": "enrich|cards", "batch_id": ..., "n_ok": ...,
     "input_tokens": ..., "output_tokens": ..., "usd": ...}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path("build/cost-history.jsonl")


def record(
    script: str,
    batch_id: str,
    n_ok: int,
    input_tokens: int,
    output_tokens: int,
    input_usd_per_m: float,
    output_usd_per_m: float,
) -> float:
    """Append one batch's actuals; returns the computed USD for display."""
    usd = (
        input_tokens * input_usd_per_m / 1_000_000
        + output_tokens * output_usd_per_m / 1_000_000
    )
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a") as f:
        f.write(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "script": script,
                    "batch_id": batch_id,
                    "n_ok": n_ok,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "usd": round(usd, 4),
                }
            )
            + "\n"
        )
    return usd
