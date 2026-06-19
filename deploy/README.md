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
- Migrations run automatically in the backend pod's `migrate` init container
  (`alembic upgrade head`) before the app container starts. If Postgres is not yet
  reachable the init container retries until it is, so no install ordering is required.
  Inspect with `kubectl logs deploy/<release>-backend -c migrate`.
- Enable the LLM fallback with `--set llm.fallbackEnabled=true --set llm.provider=openai
  --set llm.apiKey=... --set llm.model=... [--set llm.baseUrl=...]`.
- Enable TLS later with `--set ingress.tls.enabled=true` (provide `ingress.tls.secretName`).

## Verify
```bash
kubectl -n docextractor get pods,svc,ingress,pvc
curl -H 'Host: docextractor.k3s.home.lan' http://<traefik-ip>/api/health
```

## Uninstall
```bash
helm uninstall docextractor --namespace docextractor
```
> **Note:** `helm uninstall` deletes the chart's resources, **including** the `docextractor-exports`
> and `docextractor-media` PVCs (their contents are lost). The Postgres `data-*` PVC is created from
> the StatefulSet `volumeClaimTemplate`, which Helm never deletes, so the database survives an
> uninstall/re-install. To reclaim it too, delete the PVC manually or `kubectl delete namespace
> docextractor`.
