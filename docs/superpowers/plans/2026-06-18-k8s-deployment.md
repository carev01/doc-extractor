# Kubernetes Deployment (Helm chart) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy DocExtractor on the homelab k3s cluster via a Helm chart (+ rendered plain manifests + a ghcr image-build workflow).

**Architecture:** A single umbrella Helm chart under `deploy/helm/docextractor/` renders all components: in-cluster Postgres StatefulSet, backend (web) + worker co-located on RWO storage, scheduler, frontend behind a Traefik Ingress, a pre-upgrade migration hook Job, and a ConfigMap/Secret pair. Firecrawl is external (already in the cluster); the backend exposes a Service that Firecrawl calls back via in-cluster DNS.

**Tech Stack:** Helm 3, Kubernetes (k3s + Traefik), VMware CSI (RWO), GitHub Actions + ghcr, existing FastAPI/uvicorn + worker/scheduler images.

## Global Constraints

- Chart path: `deploy/helm/docextractor/`. Release-name-prefixed resources via `_helpers.tpl`.
- Storage is **RWO block only** (VMware CSI): `exports` + `media` PVCs are `ReadWriteOnce`; **backend and worker each `replicas: 1`** and the worker uses **podAffinity (required, topologyKey `kubernetes.io/hostname`) to co-locate with the backend pod** so both attach the same RWO volumes on one node.
- Images: `ghcr.io/carev01/doc-extractor-backend` and `ghcr.io/carev01/doc-extractor-frontend`; `pullPolicy: IfNotPresent`; no imagePullSecret by default (public packages).
- Ingress: Traefik (`ingressClassName: traefik`), host `docextractor.k3s.home.lan`, **HTTP** (TLS rendered only when `ingress.tls.enabled`).
- Webhook URL must be the in-cluster backend Service DNS: `http://<release>-backend.<namespace>.svc.cluster.local:8000`.
- DB URLs (which embed the password) live in the **Secret**; non-secret env in the **ConfigMap**; all app pods use `envFrom: [configMapRef, secretRef]`.
- Migrations run only via the Helm hook Job (`alembic upgrade head`), never in the backend container CMD.
- App env var names are exactly: `DOCEXTRACTOR_DATABASE_URL`, `DOCEXTRACTOR_DATABASE_URL_SYNC`, `DOCEXTRACTOR_FIRECRAWL_API_URL`, `DOCEXTRACTOR_FIRECRAWL_API_KEY`, `DOCEXTRACTOR_WEBHOOK_BASE_URL`, `DOCEXTRACTOR_CORS_ORIGINS`, `DOCEXTRACTOR_LLM_FALLBACK_ENABLED`, `DOCEXTRACTOR_LLM_PROVIDER`, `DOCEXTRACTOR_LLM_BASE_URL`, `DOCEXTRACTOR_LLM_API_KEY`, `DOCEXTRACTOR_LLM_MODEL`.
- Validation per task: `helm lint deploy/helm/docextractor` and `helm template R deploy/helm/docextractor -f deploy/helm/docextractor/values-homelab.yaml` must succeed (no templates installed/applied to a cluster in this environment). Use `helm` if present; if `helm` is not installed, install it (`curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash`) or note it and validate YAML with `python -c "import yaml,sys; list(yaml.safe_load_all(open(f)))"` on each rendered doc.
- Branch `feat/k8s-deploy`. Conventional commits.

---

### Task 1: Backend Dockerfile — move migrations out of CMD

**Files:** Modify `backend/Dockerfile`

- [ ] **Step 1:** Change the final `CMD` line from:
```dockerfile
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
```
to:
```dockerfile
# Migrations run as a separate step (docker-compose still runs them inline via
# its own command override; Kubernetes runs them as a Helm pre-upgrade Job).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```
Also remove the now-stale comment block above it that says "For Kubernetes, replace this with an init container…".

- [ ] **Step 2:** Keep docker-compose working: in `docker-compose.yml`, the `backend` service must still apply migrations. Add an explicit command to the `backend` service so compose behavior is unchanged:
```yaml
  backend:
    build: ./backend
    command: ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
```
(Insert the `command:` line right after `build: ./backend`. The other compose services and env are unchanged.)

- [ ] **Step 3: Verify** the backend image still builds:
Run: `cd backend && docker build -t docextractor-backend:plan-check . 2>&1 | tail -5`
Expected: build succeeds (ends with a success line / image id). If Docker is unavailable in this environment, instead verify the Dockerfile parses by checking the final two lines are the new CMD + no `alembic` in CMD: `tail -3 backend/Dockerfile`.

- [ ] **Step 4: Commit**
```bash
git add backend/Dockerfile docker-compose.yml
git commit -m "build(backend): move alembic migration out of image CMD (k8s runs it as a Job)"
```

