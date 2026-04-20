#!/usr/bin/env python3
"""
Load-test log generator for the ingestion pipeline.

Produces two concurrent streams at a configurable combined rate:
  • File stream  — writes JSON logs to /var/log/app/json/<service>.log
                   and plaintext logs to /var/log/app/plain/<service>-legacy.log
  • TCP stream   — sends JSON records via newline-delimited TCP to Fluent Bit

Both streams exercise the full parser + normalisation path so that downstream
Kafka consumers always see a realistic mix of structured and unstructured data.

Configuration (environment variables):
  FLUENT_BIT_TCP_HOST   default: localhost
  FLUENT_BIT_TCP_PORT   default: 5170
  LOG_RATE              total events/sec (split 50/50 between file and TCP)
  SERVICE_NAME          embedded in every record
  LOG_DIR               base directory for file output
"""

import json
import os
import random
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# ── Configuration ──────────────────────────────────────────────────────────────
TCP_HOST    = os.environ.get("FLUENT_BIT_TCP_HOST", "localhost")
TCP_PORT    = int(os.environ.get("FLUENT_BIT_TCP_PORT", "5170"))
LOG_RATE    = int(os.environ.get("LOG_RATE", "100"))
SERVICE     = os.environ.get("SERVICE_NAME", "test-service")
LOG_DIR     = Path(os.environ.get("LOG_DIR", "/var/log/app"))

# ── Sample data pools ─────────────────────────────────────────────────────────
_ENDPOINTS = [
    "/api/v1/users", "/api/v1/orders", "/api/v1/products",
    "/api/v1/payments", "/api/v1/sessions", "/health", "/metrics",
]
_HTTP_METHODS   = ["GET", "GET", "GET", "POST", "PUT", "DELETE", "PATCH"]
_STATUS_CODES   = [200, 200, 200, 200, 201, 204, 400, 401, 403, 404, 500, 503]
_LOGGERS        = ["http.handler", "db.pool", "cache.client", "worker.queue", "auth.middleware"]
_USER_IDS       = [f"usr_{i:04d}" for i in range(1, 201)]

_LEVEL_CHOICES  = ["DEBUG", "INFO", "INFO", "INFO", "INFO", "WARNING", "ERROR", "CRITICAL"]
_LEVEL_WEIGHTS  = [0.03,   0.55,  0.00,  0.00,  0.00,  0.20,      0.18,    0.04   ]

_INFO_MSGS = [
    "Request processed successfully",
    "Cache hit for key: product_catalog_v2",
    "User session refreshed",
    "Background job completed in {ms}ms",
    "Batch processed: {n} records in {ms}ms",
    "Webhook delivered to endpoint",
    "Config reloaded from remote store",
    "Health check passed all {n} dependencies",
]
_ERROR_MSGS = [
    "Connection refused to database replica",
    "Upstream service timeout after {ms}ms",
    "Failed to decode request body: unexpected EOF",
    "Rate limit exceeded for user {uid}",
    "Circuit breaker OPEN for service payment-svc",
    "Retry attempt {n}/3 for external API call",
    "Slow query detected: {ms}ms — SELECT * FROM orders WHERE …",
    "Memory pressure: heap at {pct}% of limit",
]


def _fill(template: str) -> str:
    return (template
            .replace("{ms}",  str(random.randint(1, 3500)))
            .replace("{n}",   str(random.randint(100, 10000)))
            .replace("{pct}", str(random.randint(70, 99)))
            .replace("{uid}", random.choice(_USER_IDS)))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_json_record() -> Dict[str, Any]:
    level   = random.choices(_LEVEL_CHOICES, weights=_LEVEL_WEIGHTS, k=1)[0]
    pool    = _ERROR_MSGS if level in ("ERROR", "CRITICAL", "WARNING") else _INFO_MSGS
    status  = random.choice(_STATUS_CODES)

    record: Dict[str, Any] = {
        "timestamp":    _now_iso(),
        "level":        level,
        "logger":       random.choice(_LOGGERS),
        "service_name": SERVICE,
        "message":      _fill(random.choice(pool)),
        "trace_id":     uuid.uuid4().hex,
        "span_id":      uuid.uuid4().hex[:16],
        "request_id":   uuid.uuid4().hex,
        "user_id":      random.choice(_USER_IDS),
        "http": {
            "method":      random.choice(_HTTP_METHODS),
            "endpoint":    random.choice(_ENDPOINTS),
            "status_code": status,
            "duration_ms": random.randint(1, 3000),
        },
    }

    if level in ("ERROR", "CRITICAL"):
        record["error"] = {
            "type":        "ServiceException",
            "message":     _fill(random.choice(_ERROR_MSGS)),
            "stack_trace": (
                "Traceback (most recent call last):\n"
                "  File \"handler.py\", line 42, in process_request\n"
                "    result = upstream.call(payload)\n"
                "  File \"upstream.py\", line 108, in call\n"
                "    raise ServiceException(msg)\n"
                f"ServiceException: {_fill(random.choice(_ERROR_MSGS))}"
            ),
        }

    return record


