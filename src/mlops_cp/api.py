import math
import os
import secrets
from dataclasses import asdict

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from mlops_cp.models import Evaluation, ModelVersion
from mlops_cp.registry import ModelRegistry

app = FastAPI(
    title="MLOps Control Plane",
    version="0.3.0",
    description=(
        "Durable model lifecycle registry with explicit promotion gates and "
        "bearer-protected remote mutation."
    ),
)
registry = ModelRegistry(os.environ.get("MLOPS_CP_DB_PATH", "./data/control-plane.db"))


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    artifact_uri: str = Field(min_length=1, max_length=2000)
    dataset_fingerprint: str = Field(min_length=1, max_length=500)


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(min_length=1, max_length=200)
    value: float = Field(allow_inf_nan=False)
    threshold: float = Field(allow_inf_nan=False)
    higher_is_better: bool = True


async def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    token = os.environ.get("MLOPS_CP_API_TOKEN")
    if token:
        scheme, _, supplied = (authorization or "").partition(" ")
        if (
            scheme.lower() != "bearer"
            or not supplied
            or not secrets.compare_digest(supplied, token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Valid bearer token required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return

    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Remote access requires MLOPS_CP_API_TOKEN.",
        )


protected = [Depends(require_auth)]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models", dependencies=protected)
def list_models() -> list[dict[str, object]]:
    return [asdict(model) for model in registry.list()]


@app.post("/v1/models", dependencies=protected)
def register_model(request: RegisterRequest) -> dict[str, object]:
    try:
        model = registry.register(
            ModelVersion(
                name=request.name,
                version=request.version,
                artifact_uri=request.artifact_uri,
                dataset_fingerprint=request.dataset_fingerprint,
            )
        )
        return asdict(model)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/v1/models/{name}/{version}/history", dependencies=protected)
def model_history(name: str, version: str) -> list[dict[str, object]]:
    try:
        registry.get(name, version)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="model not found") from error
    return [asdict(event) for event in registry.history(name, version)]


@app.post("/v1/models/{name}/{version}/evaluations", dependencies=protected)
def add_evaluation(
    name: str,
    version: str,
    request: EvaluationRequest,
) -> dict[str, object]:
    if not math.isfinite(request.value) or not math.isfinite(request.threshold):
        raise HTTPException(status_code=400, detail="evaluation values must be finite")
    try:
        model = registry.add_evaluation(
            name,
            version,
            Evaluation(
                metric=request.metric,
                value=request.value,
                threshold=request.threshold,
                higher_is_better=request.higher_is_better,
            ),
        )
        return asdict(model)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="model not found") from error


@app.post("/v1/models/{name}/{version}/candidate", dependencies=protected)
def promote_candidate(name: str, version: str) -> dict[str, object]:
    try:
        decision = registry.promote_candidate(name, version)
        return asdict(decision)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="model not found") from error


@app.post("/v1/models/{name}/{version}/production", dependencies=protected)
def promote_production(name: str, version: str) -> dict[str, str]:
    try:
        registry.promote_production(name, version)
        return {"stage": "production"}
    except KeyError as error:
        raise HTTPException(status_code=404, detail="model not found") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
