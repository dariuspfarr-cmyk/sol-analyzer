#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║   SOL Analyzer — Lokaler Server (verbindet alles)         ║
║   Web-Tools (HTML)  +  JSON-API  +  Python-Bot            ║
╚══════════════════════════════════════════════════════════╝
"""

import json
import os
import threading
import io
import time
import queue
from contextlib import redirect_stdout
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).parent
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")

# Intervall für automatische Bot-Läufe (Stunden). 0 = aus.
# 2h-Takt: 15m/1h-Signale aus dem MTF-Scan bleiben frisch genug für den
# Paper Trader (15m-Signale veralten nach 2h). Kosten bleiben begrenzt —
# Budget-Guardian und 1-KI-Call-pro-Lauf-Limit gelten weiterhin.
AUTO_RUN_HOURS = float(os.getenv("AUTO_RUN_HOURS", "2"))


def _lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ── SSE-Event-Bus ──────────────────────────────────────────────────────────────
# Alle verbundenen Browser-Clients hören hier zu.
_sse_clients: list[queue.Queue] = []
_sse_lock = threading.Lock()


def _sse_broadcast(event: str, data: dict) -> None:
    """Schickt ein Event an alle verbundenen Browser sofort."""
    msg = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


# ── Signal-Watcher: bemerkt neue Signale in der DB ────────────────────────────
_last_signal_count = 0


def _watch_signals() -> None:
    """Läuft im Hintergrund, prüft alle 3s ob neue Signale da sind."""
    global _last_signal_count
    while True:
        try:
            import signal_logger
            cnt = signal_logger.count()
            total = cnt.get("total", 0)
            if total != _last_signal_count:
                _last_signal_count = total
                # Neuestes Signal holen und broadcasten (nur 1 Zeile laden)
                latest = signal_logger.get_all_signals(include_open=True, limit=1)
                _sse_broadcast("new_signal", {
                    "signal": latest[0] if latest else {},
                    "stats":  cnt,
                })
        except Exception:
            pass
        time.sleep(3)


# ── Paper-Trader-Watcher: spiegelt Trade-Open/Close/Balance live ins Dashboard ─
_last_paper_sig = None


def _watch_paper() -> None:
    """
    Bemerkt diskrete Paper-Trader-Änderungen (Position eröffnet/geschlossen,
    Balance bewegt) und pusht den vollständigen Status sofort via SSE — so
    erscheinen Live-Closes im Dashboard ohne 20s-Poll-Verzögerung.
    Liest state.json leichtgewichtig (nur Signatur), Vollabruf nur bei Änderung.
    """
    global _last_paper_sig
    import json as _j
    state_file = BASE / "state.json"
    while True:
        try:
            if state_file.exists():
                with open(state_file, encoding="utf-8") as f:
                    s = _j.load(f)
                sig = (
                    int(s.get("total_trades", 0)),
                    round(float(s.get("balance", 0.0)), 2),
                    len(s.get("positions", []) or []),
                )
                if sig != _last_paper_sig:
                    first = _last_paper_sig is None
                    _last_paper_sig = sig
                    if not first:   # erster Lauf = nur Baseline merken, kein Push
                        try:
                            import paper_trader
                            _sse_broadcast("paper_update", paper_trader.get_status())
                        except Exception:
                            pass
        except Exception:
            pass
        time.sleep(4)


# ── Laufzeit-Status des Bot-Durchlaufs ────────────────────────────────────────
_run_lock  = threading.Lock()
_run_state = {"running": False, "log": [], "finished_at": None, "error": None,
              "consecutive_errors": 0, "circuit_open": False}


def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ── API-Daten ─────────────────────────────────────────────────────────────────
def api_stats() -> dict:
    out = {"updated": datetime.now(timezone.utc).isoformat()}
    try:
        import signal_logger
        cnt = signal_logger.count()
        decided = cnt["win"] + cnt["loss"]
        cnt["win_rate"] = round(cnt["win"] / decided * 100, 1) if decided else None
        out["signals"] = cnt
    except Exception as e:
        out["signals"] = {"error": str(e)}
    model = _read_json(BASE / "model_report.json")
    out["model"] = {
        "active":   bool(model.get("ersetzt_haiku")),
        "accuracy": model.get("accuracy_pct"),
        "samples":  model.get("trainings_samples"),
    } if model else {"active": False, "accuracy": None, "samples": None}
    try:
        import cost_tracker
        out["cost_month"] = round(cost_tracker.get_monthly_total(), 5)
    except Exception:
        out["cost_month"] = None
    perf = _read_json(BASE / "performance_report.json")
    out["by_setup"] = perf.get("nach_setup_typ", {})
    return out


def api_submit_signal(body: dict) -> dict:
    """Empfängt ein Auto-KI-Signal vom Browser und speichert es in signals.db."""
    try:
        import signal_logger
        sig_id = signal_logger.log_autoki_signal(
            direction = body.get("direction", "long"),
            entry     = float(body.get("entry", 0)),
            sl        = float(body.get("sl", 0)),
            tp        = float(body.get("tp", 0)),
            rsi       = float(body.get("rsi", 50)),
            label     = str(body.get("label", "AUTO_KI")),
            conf      = float(body.get("conf") or 0.5),
            timeframe = str(body.get("timeframe", "4h")),
        )
        _sse_broadcast("new_signal", {"signal_id": sig_id, "source": "AUTO_KI"})
        return {"ok": True, "signal_id": sig_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_signals(limit: int = 50, source: str = None) -> list:
    try:
        import signal_logger
        if source:
            # Mit Quellen-Filter: größeres Fenster laden, filtern, dann begrenzen
            allowed = {s.strip().upper() for s in source.split(",")}
            rows = signal_logger.get_all_signals(include_open=True, limit=max(limit * 5, 200))
            rows = [r for r in rows if (r.get("source") or "LIVE").upper() in allowed]
            return rows[:limit]                      # bereits neueste-zuerst
        # Ohne Filter: direkt die N neuesten aus der DB (kein Full-Table-Load)
        return signal_logger.get_all_signals(include_open=True, limit=limit)
    except Exception as e:
        return [{"error": str(e)}]


def api_charts() -> list:
    charts_dir = BASE / "charts"
    if not charts_dir.exists():
        return []
    pngs = sorted(charts_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [f"charts/{p.name}" for p in pngs]


def api_schedule_status() -> dict:
    """Nächster geplanter automatischer Bot-Lauf."""
    if AUTO_RUN_HOURS <= 0:
        return {"auto": False}
    with _run_lock:
        fa = _run_state.get("finished_at")
    if fa:
        from datetime import timedelta
        last = datetime.fromisoformat(fa)
        nxt  = last + timedelta(hours=AUTO_RUN_HOURS)
        secs = max(0, int((nxt - datetime.now(timezone.utc)).total_seconds()))
        return {"auto": True, "interval_h": AUTO_RUN_HOURS,
                "next_in_secs": secs, "next_at": nxt.isoformat()}
    return {"auto": True, "interval_h": AUTO_RUN_HOURS, "next_in_secs": None}


# ── Bot-Durchlauf ─────────────────────────────────────────────────────────────
def _run_bot() -> None:
    buf = io.StringIO()
    _sse_broadcast("run_start", {"ts": datetime.now(timezone.utc).isoformat()})
    error = None
    try:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        import importlib, sol_analysis_bot
        importlib.reload(sol_analysis_bot)
        with redirect_stdout(buf):
            sol_analysis_bot.main()
    except Exception as e:
        error = str(e)
    finally:
        with _run_lock:
            _run_state["running"]     = False
            _run_state["finished_at"] = datetime.now(timezone.utc).isoformat()
            _run_state["log"]         = buf.getvalue().splitlines()
            _run_state["error"]       = error
            if error:
                _run_state["consecutive_errors"] += 1
                if _run_state["consecutive_errors"] >= 3 and not _run_state["circuit_open"]:
                    _run_state["circuit_open"] = True
            else:
                _run_state["consecutive_errors"] = 0
                _run_state["circuit_open"]        = False
        _sse_broadcast("run_done", {
            "ts":    _run_state["finished_at"],
            "error": error,
            "log":   _run_state["log"][-20:],
        })
        if _run_state.get("circuit_open"):
            _sse_broadcast("circuit_break", {
                "msg":    "⚠️ Auto-Run deaktiviert: 3 Fehler in Folge. Bitte manuell prüfen.",
                "errors": _run_state["consecutive_errors"],
            })
    # Vollständigen Lernzyklus nach jedem Analyse-Lauf anstoßen
    try:
        import strategy_evolver
        evo = strategy_evolver.run()
        if not evo.get("skipped"):
            _sse_broadcast("strategy_update", evo)
    except Exception:
        pass
    # Markt-Research aktualisieren und Browser informieren
    try:
        import web_researcher
        research = web_researcher.run()
        _sse_broadcast("research_update", research.get("analysis", {}))
    except Exception:
        pass


def api_paper() -> dict:
    """Paper-Trader-Status für das Dashboard."""
    try:
        import paper_trader
        return paper_trader.get_status()
    except Exception as e:
        return {"active": False, "running": False, "error": str(e)}


def _launch_paper_thread() -> None:
    """Startet den Paper-Trader als Daemon-Thread."""
    def _run():
        try:
            import paper_trader
            paper_trader.run_forever()
        except Exception as e:
            import traceback
            with open(BASE / "error.log", "a", encoding="utf-8") as f:
                f.write(f"[paper_trader] {e}\n{traceback.format_exc()}\n")
    threading.Thread(target=_run, daemon=True, name="paper-trader").start()


def api_paper_toggle() -> dict:
    """Startet oder stoppt den Paper Trader."""
    try:
        import paper_trader
        if paper_trader.is_running():
            paper_trader._stop_event.set()
            return {"running": False, "msg": "Paper Trader gestoppt"}
        else:
            _launch_paper_thread()
            return {"running": True, "msg": "Paper Trader gestartet"}
    except Exception as e:
        return {"running": False, "error": str(e)}


def api_learning() -> dict:
    """
    Aggregiert alle Lern-Daten des Bots für das Intelligence-Dashboard:
    Gewichte · Patterns · Stunden · Setup-Performance · Bias · Thresholds · Evolution
    """
    import json as _j
    out: dict = {}

    # ── Backtest-Gewichte + Patterns ─────────────────────────────────
    bw_file = BASE / "backtest_weights.json"
    if bw_file.exists():
        try:
            bw = _j.loads(bw_file.read_text(encoding="utf-8"))
            out["live_weight"]      = bw.get("live_weight",      0.7)
            out["backtest_weight"]  = bw.get("backtest_weight",  0.3)
            out["live_samples"]     = bw.get("live_samples",     0)
            out["total_samples"]    = bw.get("total_samples",    0)
            out["erstellt_am"]      = bw.get("erstellt_am",      "")
            pats = sorted(bw.get("patterns", {}).values(),
                          key=lambda x: x["score"], reverse=True)
            out["top_patterns"]         = pats[:12]
            out["hourly_performance"]   = bw.get("hourly_performance",   {})
            out["setup_performance_bt"] = bw.get("setup_performance",    {})
            out["timeframe_performance"]= bw.get("timeframe_performance",{})
        except Exception:
            pass

    # ── Signal-Gewichte aus state.json ───────────────────────────────
    state_file = BASE / "state.json"
    if state_file.exists():
        try:
            s = _j.loads(state_file.read_text(encoding="utf-8"))
            out["signal_weights"] = s.get("signal_weights", {})
        except Exception:
            pass

    # ── Strategie-Parameter ──────────────────────────────────────────
    sp_file = BASE / "strategy_params.json"
    if sp_file.exists():
        try:
            out["strategy_params"] = _j.loads(sp_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # ── Performance-Report ───────────────────────────────────────────
    pr_file = BASE / "performance_report.json"
    if pr_file.exists():
        try:
            pr = _j.loads(pr_file.read_text(encoding="utf-8"))
            out["gesamt"]                  = pr.get("gesamt",         {})
            out["setup_performance_live"]  = pr.get("nach_setup_typ", {})
            out["bias_performance"]        = pr.get("nach_bias",      {})
            out["timeframe_perf_live"]     = pr.get("nach_timeframe", {})
            out["volumen_filter"]          = pr.get("volumen_filter",  {})
            out["api_kosten"]              = pr.get("api_kosten_monat_usd", {})
            out["report_date"]             = pr.get("erstellt_am",    "")
        except Exception:
            pass

    # ── Strategie-Evolution ──────────────────────────────────────────
    evo_file = BASE / "strategy_evolution.json"
    if evo_file.exists():
        try:
            evo = _j.loads(evo_file.read_text(encoding="utf-8"))
            if evo:
                out["evolution_latest"] = evo[-1]
                out["model_accuracy"]   = evo[-1].get("model_accuracy")
                out["model_active"]     = out["model_accuracy"] is not None
                out["wr_history"] = [
                    {
                        "ts":    e.get("ts", ""),
                        "wr":    (e.get("metrics_after") or {}).get("win_rate"),
                        "delta": e.get("win_rate_delta", 0),
                        "n":     (e.get("metrics_after") or {}).get("total", 0),
                    }
                    for e in evo[-20:]
                ]
        except Exception:
            pass

    # ── Threshold-Änderungs-Log ──────────────────────────────────────
    log_file = BASE / "threshold_changes.log"
    if log_file.exists():
        try:
            lines = log_file.read_text(encoding="utf-8").strip().splitlines()
            out["threshold_changes"] = lines[-30:]
        except Exception:
            pass

    return out


def api_daily_perf() -> dict:
    """Tägliche Performance-Zusammenfassung aus daily_performance.json."""
    try:
        f = BASE / "daily_performance.json"
        if not f.exists():
            return {"history": []}
        import json as _j
        return {"history": _j.loads(f.read_text(encoding="utf-8"))}
    except Exception as e:
        return {"error": str(e)}


def api_strategy_rules() -> dict:
    """Gibt die zuletzt synthetisierten Strategie-Regeln zurück."""
    import json as _j
    f = BASE / "strategy_rules.json"
    if not f.exists():
        return {"total_rules": 0, "rules": [], "generation": 0, "summary": {}}
    try:
        return _j.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": str(e)}


def api_strategy_activate(body: bytes) -> dict:
    """Aktiviert ein Strategie-Profil. Body: {"profile_id": "..."}"""
    import json as _j
    try:
        data = _j.loads(body)
        pid  = str(data.get("profile_id", "balanced"))
        # Validieren: Profil muss in strategy_rules.json bekannt sein
        rf = BASE / "strategy_rules.json"
        if rf.exists():
            rd = _j.loads(rf.read_text(encoding="utf-8"))
            valid = set(rd.get("profiles", {}).keys())
            if valid and pid not in valid:
                return {"error": f"Unbekanntes Profil: {pid}"}
        record = {
            "profile_id":   pid,
            "activated_at": datetime.now(timezone.utc).isoformat(),
        }
        (BASE / "active_strategy.json").write_text(
            _j.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return {"ok": True, "profile_id": pid}
    except Exception as e:
        return {"error": str(e)}


def api_strategy(n: int = 10) -> dict:
    """Strategie-Entwicklungshistorie für das Dashboard."""
    try:
        import strategy_evolver
        return {
            "history": strategy_evolver.get_history(n),
            "latest":  strategy_evolver.get_latest(),
        }
    except Exception as e:
        return {"error": str(e)}


PARAMS_FILE = BASE / "strategy_params.json"


def api_get_strategy_params() -> dict:
    """Gibt die zuletzt gespeicherten Genetic-Optimizer-Parameter zurück."""
    try:
        if PARAMS_FILE.exists():
            with open(PARAMS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def api_save_strategy_params(body: dict) -> dict:
    """Speichert Genetic-Optimizer-Parameter (aus Browser) persistent."""
    try:
        with open(PARAMS_FILE, "w", encoding="utf-8") as f:
            json.dump(body, f, indent=2, ensure_ascii=False)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_research() -> dict:
    """Gibt gecachte Research-Daten zurück (kein Netzwerkabruf)."""
    try:
        import web_researcher
        return web_researcher.get_cached()
    except Exception as e:
        return {"error": str(e), "analysis": {}, "sources": {}}


def api_run_research() -> dict:
    """Löst sofortigen Research-Abruf aus und gibt Ergebnis zurück."""
    try:
        import web_researcher
        result = web_researcher.run()
        _sse_broadcast("research_update", result.get("analysis", {}))
        return result
    except Exception as e:
        return {"error": str(e)}


def start_run() -> dict:
    with _run_lock:
        if _run_state["running"]:
            return {"status": "already_running"}
        _run_state.update(running=True, log=[], finished_at=None, error=None)
    threading.Thread(target=_run_bot, daemon=True).start()
    return {"status": "started"}


def run_status() -> dict:
    with _run_lock:
        return dict(_run_state)


# ── Auto-Scheduler ────────────────────────────────────────────────────────────
def _auto_scheduler() -> None:
    """Startet den Bot automatisch alle AUTO_RUN_HOURS Stunden."""
    if AUTO_RUN_HOURS <= 0:
        return
    # Erster Lauf nach 10 Sekunden (Server muss hochgefahren sein)
    time.sleep(10)
    while True:
        with _run_lock:
            circuit_open = _run_state.get("circuit_open", False)
        if circuit_open:
            print("[Auto] ⛔ Circuit-Breaker aktiv — Auto-Run pausiert. Bitte Fehler prüfen.")
            time.sleep(3600)   # stündlich erneut prüfen ob manuell zurückgesetzt
            continue
        print(f"[Auto] Starte geplanten Bot-Lauf ({AUTO_RUN_HOURS}h-Intervall)...")
        start_run()
        # Warten bis der Lauf fertig ist
        while True:
            time.sleep(5)
            with _run_lock:
                if not _run_state["running"]:
                    break
        print(f"[Auto] Lauf abgeschlossen. Nächster in {AUTO_RUN_HOURS}h.")
        time.sleep(AUTO_RUN_HOURS * 3600)


# ── HTTP-Handler ──────────────────────────────────────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE), **kwargs)

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or "") and "/api/events" not in (self.path or ""):
            super().log_message(fmt, *args)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self):
        """Server-Sent Events: hält die Verbindung offen und schickt live Events."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # Initiale Verbindungsbestätigung
        try:
            self.wfile.write(b"event: connected\ndata: {}\n\n")
            self.wfile.flush()
        except Exception:
            return
        q: queue.Queue = queue.Queue(maxsize=20)
        with _sse_lock:
            _sse_clients.append(q)
        try:
            while True:
                try:
                    msg = q.get(timeout=25)   # alle 25s ein Keep-alive falls nichts kommt
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # Keep-alive: verhindert Browser-Timeout
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_clients.remove(q)
                except ValueError:
                    pass

    def _route(self) -> bool:
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path == "/api/events":
            self._send_sse()
        elif path == "/api/stats":
            self._send_json(api_stats())
        elif path == "/api/signals":
            limit  = int(qs.get("limit",  ["50"])[0])
            source = qs.get("source", [None])[0]
            self._send_json(api_signals(limit, source))
        elif path == "/api/signals/submit" and self.command == "POST":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) if length else b"{}")
            self._send_json(api_submit_signal(body))
        elif path == "/api/charts":
            self._send_json(api_charts())
        elif path == "/api/run" and self.command == "POST":
            self._send_json(start_run())
        elif path == "/api/run/status":
            self._send_json(run_status())
        elif path == "/api/schedule":
            self._send_json(api_schedule_status())
        elif path == "/api/strategy":
            n = int(qs.get("n", ["10"])[0])
            self._send_json(api_strategy(n))
        elif path == "/api/paper":
            self._send_json(api_paper())
        elif path == "/api/paper/toggle" and self.command == "POST":
            self._send_json(api_paper_toggle())
        elif path == "/api/strategy-params" and self.command == "POST":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) if length else b"{}")
            self._send_json(api_save_strategy_params(body))
        elif path == "/api/strategy-params":
            self._send_json(api_get_strategy_params())
        elif path == "/api/config":
            import config as _cfg
            self._send_json(_cfg.load())
        elif path == "/api/circuit-reset" and self.command == "POST":
            with _run_lock:
                _run_state["circuit_open"]        = False
                _run_state["consecutive_errors"]  = 0
            self._send_json({"ok": True, "msg": "Circuit-Breaker zurückgesetzt"})
        elif path == "/api/research/run" and self.command == "POST":
            self._send_json(api_run_research())
        elif path == "/api/research":
            self._send_json(api_research())
        elif path == "/api/daily-perf":
            self._send_json(api_daily_perf())
        elif path == "/api/learning":
            self._send_json(api_learning())
        elif path == "/api/strategy-rules":
            self._send_json(api_strategy_rules())
        elif path == "/api/strategy-activate" and self.command == "POST":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length) if length else b"{}"
            self._send_json(api_strategy_activate(body))
        else:
            return False
        return True

    def do_GET(self):
        if self.path.startswith("/api/"):
            if not self._route():
                self._send_json({"error": "unknown endpoint"}, 404)
            return
        super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            if not self._route():
                self._send_json({"error": "unknown endpoint"}, 404)
            return
        self.send_response(405)
        self.end_headers()