---

### Task 2: Chart scaffold — Chart.yaml, values, helpers, .helmignore

**Files:**
- Create: `deploy/helm/docextractor/Chart.yaml`
- Create: `deploy/helm/docextractor/.helmignore`
- Create: `deploy/helm/docextractor/templates/_helpers.tpl`
- Create: `deploy/helm/docextractor/values.yaml`
- Create: `deploy/helm/docextractor/values-homelab.yaml`

**Produces (used by every later task):** the helper template names `docextractor.fullname`, `docextractor.labels`, `docextractor.selectorLabels` (per-component via a `component` arg), `docextractor.databaseUrl`, `docextractor.databaseUrlSync`; the `.Values` structure below.

- [ ] **Step 1:** `Chart.yaml`:
```yaml
apiVersion: v2
name: docextractor
description: DocExtractor — vendor documentation extraction (FastAPI + worker + scheduler + React)
type: application
version: 0.1.0
appVersion: "1.0.0"
```

- [ ] **Step 2:** `.helmignore`:
```
.git
*.md
.DS_Store
```

- [ ] **Step 3:** `templates/_helpers.tpl`:
```yaml
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
```

- [ ] **Step 4:** `values.yaml` (documented defaults):
```yaml
# -- Image settings (ghcr; public packages so no pull secret needed)
image:
  backend:
    repository: ghcr.io/carev01/doc-extractor-backend
    tag: latest
  frontend:
    repository: ghcr.io/carev01/doc-extractor-frontend
    tag: latest
  pullPolicy: IfNotPresent
imagePullSecrets: []   # e.g. [{ name: ghcr-creds }] if packages are private

# -- In-cluster PostgreSQL (set enabled=false to use an external DB)
postgres:
  enabled: true
  image: postgres:16-alpine
  user: docextractor
  password: docextractor_dev      # OVERRIDE in values-homelab / --set
  database: docextractor
  storageClassName: ""            # "" = cluster default; set your VMware CSI class
  storageSize: 10Gi
externalDatabaseUrl: ""           # used only when postgres.enabled=false
externalDatabaseUrlSync: ""

# -- Shared RWO storage for exports + media (written by worker, served by backend)
storage:
  storageClassName: ""            # your VMware CSI (RWO) class; "" = default
  accessMode: ReadWriteOnce       # RWX later -> ReadWriteMany (then drop worker podAffinity)
  exportsSize: 5Gi
  mediaSize: 5Gi

# -- Firecrawl (external, already in the cluster)
firecrawl:
  apiUrl: http://firecrawl.k3s.home.lan
  apiKey: ""                      # OVERRIDE

# -- LLM fallback (off by default)
llm:
  fallbackEnabled: false
  provider: anthropic
  baseUrl: ""
  model: ""
  apiKey: ""

# -- Ingress (Traefik)
ingress:
  enabled: true
  className: traefik
  host: docextractor.k3s.home.lan
  tls:
    enabled: false
    secretName: docextractor-tls

# -- Replicas (backend/worker pinned to 1 due to RWO; see storage)
replicas:
  backend: 1
  worker: 1
  scheduler: 1
  frontend: 1

resources:
  backend:   { requests: { cpu: 100m, memory: 256Mi }, limits: { cpu: "1", memory: 1Gi } }
  worker:    { requests: { cpu: 100m, memory: 256Mi }, limits: { cpu: "1", memory: 1Gi } }
  scheduler: { requests: { cpu: 50m,  memory: 128Mi }, limits: { cpu: 250m, memory: 256Mi } }
  frontend:  { requests: { cpu: 25m,  memory: 32Mi },  limits: { cpu: 100m, memory: 128Mi } }
  postgres:  { requests: { cpu: 100m, memory: 256Mi }, limits: { cpu: "1", memory: 1Gi } }
```

- [ ] **Step 5:** `values-homelab.yaml` (concrete overrides; leave secrets blank for the operator to `--set`):
```yaml
postgres:
  password: "CHANGE_ME"
  storageClassName: ""        # set to your VMware CSI RWO StorageClass name
storage:
  storageClassName: ""        # set to your VMware CSI RWO StorageClass name
firecrawl:
  apiUrl: http://firecrawl.k3s.home.lan
  apiKey: "CHANGE_ME"
ingress:
  host: docextractor.k3s.home.lan
image:
  backend: { tag: latest }
  frontend: { tag: latest }
```

- [ ] **Step 6: Validate**
Run: `helm lint deploy/helm/docextractor` → Expected: `1 chart(s) linted, 0 chart(s) failed` (warnings about missing icon are fine). (No templates yet besides helpers — lint passes on an empty-template chart with values.)

