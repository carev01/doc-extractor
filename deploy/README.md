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

## Uninstall
```bash
helm uninstall docextractor --namespace docextractor
```
> **Note:** The ConfigMap (`docextractor-config`) and Secret (`docextractor-secret`) are
> Helm hook resources (needed so the pre-install migration Job has its env before the
> main Deployments start). Because of this, `helm uninstall` leaves them behind.
> Clean them up manually if you are removing the namespace:
> ```bash
> kubectl -n docextractor delete configmap docextractor-config docextractor-frontend-nginx \
>   secret docextractor-secret
> # or simply:
> kubectl delete namespace docextractor
> ```
> A re-install handles them automatically via the `before-hook-creation` delete policy.
