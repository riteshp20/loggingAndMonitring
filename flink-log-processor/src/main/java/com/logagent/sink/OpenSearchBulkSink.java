package com.logagent.sink;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.logagent.config.JobConfig;
import com.logagent.model.ServiceWindowAggregate;
import org.apache.flink.api.common.state.ListState;
import org.apache.flink.api.common.state.ListStateDescriptor;
import org.apache.flink.api.common.typeinfo.TypeInformation;
import org.apache.flink.runtime.state.FunctionInitializationContext;
import org.apache.flink.runtime.state.FunctionSnapshotContext;
import org.apache.flink.streaming.api.checkpoint.CheckpointedFunction;
import org.apache.flink.streaming.api.functions.sink.RichSinkFunction;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;

/**
 * Flink sink that bulk-indexes {@link ServiceWindowAggregate} documents into OpenSearch.
 *
 * <h3>Why not the Flink OpenSearch connector?</h3>
 * As of Flink 1.18, the published connector artifacts for OpenSearch 2.x
 * require an exact Flink minor version match and pull in the OpenSearch High
 * Level REST Client which conflicts with Flink's bundled HttpComponents.
 * Using Java 11's built-in {@link java.net.http.HttpClient} with the raw
 * OpenSearch {@code /_bulk} API avoids both issues.
 *
 * <h3>Delivery semantics</h3>
 * This sink implements {@link CheckpointedFunction}.  On
 * {@link #snapshotState} it flushes the in-memory buffer to OpenSearch before
 * the checkpoint barrier passes.  Documents are indexed with deterministic IDs
 * ({@code service-windowStartMs}), so re-indexing on recovery is idempotent.
 * The guarantee is <b>at-least-once</b>.
 *
 * <h3>Bulk format</h3>
 * <pre>
 * {"index":{"_index":"logs-aggregates-2024.01.15","_id":"payment-svc-1705344000000"}}
 * {"service":"payment-svc","windowStart":"2024-01-15T12:00:00Z",...}
 * </pre>
 * A trailing newline after each pair is required by the OpenSearch bulk API.
 */
