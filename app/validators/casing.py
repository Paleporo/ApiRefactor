"""Verifica e conversione deterministica delle convenzioni di naming.

Scomposizione in parole (poi ricomposte nel casing richiesto):
- separatori misti `_`, `-`, spazi, `.`: `payment-order_id` -> payment, order, id
- confine minuscola/cifra -> maiuscola: `payerIban` -> payer, Iban; `sha256Hash` -> sha256, Hash
- acronimi seguiti da una parola: `IBANCode` -> IBAN, Code; `HTTPStatus` -> HTTP, Status
- le cifre restano attaccate alla parola che le precede: `line_2` -> line, 2 -> camel `line2`, kebab `line-2`
Esempi: `payer_IBAN` -> camel `payerIban`; `Payment_Instructions` -> kebab `payment-instructions`;
`x_request_id` -> Train `X-Request-Id`; `userID` -> camel `userId`; `payment_order` -> Pascal `PaymentOrder`.
`convert_checked` rifiuta i risultati che non rispettano comunque il casing (es. `2fa_code` in camel -> `2faCode`).
"""

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


def convert_checked(name: str, casing: Casing) -> str | None:
    """Nome convertito se diverso dall'originale e conforme al casing richiesto; altrimenti None."""
    converted = convert(name, casing)
    return converted if converted != name and matches(converted, casing) else None
