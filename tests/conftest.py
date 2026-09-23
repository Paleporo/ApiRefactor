import shutil
from pathlib import Path

import pytest

from app.config import AppConfig
from app.external import find_tool
from app.logging_setup import configure_logging
from tests.fake_agents import FakeAgents

ROOT = Path(__file__).resolve().parent.parent
APIS = ROOT / "apis"

configure_logging("INFO")


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