- [ ] **Step 7: Commit**
```bash
git add deploy/helm/docextractor/Chart.yaml deploy/helm/docextractor/.helmignore deploy/helm/docextractor/templates/_helpers.tpl deploy/helm/docextractor/values.yaml deploy/helm/docextractor/values-homelab.yaml
git commit -m "feat(deploy): Helm chart scaffold (Chart, values, helpers)"
```

---

### Task 3: ConfigMap + Secret

**Files:**
- Create: `deploy/helm/docextractor/templates/configmap.yaml`
- Create: `deploy/helm/docextractor/templates/secret.yaml`

**Consumes:** `docextractor.databaseUrl`, `docextractor.databaseUrlSync`, `docextractor.backendServiceName`, labels helpers.
**Produces:** ConfigMap named `{{ .Release.Name }}-config` and Secret named `{{ .Release.Name }}-secret`, both consumed via `envFrom` by all app pods (Tasks 5–8).

- [ ] **Step 1:** `configmap.yaml`:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-config
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
data:
  DOCEXTRACTOR_FIRECRAWL_API_URL: {{ .Values.firecrawl.apiUrl | quote }}
  DOCEXTRACTOR_WEBHOOK_BASE_URL: "http://{{ include "docextractor.backendServiceName" . }}.{{ .Release.Namespace }}.svc.cluster.local:8000"
  DOCEXTRACTOR_CORS_ORIGINS: "http://{{ .Values.ingress.host }}{{ if .Values.ingress.tls.enabled }},https://{{ .Values.ingress.host }}{{ end }}"
  DOCEXTRACTOR_LLM_FALLBACK_ENABLED: {{ .Values.llm.fallbackEnabled | quote }}
  DOCEXTRACTOR_LLM_PROVIDER: {{ .Values.llm.provider | quote }}
  DOCEXTRACTOR_LLM_BASE_URL: {{ .Values.llm.baseUrl | quote }}
  DOCEXTRACTOR_LLM_MODEL: {{ .Values.llm.model | quote }}
```

- [ ] **Step 2:** `secret.yaml` (`stringData` so values aren't pre-encoded; Kubernetes encodes them):
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Release.Name }}-secret
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
type: Opaque
stringData:
  DOCEXTRACTOR_DATABASE_URL: {{ include "docextractor.databaseUrl" . | quote }}
  DOCEXTRACTOR_DATABASE_URL_SYNC: {{ include "docextractor.databaseUrlSync" . | quote }}
  DOCEXTRACTOR_FIRECRAWL_API_KEY: {{ .Values.firecrawl.apiKey | quote }}
  DOCEXTRACTOR_LLM_API_KEY: {{ .Values.llm.apiKey | quote }}
{{- if .Values.postgres.enabled }}
  POSTGRES_PASSWORD: {{ .Values.postgres.password | quote }}
{{- end }}
```

- [ ] **Step 3: Validate**
Run: `helm template R deploy/helm/docextractor -f deploy/helm/docextractor/values-homelab.yaml --set firecrawl.apiKey=x --set postgres.password=y -s templates/configmap.yaml -s templates/secret.yaml`
Expected: renders a ConfigMap with the webhook URL `http://R-backend.default.svc.cluster.local:8000` and a Secret whose `DOCEXTRACTOR_DATABASE_URL` is `postgresql+asyncpg://docextractor:y@R-postgres:5432/docextractor`.

- [ ] **Step 4: Commit**
```bash
git add deploy/helm/docextractor/templates/configmap.yaml deploy/helm/docextractor/templates/secret.yaml
git commit -m "feat(deploy): ConfigMap + Secret (env, DB URLs, webhook Service DNS)"
```

---

### Task 4: Postgres StatefulSet + headless Service

**Files:**
- Create: `deploy/helm/docextractor/templates/postgres-statefulset.yaml`
- Create: `deploy/helm/docextractor/templates/postgres-service.yaml`

**Consumes:** `docextractor.postgresServiceName`, labels/selectorLabels, Secret `{{ .Release.Name }}-secret` (`POSTGRES_PASSWORD`).

- [ ] **Step 1:** `postgres-service.yaml` (headless):
```yaml
{{- if .Values.postgres.enabled }}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "docextractor.postgresServiceName" . }}
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
spec:
  clusterIP: None
  selector:
    {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "postgres") | nindent 4 }}
  ports:
    - name: postgres
      port: 5432
      targetPort: 5432
{{- end }}
```

