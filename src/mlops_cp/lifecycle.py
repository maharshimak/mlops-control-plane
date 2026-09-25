from __future__ import annotations

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "registered": frozenset({"candidate", "archived"}),
    "candidate": frozenset({"production", "archived"}),
    "production": frozenset({"archived"}),
    "archived": frozenset(),
}


class InvalidLifecycleTransition(ValueError):
    pass


def validate_transition(current: str, target: str) -> None:
    if current not in ALLOWED_TRANSITIONS:
        raise InvalidLifecycleTransition(f"Unknown lifecycle stage: {current}")
    if target not in ALLOWED_TRANSITIONS:
        raise InvalidLifecycleTransition(f"Unknown lifecycle stage: {target}")
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidLifecycleTransition(
            f"Invalid lifecycle transition: {current} -> {target}"
        )
