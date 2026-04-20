package com.logagent;

import com.logagent.config.JobConfig;
import com.logagent.deserialization.RawLogDeserializer;
import com.logagent.model.NormalizedLogEvent;
import com.logagent.model.RawLogEvent;
import com.logagent.model.ServiceWindowAggregate;
import com.logagent.operators.LogNormalizationFunction;
import com.logagent.operators.ServiceWindowAggregator;
import com.logagent.operators.WindowResultFunction;
import com.logagent.serialization.JsonSerializer;
import com.logagent.sink.OpenSearchBulkSink;
import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.functions.FilterFunction;
import org.apache.flink.connector.kafka.sink.KafkaRecordSerializationSchema;
import org.apache.flink.connector.kafka.sink.KafkaSink;
import org.apache.flink.connector.kafka.source.KafkaSource;
import org.apache.flink.connector.kafka.source.enumerator.initializer.OffsetsInitializer;
import org.apache.flink.streaming.api.CheckpointingMode;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.datastream.SingleOutputStreamOperator;
import org.apache.flink.streaming.api.environment.CheckpointConfig;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.windowing.assigners.TumblingEventTimeWindows;
import org.apache.flink.streaming.api.windowing.time.Time;
import org.apache.flink.util.OutputTag;
import org.apache.kafka.clients.consumer.OffsetResetStrategy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.charset.StandardCharsets;
import java.time.Duration;

/**
 * Entry point for the Flink Log Monitoring Stream Processor.
 *
 * <h2>Pipeline topology</h2>
 * <pre>
 *  KafkaSource[raw-logs]
 *      │
 *      ▼  WatermarkStrategy (bounded out-of-order, 30 s)
 *  flatMap: LogNormalizationFunction
 *      │
 *      ├──[A] KafkaSink[processed-logs]          — individual normalised events
 *      │
 *      └──[B] keyBy(service)
 *                 │
 *             TumblingEventTimeWindows(1 min)
 *             allowedLateness(30 s)
 *             sideOutput → dead-letter-late
 *                 │
 *             aggregate(ServiceWindowAggregator, WindowResultFunction)
 *                 │
 *                 ├──[C] KafkaSink[processed-logs]      — SERVICE_WINDOW_AGGREGATE records
 *                 └──[D] OpenSearchBulkSink              — logs-aggregates-YYYY.MM.dd index
 *
 *  Dead-letter paths:
 *      normalization failures → counted metric only (record is degraded, not dropped)
 *      late events (past allowedLateness) → KafkaSink[dead-letter-logs]
 * </pre>
 *
 * <h2>Checkpointing</h2>
 * EXACTLY_ONCE checkpointing every 30 s backed by RocksDB with incremental
 * snapshots uploaded to S3.  This means recovery rewinds Kafka to the last
 * checkpoint and re-processes at most ~30 s of events.  All sinks are either
 * idempotent (OpenSearch, keyed by document ID) or at-least-once (Kafka).
 *
 * <h2>Watermarking</h2>
 * {@code forBoundedOutOfOrderness(30 s)} handles Fluent Bit buffering jitter and
 * Kafka consumer lag.  {@code withIdleness(5 min)} prevents a silent partition
 * (low-traffic service) from stalling the global watermark and blocking all window
 * closings.
 */
public class StreamingJob {

    private static final Logger LOG = LoggerFactory.getLogger(StreamingJob.class);

    /** Side-output tag for events arriving after watermark + allowedLateness. */
    static final OutputTag<NormalizedLogEvent> LATE_EVENTS_TAG =
            new OutputTag<NormalizedLogEvent>("late-events") {};

