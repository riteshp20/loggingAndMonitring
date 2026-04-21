from dataclasses import dataclass


@dataclass
class BaselineData:
    """Stored state for one (service, metric) pair."""
    mean: float        # Simple running arithmetic mean
    std_dev: float     # EWMA-based standard deviation
    ewma: float        # Exponentially weighted moving average (primary baseline)
    sample_count: int  # Total samples ingested (all time)
    last_updated: float  # Unix timestamp of last update
