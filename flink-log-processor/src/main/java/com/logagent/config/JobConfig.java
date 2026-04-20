package com.logagent.config;

import java.io.Serializable;
import java.util.Objects;

/**
 * Immutable configuration bag.  All values are read from environment variables
 * at startup so the same fat JAR runs in every environment without recompilation.
 *
 * <p>Defaults are production-safe choices; override them in your container env.
 */
public final class JobConfig implements Serializable {

    // ── Kafka ─────────────────────────────────────────────────────────────────
    private final String  kafkaBrokers;
    private final String  rawLogsTopic;
    private final String  processedLogsTopic;
    private final String  deadLetterTopic;
    private final String  consumerGroupId;

    // ── OpenSearch ────────────────────────────────────────────────────────────
    private final String  openSearchHost;
    private final int     openSearchPort;
    private final String  openSearchUsername;
    private final String  openSearchPassword;
    private final int     openSearchBulkActions;
    private final long    openSearchBulkFlushMs;

    // ── Flink checkpointing ───────────────────────────────────────────────────
    private final String  checkpointDir;
    private final long    checkpointIntervalMs;

    // ── Watermarking ──────────────────────────────────────────────────────────
    /** Max how late an event can arrive before being routed to the side output. */
    private final long    maxOutOfOrdernessMs;
    /** Max how late (beyond the watermark) a window still accepts updates. */
    private final long    allowedLatenessMs;

    // ── Parallelism ───────────────────────────────────────────────────────────
    private final int     defaultParallelism;

    // ── Constructor ───────────────────────────────────────────────────────────

    private JobConfig(Builder b) {
        this.kafkaBrokers           = b.kafkaBrokers;
        this.rawLogsTopic           = b.rawLogsTopic;
        this.processedLogsTopic     = b.processedLogsTopic;
        this.deadLetterTopic        = b.deadLetterTopic;
        this.consumerGroupId        = b.consumerGroupId;
        this.openSearchHost         = b.openSearchHost;
        this.openSearchPort         = b.openSearchPort;
        this.openSearchUsername     = b.openSearchUsername;
        this.openSearchPassword     = b.openSearchPassword;
        this.openSearchBulkActions  = b.openSearchBulkActions;
        this.openSearchBulkFlushMs  = b.openSearchBulkFlushMs;
        this.checkpointDir          = b.checkpointDir;
        this.checkpointIntervalMs   = b.checkpointIntervalMs;
        this.maxOutOfOrdernessMs    = b.maxOutOfOrdernessMs;
        this.allowedLatenessMs      = b.allowedLatenessMs;
        this.defaultParallelism     = b.defaultParallelism;
    }

    // ── Factory ───────────────────────────────────────────────────────────────

