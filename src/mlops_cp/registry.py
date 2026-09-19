from mlops_cp.models import Evaluation, ModelVersion
from mlops_cp.policy import PromotionDecision, evaluate_promotion

ALLOWED_STAGES = {"registered", "candidate", "production", "archived"}


class ModelRegistry:
    def __init__(self) -> None:
        self._models: dict[str, ModelVersion] = {}

    def register(self, model: ModelVersion) -> ModelVersion:
        if model.key in self._models:
            raise ValueError(f"Model version already exists: {model.key}")
        if model.stage != "registered":
            raise ValueError("New models must enter in the registered stage.")
        self._models[model.key] = model
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
        return decision

    def promote_production(self, name: str, version: str) -> None:
        model = self.get(name, version)
        if model.stage != "candidate":
            raise ValueError("Only candidate models may be promoted to production.")
        if not evaluate_promotion(model).allowed:
            raise ValueError("Current evaluations no longer satisfy promotion policy.")

        for other in self._models.values():
            if other.name == name and other.stage == "production":
                other.stage = "archived"

        model.stage = "production"

    def list(self) -> list[ModelVersion]:
        return sorted(
            self._models.values(),
            key=lambda item: (item.name, item.version),
        )
