from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from urllib import parse, request


@dataclass(frozen=True, slots=True)
class DeploymentCommand:
    model_key: str
    artifact_uri: str
    action: str
    traffic_percent: int
    reason: str

    def __post_init__(self) -> None:
        if not self.model_key.strip() or not self.artifact_uri.strip():
            raise ValueError("model_key and artifact_uri are required.")
        if self.action not in {"deploy", "set_traffic", "rollback"}:
            raise ValueError("unsupported deployment action.")
        if isinstance(self.traffic_percent, bool) or not isinstance(self.traffic_percent, int):
            raise TypeError("traffic_percent must be an integer.")
        if not 0 <= self.traffic_percent <= 100:
            raise ValueError("traffic_percent must be between 0 and 100.")


@dataclass(frozen=True, slots=True)
class DeploymentReceipt:
    accepted: bool
    deployment_id: str | None
    detail: str


class DeploymentTarget(Protocol):
    def execute(self, command: DeploymentCommand) -> DeploymentReceipt: ...


@dataclass(slots=True)
class HTTPDeploymentTarget:
    """Explicit outbound deployment boundary for an infrastructure controller.

    HTTPS is required except for localhost development. The target receives a
    small typed command rather than arbitrary code or shell instructions.
    """

    endpoint: str
    bearer_token: str = ""
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = parse.urlparse(self.endpoint)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
            raise ValueError("deployment endpoint must use HTTPS except on localhost.")
        if not parsed.hostname:
            raise ValueError("deployment endpoint requires a hostname.")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    def execute(self, command: DeploymentCommand) -> DeploymentReceipt:
        payload = json.dumps(
            {
                "model_key": command.model_key,
                "artifact_uri": command.artifact_uri,
                "action": command.action,
                "traffic_percent": command.traffic_percent,
                "reason": command.reason,
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        req = request.Request(self.endpoint, data=payload, headers=headers, method="POST")
        with request.urlopen(req, timeout=self.timeout_seconds) as response:
            body = json.loads(response.read().decode() or "{}")
        return DeploymentReceipt(
            accepted=bool(body.get("accepted", False)),
            deployment_id=(
                str(body["deployment_id"]) if body.get("deployment_id") is not None else None
            ),
            detail=str(body.get("detail", "")),
        )
