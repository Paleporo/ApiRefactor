import logging
import shutil
from pathlib import Path

import pytest

from app.config import AppConfig
from app.external import find_tool
from app.logging_setup import configure_logging
from tests.fake_agents import FakeAgents

ROOT = Path(__file__).resolve().parent.parent
APIS = ROOT / "apis"
CRITIC_QUALITY_RESULTS: list[dict] = []  # riempito da tests/test_critic_quality.py

configure_logging("INFO")


@pytest.fixture(autouse=True)
def _isolate_logging():
    """Chi chiama configure_logging (es. la CLI) lega il logger allo stdout catturato del test, poi chiuso."""
    logger = logging.getLogger("app")
    saved_handlers, saved_level = logger.handlers[:], logger.level
    yield
    logger.handlers[:] = saved_handlers
    logger.setLevel(saved_level)


@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    """Copia delle regole del progetto (così cache e modifiche restano isolate nel tmp del test)."""
    dest = tmp_path / "rules"
    shutil.copytree(ROOT / "rules", dest)
    return dest


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig().with_overrides(
        compiled_rules_cache_dir=str(tmp_path / "cache"),
        output_dir=str(tmp_path / "output"),
        llm_call_timeout_seconds=10,
        run_timeout_seconds=300,
    )


@pytest.fixture
def agents() -> FakeAgents:
    return FakeAgents()


def pytest_collection_modifyitems(config, items):
    """I test che usano Spectral/swagger2openapi richiedono i tool Node: skip esplicito se mancano."""
    missing = [t for t in ("spectral", "swagger2openapi") if find_tool(t) is None]
    if not missing:
        return
    marker = pytest.mark.skip(reason=f"tool Node mancanti: {missing} (esegui `npm install` nella root)")
    for item in items:
        if "needs_node" in item.keywords:
            item.add_marker(marker)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Esito e durata dei casi di qualità del Critic (solo se eseguiti): terminale + output/critic-quality.json."""
    if not CRITIC_QUALITY_RESULTS:
        return
    import json

    tr = terminalreporter
    tr.section("Qualità del Critic (Ollama reale)")
    for r in CRITIC_QUALITY_RESULTS:
        tr.write_line(f"{'OK  ' if r['ok'] else 'FAIL'} {r['seconds']:>7.1f}s  {r['case']}  "
                      f"(accepted={r['accepted']}, bloccanti={len(r['blocking'])}, pertinenti={r['pertinent']})")
        for b in r["blocking"]:
            tr.write_line(f"            - {b}")
    out = ROOT / "output" / "critic-quality.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(CRITIC_QUALITY_RESULTS, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tr.write_line(f"Report: {out}")
