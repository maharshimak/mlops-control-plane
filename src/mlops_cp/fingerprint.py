import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def dataset_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    canonical = json.dumps(
        [dict(sorted(row.items())) for row in rows],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
