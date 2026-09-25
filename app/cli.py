"""CLI: `python -m app.cli refactor --input apis/spec.yaml --output output/ --rules rules/`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.config import load_config
from app.errors import EmptySpecError, PipelineError
from app.logging_setup import configure_logging, get_logger

# 3 = errore di input/configurazione (PipelineError); 4 = successo con modifiche breaking da comunicare ai client
EXIT_CODES = {"SUCCESS": 0, "NEEDS_REVIEW": 1, "FAILED": 2, "SUCCESS_WITH_BREAKING_CHANGES": 4}
log = get_logger("cli")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="api-refactor", description="AI OpenAPI Refactoring & Validation Engine")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="path di config.yaml (default: ./config.yaml)")
        p.add_argument("--rules", help="cartella delle regole (override di rulesDir)")
        p.add_argument("--verbose", action="store_true", help="log DEBUG: frammenti inviati all'LLM e risposte")
        p.add_argument("--log-level", choices=["debug", "info", "warning", "error"], help="override di logLevel")
        p.add_argument("--refactor-model", help="override di refactorModel")
        p.add_argument("--critic-model", help="override di criticModel")
        p.add_argument("--ollama-host", help="override di ollamaHost")

    ref = sub.add_parser("refactor", help="rifattorizza una specifica OpenAPI/Swagger")
    ref.add_argument("--input", required=True, help="file di specifica (YAML o JSON, Swagger 2.0 / OpenAPI 3.x)")
    ref.add_argument("--output", help="cartella di output (override di outputDir)")
    ref.add_argument("--max-iterations", type=int, help="override di maxIterations")
    ref.add_argument("--target-version", choices=["3.0", "3.1"], help="override di targetOpenApiVersion")
    ref.add_argument("--api-lifecycle", choices=["draft", "published"], help="override di apiLifecycle")
    ref.add_argument("--run-timeout", type=float, help="override di runTimeoutSeconds")
    ref.add_argument("--resume", action="store_true",
                     help="riprende una run interrotta dall'ultimo checkpoint (output/<api>/.checkpoint)")
    common(ref)

    pre = sub.add_parser("preflight", help="verifica Ollama, modelli e tool esterni senza eseguire la pipeline")
    common(pre)

    comp = sub.add_parser("compile-rules", help="compila (o legge dalla cache) le regole in linguaggio naturale")
    common(comp)
    return parser


def _config(args: argparse.Namespace):
    config = load_config(args.config)
    level = "DEBUG" if args.verbose else (args.log_level.upper() if args.log_level else None)
    return config.with_overrides(
        log_level=level,
        rules_dir=args.rules,
        refactor_model=args.refactor_model,
        critic_model=args.critic_model,
        ollama_host=args.ollama_host,
        output_dir=getattr(args, "output", None),
        max_iterations=getattr(args, "max_iterations", None),
        target_openapi_version=getattr(args, "target_version", None),
        api_lifecycle=getattr(args, "api_lifecycle", None),
        run_timeout_seconds=getattr(args, "run_timeout", None),
    )


async def _preflight(config) -> int:
    from app.llm.ollama_provider import OllamaProvider
    from app.pipeline import RefactorPipeline
    from app.rules.loader import spectral_ruleset_files

    pipeline = RefactorPipeline(config, OllamaProvider(config))
    await pipeline.preflight(needs_spectral=bool(spectral_ruleset_files(Path(config.rules_dir))))
    from app.external import find_tool
    s2o = find_tool(config.swagger2openapi_command)
    log.info("[PREFLIGHT] OK — Ollama %s, modelli %s / %s; swagger2openapi: %s", config.ollama_host,
             config.refactor_model, config.critic_model, s2o or "NON TROVATO (serve solo per input Swagger 2.0)")
    return 0


async def _compile_rules(config) -> int:
    from app.llm.ollama_provider import OllamaProvider
    from app.llm.structured import StructuredLlm
    from app.rules.interpreter import RuleInterpreter
    from app.rules.registry import build_registry

    provider = OllamaProvider(config)
    await provider.preflight()
    llm = StructuredLlm(provider, config.llm_call_timeout_seconds, config.llm_technical_retries)
    registry, _ = await build_registry(Path(config.rules_dir), RuleInterpreter(llm, Path(config.compiled_rules_cache_dir)))
    for rule in registry.compiled:
        kinds = ", ".join(r.kind for r in rule.requirements)
        log.info("  %s [%s] %s -> %s", rule.id, rule.severity.value, rule.scope.value, kinds)
    return 0


async def _refactor(config, args) -> int:
    from app.service import refactor_file

    outcome, result = await refactor_file(config, args.input, config.output_dir, config.rules_dir,
                                          resume=args.resume)
    print()
    print(f"Stato finale: {outcome.status}  (iterazioni: {outcome.iterations}, chiamate LLM: {result.llm_calls}, "
          f"{result.elapsed_seconds:.1f}s)")
    if result.resumed:
        print(f"  Ripresa da checkpoint: {result.replayed_llm_calls} risposte LLM riutilizzate")
    if result.breaking_changes:
        if result.api_lifecycle == "draft":
            print(f"  {len(result.breaking_changes)} modifiche breaking, ammesse (apiLifecycle: draft):")
        else:
            print(f"  ATTENZIONE: {len(result.breaking_changes)} modifiche BREAKING nell'output "
                  "(i client esistenti vanno aggiornati):")
        for b in result.breaking_changes:
            print(f"    * {b['type']} @ {b['location']} (ruleId {b['ruleId']})")
    for reason in outcome.reasons:
        print(f"  - {reason}")
    print(f"Output: {outcome.output_dir}")
    return EXIT_CODES[outcome.status]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging("INFO")
    try:
        config = _config(args)
        configure_logging(config.log_level)
        if args.command == "preflight":
            return asyncio.run(_preflight(config))
        if args.command == "compile-rules":
            return asyncio.run(_compile_rules(config))
        return asyncio.run(_refactor(config, args))
    except EmptySpecError as exc:
        log.info("%s", exc)
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return exc.exit_code
    except KeyboardInterrupt:
        log.error("Interrotto dall'utente: riprendi con lo stesso comando e --resume")
        return 130


if __name__ == "__main__":
    sys.exit(main())