- [ ] **Step 2:** `postgres-statefulset.yaml`:
```yaml
{{- if .Values.postgres.enabled }}
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ .Release.Name }}-postgres
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
spec:
  serviceName: {{ include "docextractor.postgresServiceName" . }}
  replicas: 1
  selector:
    matchLabels:
      {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "postgres") | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "postgres") | nindent 8 }}
    spec:
      containers:
        - name: postgres
          image: {{ .Values.postgres.image }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          env:
            - name: POSTGRES_DB
              value: {{ .Values.postgres.database | quote }}
            - name: POSTGRES_USER
              value: {{ .Values.postgres.user | quote }}
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Release.Name }}-secret
                  key: POSTGRES_PASSWORD
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
          ports:
            - containerPort: 5432
          readinessProbe:
            exec: { command: ["pg_isready", "-U", "{{ .Values.postgres.user }}"] }
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            exec: { command: ["pg_isready", "-U", "{{ .Values.postgres.user }}"] }
            initialDelaySeconds: 30
            periodSeconds: 20
          resources:
            {{- toYaml .Values.resources.postgres | nindent 12 }}
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        {{- if .Values.postgres.storageClassName }}
        storageClassName: {{ .Values.postgres.storageClassName }}
        {{- end }}
        resources:
          requests:
            storage: {{ .Values.postgres.storageSize }}
{{- end }}
```

- [ ] **Step 3: Validate**
Run: `helm template R deploy/helm/docextractor -f deploy/helm/docextractor/values-homelab.yaml --set firecrawl.apiKey=x --set postgres.password=y -s templates/postgres-statefulset.yaml -s templates/postgres-service.yaml`
Expected: a StatefulSet `R-postgres` (1 replica, volumeClaimTemplate `data` RWO) + a headless Service. Also confirm gating: add `--set postgres.enabled=false` → both render empty.

- [ ] **Step 4: Commit**
```bash
git add deploy/helm/docextractor/templates/postgres-statefulset.yaml deploy/helm/docextractor/templates/postgres-service.yaml
git commit -m "feat(deploy): in-cluster Postgres StatefulSet + headless Service"
```

---

### Task 5: Shared PVCs + backend Deployment + Service

**Files:**
- Create: `deploy/helm/docextractor/templates/pvc-exports.yaml`
- Create: `deploy/helm/docextractor/templates/pvc-media.yaml`
- Create: `deploy/helm/docextractor/templates/backend-deployment.yaml`
- Create: `deploy/helm/docextractor/templates/backend-service.yaml`

**Produces:** PVCs `{{ .Release.Name }}-exports` and `{{ .Release.Name }}-media`; backend Deployment with selectorLabels component `backend` (the worker's podAffinity targets these); backend Service `{{ .Release.Name }}-backend` on port 8000.

- [ ] **Step 1:** `pvc-exports.yaml`:
```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ .Release.Name }}-exports
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
spec:
  accessModes: ["{{ .Values.storage.accessMode }}"]
  {{- if .Values.storage.storageClassName }}
  storageClassName: {{ .Values.storage.storageClassName }}
  {{- end }}
  resources:
    requests:
      storage: {{ .Values.storage.exportsSize }}
```

- [ ] **Step 2:** `pvc-media.yaml` (identical shape, media name/size):
```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ .Release.Name }}-media
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
spec:
  accessModes: ["{{ .Values.storage.accessMode }}"]
  {{- if .Values.storage.storageClassName }}
  storageClassName: {{ .Values.storage.storageClassName }}
  {{- end }}
  resources:
    requests:
      storage: {{ .Values.storage.mediaSize }}
```

- [ ] **Step 3:** `backend-service.yaml`:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ include "docextractor.backendServiceName" . }}
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "backend") | nindent 4 }}
  ports:
    - name: http
      port: 8000
      targetPort: 8000
```

- [ ] **Step 4:** `backend-deployment.yaml`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-backend
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicas.backend }}
  strategy:
    type: Recreate   # RWO volume can attach to only one pod-set at a time
  selector:
    matchLabels:
      {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "backend") | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "backend") | nindent 8 }}
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets: {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: backend
          image: "{{ .Values.image.backend.repository }}:{{ .Values.image.backend.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
          envFrom:
            - configMapRef: { name: {{ .Release.Name }}-config }
            - secretRef: { name: {{ .Release.Name }}-secret }
          ports:
            - containerPort: 8000
          readinessProbe:
            httpGet: { path: /api/health, port: 8000 }
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet: { path: /api/health, port: 8000 }
            initialDelaySeconds: 30
            periodSeconds: 20
          resources:
            {{- toYaml .Values.resources.backend | nindent 12 }}
          volumeMounts:
            - { name: exports, mountPath: /app/exports }
            - { name: media, mountPath: /app/media }
      volumes:
        - name: exports
          persistentVolumeClaim: { claimName: {{ .Release.Name }}-exports }
        - name: media
          persistentVolumeClaim: { claimName: {{ .Release.Name }}-media }
```

