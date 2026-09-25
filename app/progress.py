"""Avanzamento della run: una riga di log per frammento completato + file di stato `reports/progress.json`.

La stima di fine fase usa la durata media dei frammenti già completati nella fase, escluse le risposte prese
dal checkpoint (`--resume`), che durano ~0 e renderebbero la stima troppo ottimista. Il file di stato è
riscritto in modo atomico (file temporaneo + rename) a ogni frammento, così `app.cli status` può leggerlo da
un altro terminale mentre la run è in corso.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.logging_setup import get_logger

log = get_logger("progress")

PHASE_TAGS = {"plan": "PLAN", "critic": "CRITIC", "correction": "CORRECTION"}
PHASE_NAMES = {"compile": "compilazione regole", "validation": "validazione", "plan": "pianificazione",
               "engine": "applicazione modifiche", "critic": "revisione Critic", "correction": "correzione",
               "final": "validazione finale", "done": "terminata"}


def fmt_duration(seconds: float) -> str:
    seconds = int(round(max(seconds, 0)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


class ProgressTracker:
    def __init__(self, path: Path | None, *, source: str, max_iterations: int, stale_after_seconds: float,
                 llm: Any = None, clock: Callable[[], float] = time.time):
        self.path = path
        self.source = source
        self.max_iterations = max_iterations
        self.stale_after = stale_after_seconds
        self.llm = llm  # StructuredLlm: conteggi delle chiamate
        self.clock = clock
        self.run_started = clock()
        self.status = "running"
        self.phase = "compile"
        self.iteration = 0
        self._reset(0, 0)

    def _reset(self, total: int, skipped: int) -> None:
        self.phase_started = self.clock()
        self.total, self.skipped = total, skipped
        self.done = self.from_checkpoint = self.failed = 0
        self.durations: list[float] = []

    # ── eventi ─────────────────────────────────────────────────────────
    def set_iteration(self, iteration: int) -> None:
        self.iteration = iteration
        self._write()

    def set_phase(self, phase: str, total: int = 0, skipped: int = 0, announce: bool = False) -> None:
        self.phase = phase
        self._reset(total, skipped)
        if announce:
            extra = f", {skipped} saltati perché solo deterministici" if skipped else ""
            log.info("[%s] %d frammenti da elaborare%s", PHASE_TAGS.get(phase, phase.upper()), total, extra)
        self._write()

    def fragment_done(self, label: str, seconds: float, *, replayed: bool = False, failed: bool = False) -> None:
        self.done += 1
        if replayed:
            self.from_checkpoint += 1
        else:
            self.durations.append(seconds)  # solo chiamate reali nella media
        if failed:
            self.failed += 1
        eta = self.eta_at()
        took = "da checkpoint" if replayed else f"{seconds:.1f}s" + (" (FALLITO)" if failed else "")
        log.info("[%s] %d/%d %s: %s | trascorso %s | stima fine fase %s",
                 PHASE_TAGS.get(self.phase, self.phase.upper()), self.done, self.total, label or "/", took,
                 fmt_duration(self.clock() - self.phase_started),
                 datetime.fromtimestamp(eta).strftime("%H:%M") if eta else
                 ("completata" if self.done >= self.total else "n/d"))
        self._write()

    def finish(self, status: str) -> None:
        self.status = status
        self.phase = "done"
        self._reset(0, 0)
        self._write()

    # ── stima e stato ──────────────────────────────────────────────────
    def eta_at(self) -> float | None:
        remaining = self.total - self.done
        if remaining <= 0 or not self.durations:
            return None
        return self.clock() + remaining * (sum(self.durations) / len(self.durations))

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        stats = getattr(self.llm, "stats", {}) or {}
        journal = getattr(self.llm, "journal", None)
        eta = self.eta_at()
        return {
            "source": self.source,
            "status": self.status,
            "phase": self.phase,
            "phaseName": PHASE_NAMES.get(self.phase, self.phase),
            "iteration": self.iteration,
            "maxIterations": self.max_iterations,
            "fragments": {"done": self.done, "total": self.total, "fromCheckpoint": self.from_checkpoint,
                          "failed": self.failed, "skipped": self.skipped},
            "llm": {"calls": int(sum(s.get("calls", 0) for s in stats.values())),
                    "failed": int(sum(s.get("failures", 0) for s in stats.values())),
                    "fromCheckpoint": getattr(journal, "replayed", 0) if journal else 0},
            "runElapsedSeconds": round(now - self.run_started, 1),
            "phaseElapsedSeconds": round(now - self.phase_started, 1),
            "phaseEta": datetime.fromtimestamp(eta).isoformat(timespec="seconds") if eta else None,
            "phaseEtaSeconds": round(eta - now, 1) if eta else None,
            "lastUpdate": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "lastUpdateEpoch": round(now, 3),
            "staleAfterSeconds": self.stale_after,
        }

    def _write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.snapshot(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)  # atomico: chi legge vede sempre un file completo


def render_status(progress: dict[str, Any], now: float | None = None) -> str:
    """Riepilogo leggibile di progress.json (comando `status`)."""
    now = time.time() if now is None else now
    f, llm = progress["fragments"], progress["llm"]
    running = progress["status"] == "running"
    lines = [f"Run: {progress['source']} — {'in corso' if running else 'terminata: ' + progress['status']}"]
    if running:
        lines.append(f"Iterazione: {progress['iteration']}/{progress['maxIterations']}   "
                     f"Fase: {progress['phaseName']}")
        if f["total"]:
            detail = []
            if f["fromCheckpoint"]:
                detail.append(f"{f['fromCheckpoint']} da checkpoint")
            if f["failed"]:
                detail.append(f"{f['failed']} falliti")
            if f["skipped"]:
                detail.append(f"{f['skipped']} saltati")
            eta = progress.get("phaseEta")
            lines.append(f"Frammenti: {f['done']}/{f['total']}" + (f" ({', '.join(detail)})" if detail else "")
                         + f" | trascorso fase {fmt_duration(progress['phaseElapsedSeconds'])}"
                         + f" | stima fine fase {eta[11:16] if eta else 'n/d'}")
    lines.append(f"Chiamate LLM: {llm['calls']} ({llm['failed']} fallite, {llm['fromCheckpoint']} da checkpoint)")
    lines.append(f"Tempo totale: {fmt_duration(progress['runElapsedSeconds'])} | ultimo aggiornamento "
                 f"{progress['lastUpdate'][11:19]}")
    idle = now - progress.get("lastUpdateEpoch", now)
    if running and idle > progress.get("staleAfterSeconds", float("inf")):
        lines.append(f"ATTENZIONE: nessun avanzamento da {fmt_duration(idle)} "
                     "(la run potrebbe essere bloccata o interrotta)")
    return "\n".join(lines)
