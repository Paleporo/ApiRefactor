"""Indice dei $ref: mappa nome->definizione costruita una volta, con rilevamento esplicito dei cicli.

Serve al context slicing (passare all'LLM solo le definizioni puntuali referenziate), ai controlli
globali deterministici ($ref rotti) e al semantic diff (contesto request/response degli schema).
"""

from __future__ import annotations

from typing import Any, Iterator

from pydantic import BaseModel

from app.model import pointer as jp


class RefUse(BaseModel):
    at: str  # JSON pointer del nodo che contiene il $ref
    ref: str  # valore del $ref, es. "#/components/schemas/User"


class RefIndex:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.uses: list[RefUse] = list(self._walk(data, ""))
        self.definitions: dict[str, Any] = {}
        for use in self.uses:
            target = self.target_pointer(use.ref)
            if target is not None and target not in self.definitions:
                node = jp.resolve(data, target, default=None)
                if node is not None:
                    self.definitions[target] = node
        self.cycles: list[list[str]] = self._find_cycles()

    # ── costruzione ────────────────────────────────────────────────────
    @staticmethod
    def _walk(node: Any, pointer: str) -> Iterator[RefUse]:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                yield RefUse(at=pointer, ref=ref)
            for key, value in node.items():
                if key != "$ref":
                    yield from RefIndex._walk(value, jp.child(pointer, key))
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                yield from RefIndex._walk(value, jp.child(pointer, idx))

    @staticmethod
    def target_pointer(ref: str) -> str | None:
        """'#/components/schemas/X' -> '/components/schemas/X'; i ref esterni non sono indicizzati."""
        if not ref.startswith("#"):
            return None
        return ref[1:] or ""

    def direct_refs(self, pointer: str) -> list[str]:
        """Target (pointer) dei $ref contenuti nel sottoalbero `pointer`, senza ripetizioni."""
        seen: list[str] = []
        for use in self.uses:
            if jp.is_prefix(pointer, use.at):
                target = self.target_pointer(use.ref)
                if target is not None and target not in seen:
                    seen.append(target)
        return seen

    def _find_cycles(self) -> list[list[str]]:
        graph = {t: [r for r in self.direct_refs(t) if r in self.definitions] for t in self.definitions}
        cycles: list[list[str]] = []
        state: dict[str, int] = {}  # 1 = in stack, 2 = done
        stack: list[str] = []

        def visit(node: str) -> None:
            state[node] = 1
            stack.append(node)
            for nxt in graph.get(node, []):
                if state.get(nxt) == 1:
                    cycle = stack[stack.index(nxt):] + [nxt]
                    if not any(set(cycle) == set(c) for c in cycles):
                        cycles.append(cycle)
                elif nxt not in state:
                    visit(nxt)
            stack.pop()
            state[node] = 2

        for node in graph:
            if node not in state:
                visit(node)
        return cycles

    # ── query ──────────────────────────────────────────────────────────
    def broken_refs(self) -> list[RefUse]:
        broken = []
        for use in self.uses:
            target = self.target_pointer(use.ref)
            if target is None:
                continue  # esterni: gestiti dal bundling in fase di load
            if not jp.exists(self.data, target):
                broken.append(use)
        return broken

    def closure(self, pointer: str, max_depth: int = 1) -> tuple[dict[str, Any], list[str]]:
        """Definizioni referenziate da `pointer` fino a `max_depth` livelli, interrompendo i cicli.

        Ritorna (definizioni, ref_troncati): i ref non espansi (per profondità o ciclo) sono elencati
        per nome, così il modello sa che esistono senza ricevere l'intero grafo.
        """
        collected: dict[str, Any] = {}
        truncated: list[str] = []
        frontier = [(t, 1) for t in self.direct_refs(pointer)]
        visited = {pointer}
        while frontier:
            target, depth = frontier.pop(0)
            if target in visited:
                continue  # ciclo o già incluso: stop
            visited.add(target)
            if target not in self.definitions:
                continue
            if depth > max_depth:
                truncated.append(target)
                continue
            collected[target] = self.definitions[target]
            frontier.extend((t, depth + 1) for t in self.direct_refs(target))
        return collected, truncated

    def resolve_ref(self, node: Any, max_hops: int = 20) -> Any:
        """Segue una catena di $ref fino a un nodo concreto (con guardia contro i cicli)."""
        seen: set[str] = set()
        while isinstance(node, dict) and isinstance(node.get("$ref"), str) and max_hops > 0:
            target = self.target_pointer(node["$ref"])
            if target is None or target in seen:
                return node
            seen.add(target)
            node = jp.resolve(self.data, target, default=None)
            max_hops -= 1
        return node

    def usage_contexts(self) -> dict[str, set[str]]:
        """Per ogni componente, dove viene usato: 'request' e/o 'response' (propagato attraverso i ref, ciclo-safe)."""
        contexts: dict[str, set[str]] = {}
        for use in self.uses:
            target = self.target_pointer(use.ref)
            if target is None or not use.at.startswith("/paths/"):
                continue
            tokens = jp.split(use.at)
            ctx = "response" if "responses" in tokens else "request"
            contexts.setdefault(target, set()).add(ctx)
        changed = True
        while changed:  # propagazione: se A (request) referenzia B, anche B è request
            changed = False
            for source, ctxs in list(contexts.items()):
                for target in self.direct_refs(source):
                    before = contexts.setdefault(target, set())
                    if not ctxs <= before:
                        before |= ctxs
                        changed = True
        return contexts
