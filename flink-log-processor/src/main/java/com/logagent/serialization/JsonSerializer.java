package com.logagent.serialization;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import org.apache.flink.api.common.serialization.SerializationSchema;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.charset.StandardCharsets;

/**
 * Generic Flink {@link SerializationSchema} that converts any object to
 * UTF-8-encoded JSON bytes for writing to Kafka.
 *
 * <p>The {@link ObjectMapper} is initialised lazily on first use (or via the
 * Flink {@link #open} lifecycle hook) because it is not Java-serialisable.
 *
 * @param <T> the type to serialise
 */
public class JsonSerializer<T> implements SerializationSchema<T> {

    private static final Logger LOG = LoggerFactory.getLogger(JsonSerializer.class);

    private transient ObjectMapper mapper;

    @Override
    public void open(InitializationContext context) {
        mapper = buildMapper();
    }

    @Override
    public byte[] serialize(T element) {
        if (mapper == null) {
            mapper = buildMapper();
        }
        try {
            return mapper.writeValueAsBytes(element);
        } catch (Exception e) {
            LOG.error("Failed to serialise {}: {}", element.getClass().getSimpleName(), e.getMessage(), e);
            // Return a minimal error JSON rather than null so Kafka still receives a record.
            return ("{\"_error\":\"serialisation_failed\",\"type\":\""
                    + element.getClass().getSimpleName() + "\"}")
                    .getBytes(StandardCharsets.UTF_8);
        }
    }

    private static ObjectMapper buildMapper() {
        return new ObjectMapper()
                .registerModule(new JavaTimeModule())
                .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
    }
}
