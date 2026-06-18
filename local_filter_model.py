"""
Lokales XGBoost-Filtermodell — ersetzt Haiku-API-Call nach 200+ Signalen.

Features:  setup_type, timeframe, bias, confidence, volume_ratio,
           zone_position, time_of_day, day_of_week
Target:    WIN (1) vs LOSS/EXPIRED (0)

Trainiert sich jeden Sonntag neu. Wird nur verwendet wenn Accuracy > 60%.
Spart Haiku-API-Kosten wenn genug Trainingsdaten vorhanden.
"""

import json
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MODEL_FILE  = Path(__file__).parent / "filter_model.pkl"
REPORT_FILE = Path(__file__).parent / "model_report.json"

MIN_SAMPLES  = 200
MIN_ACCURACY = None  # wird aus config geladen


# ── Feature-Engineering ───────────────────────────────────────────────────────
_SETUP_MAP = {
    "BOS": 0, "CHoCH": 1, "EQH": 2, "EQL": 3,
    "Zone": 4, "Volume": 5, "Unknown": 6,
}
_CONF_MAP = {"low": 0, "medium": 1, "high": 2}
_BIAS_MAP = {"bullish": 1, "bearish": -1, "neutral": 0}
_ZONE_MAP = {"discount": -1, "neutral": 0, "premium": 1}
_TF_MAP   = {"1m": 0, "5m": 1, "15m": 2, "30m": 3, "1h": 4,
              "2h": 5, "4h": 6, "1d": 7, "1w": 8}

FEATURE_NAMES = [
    "setup_type_enc", "timeframe_enc", "bias_enc", "confidence_enc",
    "confidence_score", "volume_ratio", "zone_position_enc",
    "time_of_day", "day_of_week",
]


def _encode_signal(s: dict) -> Optional[list]:
    """Enkodiert ein Signal-Dict in einen Feature-Vektor."""
    try:
        row = [
            _SETUP_MAP.get(s.get("setup_type", "Unknown"), 6),
            _TF_MAP.get(str(s.get("timeframe", "4h")), 6),
            _BIAS_MAP.get(s.get("bias", "neutral"), 0),
            _CONF_MAP.get(s.get("confidence", "low"), 0),
            float(s.get("confidence_score") or 0.0),
            float(s.get("volume_ratio") or 1.0),
            _ZONE_MAP.get(s.get("zone_position", "neutral"), 0),
            int(s.get("time_of_day") or 12),
            int(s.get("day_of_week") or 0),
        ]
        return row
    except (ValueError, TypeError):
        return None


def _encode_from_zones(zones: dict, df, trigger_reason: str,
                        timeframe: str) -> Optional[list]:
    """Enkodiert live Zones-Daten für Inferenz (ohne gespeichertes Signal)."""
    import signal_logger as sl
    setup_type, bias, _ = sl._parse_trigger(trigger_reason)
    _, _, conf_score = _calc_conf_from_zones(zones, df, trigger_reason)

    now      = datetime.now(timezone.utc)
    vol      = df["volume"].values
    vol_ratio = float(vol[-1] / vol[-21:-1].mean()) if len(vol) >= 21 else 1.0

    eq       = zones.get("equilibrium", zones.get("price_now", 1))
    p_bot    = zones.get("premium_bottom", eq * 1.05)
    d_top    = zones.get("discount_top",   eq * 0.95)
    price    = zones.get("price_now", 0)
    if price >= p_bot:
        zone_enc = _ZONE_MAP["premium"]
    elif price <= d_top:
        zone_enc = _ZONE_MAP["discount"]
    else:
        zone_enc = _ZONE_MAP["neutral"]

    conf_int = _CONF_MAP.get(conf_score[0], 0)

    return [
        _SETUP_MAP.get(setup_type, 6),
        _TF_MAP.get(timeframe, 6),
        _BIAS_MAP.get(bias, 0),
        conf_int,
        float(conf_score[1]),
        round(vol_ratio, 3),
        zone_enc,
        now.hour,
        now.weekday(),
    ]


def _calc_conf_from_zones(zones, df, trigger_reason: str) -> tuple:
    """Kleine Kopie aus signal_logger für die Live-Inferenz."""
    import signal_logger as sl
    _, _, all_trig = sl._parse_trigger(trigger_reason)
    vol   = df["volume"].values
    vr    = float(vol[-1] / vol[-21:-1].mean()) if len(vol) >= 21 else 1.0
    level, score = sl._calc_confidence(all_trig, vr)
    return level, score, (level, score)


