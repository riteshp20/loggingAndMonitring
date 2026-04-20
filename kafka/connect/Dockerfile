FROM confluentinc/cp-kafka-connect:7.6.2

# Pin exact connector versions — never use "latest" in a production image.
#
# kafka-connect-s3 10.5.9:
#   - Supports ByteArrayFormat (store raw JSON bytes, no schema registry needed)
#   - Hive-compatible S3 path partitioning (year=/month=/day=/hour=)
#   - Compatible with LocalStack and real AWS S3 / S3-compatible stores
#   - Requires CP 7.x and Java 11+
#
# kafka-connect-s3-source 2.5.5 (optional; enables replaying archived logs):
#   - Reads .json.gz files from S3 back into a Kafka topic
#   - Useful for reprocessing after anomaly-detector model updates

RUN confluent-hub install --no-prompt confluentinc/kafka-connect-s3:10.5.9 \
 && confluent-hub install --no-prompt confluentinc/kafka-connect-s3-source:2.5.5
