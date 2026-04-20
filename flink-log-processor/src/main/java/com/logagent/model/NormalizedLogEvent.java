package com.logagent.model;

import java.io.Serializable;

/**
 * Canonical log event schema produced by {@link com.logagent.operators.LogNormalizationFunction}.
 *
 * All string fields default to {@code ""} so downstream code never needs null
 * checks.  {@link #timestampMs} drives Flink's event-time processing; it is
 * set to the parsed value of the log's own timestamp field, falling back to
 * the Kafka ingestion time when no usable timestamp can be parsed.
 *
 * This is the record type flowing through the entire Flink pipeline after
 * the normalisation step, so it must remain {@link Serializable} for state
 * and side-output handling.
 */
public class NormalizedLogEvent implements Serializable {

    /** Unix epoch milliseconds — used as event timestamp by the watermark strategy. */
    private long timestampMs;

    /** ISO-8601 string representation of the same timestamp. */
    private String timestamp = "";

    /** Canonical service name, e.g. "payment-svc". Never null. */
    private String service = "";

    /** Normalised log level. Never null — defaults to {@link LogLevel#UNKNOWN}. */
    private LogLevel level = LogLevel.UNKNOWN;

    /** Human-readable log message. Never null. */
    private String message = "";

    /** OpenTelemetry trace identifier, or {@code ""} if absent. */
    private String traceId = "";

    /** OpenTelemetry span identifier, or {@code ""} if absent. */
    private String spanId = "";

    /** Emitting hostname / pod name. */
    private String host = "";

    /** Deployment environment: prod, staging, local, … */
    private String env = "";

    /** Logger / class name emitting the record. */
    private String logger = "";

    /**
     * HTTP request duration in milliseconds, if available in the log payload.
     * {@code null} when the record is not an HTTP access log.
     */
    private Long latencyMs;

    /**
     * The original raw JSON string from Kafka.
     * Preserved for DLQ reprocessing and debug correlation.
     */
    private String raw = "";

    // ── Getters ───────────────────────────────────────────────────────────────

    public long      getTimestampMs() { return timestampMs; }
    public String    getTimestamp()   { return timestamp;   }
    public String    getService()     { return service;     }
    public LogLevel  getLevel()       { return level;       }
    public String    getMessage()     { return message;     }
    public String    getTraceId()     { return traceId;     }
    public String    getSpanId()      { return spanId;      }
    public String    getHost()        { return host;        }
    public String    getEnv()         { return env;         }
    public String    getLogger()      { return logger;      }
    public Long      getLatencyMs()   { return latencyMs;   }
    public String    getRaw()         { return raw;         }

    // ── Setters ───────────────────────────────────────────────────────────────

    public void setTimestampMs(long v)    { this.timestampMs = v; }
    public void setTimestamp(String v)    { this.timestamp   = v != null ? v : ""; }
    public void setService(String v)      { this.service     = v != null ? v : ""; }
    public void setLevel(LogLevel v)      { this.level       = v != null ? v : LogLevel.UNKNOWN; }
    public void setMessage(String v)      { this.message     = v != null ? v : ""; }
    public void setTraceId(String v)      { this.traceId     = v != null ? v : ""; }
    public void setSpanId(String v)       { this.spanId      = v != null ? v : ""; }
    public void setHost(String v)         { this.host        = v != null ? v : ""; }
    public void setEnv(String v)          { this.env         = v != null ? v : ""; }
    public void setLogger(String v)       { this.logger      = v != null ? v : ""; }
    public void setLatencyMs(Long v)      { this.latencyMs   = v; }
    public void setRaw(String v)          { this.raw         = v != null ? v : ""; }
}
