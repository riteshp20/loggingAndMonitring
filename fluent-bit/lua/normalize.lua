-- normalize.lua
-- Canonicalizes log records from heterogeneous sources into a single schema.
--
-- Input shapes this function handles:
--   JSON-sourced  : all fields already at top level (level, message, timestamp, …)
--   Plaintext     : raw line in record["log"]; may contain inline timestamp + level
--
-- Output schema (always present, empty string when absent):
--   @timestamp, message, log_level, logger, trace_id, span_id, error,
--   source_file, extra (table of any unrecognised fields)

-- Map of raw level strings → canonical uppercase form
local LEVEL_CANON = {
    WARN     = "WARNING",
    ERR      = "ERROR",
    CRIT     = "CRITICAL",
    FATAL    = "CRITICAL",
    TRACE    = "DEBUG",
}

-- Keys that are lifted into canonical fields and must not bleed into "extra"
local RESERVED = {
    ["@timestamp"]=1, ["timestamp"]=1, ["time"]=1, ["Time"]=1,
    ["message"]=1,    ["msg"]=1,       ["log"]=1,
    ["level"]=1,      ["severity"]=1,  ["log_level"]=1, ["lvl"]=1,
    ["logger"]=1,     ["logger_name"]=1, ["source"]=1,
    ["trace_id"]=1,   ["traceId"]=1,   ["trace"]=1,
    ["span_id"]=1,    ["spanId"]=1,
    ["error"]=1,      ["exception"]=1, ["stack_trace"]=1,
    ["source_file"]=1,
    ["hostname"]=1,   ["environment"]=1, ["service_name"]=1, ["agent_version"]=1,
}

-- Scan a plaintext line for a known log level token (word-boundary safe).
local LEVEL_SCAN_ORDER = {
    "CRITICAL", "FATAL", "ERROR", "WARNING", "WARN", "INFO", "DEBUG", "TRACE",
}
local function extract_level_from_text(text)
    if not text or text == "" then return nil end
    local upper = string.upper(text)
    for _, lvl in ipairs(LEVEL_SCAN_ORDER) do
        -- match surrounded by non-alpha chars or at string boundaries
        if string.match(upper, "%A" .. lvl .. "%A")
        or string.match(upper, "^"  .. lvl .. "%A")
        or string.match(upper, "%A" .. lvl .. "$")
        or upper == lvl
        then
            return lvl
        end
    end
    return nil
end

-- Attempt to parse a plaintext log line into structured fields.
-- Returns a table with at least {message=…} and optionally {timestamp, log_level, logger}.
local function parse_plaintext(line)
    if not line or line == "" then
        return { message = "" }
    end

    -- Pattern: ISO-timestamp  LEVEL  [optional-logger]  message
    -- e.g. "2024-01-15T12:34:56.789Z INFO [com.example.Svc] Request OK"
    -- e.g. "2024-01-15 12:34:56,123 ERROR Something failed"
    local ts, lvl, logger, msg

    ts, lvl, logger, msg = string.match(line,
        "^(%d%d%d%d%-%d%d%-%d%dT%d%d:%d%d:%d%d[%.%d]*Z?)%s+"
        .. "([A-Z]+)%s+%[([^%]]+)%]%s+(.+)$")
    if ts then
        return { timestamp = ts, log_level = lvl, logger = logger, message = msg }
    end

    ts, lvl, msg = string.match(line,
        "^(%d%d%d%d%-%d%d%-%d%dT%d%d:%d%d:%d%d[%.%d]*Z?)%s+([A-Z]+)%s+(.+)$")
    if ts then
        return { timestamp = ts, log_level = lvl, message = msg }
    end

    -- Space-separated date variant: "2024-01-15 12:34:56,123 ERROR msg"
    ts, lvl, msg = string.match(line,
        "^(%d%d%d%d%-%d%d%-%d%d %d%d:%d%d:%d%d[%.,]?%d*)%s+([A-Z]+)%s+(.+)$")
    if ts then
        return { timestamp = ts, log_level = lvl, message = msg }
    end

    -- No recognisable structure — treat the whole line as the message
    return { message = line }
end

-- ── Public filter function ───────────────────────────────────────────────────

function normalize(tag, timestamp, record)
    local out = {}

    -- Detect whether this came from a plaintext tail (has raw "log" key)
    local raw_line = record["log"]
    local plain = raw_line and parse_plaintext(raw_line) or nil
    local src   = plain or record   -- prefer parsed plain; fall back to record fields

    -- @timestamp: prefer the parsed value, then common field names
    out["@timestamp"] = src["timestamp"]
                     or src["@timestamp"]
                     or src["time"]
                     or record["Time"]
                     or ""

    -- message
    out["message"] = src["message"]
                  or src["msg"]
                  or raw_line
                  or ""

    -- log_level: normalise to canonical form
    local raw_lvl = src["level"]
                 or src["severity"]
                 or src["log_level"]
                 or src["lvl"]
    if not raw_lvl and raw_line then
        raw_lvl = extract_level_from_text(raw_line)
    end
    local lvl_up = string.upper(raw_lvl or "INFO")
    out["log_level"] = LEVEL_CANON[lvl_up] or lvl_up

    -- identity / tracing
    out["logger"]      = src["logger"]   or src["logger_name"] or src["source"] or ""
    out["trace_id"]    = src["trace_id"] or src["traceId"]     or src["trace"]  or ""
    out["span_id"]     = src["span_id"]  or src["spanId"]                       or ""

    -- error detail (keep as-is; may be a string or nested table)
    out["error"]       = src["error"]    or src["exception"]   or src["stack_trace"] or ""
    out["source_file"] = record["source_file"] or ""

    -- Carry forward any non-reserved fields under "extra" to avoid schema pollution
    local extra    = {}
    local has_extra = false
    for k, v in pairs(record) do
        if not RESERVED[k] then
            extra[k]  = v
            has_extra = true
        end
    end
    if has_extra then
        out["extra"] = extra
    end

    return 1, timestamp, out
end