- [ ] **Step 5: Validate**
Run: `helm template R deploy/helm/docextractor -f deploy/helm/docextractor/values-homelab.yaml --set firecrawl.apiKey=x --set postgres.password=y -s templates/backend-deployment.yaml -s templates/backend-service.yaml -s templates/pvc-exports.yaml -s templates/pvc-media.yaml`
Expected: Deployment `R-backend` (envFrom config+secret, mounts exports/media, `/api/health` probes, `strategy: Recreate`), Service on 8000, two RWO PVCs.

- [ ] **Step 6: Commit**
```bash
git add deploy/helm/docextractor/templates/pvc-exports.yaml deploy/helm/docextractor/templates/pvc-media.yaml deploy/helm/docextractor/templates/backend-deployment.yaml deploy/helm/docextractor/templates/backend-service.yaml
git commit -m "feat(deploy): shared RWO PVCs + backend Deployment + Service"
```

---

### Task 6: Worker + Scheduler Deployments

**Files:**
- Create: `deploy/helm/docextractor/templates/worker-deployment.yaml`
- Create: `deploy/helm/docextractor/templates/scheduler-deployment.yaml`

**Consumes:** the backend selectorLabels (component `backend`) for the worker's podAffinity; the exports/media PVCs from Task 5.

- [ ] **Step 1:** `worker-deployment.yaml` (co-located with the backend via required podAffinity; mounts the same RWO PVCs):
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-worker
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicas.worker }}
  strategy:
    type: Recreate
  selector:
    matchLabels:
      {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "worker") | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "worker") | nindent 8 }}
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets: {{- toYaml . | nindent 8 }}
      {{- end }}
      affinity:
        podAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "backend") | nindent 18 }}
              topologyKey: kubernetes.io/hostname
      containers:
        - name: worker
          image: "{{ .Values.image.backend.repository }}:{{ .Values.image.backend.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command: ["python", "-m", "app.worker"]
          envFrom:
            - configMapRef: { name: {{ .Release.Name }}-config }
            - secretRef: { name: {{ .Release.Name }}-secret }
          livenessProbe:
            exec: { command: ["sh", "-c", "pgrep -f 'app.worker' >/dev/null"] }
            initialDelaySeconds: 30
            periodSeconds: 30
          resources:
            {{- toYaml .Values.resources.worker | nindent 12 }}
          volumeMounts:
            - { name: exports, mountPath: /app/exports }
            - { name: media, mountPath: /app/media }
      volumes:
        - name: exports
          persistentVolumeClaim: { claimName: {{ .Release.Name }}-exports }
        - name: media
          persistentVolumeClaim: { claimName: {{ .Release.Name }}-media }
```

- [ ] **Step 2:** `scheduler-deployment.yaml` (no volumes, no affinity):
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-scheduler
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicas.scheduler }}
  selector:
    matchLabels:
      {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "scheduler") | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "scheduler") | nindent 8 }}
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets: {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: scheduler
          image: "{{ .Values.image.backend.repository }}:{{ .Values.image.backend.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command: ["python", "-m", "app.scheduler"]
          envFrom:
            - configMapRef: { name: {{ .Release.Name }}-config }
            - secretRef: { name: {{ .Release.Name }}-secret }
          livenessProbe:
            exec: { command: ["sh", "-c", "pgrep -f 'app.scheduler' >/dev/null"] }
            initialDelaySeconds: 30
            periodSeconds: 30
          resources:
            {{- toYaml .Values.resources.scheduler | nindent 12 }}
```

- [ ] **Step 3: Validate**
Run: `helm template R deploy/helm/docextractor -f deploy/helm/docextractor/values-homelab.yaml --set firecrawl.apiKey=x --set postgres.password=y -s templates/worker-deployment.yaml -s templates/scheduler-deployment.yaml`
Expected: worker Deployment with `podAffinity` matching `app.kubernetes.io/component: backend` on `topologyKey: kubernetes.io/hostname` + exports/media mounts; scheduler Deployment with no volumes/affinity. Pipe to `python -c "import yaml,sys;list(yaml.safe_load_all(sys.stdin))"` to confirm valid YAML.

- [ ] **Step 4: Commit**
```bash
git add deploy/helm/docextractor/templates/worker-deployment.yaml deploy/helm/docextractor/templates/scheduler-deployment.yaml
git commit -m "feat(deploy): worker (co-located podAffinity) + scheduler Deployments"
```

---

### Task 7: Frontend — nginx ConfigMap + Deployment + Service + Ingress

**Files:**
- Create: `deploy/helm/docextractor/templates/frontend-nginx-configmap.yaml`
- Create: `deploy/helm/docextractor/templates/frontend-deployment.yaml`
- Create: `deploy/helm/docextractor/templates/frontend-service.yaml`
- Create: `deploy/helm/docextractor/templates/ingress.yaml`

**Consumes:** `docextractor.backendServiceName` (nginx upstream + same namespace DNS).

