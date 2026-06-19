"""
improvement_scanner — automatischer Verbesserungs- & Optimierungs-Scanner.

Analysiert das GESAMTE SOL-Analyzer-Tool aus mehreren Quellen, leitet daraus
konkrete, priorisierte Verbesserungen ab und schreibt sie in einen Backlog,
den Claude Code bei jedem Start liest (siehe CLAUDE.md) und abarbeiten kann.

Quellen
  • error.log                 — wiederkehrende Laufzeitfehler (Bugs)
  • performance_report.json   — verlierende Setups, schwache TFs/Bias, R:R, EXPIRED
  • state.json                — Profit-Factor, Drawdown, Verlustserien
  • Code-Metriken             — sehr große Dateien, TODO/FIXME/HACK-Marker
  • Modul-Health              — Module, die nicht importieren
  • ruff / ESLint (optional)  — echte Lint-Bugs

Ausgabe
  IMPROVEMENTS.md   — menschenlesbarer, priorisierter Backlog (für Claude Code)
  improvements.json — Status-Tracking (open/resolved), Dedup, Erst-/Letzt-Sichtung

Selbst-aktualisierend: behobene Probleme verschwinden beim nächsten Lauf
automatisch aus dem offenen Backlog; neue tauchen auf.

Aufruf:  python improvement_scanner.py            (voll, inkl. Linter)
         python improvement_scanner.py --fast     (ohne Linter-Subprozesse)
"""

from __future__ import annotations
import json
import re
import sys
import glob
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

BASE          = Path(__file__).parent
MD_FILE       = BASE / "IMPROVEMENTS.md"
JSON_FILE     = BASE / "improvements.json"

