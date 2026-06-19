{{- define "docextractor.fullname" -}}
{{- printf "%s-%s" .Release.Name "docextractor" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "docextractor.labels" -}}
app.kubernetes.io/name: docextractor
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- /* selectorLabels expects a dict: (dict "ctx" . "component" "backend") */ -}}
{{- define "docextractor.selectorLabels" -}}
app.kubernetes.io/name: docextractor
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- /* Service name of the backend (Firecrawl webhook + nginx upstream target). */ -}}
{{- define "docextractor.backendServiceName" -}}
{{ .Release.Name }}-backend
{{- end -}}

{{- /* postgres Service name */ -}}
{{- define "docextractor.postgresServiceName" -}}
{{ .Release.Name }}-postgres
{{- end -}}

{{- define "docextractor.databaseUrl" -}}
{{- if .Values.postgres.enabled -}}
postgresql+asyncpg://{{ .Values.postgres.user }}:{{ .Values.postgres.password }}@{{ include "docextractor.postgresServiceName" . }}:5432/{{ .Values.postgres.database }}
{{- else -}}
{{ .Values.externalDatabaseUrl }}
{{- end -}}
{{- end -}}

{{- define "docextractor.databaseUrlSync" -}}
{{- if .Values.postgres.enabled -}}
postgresql+psycopg2://{{ .Values.postgres.user }}:{{ .Values.postgres.password }}@{{ include "docextractor.postgresServiceName" . }}:5432/{{ .Values.postgres.database }}
{{- else -}}
{{ .Values.externalDatabaseUrlSync }}
{{- end -}}
{{- end -}}