    public static JobConfig fromEnvironment() {
        return new Builder()
            .kafkaBrokers(   env("KAFKA_BROKERS",              "kafka-1:9092,kafka-2:9092,kafka-3:9092"))
            .rawLogsTopic(   env("KAFKA_RAW_LOGS_TOPIC",       "raw-logs"))
            .processedLogsTopic(env("KAFKA_PROCESSED_LOGS_TOPIC", "processed-logs"))
            .deadLetterTopic(env("KAFKA_DEAD_LETTER_TOPIC",    "dead-letter-logs"))
            .consumerGroupId(env("KAFKA_CONSUMER_GROUP_ID",    "stream-processor-v1"))
            .openSearchHost( env("OPENSEARCH_HOST",            "opensearch"))
            .openSearchPort( intEnv("OPENSEARCH_PORT",         9200))
            .openSearchUsername(env("OPENSEARCH_USERNAME",     "admin"))
            .openSearchPassword(env("OPENSEARCH_PASSWORD",     "admin"))
            .openSearchBulkActions(intEnv("OPENSEARCH_BULK_ACTIONS", 1000))
            .openSearchBulkFlushMs(longEnv("OPENSEARCH_BULK_FLUSH_MS", 10_000L))
            .checkpointDir(  env("FLINK_CHECKPOINT_DIR",       "s3://checkpoints/flink/log-processor"))
            .checkpointIntervalMs(longEnv("FLINK_CHECKPOINT_INTERVAL_MS", 30_000L))
            .maxOutOfOrdernessMs(longEnv("WATERMARK_MAX_OUT_OF_ORDER_MS", 30_000L))
            .allowedLatenessMs(longEnv("WINDOW_ALLOWED_LATENESS_MS",     30_000L))
            .defaultParallelism(intEnv("FLINK_PARALLELISM",    4))
            .build();
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private static String env(String key, String defaultValue) {
        String value = System.getenv(key);
        return (value != null && !value.isBlank()) ? value : defaultValue;
    }

    private static int intEnv(String key, int defaultValue) {
        String value = System.getenv(key);
        try {
            return (value != null && !value.isBlank()) ? Integer.parseInt(value.strip()) : defaultValue;
        } catch (NumberFormatException e) {
            return defaultValue;
        }
    }

    private static long longEnv(String key, long defaultValue) {
        String value = System.getenv(key);
        try {
            return (value != null && !value.isBlank()) ? Long.parseLong(value.strip()) : defaultValue;
        } catch (NumberFormatException e) {
            return defaultValue;
        }
    }

    // ── Getters ───────────────────────────────────────────────────────────────

    public String  getKafkaBrokers()           { return kafkaBrokers; }
    public String  getRawLogsTopic()           { return rawLogsTopic; }
    public String  getProcessedLogsTopic()     { return processedLogsTopic; }
    public String  getDeadLetterTopic()        { return deadLetterTopic; }
    public String  getConsumerGroupId()        { return consumerGroupId; }
    public String  getOpenSearchHost()         { return openSearchHost; }
    public int     getOpenSearchPort()         { return openSearchPort; }
    public String  getOpenSearchUsername()     { return openSearchUsername; }
    public String  getOpenSearchPassword()     { return openSearchPassword; }
    public int     getOpenSearchBulkActions()  { return openSearchBulkActions; }
    public long    getOpenSearchBulkFlushMs()  { return openSearchBulkFlushMs; }
    public String  getCheckpointDir()          { return checkpointDir; }
    public long    getCheckpointIntervalMs()   { return checkpointIntervalMs; }
    public long    getMaxOutOfOrdernessMs()    { return maxOutOfOrdernessMs; }
    public long    getAllowedLatenessMs()       { return allowedLatenessMs; }
    public int     getDefaultParallelism()     { return defaultParallelism; }

    // ── Builder ───────────────────────────────────────────────────────────────

    public static final class Builder {
        String  kafkaBrokers           = "localhost:9092";
        String  rawLogsTopic           = "raw-logs";
        String  processedLogsTopic     = "processed-logs";
        String  deadLetterTopic        = "dead-letter-logs";
        String  consumerGroupId        = "stream-processor-v1";
        String  openSearchHost         = "localhost";
        int     openSearchPort         = 9200;
        String  openSearchUsername     = "admin";
        String  openSearchPassword     = "admin";
        int     openSearchBulkActions  = 1000;
        long    openSearchBulkFlushMs  = 10_000L;
        String  checkpointDir          = "file:///tmp/flink-checkpoints";
        long    checkpointIntervalMs   = 30_000L;
        long    maxOutOfOrdernessMs    = 30_000L;
        long    allowedLatenessMs      = 30_000L;
        int     defaultParallelism     = 4;

        public Builder kafkaBrokers(String v)           { this.kafkaBrokers          = Objects.requireNonNull(v); return this; }
        public Builder rawLogsTopic(String v)           { this.rawLogsTopic          = Objects.requireNonNull(v); return this; }
        public Builder processedLogsTopic(String v)     { this.processedLogsTopic    = Objects.requireNonNull(v); return this; }
        public Builder deadLetterTopic(String v)        { this.deadLetterTopic       = Objects.requireNonNull(v); return this; }
        public Builder consumerGroupId(String v)        { this.consumerGroupId       = Objects.requireNonNull(v); return this; }
        public Builder openSearchHost(String v)         { this.openSearchHost        = Objects.requireNonNull(v); return this; }
        public Builder openSearchPort(int v)            { this.openSearchPort        = v; return this; }
        public Builder openSearchUsername(String v)     { this.openSearchUsername    = Objects.requireNonNull(v); return this; }
        public Builder openSearchPassword(String v)     { this.openSearchPassword    = Objects.requireNonNull(v); return this; }
        public Builder openSearchBulkActions(int v)     { this.openSearchBulkActions = v; return this; }
        public Builder openSearchBulkFlushMs(long v)    { this.openSearchBulkFlushMs = v; return this; }
        public Builder checkpointDir(String v)          { this.checkpointDir         = Objects.requireNonNull(v); return this; }
        public Builder checkpointIntervalMs(long v)     { this.checkpointIntervalMs  = v; return this; }
        public Builder maxOutOfOrdernessMs(long v)      { this.maxOutOfOrdernessMs   = v; return this; }
        public Builder allowedLatenessMs(long v)        { this.allowedLatenessMs     = v; return this; }
        public Builder defaultParallelism(int v)        { this.defaultParallelism    = v; return this; }

        public JobConfig build() { return new JobConfig(this); }
    }
}
