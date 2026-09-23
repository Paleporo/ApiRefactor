"""Esecuzione dei tool Node esterni (Spectral, swagger2openapi) come processi separati."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.errors import ExternalToolError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def find_tool(command: str) -> str | None:
    """Cerca il comando nel PATH, poi in ./node_modules/.bin del progetto (installazione locale via npm)."""
    if Path(command).is_file():
        return str(Path(command).resolve())
    found = shutil.which(command)
    if found:
        return found
    for candidate in (Path.cwd() / "node_modules" / ".bin" / command, PROJECT_ROOT / "node_modules" / ".bin" / command):
        for variant in (candidate, candidate.with_suffix(".cmd")):  # .cmd su Windows
            if variant.is_file():
                return str(variant)
    return None


def require_tool(command: str, install_hint: str) -> str:
    path = find_tool(command)
    if not path:
        raise ExternalToolError(f"Comando '{command}' non trovato (né nel PATH né in node_modules/.bin). {install_hint}")
    return path


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def run_process(args: list[str], timeout: float) -> ProcessResult:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    except subprocess.TimeoutExpired:
        raise ExternalToolError(f"Timeout ({timeout:.0f}s) eseguendo: {' '.join(args[:3])} ...") from None
    except OSError as exc:
        raise ExternalToolError(f"Impossibile eseguire {args[0]}: {exc}") from None
    return ProcessResult(proc.returncode, proc.stdout, proc.stderr)
