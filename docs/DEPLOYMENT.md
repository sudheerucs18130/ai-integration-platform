# DevOps Deployment Guide

## Local Docker

```bash
docker compose up --build
```

Open:

```text
http://127.0.0.1:8000
```

## GitHub Actions

The workflow in `.github/workflows/ci-cd.yml` does three things:

1. Compiles `server.py`.
2. Starts the app and runs API smoke tests.
3. Builds and pushes a container image to GitHub Container Registry.

Published image format:

```text
ghcr.io/<owner>/<repo>:latest
ghcr.io/<owner>/<repo>:<commit-sha>
```

## Kubernetes or OpenShift

Update the image in:

```text
deploy/kubernetes/deployment.yaml
```

Replace:

```text
ghcr.io/OWNER/autonomous-integration-platform:latest
```

With your actual image:

```text
ghcr.io/<owner>/<repo>:latest
```

Create secrets:

```bash
kubectl apply -f deploy/kubernetes/secret.example.yaml
```

Apply deployment:

```bash
kubectl apply -f deploy/kubernetes/pvc.yaml
kubectl apply -f deploy/kubernetes/deployment.yaml
kubectl apply -f deploy/kubernetes/service.yaml
```

Port forward for testing:

```bash
kubectl port-forward service/autonomous-integration-platform 8000:80
```

## GitHub Push

If you already have a GitHub repository:

```bash
git remote add origin https://github.com/<owner>/<repo>.git
git branch -M main
git push -u origin main
```

If you use SSH:

```bash
git remote add origin git@github.com:<owner>/<repo>.git
git branch -M main
git push -u origin main
```

## Required Secrets For Real IBM Deployment

Configure these in your deployment platform:

```text
IBM_APPLICATIONS
IBM_APPLICATION_URLS
IBM_TELEMETRY_URL
IBM_TELEMETRY_TOKEN
IBM_TELEMETRY_HEADERS
AIP_SOURCE_MODE
AIP_DB_PATH
```

Use platform secrets rather than committing credentials.
