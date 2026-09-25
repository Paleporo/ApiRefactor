import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.errors import LlmCallError, PreflightError
from app.llm.base import AgentRole, LlmProvider, LlmRequest
from app.llm.fake import FakeLlmProvider
from app.llm.ollama_provider import OllamaProvider
from app.llm.structured import StructuredLlm
from app.pipeline import RefactorPipeline, RunStatus
from app.refactor.operations import OperationsProposal
from tests.conftest import APIS

REQ = LlmRequest(role=AgentRole.REFACTOR, task="t", system="s", user="u")


async def test_technical_retry_recovers_from_invalid_output():
    answers = iter(["not json at all", '<think>ragiono...</think>```json\n{"operations": [], "rationale": "ok"}\n```'])
    provider = FakeLlmProvider(lambda r: next(answers))
    result = await StructuredLlm(provider, call_timeout=5, technical_retries=1).generate(REQ, OperationsProposal)
    assert result.rationale == "ok"
    assert "non rispettava lo schema" in provider.requests[1].user  # l'errore torna come feedback


async def test_technical_retries_exhausted_raise():
    provider = FakeLlmProvider(lambda r: '{"operations": [{"type": "NOPE"}]}')
    with pytest.raises(LlmCallError, match="dopo 3 tentativi"):
        await StructuredLlm(provider, call_timeout=5, technical_retries=2).generate(REQ, OperationsProposal)


class SlowProvider(LlmProvider):
    def model_for(self, role):
        return "slow"

    async def complete(self, request, json_schema, timeout):
        await asyncio.sleep(10)
        return "{}"

    async def preflight(self):
        return None


async def test_per_call_timeout():
    with pytest.raises(LlmCallError, match="timeout"):
        await StructuredLlm(SlowProvider(), call_timeout=0.2, technical_retries=1).generate(REQ, OperationsProposal)


async def test_preflight_fails_fast_when_ollama_unreachable():
    provider = OllamaProvider(AppConfig().with_overrides(ollama_host="http://127.0.0.1:9", llm_call_timeout_seconds=2))
    with pytest.raises(PreflightError, match="non raggiungibile"):
        await provider.preflight()


@pytest.mark.needs_node
async def test_failed_fragments_are_reported_and_prevent_success(config, agents, rules_dir):
    agents.refactor = lambda r: LlmCallError("modello caduto")
    # caso 001: SEC-001 resta all'LLM (le operation hanno già un requisito apiKey), che qui fallisce
    result = await RefactorPipeline(config, agents.provider(), rules_dir).run(APIS / "case-001-swagger2-legacy.yaml")
    assert result.plans[0].failed_fragments
    assert result.status == RunStatus.NEEDS_REVIEW
    assert any("chiamate LLM fallite" in r for r in result.reasons)


@pytest.mark.needs_node
async def test_run_timeout_exits_cleanly_with_needs_review(config, agents, rules_dir):
    import time

    def slow_critic(request):
        time.sleep(1.2)
        return {"accepted": True, "issues": []}

    agents.critic = slow_critic
    cfg = config.with_overrides(run_timeout_seconds=1.0, llm_technical_retries=0)
    result = await RefactorPipeline(cfg, agents.provider(), rules_dir).run(APIS / "case-003-no-problem-details.yaml")
    assert result.status == RunStatus.NEEDS_REVIEW
    assert any("Budget complessivo" in r for r in result.reasons)
    assert result.final_governance is not None  # la validazione finale deterministica gira comunque


@pytest.mark.needs_node
async def test_pipeline_without_rules_runs_basic_validation_with_warning(config, agents, tmp_path):
    empty = tmp_path / "norules"
    empty.mkdir()
    result = await RefactorPipeline(config, agents.provider(), empty).run(APIS / "case-003-no-problem-details.yaml")
    assert result.status == RunStatus.SUCCESS
    assert any("NESSUNA REGOLA" in w for w in result.registry.info.warnings)
    assert result.final_governance == []


def test_fastapi_app_exposes_health():
    from app.api import app
    assert TestClient(app).get("/health").json() == {"status": "ok"}


async def test_ollama_provider_request_shape_and_preflight_with_mock_transport():
    import json

    import httpx
    import ollama

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            name = json.loads(request.content)["model"]
            caps = ["completion", "thinking"] if name.startswith("deepseek") else ["completion"]
            return httpx.Response(200, json={"modelfile": "", "parameters": "", "template": "", "details": {},
                                             "model_info": {}, "capabilities": caps})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"model": "qwen3-coder:30b", "name": "qwen3-coder:30b"},
                                                        {"model": "deepseek-r1:14b", "name": "deepseek-r1:14b"}]})
        seen.setdefault("bodies", []).append(json.loads(request.content))
        seen["body"] = seen["bodies"][-1]
        return httpx.Response(200, json={"model": "qwen3-coder:30b", "created_at": "2026-01-01T00:00:00Z",
                                         "done": True, "message": {"role": "assistant",
                                                                   "content": '{"operations": [], "rationale": "x"}'}})

    config = AppConfig()
    provider = OllamaProvider(config)
    provider.client = ollama.AsyncClient(host=config.ollama_host, transport=httpx.MockTransport(handler))
    await provider.preflight()
    out = await StructuredLlm(provider, 5, 0).generate(REQ, OperationsProposal)
    assert out.rationale == "x"
    body = seen["body"]
    assert body["model"] == "qwen3-coder:30b" and body["format"]["title"] == "OperationsProposal"
    assert body["options"]["num_ctx"] == config.llm_context_token_budget + 4096
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert provider.model_for(AgentRole.CRITIC) == "deepseek-r1:14b"
    assert "think" not in body  # qwen3-coder non ha la capability "thinking": il parametro non viene passato

    # Critic su un modello "thinking": `think` passato esplicitamente, false di default (criticThink)
    critic_req = REQ.model_copy(update={"role": AgentRole.CRITIC})
    await StructuredLlm(provider, 5, 0).generate(critic_req, OperationsProposal)
    assert seen["body"]["model"] == "deepseek-r1:14b" and seen["body"]["think"] is False

    provider.config = config.with_overrides(critic_think=True)
    await StructuredLlm(provider, 5, 0).generate(critic_req, OperationsProposal)
    assert seen["body"]["think"] is True
