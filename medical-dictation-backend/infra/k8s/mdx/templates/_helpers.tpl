{{- define "mdx.labels" -}}
app.kubernetes.io/part-of: mdx
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "mdx.image" -}}
{{- if .root.Values.global.imageRegistry -}}
{{ .root.Values.global.imageRegistry }}/{{ .image }}
{{- else -}}
{{ .image }}
{{- end -}}
{{- end -}}
