from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from threading import RLock

from mlops_cp.lifecycle import validate_transition
from mlops_cp.models import Evaluation, LifecycleEvent, ModelVersion
from mlops_cp.policy import PromotionDecision, evaluate_promotion
from mlops_cp.storage import connect, transaction

ALLOWED_STAGES = {"registered", "candidate", "production", "archived"}


class ModelRegistry:
    def __init__(self, database_path: str | None = None) -> None:
        self.database_path = (
            str(Path(database_path).expanduser().resolve())
            if database_path not in (None, ":memory:") else None
        )
        self._models: dict[str, ModelVersion] = {}
        self._events: list[LifecycleEvent] = []
        self._lock = RLock()
        self._governed = False
        if self.database_path is not None:
            self._initialize_storage()

    def _initialize_storage(self) -> None:
        assert self.database_path is not None
        with transaction(self.database_path) as connection:
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_key TEXT NOT NULL,
                    from_stage TEXT,
                    to_stage TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_model "
                "ON lifecycle_events(model_key, id)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_production_per_model "
                "ON model_versions(name) WHERE stage = 'production'"
            )

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection | None]:
        if self.database_path is None:
            with self._lock:
                yield None
        else:
            with transaction(self.database_path) as connection:
                yield connection

    def _read(self):
        assert self.database_path is not None
        return connect(self.database_path)

    def _select(self, *, model_key: str | None = None) -> list[ModelVersion]:
        if self.database_path is None:
            with self._lock:
                if model_key is None:
                    return sorted(self._models.values(), key=lambda item: (item.name, item.version))
                return [self._models[model_key]] if model_key in self._models else []
        query = (
            "SELECT name, version, artifact_uri, dataset_fingerprint, stage, "
            "evaluations_json, created_at FROM model_versions"
        )
        with self._read() as connection:
            if model_key is None:
                rows = connection.execute(query + " ORDER BY name, version").fetchall()
            else:
                rows = connection.execute(query + " WHERE model_key = ?", (model_key,)).fetchall()
        models = []
        for row in rows:
            fields = dict(row)
            evaluations = [Evaluation(**item) for item in json.loads(fields.pop("evaluations_json"))]
            models.append(ModelVersion(**fields, evaluations=evaluations))
        return models

    @staticmethod
    def _evaluations_json(model: ModelVersion) -> str:
        return json.dumps([asdict(evaluation) for evaluation in model.evaluations], sort_keys=True)

    def _record_transition(
        self,
        model: ModelVersion,
        *,
        from_stage: str | None,
        to_stage: str,
        reason: str,
    ) -> None:
        event = LifecycleEvent(
            model_key=model.key, from_stage=from_stage, to_stage=to_stage, reason=reason
        )
        if self.database_path is None:
            self._events.append(event)
            return
        with self._read() as connection:
            connection.execute(
                """
                INSERT INTO lifecycle_events(model_key, from_stage, to_stage, reason, created_at)
                VALUES (:model_key, :from_stage, :to_stage, :reason, :created_at)
                """,
                asdict(event),
            )

    def history(self, name: str, version: str) -> list[LifecycleEvent]:
        model_key = f"{name}:{version}"
        if self.database_path is None:
            with self._lock:
                return [event for event in self._events if event.model_key == model_key]
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT model_key, from_stage, to_stage, reason, created_at
                FROM lifecycle_events WHERE model_key = ? ORDER BY id
                """,
                (model_key,),
            ).fetchall()
        return [LifecycleEvent(**dict(row)) for row in rows]

    def register(self, model: ModelVersion) -> ModelVersion:
        if model.stage != "registered":
            raise ValueError("New models must enter in the registered stage.")
        with self._write() as connection:
            if self._select(model_key=model.key):
                raise ValueError(f"Model version already exists: {model.key}")
            if connection is not None:
                connection.execute(
                    """
                    INSERT INTO model_versions(
                        model_key, name, version, artifact_uri, dataset_fingerprint,
                        stage, evaluations_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (model.key, model.name, model.version, model.artifact_uri,
                     model.dataset_fingerprint, model.stage, self._evaluations_json(model),
                     model.created_at),
                )
            self._record_transition(
                model, from_stage=None, to_stage="registered", reason="model registered"
            )
            if connection is None:
                self._models[model.key] = model
        return model

    def get(self, name: str, version: str) -> ModelVersion:
        key = f"{name}:{version}"
        models = self._select(model_key=key)
        if not models:
            raise KeyError(key)
        return models[0]

    def add_evaluation(self, name: str, version: str, evaluation: Evaluation) -> ModelVersion:
        with self._write() as connection:
            model = self.get(name, version)
            model = replace(model, evaluations=[*model.evaluations, evaluation])
            if connection is None:
                self._models[model.key] = model
            else:
                connection.execute(
                    "UPDATE model_versions SET evaluations_json = ? WHERE model_key = ?",
                    (self._evaluations_json(model), model.key),
                )
            return model

    def _transition(self, model: ModelVersion, stage: str, reason: str) -> None:
        validate_transition(model.stage, stage)
        self._record_transition(model, from_stage=model.stage, to_stage=stage, reason=reason)
        if self.database_path is None:
            self._models[model.key] = replace(model, stage=stage)
        else:
            with self._read() as connection:
                connection.execute(
                    "UPDATE model_versions SET stage = ? WHERE model_key = ?", (stage, model.key)
                )

    def promote_candidate(self, name: str, version: str) -> PromotionDecision:
        with self._write():
            model = self.get(name, version)
            decision = evaluate_promotion(model)
            if decision.allowed:
                self._transition(model, "candidate", "promotion policy satisfied")
            return decision

    def promote_production(self, name: str, version: str) -> None:
        """Apply the basic transition only to a standalone registry."""
        with self._write() as connection:
            governed = self._governed or (
                connection is not None
                and connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='candidate_evidence'"
                ).fetchone() is not None
            )
            if governed:
                raise ValueError("Use GovernanceService to promote in a governed registry.")
            self._promote_production(name, version)

    def _promote_production(self, name: str, version: str) -> None:
        with self._write():
            model = self.get(name, version)
            validate_transition(model.stage, "production")
            if not evaluate_promotion(model).allowed:
                raise ValueError("Current evaluations no longer satisfy promotion policy.")
            for other in self.list():
                if other.name == name and other.stage == "production":
                    self._transition(other, "archived", "replaced by newer production version")
            self._transition(model, "production", "candidate promoted to production")

    def list(self) -> list[ModelVersion]:
        return self._select()
