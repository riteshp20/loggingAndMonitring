# ── Stage 1: Build fat JAR ────────────────────────────────────────────────────
FROM maven:3.9.14-eclipse-temurin-25 AS build

WORKDIR /build

# Cache the dependency layer separately — only re-downloads when pom.xml changes.
COPY pom.xml .
RUN mvn dependency:go-offline --batch-mode -q

COPY src/ src/
RUN mvn package -DskipTests --batch-mode -q

# ── Stage 2: Flink runtime ────────────────────────────────────────────────────
FROM flink:1.18.1-scala_2.12-java17

# ── S3 filesystem plugin ──────────────────────────────────────────────────────
# flink-s3-fs-hadoop uses the Hadoop S3A connector.  It must live in the
# plugins/ directory so Flink loads it in an isolated classloader — placing
# it on the regular classpath causes conflicts with Hadoop's own Hadoop FS.
#
# The JAR is shipped in /opt/flink/opt/ in the official image; we just move it
# to the correct plugin directory.
RUN mkdir -p /opt/flink/plugins/s3-fs-hadoop \
 && cp /opt/flink/opt/flink-s3-fs-hadoop-*.jar /opt/flink/plugins/s3-fs-hadoop/

# ── Copy the fat JAR ──────────────────────────────────────────────────────────
COPY --from=build /build/target/flink-log-processor-*.jar \
     /opt/flink/usrlib/flink-log-processor.jar

# ── Copy log4j2 config ────────────────────────────────────────────────────────
# Overrides the default Flink log4j2 so our package loggers are quieted
# and the job's own loggers are at INFO.
COPY src/main/resources/log4j2.properties \
     /opt/flink/conf/log4j-console.properties