- [ ] **Step 1:** `frontend-nginx-configmap.yaml` — the existing `frontend/nginx.conf` with `proxy_pass` pointed at the backend Service (note: nginx needs a resolver or a static upstream; use the Service's short name which resolves within the namespace):
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-frontend-nginx
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
data:
  default.conf: |
    server {
        listen 80;
        root /usr/share/nginx/html;
        index index.html;

        location /api/ {
            proxy_pass http://{{ include "docextractor.backendServiceName" . }}:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_read_timeout 600s;
            proxy_send_timeout 60s;
        }

        location /media/ {
            proxy_pass http://{{ include "docextractor.backendServiceName" . }}:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }

        location / {
            try_files $uri $uri/ /index.html;
        }
    }
```

- [ ] **Step 2:** `frontend-service.yaml`:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ .Release.Name }}-frontend
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "frontend") | nindent 4 }}
  ports:
    - name: http
      port: 80
      targetPort: 80
```

- [ ] **Step 3:** `frontend-deployment.yaml` (mounts the nginx ConfigMap over the default conf):
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-frontend
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicas.frontend }}
  selector:
    matchLabels:
      {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "frontend") | nindent 6 }}
  template:
    metadata:
      annotations:
        checksum/nginx: {{ include (print $.Template.BasePath "/frontend-nginx-configmap.yaml") . | sha256sum }}
      labels:
        {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "frontend") | nindent 8 }}
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets: {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: frontend
          image: "{{ .Values.image.frontend.repository }}:{{ .Values.image.frontend.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - containerPort: 80
          readinessProbe:
            httpGet: { path: /, port: 80 }
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet: { path: /, port: 80 }
            initialDelaySeconds: 15
            periodSeconds: 20
          resources:
            {{- toYaml .Values.resources.frontend | nindent 12 }}
          volumeMounts:
            - name: nginx-conf
              mountPath: /etc/nginx/conf.d/default.conf
              subPath: default.conf
      volumes:
        - name: nginx-conf
          configMap:
            name: {{ .Release.Name }}-frontend-nginx
```

- [ ] **Step 4:** `ingress.yaml`:
```yaml
{{- if .Values.ingress.enabled }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ .Release.Name }}-ingress
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
spec:
  ingressClassName: {{ .Values.ingress.className }}
  {{- if .Values.ingress.tls.enabled }}
  tls:
    - hosts: [{{ .Values.ingress.host | quote }}]
      secretName: {{ .Values.ingress.tls.secretName }}
  {{- end }}
  rules:
    - host: {{ .Values.ingress.host | quote }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ .Release.Name }}-frontend
                port:
                  number: 80
{{- end }}
```

- [ ] **Step 5: Validate**
Run: `helm template R deploy/helm/docextractor -f deploy/helm/docextractor/values-homelab.yaml --set firecrawl.apiKey=x --set postgres.password=y -s templates/frontend-nginx-configmap.yaml -s templates/frontend-deployment.yaml -s templates/frontend-service.yaml -s templates/ingress.yaml`
Expected: nginx ConfigMap whose `proxy_pass` is `http://R-backend:8000`; frontend Deployment mounting it at `default.conf`; Service :80; Ingress host `docextractor.k3s.home.lan`, class `traefik`, no `tls:` block (tls disabled). Confirm `--set ingress.tls.enabled=true` adds the tls block, and `--set ingress.enabled=false` renders the Ingress empty.

- [ ] **Step 6: Commit**
```bash
git add deploy/helm/docextractor/templates/frontend-nginx-configmap.yaml deploy/helm/docextractor/templates/frontend-deployment.yaml deploy/helm/docextractor/templates/frontend-service.yaml deploy/helm/docextractor/templates/ingress.yaml
git commit -m "feat(deploy): frontend Deployment/Service + templated nginx + Traefik Ingress"
```

---

### Task 8: Migration hook Job + NOTES.txt

**Files:**
- Create: `deploy/helm/docextractor/templates/migrate-job.yaml`
- Create: `deploy/helm/docextractor/templates/NOTES.txt`

