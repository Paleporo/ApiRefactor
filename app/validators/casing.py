"""Verifica e conversione deterministica delle convenzioni di naming."""

from __future__ import annotations

import re

from app.rules.models import Casing

PATTERNS = {
    Casing.PASCAL: re.compile(r"^[A-Z][a-zA-Z0-9]*$"),
    Casing.CAMEL: re.compile(r"^[a-z][a-zA-Z0-9]*$"),
    Casing.KEBAB: re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"),
    Casing.SNAKE: re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$"),
    Casing.MACRO: re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$"),
    Casing.TRAIN: re.compile(r"^[A-Z][a-zA-Z0-9]*(-[A-Z][a-zA-Z0-9]*)*$"),
}


def matches(name: str, casing: Casing) -> bool:
    return bool(PATTERNS[casing].match(name))


def words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def convert(name: str, casing: Casing) -> str:
    parts = [w.lower() for w in words(name)]
    if not parts:
        return name
    if casing == Casing.PASCAL:
        return "".join(p.capitalize() for p in parts)
    if casing == Casing.CAMEL:
        return parts[0] + "".join(p.capitalize() for p in parts[1:])
    if casing == Casing.KEBAB:
        return "-".join(parts)
    if casing == Casing.SNAKE:
        return "_".join(parts)
    if casing == Casing.MACRO:
        return "_".join(p.upper() for p in parts)
    return "-".join(p.capitalize() for p in parts)
