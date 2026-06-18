"""
Budget Guardian — überwacht Tages- und Monatslimits für API-Kosten.

Blockiert API-Calls hart, wenn ein Limit erreicht ist.
Liest die tatsächlichen Kosten aus cost_tracker (api_costs.jsonl).
Alle Ausgaben auf Deutsch.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import config

COST_LOG = Path(__file__).parent / "api_costs.jsonl"


def _spent(period: str) -> float:
    """Summiert die Kosten des aktuellen Tages ('day') oder Monats ('month')."""
    if not COST_LOG.exists():
        return 0.0
    now   = datetime.now(timezone.utc)
    total = 0.0
    try:
        with open(COST_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e  = json.loads(line)
                    ts = datetime.fromisoformat(e["ts"])
                    if period == "day":
                        match = (ts.date() == now.date())
                    else:
                        match = (ts.year == now.year and ts.month == now.month)
                    if match:
                        total += e.get("cost_usd", 0.0)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    except OSError:
        pass
    return total


def spent_today() -> float:
    return _spent("day")


def spent_this_month() -> float:
    return _spent("month")


def check() -> tuple[bool, str]:
    """
    Prüft beide Limits.
    Gibt (erlaubt, grund) zurück. erlaubt=False blockiert den API-Call.
    """
    day   = spent_today()
    month = spent_this_month()

    if day >= config.DAILY_API_LIMIT_USD:
        return False, (f"Tageslimit erreicht: ${day:.4f} / "
                       f"${config.DAILY_API_LIMIT_USD:.2f}")
    if month >= config.MONTHLY_API_LIMIT_USD:
        return False, (f"Monatslimit erreicht: ${month:.4f} / "
                       f"${config.MONTHLY_API_LIMIT_USD:.2f}")

    return True, (f"Budget OK (Tag: ${day:.4f}/${config.DAILY_API_LIMIT_USD:.2f}, "
                  f"Monat: ${month:.4f}/${config.MONTHLY_API_LIMIT_USD:.2f})")


def status() -> str:
    """Lesbarer Budget-Status für Logs/Dashboard."""
    day   = spent_today()
    month = spent_this_month()
    day_pct   = day   / config.DAILY_API_LIMIT_USD   * 100 if config.DAILY_API_LIMIT_USD else 0
    month_pct = month / config.MONTHLY_API_LIMIT_USD * 100 if config.MONTHLY_API_LIMIT_USD else 0
    return (f"💰 Budget — Heute: ${day:.4f} ({day_pct:.0f}%)  |  "
            f"Monat: ${month:.4f} ({month_pct:.0f}%)")
