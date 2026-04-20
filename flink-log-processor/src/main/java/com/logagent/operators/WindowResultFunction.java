package com.logagent.operators;

import com.logagent.model.ServiceWindowAggregate;
import com.logagent.model.ServiceWindowAggregate.ErrorMessageCount;
import com.logagent.model.WindowAccumulator;
import org.apache.flink.streaming.api.functions.windowing.ProcessWindowFunction;
import org.apache.flink.streaming.api.windowing.windows.TimeWindow;
import org.apache.flink.util.Collector;

import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.Arrays;
import java.util.Comparator;
import java.util.List;
import java.util.Map;

/**
 * Converts a {@link WindowAccumulator} (from {@link ServiceWindowAggregator})
 * into a {@link ServiceWindowAggregate} by adding window metadata and
 * computing derived statistics.
 *
 * <h3>P99 / P95 / P50 computation</h3>
 * The accumulator holds up to 10 000 raw latency samples via reservoir sampling.
 * This function sorts the filled portion of that reservoir and reads off
 * percentile indices.  Sorting 10 000 longs takes ~1 ms; this is called at
 * most once per (service × minute), so it has no meaningful throughput impact.
 *
 * <h3>Top-5 error messages</h3>
 * The accumulator holds a frequency map of truncated error messages.
 * This function sorts entries by descending count and returns the top 5.
 * Ties are broken lexicographically for determinism.
 */
public class WindowResultFunction
        extends ProcessWindowFunction<WindowAccumulator, ServiceWindowAggregate, String, TimeWindow> {

    private static final int TOP_ERRORS_K = 5;
    private static final DateTimeFormatter ISO = DateTimeFormatter.ISO_OFFSET_DATE_TIME;

    @Override
    public void process(
            String service,
            Context ctx,
            Iterable<WindowAccumulator> accumulators,
            Collector<ServiceWindowAggregate> out) {

        WindowAccumulator acc = accumulators.iterator().next();
        TimeWindow window = ctx.window();

        ServiceWindowAggregate result = new ServiceWindowAggregate();
        result.setService(service);
        result.setEnv(acc.env);

        // ── Window boundaries ─────────────────────────────────────────────────
        result.setWindowStartMs(window.getStart());
        result.setWindowEndMs(window.getEnd());
        result.setWindowStart(epochMsToIso(window.getStart()));
        result.setWindowEnd(epochMsToIso(window.getEnd()));

        // ── Volume ────────────────────────────────────────────────────────────
        result.setRequestVolume(acc.totalCount);
        result.setErrorCount(acc.errorCount);
        result.setWarnCount(acc.warnCount);

        // ── Rates (guard against division by zero) ────────────────────────────
        if (acc.totalCount > 0) {
            result.setErrorRate((double) acc.errorCount / acc.totalCount);
            result.setWarnRate( (double) acc.warnCount  / acc.totalCount);
        } else {
            result.setErrorRate(0.0);
            result.setWarnRate(0.0);
        }

        // ── Latency percentiles ───────────────────────────────────────────────
        if (acc.latencyFill > 0) {
            // Sort only the filled portion of the reservoir
            long[] samples = Arrays.copyOf(acc.latencySamples, acc.latencyFill);
            Arrays.sort(samples);

            result.setP99LatencyMs(percentile(samples, 99));
            result.setP95LatencyMs(percentile(samples, 95));
            result.setP50LatencyMs(percentile(samples, 50));
            result.setAvgLatencyMs(acc.latencyFill > 0
                    ? acc.totalLatencyMs / acc.latencyFill : null);
        }

        // ── Top-5 error messages ──────────────────────────────────────────────
        result.setTopErrors(topK(acc.errorMessages, TOP_ERRORS_K));

        out.collect(result);
    }

    // ── Static helpers ────────────────────────────────────────────────────────

    /**
     * Nearest-rank percentile on a <em>sorted</em> {@code long[]} array.
     * Returns {@code null} when the array is empty.
     */
    private static Long percentile(long[] sorted, int pct) {
        if (sorted.length == 0) return null;
        int idx = (int) Math.ceil(pct / 100.0 * sorted.length) - 1;
        return sorted[Math.max(0, Math.min(idx, sorted.length - 1))];
    }

    /**
     * Returns the top-K entries from a frequency map, sorted by count descending
     * then message ascending for stability.
     */
    private static List<ErrorMessageCount> topK(Map<String, Long> freqMap, int k) {
        return freqMap.entrySet().stream()
                .sorted(Map.Entry.<String, Long>comparingByValue(Comparator.reverseOrder())
                        .thenComparing(Map.Entry.comparingByKey()))
                .limit(k)
                .map(e -> new ErrorMessageCount(e.getKey(), e.getValue()))
                .toList();
    }

    private static String epochMsToIso(long epochMs) {
        return Instant.ofEpochMilli(epochMs).atOffset(ZoneOffset.UTC).format(ISO);
    }
}
