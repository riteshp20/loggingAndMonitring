"""
Tier 2 — Isolation Forest per-service anomaly scoring.

Design
──────
• One IsolationForest model per service, serialized with joblib and stored
  as a binary blob in Redis: key  if_model:{service}:blob
• Training samples (feature vectors) are stored in a 7-day rolling ZSET:
  key  if_model:{service}:training  (score = Unix timestamp)
• The model is retrained when it is absent OR more than RETRAIN_INTERVAL_SECONDS old.
• Until MIN_TRAINING_SAMPLES are available the model cannot be trained, so
  score() returns (fired=False, score=0.0, model_available=False).

Feature vector order is fixed (FEATURE_KEYS) and must remain stable across
retrains — changing it invalidates stored models.

Anomaly threshold: score_samples() < TIER2_THRESHOLD (-0.15).
sklearn's score_samples() returns the anomaly score; lower = more anomalous.
"""

import io
import json
import time
from typing import Optional

import joblib
import redis
from sklearn.ensemble import IsolationForest

TIER2_THRESHOLD: float = -0.15
RETRAIN_INTERVAL_SECONDS: int = 7 * 86_400   # 1 week
WINDOW_SECONDS: int = 7 * 86_400

# Fixed, ordered feature list used to build the sklearn input array.
# Must stay consistent across all model versions stored in Redis.
FEATURE_KEYS: list[str] = [
    "error_rate",
    "p99_latency_ms",
    "avg_latency_ms",
    "request_volume",
    "warn_rate",
]

_DEFAULT_MIN_SAMPLES = 100


class IsolationForestDetector:
    def __init__(
        self,
        redis_client: redis.Redis,
        min_training_samples: int = _DEFAULT_MIN_SAMPLES,
        retrain_interval_seconds: int = RETRAIN_INTERVAL_SECONDS,
    ) -> None:
        self._r = redis_client
        self.min_training_samples = min_training_samples
        self.retrain_interval = retrain_interval_seconds

    # ── key helpers ───────────────────────────────────────────────────────────

    def _model_key(self, service: str) -> str:
        return f"if_model:{service}:blob"

    def _training_key(self, service: str) -> str:
        return f"if_model:{service}:training"

    def _last_trained_key(self, service: str) -> str:
        return f"if_model:{service}:last_trained"

    # ── model persistence ─────────────────────────────────────────────────────

    def _load_model(self, service: str) -> Optional[IsolationForest]:
        blob = self._r.get(self._model_key(service))
        if blob is None:
            return None
        return joblib.load(io.BytesIO(blob))

    def _save_model(self, service: str, model: IsolationForest) -> None:
        buf = io.BytesIO()
        joblib.dump(model, buf)
        self._r.set(self._model_key(service), buf.getvalue())
        self._r.set(self._last_trained_key(service), time.time())

    # ── training data ─────────────────────────────────────────────────────────

    def store_training_sample(self, service: str, features: dict) -> None:
        """
        Append the current feature vector to the rolling training store.
        Entries older than 7 days are pruned on every write.
        """
        now = time.time()
        vec = [float(features.get(k, 0.0)) for k in FEATURE_KEYS]
        key = self._training_key(service)
        cutoff = now - WINDOW_SECONDS

        # Use the JSON-serialised vector as the ZSET member (with a uid suffix to
        # avoid evicting duplicate vectors that arrive at the same millisecond).
        member = f"{json.dumps(vec)}:{now:.6f}"

        pipe = self._r.pipeline(transaction=False)
        pipe.zadd(key, {member: now})
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.expire(key, WINDOW_SECONDS + 3_600)
        pipe.execute()

    def _get_training_vectors(self, service: str) -> list[list[float]]:
        raw = self._r.zrange(self._training_key(service), 0, -1)
        vectors = []
        for m in raw:
            # Member format: "{json_vec}:{timestamp}"
            json_part = m.decode().rsplit(":", 1)[0]
            vectors.append(json.loads(json_part))
        return vectors

    # ── retraining ────────────────────────────────────────────────────────────

    def maybe_retrain(self, service: str) -> bool:
        """
        Fit a new IsolationForest if the current model is absent or stale.
        Returns True if a new model was trained and saved, False otherwise.
        """
        raw_ts = self._r.get(self._last_trained_key(service))
        if raw_ts and (time.time() - float(raw_ts)) < self.retrain_interval:
            return False  # Model is still fresh

        vectors = self._get_training_vectors(service)
        if len(vectors) < self.min_training_samples:
            return False  # Not enough history yet

        model = IsolationForest(
            n_estimators=100,
            contamination=0.05,
            random_state=42,
            max_samples="auto",
        )
        model.fit(vectors)
        self._save_model(service, model)
        return True

    # ── scoring ───────────────────────────────────────────────────────────────

    def score(self, service: str, features: dict) -> tuple[bool, float, bool]:
        """
        Score the feature vector against the service's Isolation Forest.

        Returns
        ───────
        (fired, anomaly_score, model_available)
          fired          – True when anomaly_score < TIER2_THRESHOLD
          anomaly_score  – sklearn decision_function() value; positive=inlier, negative=outlier
          model_available – False until the first successful training run
        """
        # Lazy retraining: runs fast (one Redis GET) if model is fresh.
        self.maybe_retrain(service)

        model = self._load_model(service)
        if model is None:
            return False, 0.0, False

        vec = [[float(features.get(k, 0.0)) for k in FEATURE_KEYS]]
        # decision_function = score_samples - offset_ (trained threshold).
        # Positive → inlier, negative → outlier.  -0.15 means "clearly below the
        # contamination boundary", giving a buffer against marginal false positives.
        raw_score = float(model.decision_function(vec)[0])
        fired = raw_score < TIER2_THRESHOLD
        return fired, raw_score, True
