{{/*
Expand the name of the chart.
*/}}
{{- define "log-monitor.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "log-monitor.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: {{ .Values.global.partOf }}
{{- end }}

{{/*
Selector labels for a component. Usage: include "log-monitor.selectorLabels" (dict "component" "kafka" "root" .)
*/}}
{{- define "log-monitor.selectorLabels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end }}

{{/*
Standard pod labels (selector + chart labels + component version).
*/}}
{{- define "log-monitor.podLabels" -}}
{{ include "log-monitor.selectorLabels" . }}
app.kubernetes.io/part-of: {{ .root.Values.global.partOf }}
{{- end }}

{{/*
Namespace shorthand.
*/}}
{{- define "log-monitor.namespace" -}}
{{ .Values.global.namespace }}
{{- end }}

{{/*
Kafka bootstrap servers — used by every consumer/producer.
*/}}
{{- define "log-monitor.kafkaBootstrap" -}}
{{- $brokers := list -}}
{{- range $i, $e := until (int .Values.kafka.replicas) -}}
{{- $brokers = append $brokers (printf "kafka-%d.kafka-headless.%s.svc.cluster.local:9092" $i $.Values.global.namespace) -}}
{{- end -}}
{{ join "," $brokers }}
{{- end }}

{{/*
OpenSearch URL — always points to the headless service.
*/}}
{{- define "log-monitor.opensearchUrl" -}}
http://opensearch.{{ .Values.global.namespace }}.svc.cluster.local:9200
{{- end }}

{{/*
Redis URL.
*/}}
{{- define "log-monitor.redisUrl" -}}
redis://redis.{{ .Values.global.namespace }}.svc.cluster.local:6379
{{- end }}
