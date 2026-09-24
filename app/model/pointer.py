"""JSON Pointer (RFC 6901): unico modo in cui la pipeline indirizza i nodi dell'Object Model."""

from __future__ import annotations

from typing import Any, Iterable

_MISSING = object()


def escape(token: str) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def split(pointer: str) -> list[str]:
    if pointer in ("", "/"):
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"JSON pointer non valido (deve iniziare con '/'): {pointer!r}")
    return [unescape(t) for t in pointer[1:].split("/")]


def join(tokens: Iterable[Any]) -> str:
    tokens = list(tokens)
    return "" if not tokens else "/" + "/".join(escape(str(t)) for t in tokens)


def child(pointer: str, *tokens: Any) -> str:
    return join([*split(pointer), *tokens])


def parent(pointer: str) -> tuple[str, str]:
    tokens = split(pointer)
    if not tokens:
        raise ValueError("La radice non ha un parent")
    return join(tokens[:-1]), tokens[-1]


def is_prefix(prefix: str, pointer: str) -> bool:
    """True se `pointer` coincide con `prefix` o è un suo discendente."""
    p, q = split(prefix), split(pointer)
    return len(p) <= len(q) and q[: len(p)] == p


def _step(node: Any, token: str) -> Any:
    if isinstance(node, dict):
        return node.get(token, _MISSING)
    if isinstance(node, list):
        try:
            idx = int(token)
        except ValueError:
            return _MISSING
        return node[idx] if 0 <= idx < len(node) else _MISSING
    return _MISSING


def resolve(doc: Any, pointer: str, default: Any = _MISSING) -> Any:
    node = doc
    for token in split(pointer):
        node = _step(node, token)
        if node is _MISSING:
            if default is _MISSING:
                raise KeyError(pointer)
            return default
    return node


_ABSENT = object()


def exists(doc: Any, pointer: str) -> bool:
    return resolve(doc, pointer, default=_ABSENT) is not _ABSENT


def _loose_matches(node: Any, raw: list[str], limit: int = 2) -> list[list[str]]:
    """Percorsi esistenti che corrispondono a `raw` interpretando i `/` non escapati come parte dei nomi."""
    found: list[list[str]] = []

    def walk(current: Any, i: int, acc: list[str]) -> None:
        if len(found) >= limit:
            return
        if i == len(raw):
            found.append(acc)
            return
        if isinstance(current, dict):
            for j in range(i + 1, len(raw) + 1):
                key = "/".join(unescape(t) for t in raw[i:j])
                if key in current:
                    walk(current[key], j, acc + [key])
        elif isinstance(current, list):
            try:
                idx = int(raw[i])
            except ValueError:
                return
            if 0 <= idx < len(current):
                walk(current[idx], i + 1, acc + [raw[i]])

    walk(node, 0, [])
    return found


def normalize(doc: Any, pointer: str, allow_new_leaf: bool = False) -> str | None:
    """Pointer esistente equivalente a `pointer`, correggendo l'escape mancante di `/` nei nomi (es. media type
    `application/json` scritto senza `~1`). Ritorna il pointer corretto solo se è univoco, altrimenti None.

    `allow_new_leaf`: il nodo finale può non esistere (operazioni che lo creano, es. SET_FIELD): si normalizza
    il parent e l'ultimo nome può contenere `/`.
    """
    if exists(doc, pointer):
        return pointer
    if not pointer.startswith("/"):
        return None
    raw = pointer[1:].split("/")
    candidates: set[str] = {join(m) for m in _loose_matches(doc, raw)}
    if allow_new_leaf and not candidates:
        # il parent esistente più profondo; il resto dei token è il nome (con `/`) del nodo da creare
        for k in range(1, len(raw)):
            parents = [p for p in _loose_matches(doc, raw[:-k]) if isinstance(resolve(doc, join(p), None), dict)]
            if parents:
                if len(parents) == 1:
                    candidates.add(join([*parents[0], "/".join(unescape(t) for t in raw[-k:])]))
                break
    return candidates.pop() if len(candidates) == 1 else None


def set_value(doc: Any, pointer: str, value: Any, create_parents: bool = False) -> None:
    tokens = split(pointer)
    if not tokens:
        raise ValueError("Non si può sostituire la radice del documento")
    node = doc
    for token in tokens[:-1]:
        nxt = _step(node, token)
        if nxt is _MISSING:
            if not create_parents or not isinstance(node, dict):
                raise KeyError(pointer)
            nxt = node[token] = {}
        node = nxt
    last = tokens[-1]
    if isinstance(node, dict):
        node[last] = value
    elif isinstance(node, list):
        if last == "-":
            node.append(value)
        else:
            node[int(last)] = value
    else:
        raise KeyError(pointer)


def remove(doc: Any, pointer: str) -> Any:
    parent_ptr, last = parent(pointer)
    node = resolve(doc, parent_ptr)
    if isinstance(node, dict):
        if last not in node:
            raise KeyError(pointer)
        return node.pop(last)
    if isinstance(node, list):
        return node.pop(int(last))
    raise KeyError(pointer)


def from_spectral_path(path: list[Any]) -> str:
    return join(path)