PRIORITIES = ["P1", "P2", "P3", "P4"]
PRIO_LABEL = {
    "P1": "🔴 P1 — Kritisch",
    "P2": "🟠 P2 — Hoch",
    "P3": "🟡 P3 — Mittel",
    "P4": "🟢 P4 — Niedrig",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fid(category: str, key: str) -> str:
    return hashlib.sha1(f"{category}|{key}".encode("utf-8")).hexdigest()[:10]


def _finding(category, priority, title, detail, action, source, key, file=""):
    """Einheitliches Finding-Objekt."""
    return {
        "id":       _fid(category, key),
        "category": category,
        "priority": priority,
        "title":    title,
        "detail":   detail,
        "action":   action,     # konkreter Umsetzungs-Hinweis für Claude Code
        "source":   source,
        "file":     file,
    }


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  ANALYZER — jeder gibt eine Liste Findings zurück, jeder in eigenem try/except
# ══════════════════════════════════════════════════════════════════════════════

def scan_errors() -> list[dict]:
    out = []
    log = BASE / "error.log"
    if not log.exists():
        return out
    try:
        lines = [l for l in log.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except Exception:
        return out
    # Zeitstempel entfernen, Zahlen normalisieren → Gruppierung gleichartiger Fehler
    groups: dict[str, int] = {}
    for l in lines:
        msg = re.sub(r"^\[.*?\]\s*", "", l)
        norm = re.sub(r"\d+", "#", msg).strip()[:160]
        groups[norm] = groups.get(norm, 0) + 1
    for norm, cnt in sorted(groups.items(), key=lambda x: -x[1]):
        prio = "P1" if cnt >= 5 else "P2"
        out.append(_finding(
            "bug", prio,
            f"Wiederkehrender Laufzeitfehler ({cnt}×)",
            f"In error.log {cnt}× protokolliert: «{norm}»",
            "Ursache im betroffenen Modul finden und beheben; Fehler sollte "
            "danach nicht mehr in error.log auftauchen.",
            "error.log", key=norm,
        ))
    return out


def scan_trading() -> list[dict]:
    out = []
    rep = _read_json(BASE / "performance_report.json")
    if rep:
        g = rep.get("gesamt", {})

        # Verlierende Setup-Typen (statistisch belastbar ab 15 Signalen)
        for stype, s in (rep.get("nach_setup_typ") or {}).items():
            n, wr, pnl = s.get("count", 0), s.get("win_rate_pct", 50), s.get("avg_pnl_pct", 0)
            if n >= 15 and (wr < 40 or pnl < -0.5):
                out.append(_finding(
                    "trading", "P1",
                    f"Setup «{stype}» verliert Geld (WR {wr:.0f}%, Ø {pnl:+.2f}%)",
                    f"{n} Signale, Win-Rate {wr:.1f}%, Ø-PnL {pnl:+.2f}%.",
                    f"Gewicht von «{stype}» in config.json deutlich senken oder Setup "
                    f"gezielt filtern (z. B. nur in passendem Markt-Kontext zulassen). "
                    f"Win-Rate < 40% bedeutet aktiver Kapitalverlust.",
                    "performance_report.json", key=f"setup_loss_{stype}",
                ))

        # Schwache Timeframes
        for tf, s in (rep.get("nach_timeframe") or {}).items():
            n, wr = s.get("count", 0), s.get("win_rate_pct", 50)
            if n >= 15 and wr < 42:
                out.append(_finding(
                    "trading", "P2",
                    f"Timeframe {tf} schwach (WR {wr:.0f}%)",
                    f"{n} Signale auf {tf}, Win-Rate nur {wr:.1f}%.",
                    f"Schwellen/Filter für {tf} verschärfen oder {tf} niedriger "
                    f"gewichten; prüfen ob die SMC-Parameter für {tf} sinnvoll skaliert sind.",
                    "performance_report.json", key=f"tf_weak_{tf}",
                ))

        # Schwacher Bias
        for bias, s in (rep.get("nach_bias") or {}).items():
            n, wr = s.get("count", 0), s.get("win_rate_pct", 50)
            if n >= 20 and wr < 45:
                out.append(_finding(
                    "trading", "P2",
                    f"{bias.capitalize()}-Signale schwach (WR {wr:.0f}%)",
                    f"{n} {bias}-Signale, Win-Rate {wr:.1f}%.",
                    f"Prüfen ob {bias}-Setups im aktuellen Marktregime systematisch "
                    f"benachteiligt sind (z. B. Bias-Filter über web_researcher/MTF schärfen).",
                    "performance_report.json", key=f"bias_weak_{bias}",
                ))

        # Niedriges Ø R:R
        rr = g.get("avg_rr", 0)
        if rr and rr < 1.5:
            out.append(_finding(
                "trading", "P2",
                f"Ø Risk:Reward niedrig ({rr:.2f})",
                f"Durchschnittliches R:R aller Signale ist {rr:.2f} (Ziel ≥ 1.5).",
                "TP/SL-Ableitung in signal_logger._derive_sl_tp prüfen — TP zu nah "
                "oder SL zu weit. Höheres R:R macht selbst niedrigere Win-Rates profitabel.",
                "performance_report.json", key="low_rr",
            ))

        # Hohe EXPIRED-Rate
        total, exp = g.get("gesamt_signale", 0), g.get("abgelaufen", 0)
        if total and exp / total > 0.15:
            out.append(_finding(
                "trading", "P2",
                f"Hohe EXPIRED-Rate ({exp}/{total} = {exp/total*100:.0f}%)",
                f"{exp} von {total} Signalen liefen ab ohne TP/SL zu treffen.",
                "Hold-Zeiten (tf_profiles.max_hold_hours) oder Entry-Timing prüfen — "
                "viele EXPIRED bedeuten, dass Ziele zu ambitioniert oder Einstiege zu früh sind.",
                "performance_report.json", key="high_expired",
            ))

        # Lange Verlustserie
        streak, max_streak = g.get("current_streak", 0), g.get("max_loss_streak", 0)
        if streak <= -8 or max_streak >= 12:
            out.append(_finding(
                "trading", "P1",
                f"Lange Verlustserie (aktuell {streak}, max {max_streak})",
                f"Aktuelle Serie {streak}, längste Verlustserie {max_streak}.",
                "Risiko-Throttle/Circuit-Breaker prüfen; evtl. nach N Verlusten in Folge "
                "pausieren oder Strategie-Profil wechseln (active_strategy.json).",
                "performance_report.json", key="loss_streak",
            ))

        # KI-Signal-Qualität
        awr = g.get("api_win_rate_pct")
        if awr is not None and awr < 55 and g.get("api_calls", 0) >= 50:
            out.append(_finding(
                "trading", "P3",
                f"KI-Signale kaum besser als Zufall (WR {awr:.0f}%)",
                f"API-bestätigte Signale: Win-Rate {awr:.1f}% bei {g.get('api_calls')} Calls.",
                "smart_router-Schwellen / Layer-2-Prompt prüfen — die KI-Bestätigung "
                "sollte deutlich über 55% liegen, sonst lohnt der API-Call nicht.",
                "performance_report.json", key="weak_ai_wr",
            ))

    # Paper-Trader-State
    st = _read_json(BASE / "state.json")
    if st:
        pf, n = st.get("profit_factor", 1), st.get("total_trades", 0)
        if n >= 10 and pf < 1.0:
            out.append(_finding(
                "trading", "P1",
                f"Paper-Trader unprofitabel (Profit-Factor {pf:.2f})",
                f"{n} Trades, Profit-Factor {pf:.2f} (< 1.0 = Verlust).",
                "Größte Verlustquelle über performance_compare.py / nach_setup_typ "
                "identifizieren und gezielt filtern.",
                "state.json", key="pt_unprofitable",
            ))
        dd = st.get("max_drawdown", 0)
        if dd > 10:
            out.append(_finding(
                "trading", "P2",
                f"Hoher Drawdown ({dd:.1f}%)",
                f"Maximaler Drawdown des Paper-Traders: {dd:.1f}%.",
                "Positionsgrößen-/Risiko-Skalierung bei Drawdown verschärfen "
                "(paper_trader DD_SCALE).",
                "state.json", key="high_drawdown",
            ))
    return out


# Ziel des Gesamtsystems: maximale Win-Rate. Dieser Analyzer macht die WR zum
# permanenten Nordstern — jeder Rückgang oder ein Wert unter Ziel wird sofort
# als hochpriorisierter Punkt gemeldet, damit die WR nie wieder unbemerkt
# erodiert (wie zuletzt von ~85% auf 18%).
WR_TARGET = 60.0


def scan_winrate_trend() -> list[dict]:
    out = []
    ev = _read_json(BASE / "strategy_evolution.json")
    if not isinstance(ev, list):
        return out
    wrs = [(e.get("metrics_after") or {}).get("win_rate") for e in ev]
    wrs = [float(w) for w in wrs if isinstance(w, (int, float))]
    if len(wrs) < 4:
        return out

    cur         = wrs[-1]
    recent      = wrs[-3:]
    earlier     = wrs[:-3] or recent
    recent_avg  = sum(recent) / len(recent)
    earlier_avg = sum(earlier) / len(earlier)
    drop        = earlier_avg - recent_avg

    # WR fällt deutlich (Trend nach unten)
    if drop >= 5.0:
        out.append(_finding(
            "trading", "P1",
            f"Win-Rate sinkt ({earlier_avg:.0f}% → {recent_avg:.0f}%)",
            f"Mittlere Win-Rate fiel von {earlier_avg:.1f}% auf zuletzt "
            f"{recent_avg:.1f}% (aktuell {cur:.1f}%).",
            "WIN-RATE IST DAS HAUPTZIEL. Regime-Wechsel prüfen (Reversal- vs "
            "Trend-Setups), Selektivität erhöhen, verlierende Setups/Regime härter "
            "filtern. Ggf. backtester.py nutzen, um optimale Schwellen zu finden.",
            "strategy_evolution.json", key="wr_declining",
        ))

    # WR unter Zielwert
    if cur < WR_TARGET:
        out.append(_finding(
            "trading", "P2",
            f"Win-Rate unter Ziel ({cur:.0f}% < {WR_TARGET:.0f}%)",
            f"Aktuelle Win-Rate {cur:.1f}% liegt unter dem Zielwert {WR_TARGET:.0f}%.",
            "Selektiver werden: nur A+-Setups zulassen (Trend-Folge im Trend, "
            "Confluence ≥2 Trigger, hohe Konfidenz). Schwächste Setups/TFs/Bias "
            "im aktuellen Regime drosseln.",
            "strategy_evolution.json", key="wr_below_target",
        ))
    return out


def scan_code_metrics() -> list[dict]:
    out = []
    todo_hits: list[str] = []
    for path in glob.glob(str(BASE / "*.py")) + glob.glob(str(BASE / "*.html")):
        name = Path(path).name
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        lines = text.splitlines()

        # Sehr große Dateien (nur .py — schwer wartbar)
        if name.endswith(".py") and len(lines) > 1200:
            out.append(_finding(
                "quality", "P3",
                f"{name} ist sehr groß ({len(lines)} Zeilen)",
                f"{name} hat {len(lines)} Zeilen — schwer zu überblicken/testen.",
                "In thematische Module aufteilen (z. B. Execution/Sizing/Indikatoren), "
                "wie bereits bei pt_indicators.py begonnen.",
                "code-metrics", key=f"large_file_{name}", file=name,
            ))

        # TODO/FIXME/HACK-Marker
        for i, ln in enumerate(lines, 1):
            m = re.search(r"\b(TODO|FIXME|HACK|XXX)\b[:\s](.{0,80})", ln)
            if m:
                todo_hits.append(f"{name}:{i}  {m.group(1)}: {m.group(2).strip()}")

    if todo_hits:
        sample = "\n".join("  • " + h for h in todo_hits[:15])
        more = f"\n  … und {len(todo_hits)-15} weitere" if len(todo_hits) > 15 else ""
        out.append(_finding(
            "quality", "P3",
            f"{len(todo_hits)} offene TODO/FIXME/HACK-Marker im Code",
            f"Im Code hinterlegte offene Punkte:\n{sample}{more}",
            "Marker durchgehen und entweder umsetzen oder als bewusste Entscheidung "
            "entfernen.",
            "code-metrics", key="todo_markers",
        ))
    return out


def scan_health() -> list[dict]:
    out = []
    import importlib
    for path in sorted(glob.glob(str(BASE / "*.py"))):
        mod = Path(path).stem
        if mod in ("improvement_scanner",):
            continue
        try:
            importlib.import_module(mod)
        except Exception as e:
            out.append(_finding(
                "bug", "P1",
                f"Modul «{mod}» importiert nicht",
                f"Import von {mod}.py schlägt fehl: {type(e).__name__}: {e}",
                "Import-/Syntaxfehler beheben — ein nicht importierbares Modul ist "
                "ein harter Defekt.",
                "module-health", key=f"import_fail_{mod}", file=f"{mod}.py",
            ))
    return out


def scan_lint(fast: bool) -> list[dict]:
    out = []
    if fast:
        return out
    py = str(BASE / ".venv" / "Scripts" / "python.exe")
    if not Path(py).exists():
        py = sys.executable
    # ── ruff (Python) ──
    try:
        r = subprocess.run([py, "-m", "ruff", "check", ".", "--quiet", "--output-format", "concise"],
                           cwd=str(BASE), capture_output=True, text=True, timeout=90)
        n = len([l for l in r.stdout.splitlines() if re.match(r".+:\d+:\d+:", l)])
        if n > 0:
            out.append(_finding(
                "quality", "P3",
                f"ruff meldet {n} Python-Lint-Funde",
                "ruff (F/E9/B-Regeln) meldet offene Punkte.",
                "`python -m ruff check .` ausführen und beheben (ggf. `--fix`).",
                "ruff", key="ruff_findings",
            ))
    except Exception:
        pass
    # ── ESLint (JS in HTML) ──
    try:
        r = subprocess.run(["npx", "eslint", ".", "--format", "compact"],
                           cwd=str(BASE), capture_output=True, text=True, timeout=120, shell=True)
        errs = len(re.findall(r", Error -", r.stdout))
        if errs > 0:
            out.append(_finding(
                "bug", "P2",
                f"ESLint meldet {errs} JS-Fehler",
                "ESLint findet echte JS-Fehler in den HTML-Dashboards.",
                "`npm run lint` ausführen und Fehler beheben.",
                "eslint", key="eslint_errors",
            ))
    except Exception:
        pass
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  MERGE + AUSGABE
# ══════════════════════════════════════════════════════════════════════════════

def _merge(current: list[dict]) -> dict:
    """Verbindet aktuelle Findings mit dem bestehenden Tracking (Status, Historie)."""
    prev = _read_json(JSON_FILE) or {}
    prev_items = {it["id"]: it for it in prev.get("items", [])}
    now = _now()
    merged: dict[str, dict] = {}

    for f in current:
        old = prev_items.get(f["id"])
        if old:
            f["first_seen"]  = old.get("first_seen", now)
            f["occurrences"] = old.get("occurrences", 0) + 1
            # War es als erledigt markiert, taucht aber wieder auf → Regression
            f["status"]      = "open" if old.get("status") in (None, "open", "resolved") else old["status"]
            if old.get("status") == "resolved":
                f["regression"] = True
        else:
            f["first_seen"]  = now
            f["occurrences"] = 1
            f["status"]      = "open"
        f["last_seen"] = now
        merged[f["id"]] = f

    # Frühere offene Findings, die JETZT nicht mehr erkannt werden → auto-resolved
    for fid, old in prev_items.items():
        if fid not in merged and old.get("status") == "open":
            old["status"]      = "resolved"
            old["resolved_at"] = now
            merged[fid] = old

    return {"generated_at": now, "items": list(merged.values())}


def _write_md(doc: dict) -> None:
    items = [it for it in doc["items"] if it.get("status") == "open"]
    order = {p: i for i, p in enumerate(PRIORITIES)}
    items.sort(key=lambda x: (order.get(x["priority"], 9), x["category"]))

    counts = {p: sum(1 for it in items if it["priority"] == p) for p in PRIORITIES}
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# 🤖 Verbesserungs-Backlog (auto-generiert)",
        "",
        f"_Zuletzt aktualisiert: {ts} · von `improvement_scanner.py`_",
        "",
        "> **Für Claude Code:** Dies ist der automatisch erkannte Optimierungs-Backlog "
        "des SOL Analyzers. Arbeite die offenen Punkte nach Priorität ab (P1 zuerst). "
        "Jeder Punkt hat einen konkreten **Vorschlag**. Nach dem Beheben den Scanner "
        "erneut laufen lassen (`python improvement_scanner.py`) — erledigte Punkte "
        "verschwinden dann automatisch.",
        "",
        f"**Offen:** {len(items)}  ·  "
        + "  ·  ".join(f"{PRIO_LABEL[p].split(' ')[0]} {counts[p]}" for p in PRIORITIES),
        "",
    ]

    if not items:
        lines += ["", "✅ **Aktuell keine offenen Punkte erkannt.** Alles sauber.", ""]
    else:
        for p in PRIORITIES:
            group = [it for it in items if it["priority"] == p]
            if not group:
                continue
            lines.append(f"## {PRIO_LABEL[p]}  ({len(group)})")
            lines.append("")
            for it in group:
                reg = " ⚠️ *(Regression — war als behoben markiert)*" if it.get("regression") else ""
                src = f"`{it['file']}`" if it.get("file") else f"_{it['source']}_"
                lines.append(f"### [{it['category']}] {it['title']}{reg}")
                lines.append(f"- **Problem:** {it['detail']}")
                lines.append(f"- **Vorschlag:** {it['action']}")
                lines.append(f"- **Quelle:** {src}  ·  ID `{it['id']}`  ·  "
                             f"erstmals {it['first_seen'][:10]}  ·  {it['occurrences']}× gesehen")
                lines.append("")

    # Kleiner Hinweis zur Bedienung
    lines += [
        "---",
        "_Status manuell setzen: in `improvements.json` `status` auf `done`/`wontfix` "
        "ändern. Behobene objektive Funde (Fehler, Lint, verlierende Setups) "
        "verschwinden beim nächsten Scan von selbst._",
        "",
    ]
    MD_FILE.write_text("\n".join(lines), encoding="utf-8")


def run(fast: bool = False) -> dict:
    """Führt alle Analyzer aus, mergt mit Historie, schreibt Backlog. Robust."""
    findings: list[dict] = []
    for fn in (scan_errors, scan_trading, scan_winrate_trend, scan_code_metrics, scan_health):
        try:
            findings += fn()
        except Exception as e:
            print(f"  [scanner] {fn.__name__} fehlgeschlagen: {e}")
    try:
        findings += scan_lint(fast)
    except Exception as e:
        print(f"  [scanner] scan_lint fehlgeschlagen: {e}")

    doc = _merge(findings)
    JSON_FILE.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_md(doc)

    open_items = [it for it in doc["items"] if it.get("status") == "open"]
    by_p = {p: sum(1 for it in open_items if it["priority"] == p) for p in PRIORITIES}
    print(f"  [scanner] {len(open_items)} offene Punkte "
          f"(P1={by_p['P1']} P2={by_p['P2']} P3={by_p['P3']} P4={by_p['P4']}) "
          f"→ IMPROVEMENTS.md")
    return doc


if __name__ == "__main__":
    run(fast="--fast" in sys.argv)