    public static void main(String[] args) throws Exception {
        JobConfig config = JobConfig.fromEnvironment();
        LOG.info("Starting Log Stream Processor — brokers={}, group={}, checkpoint={}",
                config.getKafkaBrokers(), config.getConsumerGroupId(), config.getCheckpointDir());

        StreamExecutionEnvironment env = buildEnvironment(config);

        // ── Source ────────────────────────────────────────────────────────────
        KafkaSource<RawLogEvent> kafkaSource = buildKafkaSource(config);

        // Watermark strategy:
        //   • forBoundedOutOfOrderness(30 s) — tolerate late arrivals up to 30 s
        //   • withIdleness(5 min)            — unblock watermark when a partition
        //     is silent (e.g. a low-traffic service not emitting for 5+ minutes)
        WatermarkStrategy<RawLogEvent> watermarks = WatermarkStrategy
                .<RawLogEvent>forBoundedOutOfOrderness(
                        Duration.ofMillis(config.getMaxOutOfOrdernessMs()))
                .withTimestampAssigner((event, ts) -> {
                    // Timestamp assigned during normalisation is not yet available;
                    // use Kafka record timestamp as a proxy for the watermark source.
                    // The LogNormalizationFunction will produce the real event-time
                    // which Flink reads from NormalizedLogEvent.timestampMs.
                    return ts;
                })
                .withIdleness(Duration.ofMinutes(5));

        DataStream<RawLogEvent> rawStream = env
                .fromSource(kafkaSource, watermarks, "kafka-source[raw-logs]")
                .name("kafka-source[raw-logs]")
                .uid("kafka-source-raw-logs");

        // ── Normalisation ─────────────────────────────────────────────────────
        // Re-assign watermarks after normalisation using the parsed event timestamp
        // from the log payload itself — this is the "real" event time.
        WatermarkStrategy<NormalizedLogEvent> normalizedWatermarks = WatermarkStrategy
                .<NormalizedLogEvent>forBoundedOutOfOrderness(
                        Duration.ofMillis(config.getMaxOutOfOrdernessMs()))
                .withTimestampAssigner((event, ts) -> event.getTimestampMs())
                .withIdleness(Duration.ofMinutes(5));

        SingleOutputStreamOperator<NormalizedLogEvent> normalizedStream = rawStream
                .flatMap(new LogNormalizationFunction())
                .name("normalize-log-schema")
                .uid("normalize-log-schema")
                .assignTimestampsAndWatermarks(normalizedWatermarks)
                .name("watermark-normalized")
                .uid("watermark-normalized");

        // ── Sink A: individual normalised events → processed-logs ─────────────
        // Downstream anomaly detector needs the raw event stream, not just aggregates.
        normalizedStream
                .sinkTo(buildKafkaJsonSink(config, config.getProcessedLogsTopic()))
                .name("kafka-sink[processed-logs/events]")
                .uid("kafka-sink-normalized-events");

        // ── Windowing ─────────────────────────────────────────────────────────
        // 1-minute tumbling event-time windows, keyed by service name.
        // allowedLateness(30 s): the window re-fires for each batch of late events
        // that arrives within 30 s after the watermark passes the window end.
        // Events arriving after watermark + 30 s go to the LATE_EVENTS_TAG side output.
        SingleOutputStreamOperator<ServiceWindowAggregate> aggregatedStream = normalizedStream
                .filter((FilterFunction<NormalizedLogEvent>) e -> !e.getService().equals("unknown"))
                .name("filter-unknown-services")
                .uid("filter-unknown-services")
                .keyBy(NormalizedLogEvent::getService)
                .window(TumblingEventTimeWindows.of(Time.minutes(1)))
                .allowedLateness(Time.ofMilliseconds(config.getAllowedLatenessMs()))
                .sideOutputTag(LATE_EVENTS_TAG)
                .aggregate(new ServiceWindowAggregator(), new WindowResultFunction())
                .name("service-window-aggregate[1m]")
                .uid("service-window-aggregate");

        // ── Sink B: late events → dead-letter-logs ────────────────────────────
        aggregatedStream
                .getSideOutput(LATE_EVENTS_TAG)
                .map(e -> e)   // materialize side output into a DataStream
                .sinkTo(buildKafkaJsonSink(config, config.getDeadLetterTopic()))
                .name("kafka-sink[dead-letter-logs]")
                .uid("kafka-sink-dead-letter");

        // ── Sink C: window aggregates → processed-logs (feature vectors) ──────
        // Anomaly detector subscribes here; record type discriminator is
        // ServiceWindowAggregate.RECORD_TYPE = "SERVICE_WINDOW_AGGREGATE".
        aggregatedStream
                .sinkTo(buildKafkaJsonSink(config, config.getProcessedLogsTopic()))
                .name("kafka-sink[processed-logs/aggregates]")
                .uid("kafka-sink-aggregates");

        // ── Sink D: window aggregates → OpenSearch ────────────────────────────
        // Daily index pattern: logs-aggregates-YYYY.MM.dd
        // Document ID = {service}-{windowStartMs} — idempotent upserts on replay.
        aggregatedStream
                .addSink(new OpenSearchBulkSink(config))
                .name("opensearch-sink[logs-aggregates-*]")
                .uid("opensearch-sink-aggregates");

        env.execute("log-monitoring-stream-processor");
    }

    // ── Environment configuration ─────────────────────────────────────────────

