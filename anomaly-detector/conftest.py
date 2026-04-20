import sys
import os

# Ensure anomaly-detector/ is on sys.path so `baseline` is importable from tests/
sys.path.insert(0, os.path.dirname(__file__))
