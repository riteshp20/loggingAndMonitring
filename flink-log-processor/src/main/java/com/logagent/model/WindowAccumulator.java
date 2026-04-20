package com.logagent.model;

import java.io.Serializable;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;

/**
 * Mutable accumulator maintained by {@link com.logagent.operators.ServiceWindowAggregator}
 * for a single (service × 1-minute tumbling window) slice.
 *
 * <h3>Latency sampling</h3>
 * Storing every latency value would be unbounded. This class uses reservoir
 * sampling (Knuth's Algorithm R) capped at {@link #MAX_LATENCY_SAMPLES} entries.
 * At 500 events/sec per service × 60 sec = 30 000 samples, a cap of 10 000
 * gives a 33 % sampling fraction — sufficient for accurate P95/P99 estimates
 * (error ≲ 1 % at 33 % sampling ratio).
 *
 * <h3>Error-message frequency map</h3>
 * The map is bounded at {@link #MAX_ERROR_MESSAGES} entries.  When full, the
 * entry with the lowest count is evicted. This keeps the rarest messages around
 * briefly but ultimately retains only the most impactful patterns.
 *
 * <h3>Flink serialization</h3>
 * Flink uses Kryo to serialise accumulators for state.  All fields here are
 * primitive arrays, primitive longs, or standard-library collections — all
 * fully Kryo-serialisable without custom registration.
 */
public class WindowAccumulator implements Serializable {

    public static final int MAX_LATENCY_SAMPLES = 10_000;
    public static final int MAX_ERROR_MESSAGES  = 500;
    public static final int MAX_MESSAGE_LENGTH  = 200;

    // ── Service identity ──────────────────────────────────────────────────────
    public String service = "";
    public String env     = "";

    // ── Volume counters ───────────────────────────────────────────────────────
    public long totalCount = 0L;
    public long errorCount = 0L;
    public long warnCount  = 0L;

    // ── Latency tracking ──────────────────────────────────────────────────────
    public long   totalLatencyMs    = 0L;
    /** How many latency values have been added (may exceed MAX_LATENCY_SAMPLES). */
    public long   latencyTotal      = 0L;
    /** How many slots in latencySamples[] are currently filled. */
    public int    latencyFill       = 0;
    /** Reservoir array — always MAX_LATENCY_SAMPLES in size for predictable memory. */
    public long[] latencySamples    = new long[MAX_LATENCY_SAMPLES];

    // ── Error message frequency map ───────────────────────────────────────────
    public Map<String, Long> errorMessages = new HashMap<>();

    // ── Reservoir sampling ────────────────────────────────────────────────────

    public void addLatency(long latencyMs) {
        totalLatencyMs += latencyMs;
        if (latencyFill < MAX_LATENCY_SAMPLES) {
            latencySamples[latencyFill++] = latencyMs;
        } else {
            // Replace a uniformly random existing slot (Algorithm R).
            // latencyTotal has already been incremented by callers after this.
            int replaceIdx = ThreadLocalRandom.current().nextInt((int) Math.min(latencyTotal + 1, Integer.MAX_VALUE));
            if (replaceIdx < MAX_LATENCY_SAMPLES) {
                latencySamples[replaceIdx] = latencyMs;
            }
        }
        latencyTotal++;
    }

    public void addErrorMessage(String message) {
        if (message == null || message.isBlank()) return;

        // Truncate to prevent huge keys in the map
        String key = message.length() > MAX_MESSAGE_LENGTH
                ? message.substring(0, MAX_MESSAGE_LENGTH)
                : message;

        errorMessages.merge(key, 1L, Long::sum);

        // Evict lowest-frequency entry when the map is full
        if (errorMessages.size() > MAX_ERROR_MESSAGES) {
            errorMessages.entrySet().stream()
                    .min(Map.Entry.comparingByValue())
                    .map(Map.Entry::getKey)
                    .ifPresent(errorMessages::remove);
        }
    }

    /**
     * Merges another accumulator into this one.
     * Called by Flink when pre-aggregated results from separate sub-tasks need
     * to be combined (e.g. after re-partitioning or when merging session windows).
     */
    public void merge(WindowAccumulator other) {
        totalCount     += other.totalCount;
        errorCount     += other.errorCount;
        warnCount      += other.warnCount;
        totalLatencyMs += other.totalLatencyMs;

        // Merge error message maps
        other.errorMessages.forEach((k, v) -> errorMessages.merge(k, v, Long::sum));

        // Merge latency reservoirs: copy other's filled slots into this reservoir
        int otherFill = other.latencyFill;
        for (int i = 0; i < otherFill; i++) {
            addLatency(other.latencySamples[i]);
        }
    }
}