- [ ] **Step 1:** `migrate-job.yaml` (Helm pre-install/pre-upgrade hook):
```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ .Release.Name }}-migrate
  labels:
    {{- include "docextractor.labels" . | nindent 4 }}
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "0"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: 3
  template:
    metadata:
      labels:
        {{- include "docextractor.selectorLabels" (dict "ctx" . "component" "migrate") | nindent 8 }}
    spec:
      restartPolicy: Never
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets: {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: migrate
          image: "{{ .Values.image.backend.repository }}:{{ .Values.image.backend.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command: ["alembic", "upgrade", "head"]
          envFrom:
            - configMapRef: { name: {{ .Release.Name }}-config }
            - secretRef: { name: {{ .Release.Name }}-secret }
          resources:
            {{- toYaml .Values.resources.scheduler | nindent 12 }}
```
> Note: the hook Job consumes the same Secret/ConfigMap; on `pre-install` Helm creates the hook resources but the Secret/ConfigMap are non-hook — they are installed in the main phase. To guarantee the Secret exists before the hook runs, the Secret and ConfigMap also carry hook annotations so they are created in the pre phase. Add to BOTH `configmap.yaml` and `secret.yaml` metadata.annotations (in Task 3 files — update them now):
```yaml
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": before-hook-creation
```
Apply that annotation block to `configmap.yaml` and `secret.yaml` so they exist (weight -5) before the migrate Job (weight 0). (Because they become hook resources, they are also re-created each upgrade — which is correct, they're derived from values.)

- [ ] **Step 2:** `NOTES.txt`:
```
DocExtractor deployed as release {{ .Release.Name }} in namespace {{ .Release.Namespace }}.

Frontend (Traefik Ingress):
  http://{{ .Values.ingress.host }}

The Firecrawl webhook target (in-cluster) is:
  http://{{ include "docextractor.backendServiceName" . }}.{{ .Release.Namespace }}.svc.cluster.local:8000

Check status:
  kubectl get pods -l app.kubernetes.io/instance={{ .Release.Name }}
  kubectl logs deploy/{{ .Release.Name }}-worker

Migrations ran via the pre-upgrade Job {{ .Release.Name }}-migrate.
```

- [ ] **Step 3: Validate**
Run: `helm template R deploy/helm/docextractor -f deploy/helm/docextractor/values-homelab.yaml --set firecrawl.apiKey=x --set postgres.password=y -s templates/migrate-job.yaml` → Expected: a Job with the three hook annotations + `alembic upgrade head` + envFrom config/secret. Then full render `helm template R deploy/helm/docextractor -f deploy/helm/docextractor/values-homelab.yaml --set firecrawl.apiKey=x --set postgres.password=y | python -c "import yaml,sys;docs=list(yaml.safe_load_all(sys.stdin));print(len(docs),'docs');[d['kind'] for d in docs if d]"` → expect kinds incl. ConfigMap, Secret, Service×3, Deployment×3, StatefulSet, Job, Ingress, PVC×2.

- [ ] **Step 4: Commit**
```bash
git add deploy/helm/docextractor/templates/migrate-job.yaml deploy/helm/docextractor/templates/NOTES.txt deploy/helm/docextractor/templates/configmap.yaml deploy/helm/docextractor/templates/secret.yaml
git commit -m "feat(deploy): alembic migration as Helm pre-upgrade hook Job"
```

---

### Task 9: GitHub Actions — build & push images to ghcr

**Files:** Create `.github/workflows/images.yml`

- [ ] **Step 1:** `.github/workflows/images.yml`:
```yaml
name: Build and push images

on:
  push:
    branches: [main]
    tags: ["v*"]
  workflow_dispatch:

permissions:
  contents: read
  packages: write

jobs:
  images:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        include:
          - name: backend
            context: ./backend
            image: ghcr.io/carev01/doc-extractor-backend
          - name: frontend
            context: ./frontend
            image: ghcr.io/carev01/doc-extractor-frontend
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ matrix.image }}
          tags: |
            type=raw,value=latest,enable={{is_default_branch}}
            type=sha
            type=ref,event=tag
      - uses: docker/build-push-action@v6
        with:
          context: ${{ matrix.context }}
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          build-args: |
            VITE_API_BASE_URL=
```
(The empty `VITE_API_BASE_URL` build-arg is harmless for the backend image and required-empty for the frontend so it uses relative `/api/` paths.)

- [ ] **Step 2: Validate** the workflow YAML parses:
Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/images.yml'))" && echo OK`
Expected: `OK`.

- [ ] **Step 3: Commit**
```bash
git add .github/workflows/images.yml
git commit -m "ci: build and push backend+frontend images to ghcr"
```

---

### Task 10: Rendered manifests + README runbook + final validation

**Files:**
- Create: `deploy/rendered/docextractor.yaml`
- Create: `deploy/README.md`

- [ ] **Step 1: Lint the whole chart**
Run: `helm lint deploy/helm/docextractor`
Expected: `0 chart(s) failed`.

- [ ] **Step 2: Render the full chart to the committed manifest set**
Run:
```bash
helm template docextractor deploy/helm/docextractor \
  -f deploy/helm/docextractor/values-homelab.yaml \
  --namespace docextractor \
  --set postgres.password=CHANGE_ME --set firecrawl.apiKey=CHANGE_ME \
  > deploy/rendered/docextractor.yaml
```
Then sanity-check it parses and has all kinds:
```bash
python -c "import yaml; ds=[d for d in yaml.safe_load_all(open('deploy/rendered/docextractor.yaml')) if d]; print(sorted({d['kind'] for d in ds}))"
```
Expected kinds: `['ConfigMap','Deployment','Ingress','Job','PersistentVolumeClaim','Secret','Service','StatefulSet']`.

- [ ] **Step 3 (if available): schema-validate** with kubeconform (skip cleanly if not installed):
```bash
command -v kubeconform >/dev/null && kubeconform -strict -ignore-missing-schemas deploy/rendered/docextractor.yaml || echo "kubeconform not installed — skipped"
```
Expected: no errors, or the skip message.

- [ ] **Step 4:** `deploy/README.md` — the install/upgrade runbook:
```markdown
# DocExtractor on Kubernetes (k3s)

Helm chart: `deploy/helm/docextractor`. Rendered reference manifests:
`deploy/rendered/docextractor.yaml` (from `values-homelab.yaml`).

## Prerequisites
- k3s with Traefik (default) and a VMware CSI **RWO** StorageClass.
- Firecrawl already running in the cluster (reachable at `http://firecrawl.k3s.home.lan`).
- Images published to ghcr (the `Build and push images` GitHub Action does this on
  push to `main`). Make the ghcr packages public, or set `imagePullSecrets`.

## Install
```bash
kubectl create namespace docextractor
helm upgrade --install docextractor deploy/helm/docextractor \
  --namespace docextractor \
  -f deploy/helm/docextractor/values-homelab.yaml \
  --set postgres.password='<db-password>' \
  --set firecrawl.apiKey='<firecrawl-key>' \
  --set storage.storageClassName='<your-rwo-storageclass>' \
  --set postgres.storageClassName='<your-rwo-storageclass>'
```
Add the host to DNS / `/etc/hosts` → the Traefik ingress IP:
`docextractor.k3s.home.lan`.

## Notes
- **Storage is RWO**: backend + worker are pinned to 1 replica and co-scheduled
  (podAffinity) so they share the `exports`/`media` volumes. To scale them, switch
  to an RWX StorageClass (`--set storage.accessMode=ReadWriteMany --set storage.storageClassName=<rwx>`)
  and the co-location is no longer required.
- Migrations run automatically via the `*-migrate` pre-upgrade hook Job.
- Enable the LLM fallback with `--set llm.fallbackEnabled=true --set llm.provider=openai
  --set llm.apiKey=... --set llm.model=... [--set llm.baseUrl=...]`.
- Enable TLS later with `--set ingress.tls.enabled=true` (provide `ingress.tls.secretName`).

## Verify
```bash
kubectl -n docextractor get pods,svc,ingress,pvc
curl -H 'Host: docextractor.k3s.home.lan' http://<traefik-ip>/api/health
```
```

- [ ] **Step 5: Commit**
```bash
git add deploy/rendered/docextractor.yaml deploy/README.md
git commit -m "docs(deploy): rendered manifests + install/upgrade runbook"
```

---

## Self-Review

**Spec coverage:** chart layout → Tasks 2,7,8,10; postgres StatefulSet+gating → Task 4; backend+worker co-located RWO → Tasks 5,6; scheduler → Task 6; frontend+templated nginx+Ingress → Task 7; ConfigMap/Secret + in-cluster webhook DNS + DB URLs in Secret → Task 3; migrations Helm hook (+secret/configmap as pre-hooks so they exist first) → Task 8; images+CI ghcr → Task 9; Dockerfile CMD change + compose parity → Task 1; rendered manifests + runbook + validation → Task 10; CORS host → ConfigMap `DOCEXTRACTOR_CORS_ORIGINS` (Task 3).

**Placeholder scan:** No "TBD"/"similar to". `CHANGE_ME`/`<...>` appear only as deliberate operator-supplied secret values in the homelab values + runbook (documented), not as plan gaps. Each template has full content.

**Type/name consistency:** `docextractor.backendServiceName` = `{{ .Release.Name }}-backend` is used identically by the backend Service (Task 5), the nginx upstream + webhook URL (Tasks 7,3), and the worker podAffinity matches the backend `selectorLabels` component `backend` (Tasks 5,6). Secret/ConfigMap names `{{ .Release.Name }}-secret`/`-config` consumed by every Deployment + the Job via `envFrom`. PVC names `-exports`/`-media` consistent across Tasks 5,6. `selectorLabels` always called as `(dict "ctx" . "component" "<name>")`.

**Risk note:** Task 8 makes the ConfigMap+Secret Helm hook resources (weight -5) so they exist before the migrate Job (weight 0). Because the app Deployments are NOT hooks, they read the same-named ConfigMap/Secret in the main phase — Helm leaves hook-created resources in place (delete-policy `before-hook-creation` only), so the names resolve. The validation in Task 8/10 (full render + kind list) confirms there is exactly one ConfigMap and one Secret.
