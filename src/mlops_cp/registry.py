from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from mlops_cp.models import Evaluation, ModelVersion
from mlops_cp.policy import PromotionDecision, evaluate_promotion

ALLOWED_STAGES = {"registered", "candidate", "production", "archived"}


class ModelRegistry:
    def __init__(self, database_path: str | None = None) -> None:
        self.database_path = database_path
        self._models: dict[str, ModelVersion] = {}
        if database_path is not None:
            self._initialize_storage()
            self._load()

    def _connect(self) -> sqlite3.Connection:
        if self.database_path is None:
            raise RuntimeError("Registry persistence is not configured.")
        if self.database_path != ":memory:":
            Path(self.database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_storage(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS model_versions (
                    model_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    artifact_uri TEXT NOT NULL,
                    dataset_fingerprint TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    evaluations_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _load(self) -> None:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT name, version, artifact_uri, dataset_fingerprint, stage,
                       evaluations_json, created_at
                FROM model_versions
                ORDER BY name, version
                """
            ).fetchall()
        self._models = {}
        for row in rows:
            evaluations = [
                Evaluation(**item) for item in json.loads(row["evaluations_json"])
            ]
            model = ModelVersion(
                name=row["name"],
                version=row["version"],
                artifact_uri=row["artifact_uri"],
                dataset_fingerprint=row["dataset_fingerprint"],
                stage=row["stage"],
                evaluations=evaluations,
                created_at=row["created_at"],
            )
            self._models[model.key] = model

    def _persist(self, model: ModelVersion) -> None:
        if self.database_path is None:
            return
        payload = json.dumps(
            [asdict(evaluation) for evaluation in model.evaluations],
            sort_keys=True,
            separators=(",", ":"),
        )
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO model_versions(
                        model_key, name, version, artifact_uri, dataset_fingerprint,
                        stage, evaluations_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(model_key) DO UPDATE SET
                        artifact_uri = excluded.artifact_uri,
                        dataset_fingerprint = excluded.dataset_fingerprint,
                        stage = excluded.stage,
                        evaluations_json = excluded.evaluations_json,
                        created_at = excluded.created_at
                    """,
                    (
                        model.key,
                        model.name,
                        model.version,
                        model.artifact_uri,
                        model.dataset_fingerprint,
                        model.stage,
                        payload,
                        model.created_at,
                    ),
                )

    def register(self, model: ModelVersion) -> ModelVersion:
        if model.key in self._models:
            raise ValueError(f"Model version already exists: {model.key}")
        if model.stage != "registered":
            raise ValueError("New models must enter in the registered stage.")
        self._models[model.key] = model
        self._persist(model)
        return model

    def get(self, name: str, version: str) -> ModelVersion:
        return self._models[f"{name}:{version}"]

    def add_evaluation(
        self,
        name: str,
        version: str,
        evaluation: Evaluation,
    ) -> ModelVersion:
        model = self.get(name, version)
        model.evaluations.append(evaluation)
        self._persist(model)
        return model

    def promote_candidate(
        self,
        name: str,
        version: str,
    ) -> PromotionDecision:
        model = self.get(name, version)
        decision = evaluate_promotion(model)
        if decision.allowed:
            model.stage = "candidate"
            self._persist(model)
        return decision

    def promote_production(self, name: str, version: str) -> None:
        model = self.get(name, version)
        if model.stage != "candidate":
            raise ValueError("Only candidate models may be promoted to production.")
        if not evaluate_promotion(model).allowed:
            raise ValueError("Current evaluations no longer satisfy promotion policy.")

        changed: list[ModelVersion] = []
        for other in self._models.values():
            if other.name == name and other.stage == "production":
                other.stage = "archived"
                changed.append(other)

        model.stage = "production"
        changed.append(model)
        for item in changed:
            self._persist(item)

    def list(self) -> list[ModelVersion]:
        return sorted(
            self._models.values(),
            key=lambda item: (item.name, item.version),
        )
