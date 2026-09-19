from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from mlops_cp.models import Evaluation, ModelVersion
from mlops_cp.registry import ModelRegistry

app = FastAPI(title="MLOps Control Plane", version="0.1.0")
registry = ModelRegistry()


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    artifact_uri: str = Field(min_length=1)
    dataset_fingerprint: str = Field(min_length=1)


class EvaluationRequest(BaseModel):
    metric: str
    value: float
    threshold: float
    higher_is_better: bool = True


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
def list_models() -> list[dict[str, object]]:
    return [asdict(model) for model in registry.list()]


@app.post("/v1/models")
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


@app.post("/v1/models/{name}/{version}/evaluations")
def add_evaluation(
    name: str,
    version: str,
    request: EvaluationRequest,
) -> dict[str, object]:
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


@app.post("/v1/models/{name}/{version}/candidate")
def promote_candidate(name: str, version: str) -> dict[str, object]:
    try:
        decision = registry.promote_candidate(name, version)
        return asdict(decision)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="model not found") from error


@app.post("/v1/models/{name}/{version}/production")
def promote_production(name: str, version: str) -> dict[str, str]:
    try:
        registry.promote_production(name, version)
        return {"stage": "production"}
    except KeyError as error:
        raise HTTPException(status_code=404, detail="model not found") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
