import sys
from pathlib import Path

# Make `incident-reporter/` the importable root so tests can do:
#   from models import EnrichedAnomalyEvent
#   from enrichment.enricher import EnrichmentService
sys.path.insert(0, str(Path(__file__).parent))
