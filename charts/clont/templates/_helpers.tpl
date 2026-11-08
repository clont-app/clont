{{- define "clont.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "clont.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "clont.labels" -}}
app.kubernetes.io/name: {{ include "clont.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "clont.selectorLabels" -}}
app.kubernetes.io/name: {{ include "clont.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "clont.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "clont.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* one config source only, and it is the pod's whole configuration */}}
{{- define "clont.configSource" -}}
{{- $n := 0 -}}
{{- if .Values.existingConfigMap }}{{ $n = add1 $n }}{{ end -}}
{{- if .Values.existingSecret }}{{ $n = add1 $n }}{{ end -}}
{{- if gt $n 1 -}}
{{- fail "set only one of existingConfigMap / existingSecret" -}}
{{- end -}}
{{- if .Values.existingConfigMap -}}configMap
{{- else if .Values.existingSecret -}}secret
{{- else -}}chart
{{- end -}}
{{- end -}}
