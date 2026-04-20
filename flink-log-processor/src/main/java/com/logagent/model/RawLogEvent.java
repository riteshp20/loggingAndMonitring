package com.logagent.model;

import com.fasterxml.jackson.annotation.JsonAnyGetter;
import com.fasterxml.jackson.annotation.JsonAnySetter;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.io.Serializable;
import java.util.HashMap;
import java.util.Map;

/**
 * Represents a raw record as produced by Fluent Bit into Kafka.
 *
 * The Lua normalizer in Phase 1 already merges many field-name variants, but
 * different services still use different schemas.  All fields are nullable;
 * {@link com.logagent.operators.LogNormalizationFunction} resolves the
 * ambiguity into a single canonical {@link NormalizedLogEvent}.
 *
 * {@link #extra} absorbs any field not explicitly declared here so that
 * nothing is silently dropped before reaching downstream consumers.
 */
@JsonIgnoreProperties(ignoreUnknown = false)   // unknown → extra map below
public class RawLogEvent implements Serializable {

    // ── Timestamp field variants ─────────────────────────────────────────────
    @JsonProperty("@timestamp") private String atTimestamp;
    private String timestamp;
    private String time;

    // ── Level / severity variants ────────────────────────────────────────────
    private String level;
    private String severity;

    @JsonProperty("log_level")  private String logLevel;

    // ── Message variants ─────────────────────────────────────────────────────
    private String message;
    private String msg;
    private String log;

    // ── Service identity ─────────────────────────────────────────────────────
    @JsonProperty("service_name") private String serviceName;
    private String service;

    // ── Infrastructure ────────────────────────────────────────────────────────
    private String hostname;
    private String host;
    private String environment;
    private String env;
    @JsonProperty("agent_version") private String agentVersion;

    // ── Observability ─────────────────────────────────────────────────────────
    @JsonProperty("trace_id")  private String traceId;
    @JsonProperty("span_id")   private String spanId;
    private String logger;
    @JsonProperty("source_file") private String sourceFile;
    private String error;

    // ── Catch-all for everything else (e.g. "http", "user_id", etc.) ─────────
    private final Map<String, Object> extra = new HashMap<>();

    // ── Set by the deserializer — not from Kafka message ─────────────────────
    private transient String rawJson;
    private transient String deserializationError;

    @JsonAnySetter
    public void setExtra(String key, Object value) {
        extra.put(key, value);
    }

    @JsonAnyGetter
    public Map<String, Object> getExtra() {
        return extra;
    }

    // ── Getters ───────────────────────────────────────────────────────────────

    public String getAtTimestamp()      { return atTimestamp; }
    public String getTimestamp()        { return timestamp; }
    public String getTime()             { return time; }
    public String getLevel()            { return level; }
    public String getSeverity()         { return severity; }
    public String getLogLevel()         { return logLevel; }
    public String getMessage()          { return message; }
    public String getMsg()              { return msg; }
    public String getLog()              { return log; }
    public String getServiceName()      { return serviceName; }
    public String getService()          { return service; }
    public String getHostname()         { return hostname; }
    public String getHost()             { return host; }
    public String getEnvironment()      { return environment; }
    public String getEnv()              { return env; }
    public String getAgentVersion()     { return agentVersion; }
    public String getTraceId()          { return traceId; }
    public String getSpanId()           { return spanId; }
    public String getLogger()           { return logger; }
    public String getSourceFile()       { return sourceFile; }
    public String getError()            { return error; }
    public String getRawJson()          { return rawJson; }
    public String getDeserializationError() { return deserializationError; }

    // ── Setters ───────────────────────────────────────────────────────────────

    public void setAtTimestamp(String v)      { this.atTimestamp = v; }
    public void setTimestamp(String v)        { this.timestamp = v; }
    public void setTime(String v)             { this.time = v; }
    public void setLevel(String v)            { this.level = v; }
    public void setSeverity(String v)         { this.severity = v; }
    public void setLogLevel(String v)         { this.logLevel = v; }
    public void setMessage(String v)          { this.message = v; }
    public void setMsg(String v)              { this.msg = v; }
    public void setLog(String v)              { this.log = v; }
    public void setServiceName(String v)      { this.serviceName = v; }
    public void setService(String v)          { this.service = v; }
    public void setHostname(String v)         { this.hostname = v; }
    public void setHost(String v)             { this.host = v; }
    public void setEnvironment(String v)      { this.environment = v; }
    public void setEnv(String v)              { this.env = v; }
    public void setAgentVersion(String v)     { this.agentVersion = v; }
    public void setTraceId(String v)          { this.traceId = v; }
    public void setSpanId(String v)           { this.spanId = v; }
    public void setLogger(String v)           { this.logger = v; }
    public void setSourceFile(String v)       { this.sourceFile = v; }
    public void setError(String v)            { this.error = v; }
    public void setRawJson(String v)          { this.rawJson = v; }
    public void setDeserializationError(String v) { this.deserializationError = v; }
}
