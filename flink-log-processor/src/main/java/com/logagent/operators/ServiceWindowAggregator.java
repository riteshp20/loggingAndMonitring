package com.logagent.operators;

import com.logagent.model.LogLevel;
import com.logagent.model.NormalizedLogEvent;
import com.logagent.model.WindowAccumulator;
import org.apache.flink.api.common.functions.AggregateFunction;

/**
 * Incrementally aggregates {@link NormalizedLogEvent}s within a 1-minute
 * tumbling event-time window, keyed by service name.
 *
 * <h3>Why AggregateFunction (not ProcessWindowFunction)?</h3>
 * {@code AggregateFunction} runs {@link #add} for every record as it arrives —
 * no records are buffered in Flink state.  Only the compact accumulator
 * (~80 KB per window per service) is kept in RocksDB.  A
 * {@code ProcessWindowFunction} would buffer every raw event until the window
 * closes, blowing up state at 100 K events/sec.
 *
 * <h3>Combined pattern</h3>
 * In {@link com.logagent.StreamingJob} this function is used with
 * {@link WindowResultFunction} via
 * {@code .aggregate(aggregatorFn, windowResultFn)}.  Flink runs
 * {@code AggregateFunction.getResult()} to produce the partial result and
 * hands it to {@code ProcessWindowFunction.process()} which adds window
 * metadata (start/end timestamps).
 */
public class ServiceWindowAggregator
        implements AggregateFunction<NormalizedLogEvent, WindowAccumulator, WindowAccumulator> {

    @Override
    public WindowAccumulator createAccumulator() {
        return new WindowAccumulator();
    }

    @Override
    public WindowAccumulator add(NormalizedLogEvent event, WindowAccumulator acc) {
        // First event in this accumulator sets the identity fields
        if (acc.service.isEmpty()) {
            acc.service = event.getService();
        }
        if (acc.env.isEmpty() && !event.getEnv().isEmpty()) {
            acc.env = event.getEnv();
        }

        acc.totalCount++;

        switch (event.getLevel()) {
            case ERROR, CRITICAL -> {
                acc.errorCount++;
                // Index error and critical messages for the top-5 computation
                acc.addErrorMessage(event.getMessage());
            }
            case WARNING -> acc.warnCount++;
            default      -> { /* INFO / DEBUG / UNKNOWN — counted in totalCount only */ }
        }

        if (event.getLatencyMs() != null) {
            acc.addLatency(event.getLatencyMs());
        }

        return acc;
    }

    /**
     * Merges two accumulators.
     *
     * Flink calls this when pre-aggregated partial results must be combined —
     * for example during rescaling or when session windows are merged.
     * For plain tumbling windows it is rarely invoked but must be correct.
     */
    @Override
    public WindowAccumulator merge(WindowAccumulator a, WindowAccumulator b) {
        WindowAccumulator merged = new WindowAccumulator();

        merged.service = a.service.isEmpty() ? b.service : a.service;
        merged.env     = a.env.isEmpty()     ? b.env     : a.env;

        merged.totalCount     = a.totalCount + b.totalCount;
        merged.errorCount     = a.errorCount + b.errorCount;
        merged.warnCount      = a.warnCount  + b.warnCount;
        merged.totalLatencyMs = a.totalLatencyMs + b.totalLatencyMs;

        // Error message frequency maps: union with summed counts
        merged.errorMessages.putAll(a.errorMessages);
        b.errorMessages.forEach((k, v) -> merged.errorMessages.merge(k, v, Long::sum));

        // Latency reservoirs: replay b's samples into merged's reservoir
        // (which starts from a's reservoir via the copy above)
        for (int i = 0; i < a.latencyFill; i++) {
            merged.addLatency(a.latencySamples[i]);
        }
        for (int i = 0; i < b.latencyFill; i++) {
            merged.addLatency(b.latencySamples[i]);
        }

        return merged;
    }

    @Override
    public WindowAccumulator getResult(WindowAccumulator acc) {
        return acc;
    }
}
