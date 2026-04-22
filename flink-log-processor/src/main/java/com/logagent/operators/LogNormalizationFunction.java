package com.logagent.operators;

import com.logagent.model.LogLevel;
import com.logagent.model.NormalizedLogEvent;
import com.logagent.model.RawLogEvent;
import org.apache.flink.configuration.Configuration;
import org.apache.flink.metrics.Counter;
import org.apache.flink.streaming.api.functions.ProcessFunction;
import org.apache.flink.util.Collector;
import org.apache.flink.util.OutputTag;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.List;
import java.util.Map;

/**
 * Transforms a {@link RawLogEvent} into a {@link NormalizedLogEvent}.
 *
 * <h3>Goals</h3>
 * <ol>
 *   <li>Resolve field-name ambiguity (level / severity / log_level → canonical level).</li>
 *   <li>Parse timestamps in multiple ISO-8601 / epoch formats.</li>
 *   <li>Extract HTTP request latency from nested {@code extra.http.duration_ms}.</li>
 *   <li>Surface failures as Flink metrics rather than exceptions so the pipeline
 *       never stalls on a single malformed record.</li>
 * </ol>
 *
 * <h3>Error handling</h3>
 * Records that failed JSON deserialization (flagged by
 * {@link com.logagent.deserialization.RawLogDeserializer}) are routed to the
 * {@link #DEAD_LETTER_TAG} side output via {@code ctx.output()} so they land
 * in the {@code dead-letter-logs} Kafka topic for manual inspection and replay.
 * All other normalisation errors are logged and counted; a degraded record is
 * emitted so downstream windows still receive the correct event count.
 *
 * <p>Using {@link ProcessFunction} (rather than {@code RichFlatMapFunction}) is
 * necessary because only {@code ProcessFunction.Context} exposes
 * {@code ctx.output(OutputTag, value)} for side outputs.
 */
