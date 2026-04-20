FROM fluent/fluent-bit:3.1.7

# Pin to a specific patch release so builds are reproducible.
# fluent/fluent-bit:3.1.7 is Debian-based; curl and netcat are available via apt.

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        curl=7.* \
        netcat-openbsd \
 && rm -rf /var/lib/apt/lists/*

# Runtime state directory; the volume mount will shadow this in compose,
# but it must exist for standalone container runs.
RUN mkdir -p /var/log/fluent-bit/storage \
 && chmod 755 /var/log/fluent-bit

COPY fluent-bit.conf     /fluent-bit/etc/fluent-bit.conf
COPY parsers.conf        /fluent-bit/etc/parsers.conf
COPY lua/                /fluent-bit/etc/lua/
COPY scripts/entrypoint.sh  /usr/local/bin/entrypoint.sh
COPY scripts/healthcheck.sh /usr/local/bin/healthcheck.sh

RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.sh

# Inputs: TCP JSON (5170), UDP JSON (5171), syslog TCP (5140)
# Management: HTTP API + health (2020)
EXPOSE 5170/tcp 5171/udp 5140/tcp 2020/tcp

HEALTHCHECK --interval=15s --timeout=5s --retries=4 --start-period=20s \
    CMD /usr/local/bin/healthcheck.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