def main():
    os.environ.pop("ANTHROPIC_API_KEY", None)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    lan = _lan_ip()
    url = f"http://localhost:{PORT}/index.html"

    # Hintergrund-Threads
    threading.Thread(target=_watch_signals, daemon=True, name="signal-watcher").start()
    threading.Thread(target=_watch_paper,   daemon=True, name="paper-watcher").start()
    if AUTO_RUN_HOURS > 0:
        threading.Thread(target=_auto_scheduler, daemon=True, name="auto-scheduler").start()
    # Initiales Research 5s nach Start (nicht-blockierend)
    def _initial_research():
        time.sleep(5)
        try:
            import web_researcher
            r = web_researcher.get_cached()   # nutzt Cache falls frisch
            _sse_broadcast("research_update", r.get("analysis", {}))
        except Exception:
            pass
    threading.Thread(target=_initial_research, daemon=True, name="research-init").start()

    # Paper Trader automatisch starten (läuft 24/7 im Hintergrund)
    paper_enabled = os.getenv("PAPER_TRADER", "1") != "0"
    if paper_enabled:
        _launch_paper_thread()

    print("=" * 60)
    print("  SOL Analyzer — Server läuft")
    print(f"  Dieser PC:         {url}")
    if HOST == "0.0.0.0":
        print(f"  Andere Geräte:     http://{lan}:{PORT}/index.html")
    if AUTO_RUN_HOURS > 0:
        print(f"  Auto-Analyse:      alle {AUTO_RUN_HOURS}h (erster Lauf in 10s)")
    print(f"  Paper Trader:      {'aktiv (24/7)' if paper_enabled else 'deaktiviert ($env:PAPER_TRADER=0)'}")
    print("  Beenden mit:        Strg + C")
    print("=" * 60)

    if os.getenv("OPEN_BROWSER", "0") == "1":
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer beendet.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