public class LogNormalizationFunction
        extends ProcessFunction<RawLogEvent, NormalizedLogEvent> {

    private static final Logger LOG = LoggerFactory.getLogger(LogNormalizationFunction.class);

    /** Side-output tag for records that could not be deserialised or are irrecoverably malformed. */
    public static final OutputTag<RawLogEvent> DEAD_LETTER_TAG =
            new OutputTag<RawLogEvent>("dead-letter") {};

    private static final List<DateTimeFormatter> TIMESTAMP_FORMATTERS = List.of(
            DateTimeFormatter.ISO_OFFSET_DATE_TIME,
            DateTimeFormatter.ISO_DATE_TIME,
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss.SSS z"),
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss,SSS"),
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss.SSS"),
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")
    );

    private transient Counter normalizationErrors;
    private transient Counter deadLetterCount;

    @Override
    public void open(Configuration parameters) {
        normalizationErrors = getRuntimeContext()
                .getMetricGroup()
                .counter("normalization_errors");
        deadLetterCount = getRuntimeContext()
                .getMetricGroup()
                .counter("dead_letter_records");
    }

    @Override
    public void processElement(RawLogEvent raw, Context ctx, Collector<NormalizedLogEvent> out) {
        // Records that failed JSON deserialization go to the dead-letter side output
        // so they land in the dead-letter-logs Kafka topic for replay rather than
        // being silently dropped.
        if (raw.getDeserializationError() != null) {
            deadLetterCount.inc();
            ctx.output(DEAD_LETTER_TAG, raw);
            return;
        }

        try {
            out.collect(normalize(raw));
        } catch (Exception e) {
            normalizationErrors.inc();
            LOG.warn("Normalization error (service={}): {}",
                    firstNonNull(raw.getServiceName(), raw.getService(), "unknown"),
                    e.getMessage());
            // Emit a degraded record — window counts still work; latency/trace fields are empty
            out.collect(degraded(raw));
        }
    }

    // ── Core normalization ────────────────────────────────────────────────────

    private NormalizedLogEvent normalize(RawLogEvent r) {
        NormalizedLogEvent e = new NormalizedLogEvent();

        // ── Timestamp ────────────────────────────────────────────────────────
        String tsRaw = firstNonNull(r.getAtTimestamp(), r.getTimestamp(), r.getTime());
        long   tsMs  = parseTimestampMs(tsRaw);
        e.setTimestampMs(tsMs);
        e.setTimestamp(tsRaw != null ? tsRaw
                : Instant.ofEpochMilli(tsMs).atOffset(ZoneOffset.UTC).format(DateTimeFormatter.ISO_OFFSET_DATE_TIME));

        // ── Service / host / env ──────────────────────────────────────────────
        e.setService(  firstNonNull(r.getServiceName(),  r.getService(),     "unknown"));
        e.setHost(     firstNonNull(r.getHostname(),     r.getHost(),        "unknown"));
        e.setEnv(      firstNonNull(r.getEnvironment(),  r.getEnv(),         "unknown"));
        e.setLogger(   firstNonNull(r.getLogger(),                           ""));

        // ── Level ─────────────────────────────────────────────────────────────
        String rawLevel = firstNonNull(r.getLevel(), r.getSeverity(), r.getLogLevel());
        e.setLevel(LogLevel.parse(rawLevel));

        // ── Message ───────────────────────────────────────────────────────────
        e.setMessage(firstNonNull(r.getMessage(), r.getMsg(), r.getLog(), ""));

        // ── Tracing ───────────────────────────────────────────────────────────
        e.setTraceId(firstNonNull(r.getTraceId(), ""));
        e.setSpanId( firstNonNull(r.getSpanId(),  ""));

        // ── Latency: inspect extra.http.duration_ms ───────────────────────────
        e.setLatencyMs(extractLatencyMs(r));

        // ── Raw JSON preserved for DLQ replay ─────────────────────────────────
        e.setRaw(firstNonNull(r.getRawJson(), ""));

        return e;
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    /**
     * Parses a timestamp string into epoch milliseconds.
     * Falls back to the current wall-clock time if parsing fails, so the
     * record still participates in windows (at the cost of potential mis-assignment).
     */
    private long parseTimestampMs(String tsRaw) {
        if (tsRaw == null || tsRaw.isBlank()) {
            return System.currentTimeMillis();
        }

        // Try epoch millis / epoch seconds (numeric strings)
        if (tsRaw.chars().allMatch(c -> Character.isDigit(c) || c == '.')) {
            try {
                double d = Double.parseDouble(tsRaw);
                // Heuristic: values > 1e12 are millis, smaller are seconds
                return d > 1e12 ? (long) d : (long) (d * 1000);
            } catch (NumberFormatException ignored) { /* fall through */ }
        }

        // Try ISO-8601 and common formats
        for (DateTimeFormatter fmt : TIMESTAMP_FORMATTERS) {
            try {
                return OffsetDateTime.parse(tsRaw, fmt).toInstant().toEpochMilli();
            } catch (DateTimeParseException ignored) { /* try next */ }
        }

        LOG.debug("Could not parse timestamp '{}', using current time", tsRaw);
        return System.currentTimeMillis();
    }

    /**
     * Extracts HTTP request duration from the nested {@code extra.http} map
     * that the log-generator and many frameworks produce.
     */
    private Long extractLatencyMs(RawLogEvent r) {
        Map<String, Object> extra = r.getExtra();
        if (extra == null) return null;

        // Check extra.http.duration_ms (our log-generator schema)
        Object http = extra.get("http");
        if (http instanceof Map<?, ?> httpMap) {
            Object dur = httpMap.get("duration_ms");
            if (dur instanceof Number n) return n.longValue();
        }

        // Check top-level duration_ms or latency_ms
        for (String key : List.of("duration_ms", "latency_ms", "response_time_ms", "elapsed_ms")) {
            Object v = extra.get(key);
            if (v instanceof Number n) return n.longValue();
        }

        return null;
    }

    /** Returns the first argument that is not null and not blank. */
    private static String firstNonNull(String... candidates) {
        for (String c : candidates) {
            if (c != null && !c.isBlank()) return c;
        }
        return "";
    }

    /** Builds a degraded (best-effort) normalized event when normalization itself fails. */
    private NormalizedLogEvent degraded(RawLogEvent r) {
        NormalizedLogEvent e = new NormalizedLogEvent();
        e.setTimestampMs(System.currentTimeMillis());
        e.setService(firstNonNull(r.getServiceName(), r.getService(), "unknown"));
        e.setLevel(LogLevel.UNKNOWN);
        e.setMessage(firstNonNull(r.getMessage(), r.getMsg(), r.getLog(), ""));
        e.setRaw(firstNonNull(r.getRawJson(), ""));
        return e;
    }
}
