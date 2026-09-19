import json
from dataclasses import asdict
from pathlib import Path

from mlops_cp.models import ModelVersion


class JsonStateStore:
    """Simple append-only event log for demos and local development."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, model: ModelVersion) -> None:
        record = {
            "event": event,
            "model": asdict(model),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
