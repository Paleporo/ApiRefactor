"""Checkpoint su disco per riprendere una run interrotta (`--resume`).

La pipeline è deterministica a parità di risposte dell'LLM: il checkpoint è quindi un giornale delle risposte
LLM riuscite, scritto (con fsync) subito dopo ogni frammento elaborato — pianificazione, Critic, correzione,
compilazione delle regole. Alla ripresa la run viene rieseguita: le chiamate già fatte sono servite dal
giornale senza interrogare il modello, e la run prosegue dal punto di interruzione. Le fasi deterministiche
(validazione, Spectral) vengono rieseguite: costano secondi, non minuti.

Il giornale si usa solo se input, regole e configurazione rilevante sono identici a quelli salvati.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from app.config import AppConfig
from app.errors import PipelineError
from app.logging_setup import get_logger
from app.rules.models import DSL_VERSION

log = get_logger("checkpoint")

CHECKPOINT_VERSION = "1"
RUN_FILE = "run.json"
JOURNAL_FILE = "llm-journal.jsonl"
# parametri che non cambiano il risultato (solo tempi, log, destinazione): esclusi dall'impronta
_CONFIG_EXCLUDED = {"log_level", "output_dir", "run_timeout_seconds", "llm_call_timeout_seconds",
                    "llm_technical_retries", "external_tool_timeout_seconds", "compiled_rules_cache_dir"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(input_path: Path, rules_dir: Path, config: AppConfig) -> dict[str, str]:
    rules = hashlib.sha256()
    if rules_dir.is_dir():
        for f in sorted(p for p in rules_dir.rglob("*") if p.is_file()):
            rules.update(str(f.relative_to(rules_dir)).encode())
            rules.update(f.read_bytes())
    relevant = {k: v for k, v in config.model_dump().items() if k not in _CONFIG_EXCLUDED}
    return {
        "input": _sha256(input_path.read_bytes()),
        "rules": rules.hexdigest(),
        "config": _sha256(json.dumps(relevant, sort_keys=True, default=str).encode()),
        "version": f"checkpoint-{CHECKPOINT_VERSION}/dsl-{DSL_VERSION}",
    }


_LABELS = {"input": "file di input", "rules": "regole", "config": "configurazione", "version": "versione pipeline/DSL"}


class Checkpoint:
    def __init__(self, directory: Path, fp: dict[str, str], resume: bool):
        self.directory = directory
        self.fingerprint = fp
        self.resume = resume
        self.entries: dict[str, str] = {}
        self.replayed = 0
        self.recorded = 0

    def open(self) -> "Checkpoint":
        run_file = self.directory / RUN_FILE
        if self.resume:
            if not run_file.exists():
                raise PipelineError(f"--resume: nessun checkpoint in {self.directory} (avvia la run senza --resume)")
            saved = json.loads(run_file.read_text(encoding="utf-8")).get("fingerprint", {})
            changed = [_LABELS[k] for k in self.fingerprint if saved.get(k) != self.fingerprint[k]]
            if changed:
                raise PipelineError(f"--resume impossibile: rispetto alla run interrotta sono cambiati "
                                    f"{', '.join(changed)}. Avvia una nuova run senza --resume.")
            journal = self.directory / JOURNAL_FILE
            if journal.exists():
                for line in journal.read_text(encoding="utf-8").splitlines():
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # ultima riga troncata dall'interruzione: si rifà quella chiamata
                    self.entries[entry["key"]] = entry["raw"]
            log.info("[CHECKPOINT] ripresa da %s: %d risposte LLM disponibili", self.directory, len(self.entries))
        else:
            if self.directory.exists():
                shutil.rmtree(self.directory)
            self.directory.mkdir(parents=True)
            run_file.write_text(json.dumps({"fingerprint": self.fingerprint}, indent=2), encoding="utf-8")
        return self

    @staticmethod
    def key(parts: dict[str, Any]) -> str:
        return _sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode())

    def get(self, key: str) -> str | None:
        return self.entries.get(key)

    def put(self, key: str, raw: str, meta: dict[str, Any]) -> None:
        self.entries[key] = raw
        self.recorded += 1
        with open(self.directory / JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key, **meta, "raw": raw}, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
