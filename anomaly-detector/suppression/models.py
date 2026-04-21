from dataclasses import dataclass, field


@dataclass
class SuppressResult:
    allowed: bool              # True → individual alert can be sent
    # "allowed" | "cooldown" | "storm_active" | "storm"
    reason: str
    is_storm: bool             # True → storm threshold crossed; send system-wide incident
    storm_service_count: int   # Distinct services in the 60-second storm window
    storm_services: list       # Service names currently in the storm window
