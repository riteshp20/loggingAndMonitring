package com.logagent.model;

import java.io.Serializable;
import java.util.List;

/**
 * Feature vector produced for each (service × 1-minute window) slice.
 *
 * Written to:
 *   • Kafka topic {@code processed-logs} (keyed by service) — consumed by the
 *     anomaly detector in Phase 3.
 *   • OpenSearch index {@code logs-aggregates-YYYY.MM.dd} — consumed by
 *     dashboards and alert-rule queries.
 *
 * <h3>Re-fire behaviour</h3>
 * When late-arriving events cause a window to be re-fired (within the
 * {@code allowedLateness} period), a new {@code ServiceWindowAggregate} is
 * produced with the same {@code windowStart} / {@code windowEnd} but updated
 * counts.  Consumers must treat this as an upsert keyed on
 * {@code (service, windowStartMs)}.
 */
public class ServiceWindowAggregate implements Serializable {

    /** Discriminator so consumers can parse mixed-type topics. */
    public static final String RECORD_TYPE = "SERVICE_WINDOW_AGGREGATE";

    private String  recordType    = RECORD_TYPE;
    private String  service       = "";
    private String  env           = "";

    private long    windowStartMs;
    private long    windowEndMs;
    private String  windowStart   = "";   // ISO-8601
    private String  windowEnd     = "";   // ISO-8601

    // ── Volume ────────────────────────────────────────────────────────────────
    private long    requestVolume;

    // ── Error / warning rates  (0.0 – 1.0) ───────────────────────────────────
    private double  errorRate;
    private double  warnRate;
    private long    errorCount;
    private long    warnCount;

    // ── Latency percentiles (null when no latency data was observed) ──────────
    private Long    p99LatencyMs;
    private Long    p95LatencyMs;
    private Long    p50LatencyMs;
    private Long    avgLatencyMs;

    // ── Top-5 error messages by frequency ────────────────────────────────────
    private List<ErrorMessageCount> topErrors;

    // ── Getters ───────────────────────────────────────────────────────────────

    public String  getRecordType()    { return recordType;    }
    public String  getService()       { return service;       }
    public String  getEnv()           { return env;           }
    public long    getWindowStartMs() { return windowStartMs; }
    public long    getWindowEndMs()   { return windowEndMs;   }
    public String  getWindowStart()   { return windowStart;   }
    public String  getWindowEnd()     { return windowEnd;     }
    public long    getRequestVolume() { return requestVolume; }
    public double  getErrorRate()     { return errorRate;     }
    public double  getWarnRate()      { return warnRate;      }
    public long    getErrorCount()    { return errorCount;    }
    public long    getWarnCount()     { return warnCount;     }
    public Long    getP99LatencyMs()  { return p99LatencyMs;  }
    public Long    getP95LatencyMs()  { return p95LatencyMs;  }
    public Long    getP50LatencyMs()  { return p50LatencyMs;  }
    public Long    getAvgLatencyMs()  { return avgLatencyMs;  }
    public List<ErrorMessageCount> getTopErrors() { return topErrors; }

    // ── Setters ───────────────────────────────────────────────────────────────

    public void setRecordType(String v)    { this.recordType    = v; }
    public void setService(String v)       { this.service       = v; }
    public void setEnv(String v)           { this.env           = v; }
    public void setWindowStartMs(long v)   { this.windowStartMs = v; }
    public void setWindowEndMs(long v)     { this.windowEndMs   = v; }
    public void setWindowStart(String v)   { this.windowStart   = v; }
    public void setWindowEnd(String v)     { this.windowEnd     = v; }
    public void setRequestVolume(long v)   { this.requestVolume = v; }
    public void setErrorRate(double v)     { this.errorRate     = v; }
    public void setWarnRate(double v)      { this.warnRate      = v; }
    public void setErrorCount(long v)      { this.errorCount    = v; }
    public void setWarnCount(long v)       { this.warnCount     = v; }
    public void setP99LatencyMs(Long v)    { this.p99LatencyMs  = v; }
    public void setP95LatencyMs(Long v)    { this.p95LatencyMs  = v; }
    public void setP50LatencyMs(Long v)    { this.p50LatencyMs  = v; }
    public void setAvgLatencyMs(Long v)    { this.avgLatencyMs  = v; }
    public void setTopErrors(List<ErrorMessageCount> v) { this.topErrors = v; }

    // ── Inner: error message with frequency count ─────────────────────────────

    public static class ErrorMessageCount implements Serializable {
        private String message;
        private long   count;

        public ErrorMessageCount() {}
        public ErrorMessageCount(String message, long count) {
            this.message = message;
            this.count   = count;
        }

        public String getMessage() { return message; }
        public long   getCount()   { return count;   }
        public void   setMessage(String v) { this.message = v; }
        public void   setCount(long v)     { this.count   = v; }
    }
}
