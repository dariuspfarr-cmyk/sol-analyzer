"""
Cost Tracker — Claude API call logger with monthly budget guard.

Usage:
    import cost_tracker
    cost_tracker.log_call("claude-sonnet-4-20250514", input_tokens=800,
                          output_tokens=400, cached_input_tokens=600)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

# ── CONFIG ─────────────────────────────────────────────────────────────
MONTHLY_WARN_USD = 2.00
LOG_FILE = Path(__file__).parent / "api_costs.jsonl"

# Per-token prices in USD (Anthropic pricing, 2025)
PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {
        "input":       0.80  / 1_000_000,
        "output":      4.00  / 1_000_000,
        "cache_write": 1.00  / 1_000_000,   # writing to cache (1.25× input)
        "cache_read":  0.08  / 1_000_000,   # reading cached tokens (~10%)
    },
    "claude-sonnet-4-20250514": {
        "input":       3.00  / 1_000_000,
        "output":      15.00 / 1_000_000,
        "cache_write": 3.75  / 1_000_000,
        "cache_read":  0.30  / 1_000_000,
    },
    "claude-sonnet-4-6": {
        "input":       3.00  / 1_000_000,
        "output":      15.00 / 1_000_000,
        "cache_write": 3.75  / 1_000_000,
        "cache_read":  0.30  / 1_000_000,
    },
}
_FALLBACK = {"input": 3.00/1_000_000, "output": 15.00/1_000_000,
             "cache_write": 3.75/1_000_000, "cache_read": 0.30/1_000_000}


# ── INTERNAL ────────────────────────────────────────────────────────────
def _price(model: str) -> dict:
    return PRICING.get(model, _FALLBACK)


def _calc_cost(model: str, input_tokens: int, output_tokens: int,
               cached_input_tokens: int = 0, cache_write_tokens: int = 0) -> float:
    p = _price(model)
    uncached = max(0, input_tokens - cached_input_tokens)
    return (
        uncached             * p["input"]       +
        cached_input_tokens  * p["cache_read"]  +
        cache_write_tokens   * p["cache_write"] +
        output_tokens        * p["output"]
    )


# ── PUBLIC API ──────────────────────────────────────────────────────────
def log_call(
    model:                str,
    input_tokens:         int,
    output_tokens:        int,
    cached_input_tokens:  int = 0,
    cache_write_tokens:   int = 0,
) -> float:
    """
    Log one API call to api_costs.jsonl and print a cost summary line.
    Returns the cost of this call in USD.
    """
    cost    = _calc_cost(model, input_tokens, output_tokens,
                         cached_input_tokens, cache_write_tokens)
    monthly = get_monthly_total() + cost

    entry = {
        "ts":          datetime.now(timezone.utc).isoformat(),
        "model":       model,
        "input":       input_tokens,
        "output":      output_tokens,
        "cached_in":   cached_input_tokens,
        "cache_write": cache_write_tokens,
        "cost_usd":    round(cost, 8),
    }
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")

    # Short model tag for display (e.g. "sonnet-4" or "haiku-4-5")
    parts = model.replace("claude-", "").split("-")
    tag   = "-".join(parts[:3]) if len(parts) >= 3 else model

    cache_note = f" cache_read={cached_input_tokens:,}" if cached_input_tokens else ""
    print(
        f"  💰 [{tag}]  in={input_tokens:,}{cache_note}  out={output_tokens:,} "
        f"→ ${cost:.5f}  |  Monat gesamt: ${monthly:.4f}"
    )

    if monthly > MONTHLY_WARN_USD:
        print(f"  ⚠️  KOSTENLIMIT: ${monthly:.4f} > ${MONTHLY_WARN_USD:.2f}/Monat!")

    return cost


def get_monthly_total() -> float:
    """Return total USD spent in the current calendar month."""
    if not LOG_FILE.exists():
        return 0.0
    now   = datetime.now(timezone.utc)
    total = 0.0
    try:
        with open(LOG_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e  = json.loads(line)
                    ts = datetime.fromisoformat(e["ts"])
                    if ts.year == now.year and ts.month == now.month:
                        total += e.get("cost_usd", 0.0)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    except OSError:
        pass
    return total


def print_summary(n_days: int = 30) -> None:
    """Print a per-model cost summary for the last n_days. Useful for debugging."""
    if not LOG_FILE.exists():
        print("Keine Kostendaten gefunden.")
        return
    from collections import defaultdict
    totals: dict[str, float] = defaultdict(float)
    calls:  dict[str, int]   = defaultdict(int)
    with open(LOG_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                totals[e["model"]] += e.get("cost_usd", 0.0)
                calls[e["model"]]  += 1
            except (json.JSONDecodeError, KeyError):
                continue
    print("\n── API Kosten ──────────────────────────────────")
    for model, cost in sorted(totals.items(), key=lambda x: -x[1]):
        print(f"  {model:<45}  {calls[model]:>4} Calls  ${cost:.4f}")
    print(f"  {'GESAMT':<45}  {sum(calls.values()):>4} Calls  ${sum(totals.values()):.4f}")
    print("────────────────────────────────────────────────\n")
