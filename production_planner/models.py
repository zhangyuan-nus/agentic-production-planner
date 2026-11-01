"""Pydantic models shared by the order agent, the optimiser and the CLI."""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ProductionGoal(BaseModel):
    target_part: str
    quantity: int = Field(gt=0)
    deadline: datetime | None = None
    use_existing_stock: bool = False

    @field_validator("target_part")
    @classmethod
    def _normalise_part(cls, v: str) -> str:
        return v.strip().lower().replace(" ", "")


class StageAnalysis(BaseModel):
    part: str
    machine_number: int
    factory_id: str
    capacity_per_second: float
    active_machines: list[str]
    idle_machines: list[str]
    existing_stock: int = 0
    required_units: int = 0
    estimated_stage_seconds: float = 0.0
    is_bottleneck: bool = False


class MachineRecommendation(BaseModel):
    machine_id: str
    factory_id: str
    part: str
    action: str
    added_capacity: float


class OptimizationPlan(BaseModel):
    goal: ProductionGoal
    feasible: bool
    estimated_seconds: float | None = None
    deadline_met: bool | None = None
    bottleneck_part: str | None = None
    max_throughput_per_second: float | None = None
    stages: list[StageAnalysis]
    recommendations: list[MachineRecommendation]
    warnings: list[str]
    stocks_used: dict[str, int] = {}
