import os

OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://opensearch:9200")
OPENSEARCH_USER = os.getenv("OPENSEARCH_USERNAME", "")
OPENSEARCH_PASS = os.getenv("OPENSEARCH_PASSWORD", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
SERVICE_REGISTRY_PATH = os.getenv(
    "SERVICE_REGISTRY_PATH",
    "/app/incident-reporter/service_registry.json",
)

# JWT — set a strong secret in production; algorithm is always HS256
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production")
JWT_ALGORITHM = "HS256"

# Kafka
KAFKA_BROKERS = os.getenv("KAFKA_BROKERS", "kafka-1:9092,kafka-2:9092,kafka-3:9092")
KAFKA_ANOMALY_TOPIC = os.getenv("KAFKA_ANOMALY_TOPIC", "anomaly-events")
KAFKA_CONSUMER_GROUP_SSE = os.getenv("KAFKA_CONSUMER_GROUP_SSE", "api-sse")

# Rate limiting
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "100"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
