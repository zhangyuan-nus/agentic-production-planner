from pydantic import BaseModel
from typing import Dict, List, Optional
from datetime import datetime


class ProductionGoal(BaseModel):
    target_part: str
    quantity: int
    deadline: Optional[datetime] = None
    use_existing_stock: bool = False


class StageAnalysis(BaseModel):
    part: str
    machine_number: int
    factory_id: str
    capacity_per_second: float
    active_machines: List[str]
    idle_machines: List[str]
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
    estimated_seconds: Optional[float] = None
    deadline_met: Optional[bool] = None
    bottleneck_part: Optional[str] = None
    max_throughput_per_second: Optional[float] = None
    stages: List[StageAnalysis]
    recommendations: List[MachineRecommendation]
    warnings: List[str]
    stocks_used: Dict[str, int] = {}
