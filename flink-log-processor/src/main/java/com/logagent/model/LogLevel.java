package com.logagent.model;

import java.io.Serializable;

/**
 * Canonical log level enum.
 *
 * Normalises the many level representations that appear in the wild
 * (WARN vs WARNING, CRIT vs CRITICAL, etc.) into a single ordered set.
 * The numeric {@code severity} value enables arithmetic comparisons:
 * {@code level.severity >= LogLevel.ERROR.severity}.
 */
public enum LogLevel implements Serializable {

    DEBUG(10),
    INFO(20),
    WARNING(30),
    ERROR(40),
    CRITICAL(50),
    UNKNOWN(0);

    public final int severity;

    LogLevel(int severity) {
        this.severity = severity;
    }

    /**
     * Converts a raw string (from any of the field names the Lua normalizer
     * might produce) to the canonical enum value.  Never throws — returns
     * {@link #UNKNOWN} for null or unrecognised inputs.
     */
    public static LogLevel parse(String raw) {
        if (raw == null || raw.isBlank()) {
            return UNKNOWN;
        }
        return switch (raw.toUpperCase().strip()) {
            case "DEBUG", "TRACE", "VERBOSE"              -> DEBUG;
            case "INFO", "INFORMATION", "NOTICE"          -> INFO;
            case "WARN", "WARNING"                        -> WARNING;
            case "ERROR", "ERR"                           -> ERROR;
            case "CRITICAL", "CRIT", "FATAL", "EMERGENCY" -> CRITICAL;
            default                                       -> UNKNOWN;
        };
    }

    public boolean isAtLeast(LogLevel other) {
        return this.severity >= other.severity;
    }
}
