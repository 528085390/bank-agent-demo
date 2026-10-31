from dataclasses import dataclass
from enum import StrEnum

DAILY_RED_THRESHOLD_MINOR: int = 100000  # 1000 元，单位分


class RiskLevel(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    level: RiskLevel
    confirm_required: bool
    mfa_required: bool
    reason: str