# ── Training ───────────────────────────────────────────────────────────────────
def train_if_ready() -> Optional[dict]:
    """
    Trainiert das XGBoost-Modell wenn genügend Daten vorhanden.
    Gibt Model-Report-Dict zurück oder None wenn Training nicht möglich.
    """
    import signal_logger
    signals = signal_logger.get_all_signals(include_open=False)
    closed  = [s for s in signals if s.get("outcome") in ("WIN", "LOSS")]

    print(f"  🧠 Modell-Training: {len(closed)} abgeschlossene Signale vorhanden "
          f"(Minimum: {MIN_SAMPLES})")

    if len(closed) < MIN_SAMPLES:
        print(f"  ℹ️  Zu wenig Daten ({len(closed)} < {MIN_SAMPLES}) – "
              f"Haiku-API wird weiterhin verwendet.")
        return None

    try:
        from xgboost import XGBClassifier
        from sklearn.model_selection import cross_val_score, StratifiedKFold
        from sklearn.preprocessing import StandardScaler
        import numpy as np
    except ImportError as e:
        print(f"  ⚠️  Abhängigkeit fehlt ({e}) – Training übersprungen.")
        return None

    X, y = [], []
    for s in closed:
        feat = _encode_signal(s)
        if feat is None:
            continue
        X.append(feat)
        y.append(1 if s["outcome"] == "WIN" else 0)

    if len(X) < MIN_SAMPLES:
        return None

    X = np.array(X, dtype=float)
    y = np.array(y)

    # Balanciertes Modell
    scale_pos = max(1, (y == 0).sum() / max((y == 1).sum(), 1))
    model = XGBClassifier(
        n_estimators     = 200,
        max_depth        = 4,
        learning_rate    = 0.05,
        subsample        = 0.8,
        colsample_bytree = 0.8,
        scale_pos_weight = scale_pos,
        eval_metric      = "logloss",
        verbosity        = 0,
        random_state     = 42,
    )

    # Kreuzvalidierung für robuste Genauigkeitsschätzung
    cv      = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    scores  = cross_val_score(model, X, y, cv=cv, scoring="accuracy")
    accuracy = float(scores.mean())

    # Modell auf allen Daten trainieren
    model.fit(X, y)

    # Feature Importance
    importances = model.feature_importances_.tolist()
    feat_imp    = dict(zip(FEATURE_NAMES, [round(v, 4) for v in importances]))

    # Speichern
    with open(MODEL_FILE, "wb") as f:
        pickle.dump({"model": model, "accuracy": accuracy,
                     "trained_on": len(X), "feature_names": FEATURE_NAMES}, f)

    # Report
    report = {
        "erstellt_am":      datetime.now(timezone.utc).isoformat(),
        "accuracy":         round(accuracy, 4),
        "accuracy_pct":     round(accuracy * 100, 2),
        "trainings_samples": len(X),
        "wins_in_training": int(y.sum()),
        "losses_in_training": int((y == 0).sum()),
        "cv_scores":        [round(s, 4) for s in scores.tolist()],
        "feature_importance": feat_imp,
        "ersetzt_haiku":    accuracy >= _get_min_accuracy(),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    status = "✅ Haiku ersetzt" if report["ersetzt_haiku"] else "⚠️  Accuracy zu niedrig"
    print(f"  🧠 Modell trainiert: Accuracy = {accuracy*100:.1f}%  {status}")
    print(f"     Feature Importance: " +
          ", ".join(f"{k}={v:.3f}" for k, v in
                    sorted(feat_imp.items(), key=lambda x: -x[1])[:4]))

    return report


# ── Inferenz ────────────────────────────────────────────────────────────────────
def predict(zones: dict, df, trigger_reason: str, timeframe: str) -> bool:
    """
    Gibt True zurück wenn das lokale Modell das Setup als high-probability einstuft.
    Fallback auf True wenn Modell nicht verfügbar.
    """
    info = get_model_info()
    if info is None or not info.get("ersetzt_haiku", False):
        return True   # kein Modell → immer durchlassen (Haiku entscheidet)

    try:
        with open(MODEL_FILE, "rb") as f:
            bundle = pickle.load(f)
        model = bundle["model"]

        features = _encode_from_zones(zones, df, trigger_reason, timeframe)
        if features is None:
            return True

        import numpy as np
        prob    = model.predict_proba([features])[0][1]   # P(WIN)
        min_acc = _get_min_accuracy()
        # Schwellenwert: 0.5 bei minimaler Accuracy, höher bei besserem Modell
        threshold = 0.40 + (info["accuracy"] - min_acc) * 0.5
        decision  = bool(prob >= threshold)

        print(f"  🤖 Lokales Modell: P(WIN)={prob:.2f}  Schwelle={threshold:.2f}  "
              f"→ {'✅ WEITER' if decision else '✗ ÜBERSPRINGEN'}")
        return decision

    except Exception as e:
        print(f"  ⚠️  Modell-Inferenz fehlgeschlagen ({e}) – Fallback auf True")
        return True


def get_model_info() -> Optional[dict]:
    """Gibt Modell-Report zurück oder None wenn kein Modell vorhanden."""
    if not REPORT_FILE.exists() or not MODEL_FILE.exists():
        return None
    try:
        with open(REPORT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def is_active() -> bool:
    """Gibt True zurück wenn das lokale Modell Haiku ersetzen kann."""
    info = get_model_info()
    return info is not None and info.get("ersetzt_haiku", False)


def _get_min_accuracy() -> float:
    try:
        import config as cfg
        return float(cfg.get("LOCAL_MODEL_MIN_ACCURACY"))
    except Exception:
        return 0.60
