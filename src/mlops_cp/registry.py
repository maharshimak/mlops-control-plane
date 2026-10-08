from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from mlops_cp.lifecycle import validate_transition
from mlops_cp.models import Evaluation, LifecycleEvent, ModelVersion
from mlops_cp.policy import PromotionDecision, evaluate_promotion

ALLOWED_STAGES = {"registered", "candidate", "production", "archived"}


class ModelRegistry:
    def __init__(self, database_path: str | None = None) -> None:
        self.database_path = database_path
        self._models: dict[str, ModelVersion] = {}
        self._events: list[LifecycleEvent] = []
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

    def _refresh(self) -> None:
        if self.database_path is not None:
            self._load()

    def _persist(self, model: ModelVersion) -> None:
        if self.database_path is None:
            return
        payload = json.dumps(
            [asdict(evaluation) for evaluation in model.evaluations],
            sort_keys=True,
            separators=(",", ":"),
        )
        with closing(self._connect()) as connection, connection:
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

    def _record_transition(
        self,
        model: ModelVersion,
        *,
        from_stage: str | None,
        to_stage: str,
        reason: str,
    ) -> None:
        event = LifecycleEvent(
            model_key=model.key,
            from_stage=from_stage,
            to_stage=to_stage,
            reason=reason,
        )
        self._events.append(event)
        if self.database_path is None:
            return
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO lifecycle_events(
                    model_key, from_stage, to_stage, reason, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.model_key,
                    event.from_stage,
                    event.to_stage,
                    event.reason,
                    event.created_at,
                ),
            )

    def history(self, name: str, version: str) -> list[LifecycleEvent]:
        model_key = f"{name}:{version}"
        if self.database_path is None:
            return [event for event in self._events if event.model_key == model_key]
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT model_key, from_stage, to_stage, reason, created_at
                FROM lifecycle_events
                WHERE model_key = ?
                ORDER BY id ASC
                """,
                (model_key,),
            ).fetchall()
        return [LifecycleEvent(**dict(row)) for row in rows]

    def register(self, model: ModelVersion) -> ModelVersion:
        self._refresh()
        if model.key in self._models:
            raise ValueError(f"Model version already exists: {model.key}")
        if model.stage != "registered":
            raise ValueError("New models must enter in the registered stage.")
        # Never advertise an in-memory registration until persistence succeeds.
        self._persist(model)
        self._models[model.key] = model
        self._record_transition(
            model,
            from_stage=None,
            to_stage="registered",
            reason="model registered",
        )
        return model

    def get(self, name: str, version: str) -> ModelVersion:
        self._refresh()
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
            previous = model.stage
            validate_transition(previous, "candidate")
            model.stage = "candidate"
            self._persist(model)
            if previous != "candidate":
                self._record_transition(
                    model,
                    from_stage=previous,
                    to_stage="candidate",
                    reason="promotion policy satisfied",
                )
        return decision

    def promote_production(self, name: str, version: str) -> None:
        model = self.get(name, version)
        validate_transition(model.stage, "production")
        if not evaluate_promotion(model).allowed:
            raise ValueError("Current evaluations no longer satisfy promotion policy.")

        changes: list[tuple[ModelVersion, str, str, str]] = []
        for other in self._models.values():
            if other.name == name and other.stage == "production":
                validate_transition(other.stage, "archived")
                changes.append(
                    (
                        other,
                        other.stage,
                        "archived",
                        "replaced by newer production version",
                    )
                )

        changes.append(
            (
                model,
                model.stage,
                "production",
                "candidate promoted to production",
            )
        )
        events = [
            LifecycleEvent(
                model_key=item.key,
                from_stage=from_stage,
                to_stage=to_stage,
                reason=reason,
            )
            for item, from_stage, to_stage, reason in changes
        ]

        if self.database_path is not None:
            # Promotion, archival and audit events must commit as one unit. This
            # prevents crashes/concurrent writers from leaving no production
            # model or two production rows.
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                for item, from_stage, to_stage, _ in changes:
                    cursor = connection.execute(
                        """
                        UPDATE model_versions
                        SET stage = ?
                        WHERE model_key = ? AND stage = ?
                        """,
                        (to_stage, item.key, from_stage),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            f"Concurrent lifecycle update detected for {item.key}."
                        )
                for event in events:
                    connection.execute(
                        """
                        INSERT INTO lifecycle_events(
                            model_key, from_stage, to_stage, reason, created_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            event.model_key,
                            event.from_stage,
                            event.to_stage,
                            event.reason,
                            event.created_at,
                        ),
                    )
                connection.commit()

        for item, _, to_stage, _ in changes:
            item.stage = to_stage
        self._events.extend(events)

    def list(self) -> list[ModelVersion]:
        self._refresh()
        return sorted(
            self._models.values(),
            key=lambda item: (item.name, item.version),
        )
