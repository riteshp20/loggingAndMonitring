package com.logagent.deserialization;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.logagent.model.RawLogEvent;
import org.apache.flink.api.common.serialization.DeserializationSchema;
import org.apache.flink.api.common.typeinfo.TypeInformation;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

/**
 * Kafka → {@link RawLogEvent} deserialiser.
 *
 * <h3>Error handling strategy</h3>
 * Kafka records that cannot be parsed as JSON are <em>not</em> thrown — doing
 * so would stop the Flink task and require manual intervention.  Instead, a
 * sentinel {@link RawLogEvent} is returned with {@code deserializationError}
 * set.  The normalisation function downstream filters these out and routes them
 * to the dead-letter side output, where they can be reprocessed later.
 *
 * <h3>ObjectMapper lifecycle</h3>
 * {@link ObjectMapper} is heavyweight (200 KB of class metadata).  It is
 * created once in {@link #open} (a Flink lifecycle callback) rather than per
 * record, and declared {@code transient} so Java serialisation does not try to
 * serialise it across the network.
 */
public class RawLogDeserializer implements DeserializationSchema<RawLogEvent> {

    private static final Logger LOG = LoggerFactory.getLogger(RawLogDeserializer.class);

    private transient ObjectMapper mapper;

    @Override
    public void open(InitializationContext context) {
        mapper = new ObjectMapper()
                .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false)
                .configure(DeserializationFeature.READ_UNKNOWN_ENUM_VALUES_AS_NULL, true)
                .configure(DeserializationFeature.ACCEPT_SINGLE_VALUE_AS_ARRAY, true);
    }

    @Override
    public RawLogEvent deserialize(byte[] message) throws IOException {
        if (message == null || message.length == 0) {
            RawLogEvent empty = new RawLogEvent();
            empty.setDeserializationError("empty message");
            return empty;
        }

        String raw = new String(message, StandardCharsets.UTF_8);

        try {
            RawLogEvent event = mapper.readValue(message, RawLogEvent.class);
            event.setRawJson(raw);
            return event;
        } catch (Exception e) {
            // Log at WARN (not ERROR) — malformed records are expected at scale
            LOG.warn("Deserialization failed for message (first 200 chars): {} — {}",
                    raw.length() > 200 ? raw.substring(0, 200) + "…" : raw,
                    e.getMessage());

            RawLogEvent dead = new RawLogEvent();
            dead.setRawJson(raw);
            dead.setDeserializationError(e.getMessage());
            return dead;
        }
    }

    @Override
    public boolean isEndOfStream(RawLogEvent nextElement) {
        return false;
    }

    @Override
    public TypeInformation<RawLogEvent> getProducedType() {
        return TypeInformation.of(RawLogEvent.class);
    }
}
