"""Logging operativo solo su console (stdout), nessun file persistente."""

import logging
import sys

PHASE_LOGGER = "app"


def configure_logging(level: str = "INFO") -> None:
    # console Windows (cp1252): un carattere non rappresentabile (es. nelle risposte LLM in DEBUG)
    # viene sostituito invece di far fallire il logging
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass  # stream già sostituito (es. cattura dei test) o non riconfigurabile
    root = logging.getLogger(PHASE_LOGGER)
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False
    # le librerie HTTP sono rumorose in DEBUG: il DEBUG utile qui è il nostro (frammenti LLM)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{PHASE_LOGGER}.{name}")