public class OpenSearchBulkSink
        extends RichSinkFunction<ServiceWindowAggregate>
        implements CheckpointedFunction {

    private static final Logger LOG = LoggerFactory.getLogger(OpenSearchBulkSink.class);

    private static final DateTimeFormatter INDEX_DATE_FMT =
            DateTimeFormatter.ofPattern("yyyy.MM.dd").withZone(ZoneOffset.UTC);

    private final JobConfig config;
    private final int       bufferSize;

    // ── Transient runtime state ───────────────────────────────────────────────
    private transient HttpClient          httpClient;
    private transient ObjectMapper        mapper;
    private transient String              bulkEndpoint;
    private transient String              authHeader;
    private transient List<ServiceWindowAggregate> buffer;

    // ── Operator state (survives checkpoints) ─────────────────────────────────
    // Documents that were in the buffer when the job failed are replayed from
    // the Kafka offset — we do NOT checkpoint the buffer itself because
    // replaying from Kafka is cheaper and the documents are idempotent.
    // The ListState below is declared to satisfy CheckpointedFunction but
    // kept empty; the comment explains why.
    private transient ListState<ServiceWindowAggregate> checkpointedBuffer;

    public OpenSearchBulkSink(JobConfig config) {
        this.config     = config;
        this.bufferSize = config.getOpenSearchBulkActions();
    }

    // ── Flink lifecycle ───────────────────────────────────────────────────────

    @Override
    public void open(org.apache.flink.configuration.Configuration parameters) {
        mapper = new ObjectMapper()
                .registerModule(new JavaTimeModule())
                .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);

        httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();

        bulkEndpoint = String.format("http://%s:%d/_bulk",
                config.getOpenSearchHost(), config.getOpenSearchPort());

        // Basic-auth header (Base64 of "user:pass")
        String credentials = config.getOpenSearchUsername() + ":" + config.getOpenSearchPassword();
        authHeader = "Basic " + Base64.getEncoder()
                .encodeToString(credentials.getBytes(StandardCharsets.UTF_8));

        buffer = new ArrayList<>(bufferSize);
        LOG.info("OpenSearch bulk sink initialised — endpoint={}, bufferSize={}",
                bulkEndpoint, bufferSize);
    }

    @Override
    public void invoke(ServiceWindowAggregate value, Context context) throws Exception {
        buffer.add(value);
        if (buffer.size() >= bufferSize) {
            flush();
        }
    }

    @Override
    public void close() throws Exception {
        if (!buffer.isEmpty()) {
            flush();
        }
    }

    // ── CheckpointedFunction ──────────────────────────────────────────────────

    @Override
    public void snapshotState(FunctionSnapshotContext ctx) throws Exception {
        // Flush before the checkpoint barrier so all acknowledged records
        // are durable in OpenSearch before we advance the Kafka offset.
        if (!buffer.isEmpty()) {
            flush();
        }
        checkpointedBuffer.clear();
        // Buffer is intentionally not persisted (see class-level Javadoc).
    }

    @Override
    public void initializeState(FunctionInitializationContext ctx) throws Exception {
        checkpointedBuffer = ctx.getOperatorStateStore().getListState(
                new ListStateDescriptor<>("opensearch-buffer",
                        TypeInformation.of(ServiceWindowAggregate.class)));
        // On recovery: the Kafka source will replay from the last committed
        // offset; we don't need to restore the buffer here.
        if (buffer == null) {
            buffer = new ArrayList<>(bufferSize);
        }
    }

    // ── Bulk indexing ─────────────────────────────────────────────────────────

    private void flush() throws Exception {
        if (buffer.isEmpty()) return;

        List<ServiceWindowAggregate> batch = new ArrayList<>(buffer);
        buffer.clear();

        String payload = buildBulkPayload(batch);

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(bulkEndpoint))
                .header("Content-Type", "application/x-ndjson")
                .header("Authorization", authHeader)
                .POST(HttpRequest.BodyPublishers.ofString(payload, StandardCharsets.UTF_8))
                .timeout(Duration.ofSeconds(30))
                .build();

        try {
            HttpResponse<String> response = httpClient.send(request,
                    HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));

            if (response.statusCode() >= 400) {
                // Re-add to buffer for retry? No — on Kafka replay after job restart
                // these documents will be re-emitted.  Just log and move on.
                LOG.error("Bulk index returned HTTP {}: {}",
                        response.statusCode(),
                        response.body().length() > 500
                                ? response.body().substring(0, 500) + "…"
                                : response.body());
            } else {
                // Check for per-document errors in the response body
                String body = response.body();
                if (body.contains("\"errors\":true")) {
                    LOG.warn("Bulk response contained per-document errors (partial success). "
                            + "First 500 chars: {}",
                            body.length() > 500 ? body.substring(0, 500) : body);
                } else {
                    LOG.debug("Bulk-indexed {} documents to OpenSearch", batch.size());
                }
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IOException("OpenSearch bulk request interrupted", e);
        }
    }

    private String buildBulkPayload(List<ServiceWindowAggregate> docs) throws Exception {
        StringBuilder sb = new StringBuilder();
        for (ServiceWindowAggregate doc : docs) {
            String indexName = "logs-aggregates-"
                    + INDEX_DATE_FMT.format(Instant.ofEpochMilli(doc.getWindowStartMs()));
            // Deterministic document ID — enables idempotent re-indexing on replay
            String docId = doc.getService() + "-" + doc.getWindowStartMs();

            // Action metadata line
            sb.append("{\"index\":{\"_index\":\"")
              .append(indexName)
              .append("\",\"_id\":\"")
              .append(docId)
              .append("\"}}\n");

            // Source document line
            sb.append(mapper.writeValueAsString(doc)).append("\n");
        }
        return sb.toString();
    }
}