    private static StreamExecutionEnvironment buildEnvironment(JobConfig config) {
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(config.getDefaultParallelism());

        // How often Flink emits watermarks (200 ms balances latency vs CPU overhead)
        env.getConfig().setAutoWatermarkInterval(200L);

        // ── Checkpointing ─────────────────────────────────────────────────────
        env.enableCheckpointing(config.getCheckpointIntervalMs());

        CheckpointConfig cp = env.getCheckpointConfig();
        cp.setCheckpointingMode(CheckpointingMode.EXACTLY_ONCE);

        // Min 10 s gap between checkpoints prevents a slow checkpoint from
        // immediately triggering the next one, causing a checkpoint storm.
        cp.setMinPauseBetweenCheckpoints(10_000L);

        // If a checkpoint doesn't complete in 60 s something is wrong.
        cp.setCheckpointTimeout(60_000L);

        // Tolerate up to 3 consecutive checkpoint failures before aborting.
        cp.setTolerableCheckpointFailureNumber(3);

        // RETAIN_ON_CANCELLATION keeps the last good checkpoint so you can
        // resume from it after an intentional job cancellation.
        cp.setExternalizedCheckpointCleanup(
                CheckpointConfig.ExternalizedCheckpointCleanup.RETAIN_ON_CANCELLATION);

        // S3 checkpoint storage (LocalStack in dev, real S3 in prod)
        cp.setCheckpointStorage(config.getCheckpointDir());

        // ── RocksDB state backend with incremental snapshots ──────────────────
        // Incremental = only changed SST files are uploaded each checkpoint cycle,
        // not the full state.  Critical at high throughput: without it a 30 s
        // checkpoint could upload gigabytes of unchanged state data.
        try {
            org.apache.flink.contrib.streaming.state.EmbeddedRocksDBStateBackend rocksdb =
                    new org.apache.flink.contrib.streaming.state.EmbeddedRocksDBStateBackend(true);
            env.setStateBackend(rocksdb);
        } catch (Exception e) {
            LOG.warn("Could not configure RocksDB state backend, using default: {}", e.getMessage());
        }

        return env;
    }

    // ── Kafka source ──────────────────────────────────────────────────────────

    private static KafkaSource<RawLogEvent> buildKafkaSource(JobConfig config) {
        return KafkaSource.<RawLogEvent>builder()
                .setBootstrapServers(config.getKafkaBrokers())
                .setTopics(config.getRawLogsTopic())
                .setGroupId(config.getConsumerGroupId())
                // On first start: read from the earliest available offset.
                // After the group has committed offsets: resume from there.
                .setStartingOffsets(
                        OffsetsInitializer.committedOffsets(OffsetResetStrategy.EARLIEST))
                .setValueOnlyDeserializer(new RawLogDeserializer())
                // Discover new partitions every 60 s (handles topic partition expansion)
                .setProperty("partition.discovery.interval.ms", "60000")
                // Only read committed messages — safe if producers use transactions
                .setProperty("isolation.level", "read_committed")
                // Batch-fetch for throughput
                .setProperty("fetch.max.bytes",          "52428800")
                .setProperty("max.partition.fetch.bytes","10485760")
                .setProperty("fetch.min.bytes",          "1048576")
                .setProperty("fetch.max.wait.ms",        "500")
                .build();
    }

    // ── Kafka sink (generic JSON) ─────────────────────────────────────────────

    private static <T> KafkaSink<T> buildKafkaJsonSink(JobConfig config, String topic) {
        return KafkaSink.<T>builder()
                .setBootstrapServers(config.getKafkaBrokers())
                .setRecordSerializer(
                        KafkaRecordSerializationSchema.<T>builder()
                                .setTopic(topic)
                                .setValueSerializationSchema(new JsonSerializer<>())
                                .build())
                // AT_LEAST_ONCE avoids the transaction overhead of EXACTLY_ONCE.
                // Both Kafka sinks are idempotent consumers:
                //   processed-logs: anomaly detector uses the latest aggregate per key
                //   dead-letter-logs: deduplication not required
                .setDeliveryGuarantee(
                        org.apache.flink.connector.base.DeliveryGuarantee.AT_LEAST_ONCE)
                .setProperty("compression.type",              "lz4")
                .setProperty("batch.size",                    "65536")
                .setProperty("linger.ms",                     "50")
                .setProperty("request.timeout.ms",            "30000")
                .setProperty("retries",                       "5")
                .setProperty("retry.backoff.ms",              "500")
                .setProperty("max.in.flight.requests.per.connection", "5")
                .build();
    }
}
