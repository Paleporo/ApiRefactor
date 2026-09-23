"""System prompt per ruolo. Contengono solo istruzioni di ruolo/formato: nessuna regola di business."""

from importlib import resources


def load_prompt(name: str) -> str:
    return resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
