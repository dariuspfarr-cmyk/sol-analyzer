"""
Zentrale Konfiguration mit sicheren MIN/MAX-Grenzen.
Aktuelle Werte liegen in config.json – wird von threshold_optimizer.py automatisch angepasst.
Niemals die BOUNDS-Definitionen ändern; nur config.json-Werte werden modifiziert.
"""

import json
from pathlib import Path
from typing import Any

CONFIG_FILE = Path(__file__).parent / "config.json"

# ── Billing-Schutz (NIEMALS API-Key-Abrechnung erlauben) ──────────────────────
FORCE_OAUTH_ONLY      = True    # Nur OAuth/Subscription, keine API-Key-Abrechnung
DAILY_API_LIMIT_USD   = 0.08    # Hartes Tageslimit
MONTHLY_API_LIMIT_USD = 1.50    # Hartes Monatslimit
ANTHROPIC_MODEL_HAIKU  = "claude-haiku-4-5"
ANTHROPIC_MODEL_SONNET = "claude-sonnet-4-6"

# ── Sichere Grenzen (werden vom Optimizer nie überschritten) ──────────────────
BOUNDS: dict[str, dict] = {
    # Volumen-Filter
    "VOLUME_SPIKE_MULTIPLIER":  {"min": 1.2,  "max": 5.0,  "default": 2.0},
    # Haiku-Striktheit (>1.0 = strenger, <1.0 = lockerer)
    "HAIKU_STRICTNESS":         {"min": 0.3,  "max": 3.0,  "default": 1.0},
    # Mindest-Konfidenz pro Setup-Typ (0=low, 1=medium, 2=high)
    "BOS_MIN_CONFIDENCE":       {"min": 0,    "max": 2,    "default": 1},
    "CHoCH_MIN_CONFIDENCE":     {"min": 0,    "max": 2,    "default": 1},
    "EQH_MIN_CONFIDENCE":       {"min": 0,    "max": 2,    "default": 0},
    "EQL_MIN_CONFIDENCE":       {"min": 0,    "max": 2,    "default": 0},
    "Zone_MIN_CONFIDENCE":      {"min": 0,    "max": 2,    "default": 0},
    # Trigger-Gewichte (beeinflussen Konfidenz-Score)
    "BOS_WEIGHT":               {"min": 0.1,  "max": 2.0,  "default": 1.0},
    "CHoCH_WEIGHT":             {"min": 0.1,  "max": 3.0,  "default": 1.0},
    "EQH_WEIGHT":               {"min": 0.1,  "max": 3.0,  "default": 0.8},
    "EQL_WEIGHT":               {"min": 0.1,  "max": 3.0,  "default": 0.8},
    "Zone_WEIGHT":              {"min": 0.1,  "max": 2.0,  "default": 0.9},
    # Lokales Modell
    "LOCAL_MODEL_MIN_ACCURACY": {"min": 0.50, "max": 0.90, "default": 0.60},
    # Max. Anpassung pro Optimizer-Zyklus (%)
    "MAX_CHANGE_PER_CYCLE":     {"min": 0.05, "max": 0.30, "default": 0.20},
    # ── Signal-Erkennungsparameter (vom Optimizer anpassbar) ──────────────────
    # Pivot-Lookback: Fenster für Swing-High/Low-Erkennung (Kerzen)
    "PIVOT_LB":                 {"min": 3,     "max": 15,   "default": 5},
    # EQH/EQL-Toleranz: max. relative Preisabweichung für Equal Highs/Lows
    "EQH_TOLERANCE":            {"min": 0.001, "max": 0.006, "default": 0.0015},
    # CHoCH-Fenster: Anzahl Kerzen für Trendstruktur-Erkennung
    "CHOCH_WINDOW":             {"min": 4,     "max": 20,   "default": 10},
}

CONFIDENCE_LEVELS = ["low", "medium", "high"]   # 0 / 1 / 2


# ── Öffentliche API ───────────────────────────────────────────────────────────
_load_cache: dict | None = None
_load_mtime: float = -1.0


def load() -> dict[str, Any]:
    """
    Lädt config.json, füllt fehlende Keys mit Defaults auf.
    Cached per Datei-mtime — wird sehr häufig aufgerufen, daher kein Datei-I/O,
    solange config.json unverändert ist. Gibt stets eine Kopie zurück.
    """
    global _load_cache, _load_mtime
    try:
        mt = CONFIG_FILE.stat().st_mtime if CONFIG_FILE.exists() else 0.0
    except OSError:
        mt = 0.0
    if _load_cache is not None and mt == _load_mtime:
        return dict(_load_cache)

    defaults = {k: v["default"] for k, v in BOUNDS.items()}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                stored = json.load(f)
            defaults.update({k: v for k, v in stored.items() if k in BOUNDS})
        except (json.JSONDecodeError, OSError):
            pass
    _load_cache, _load_mtime = defaults, mt
    return dict(_load_cache)


def save(cfg: dict[str, Any]) -> None:
    """Speichert Konfiguration sicher (alle Werte geclamped)."""
    global _load_mtime
    clamped = {k: clamp(k, v) for k, v in cfg.items() if k in BOUNDS}
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(clamped, f, indent=2, ensure_ascii=False)
    _load_mtime = -1.0   # Cache invalidieren → nächster load() liest neu


def get(key: str) -> Any:
    """Gibt einen einzelnen Threshold-Wert zurück."""
    return load().get(key, BOUNDS[key]["default"])


def clamp(key: str, value: float) -> float:
    """Begrenzt einen Wert auf den sicheren Bereich."""
    b = BOUNDS[key]
    return max(b["min"], min(b["max"], float(value)))


def max_delta(key: str, current: float) -> float:
    """Berechnet den maximal erlaubten Schritt (+/-) für einen Parameter."""
    span  = BOUNDS[key]["max"] - BOUNDS[key]["min"]
    limit = get("MAX_CHANGE_PER_CYCLE")
    return span * limit


def reset_to_defaults() -> None:
    """Setzt alle Thresholds auf Standardwerte zurück."""
    save({k: v["default"] for k, v in BOUNDS.items()})


def summary() -> str:
    """Gibt eine lesbare Zusammenfassung aller Thresholds zurück."""
    cfg = load()
    lines = ["── Aktuelle Thresholds ─────────────────────────────"]
    for k, v in cfg.items():
        b    = BOUNDS[k]
        dflt = b["default"]
        mark = "  ← geändert" if v != dflt else ""
        lines.append(f"  {k:<35} = {v!s:<8}  [{b['min']} … {b['max']}]{mark}")
    lines.append("────────────────────────────────────────────────────")
    return "\n".join(lines)


# Beim Import sofort initialisieren (erstellt config.json falls nicht vorhanden)
if not CONFIG_FILE.exists():
    reset_to_defaults()