def _make_plaintext_line() -> str:
    level   = random.choices(["DEBUG", "INFO", "WARNING", "ERROR"], weights=[0.05, 0.70, 0.15, 0.10], k=1)[0]
    pool    = _ERROR_MSGS if level in ("ERROR", "WARNING") else _INFO_MSGS
    ts      = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    logger  = f"{SERVICE}.legacy"
    msg     = _fill(random.choice(pool))
    return f"{ts} {level:<8} [{logger}] {msg}"


# ── TCP sender with reconnect ─────────────────────────────────────────────────

class TCPSender:
    def __init__(self, host: str, port: int, connect_timeout: float = 5.0) -> None:
        self.host            = host
        self.port            = port
        self.connect_timeout = connect_timeout
        self._sock: Optional[socket.socket] = None
        self._lock           = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        for attempt in range(15):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(self.connect_timeout)
                s.connect((self.host, self.port))
                s.settimeout(None)
                self._sock = s
                _log(f"TCP connected to {self.host}:{self.port}")
                return
            except (OSError, ConnectionRefusedError) as exc:
                delay = min(2 ** attempt, 30)
                _log(f"TCP connect attempt {attempt + 1} failed ({exc}); retrying in {delay}s")
                time.sleep(delay)
        raise RuntimeError(f"Could not connect to {self.host}:{self.port} after 15 attempts")

    def send(self, record: Dict[str, Any]) -> bool:
        payload = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            for attempt in range(3):
                try:
                    if self._sock is None:
                        self._connect()
                    self._sock.sendall(payload)  # type: ignore[union-attr]
                    return True
                except OSError:
                    self._sock = None
                    if attempt < 2:
                        _log("TCP send failed; reconnecting…")
                        try:
                            self._connect()
                        except RuntimeError:
                            return False
        return False

    def close(self) -> None:
        with self._lock:
            if self._sock:
                self._sock.close()
                self._sock = None


# ── Worker threads ─────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[generator] {msg}", flush=True)


def file_worker(rate: int, stop: threading.Event) -> None:
    json_dir  = LOG_DIR / "json"
    plain_dir = LOG_DIR / "plain"
    json_dir.mkdir(parents=True, exist_ok=True)
    plain_dir.mkdir(parents=True, exist_ok=True)

    json_path  = json_dir  / f"{SERVICE}.log"
    plain_path = plain_dir / f"{SERVICE}-legacy.log"

    interval = 1.0 / max(rate, 1)

    with open(json_path, "a", buffering=65536) as jf, \
         open(plain_path, "a", buffering=65536) as pf:
        tick = 0
        while not stop.is_set():
            t0 = time.monotonic()

            jf.write(json.dumps(_make_json_record(), separators=(",", ":")) + "\n")
            pf.write(_make_plaintext_line() + "\n")
            tick += 1

            # Flush every ~1 000 lines so Fluent Bit tail picks them up promptly
            if tick % 1000 == 0:
                jf.flush()
                pf.flush()

            elapsed = time.monotonic() - t0
            sleep   = interval - elapsed
            if sleep > 0:
                time.sleep(sleep)


def tcp_worker(sender: TCPSender, rate: int, stop: threading.Event) -> None:
    interval = 1.0 / max(rate, 1)
    while not stop.is_set():
        t0 = time.monotonic()
        sender.send(_make_json_record())
        elapsed = time.monotonic() - t0
        sleep   = interval - elapsed
        if sleep > 0:
            time.sleep(sleep)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    half = max(1, LOG_RATE // 2)
    _log(f"Service     : {SERVICE}")
    _log(f"Target rate : {LOG_RATE} events/sec  ({half} file + {half} TCP)")
    _log(f"Log dir     : {LOG_DIR}")
    _log(f"TCP target  : {TCP_HOST}:{TCP_PORT}")

    stop   = threading.Event()
    sender = TCPSender(TCP_HOST, TCP_PORT)

    threads = [
        threading.Thread(target=file_worker, args=(half, stop),         name="file",  daemon=True),
        threading.Thread(target=tcp_worker,  args=(sender, half, stop), name="tcp",   daemon=True),
    ]
    for t in threads:
        t.start()

    _log("Workers started. Ctrl-C to stop.")
    try:
        iteration = 0
        while True:
            time.sleep(10)
            iteration += 1
            _log(f"tick {iteration * 10}s — file={half}/s tcp={half}/s")
    except KeyboardInterrupt:
        _log("Stopping…")
        stop.set()
        for t in threads:
            t.join(timeout=5)
        sender.close()
        _log("Done.")


if __name__ == "__main__":
    main()
