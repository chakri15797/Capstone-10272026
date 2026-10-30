# Readmission Risk API

FastAPI service that returns readmission risk predictions using the model in `artifacts/`. The API exposes request metrics for Prometheus and is configured for Kubernetes deployment with Grafana dashboards.

For a complete explanation of the application's browser, API, authentication, token, storage, model, monitoring, setup, and deployment functionality, see the [Functionality Guide](FUNCTIONALITY.md).

## Run Locally

Requirements: Python 3.12+ and Docker (optional for the Docker workflow).

### Start or stop the complete local workflow

The cross-platform Python scripts below start model training and create local port-forwards to the existing Kubernetes API and monitoring services. Ensure the Kubernetes cluster and the `readmission-risk-api`, `grafana`, and `prometheus` services are running first. Install the project dependencies, then run these commands from the repository root on Windows, macOS, or Linux:

```powershell
python start_services.py
```

```bash
python3 start_services.py
```

Use the matching Python command to stop only the training and port-forward processes started by the start script:

```powershell
python stop_services.py
```

```bash
python3 stop_services.py
```

The scripts write logs and process state under `.run/`. They leave processes that were already running alone; they also avoid starting another training run if one is already active. The startup capture records each API, Prometheus, and Grafana pod's creation-to-ready duration in `.run/logs/cold_starts.jsonl` and publishes the latest timings to Prometheus through Pushgateway. Apply `k8s/monitoring.yaml` to install Pushgateway and configure Prometheus to scrape it, then re-import the updated Grafana dashboard to see the **Application Cold Start** panel. The watcher records existing ready pods on startup and newly created pods; it measures Kubernetes pod startup through readiness, not first-prediction model loading. Stopping training early discards its unfinished work. Each new training run saves a uniquely named model and matching evaluation report in `artifacts/`, preserving earlier runs. The API loads `artifacts/final_readmission_model.joblib` at startup; rebuild and redeploy the API image after changing that artifact.

The current main model is the Random Forest in `artifacts/final_readmission_model.joblib`; its evaluation is `artifacts/model_evaluation_report.json`. The previous Gradient Boosting model is preserved as `artifacts/backup_gradient_boosting_model.joblib`, with metrics in `artifacts/backup_gradient_boosting_evaluation.json`. This backup is kept for manual promotion; API inference does not automatically fail over between models.

Install the full development and training environment. The form and prediction API fail closed unless a database-backed account store is configured; use the Compose setup below for a ready-to-run local API. A bare `uvicorn` or `docker run` without `DATABASE_URL` and `APP_DATA_ENCRYPTION_KEY` will not accept logins or predictions.

```powershell
python -m pip install -r requirements-dev.txt
```

### First-time setup

Run the setup script from the repository root and choose the environment to initialize:

```powershell
python first_time_setup.py --target compose
python first_time_setup.py --target kubernetes
python first_time_setup.py --target both
```

The script creates a local `.env` for Compose when needed, generates missing secrets without replacing configured values, starts MySQL and the API, and checks `/health`. For Kubernetes, it creates the database Secret only when absent, applies MySQL, builds and loads the API image specified in `k8s/readmission-api.yml`, deploys the API, and verifies health. It will not overwrite an existing Kubernetes Secret; if a Secret exists but lacks required keys, the script stops and identifies the missing key names.

Kubernetes setup requires an active cluster selected in `kubectl`, Docker, and a cluster that can load a locally built image (Docker Desktop Kubernetes or kind). Ensure the cluster has a default StorageClass for the MySQL volume. The generated bootstrap credentials are saved in `.run/first_time_credentials.json`, which is ignored by Git; keep that file private. The bootstrap password initializes an admin only when one does not already exist. For Kubernetes, run `python start_services.py` after setup to open the API at <http://127.0.0.1:8001/login>.

### Run the API with MySQL request/audit storage

The Compose stack runs MySQL with a persistent Docker volume and configures the API to store encrypted request and response bodies, audit events, and per-request status/latency records. Prometheus remains the time-series metrics backend for the existing dashboards. Payload and audit details are encrypted in the application with a Fernet key; keep that key secret and backed up, since losing it makes stored payloads unreadable. Database metadata such as timestamps, endpoint paths, and status codes is not encrypted.

Create a local `.env` file and replace the example values with unique, strong secrets. Choose a bootstrap administrator username and password of at least 12 characters; the initial admin account is created the first time the API connects to MySQL and is not reset on later restarts. If you already have a `.env`, add the auth variables from `.env.example` without replacing your existing secrets. Generate an encryption key after installing the development requirements:

```powershell
Copy-Item .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the generated key as `APP_DATA_ENCRYPTION_KEY` in `.env`, then start the stack:

```powershell
docker compose up --build -d
docker compose ps
```

The sign-in page is at <http://127.0.0.1:8000/login>; use the bootstrap admin account to sign in and open **Account management** to create, disable, or reset user accounts. Passwords are stored as scrypt hashes. Browser sessions expire after 8 hours, use HttpOnly/SameSite cookies, and prediction requests require CSRF protection. `AUTH_COOKIE_SECURE=true` requires HTTPS except on localhost; use HTTPS before exposing the service to other machines. The JSON prediction endpoint accepts the same authenticated session cookie; obtain its CSRF header value from `GET /auth/csrf`.

Swagger UI supports authenticated predictions too: sign in at `/login` in the same browser and host, then open `/docs`. Unauthenticated requests to `/docs` and `/openapi.json` redirect to sign-in. The schema shows only the prediction and health endpoints; browser login, logout, account management, and form routes are omitted. The UI sends the session cookie and automatically obtains and attaches the CSRF header for mutating requests.

For a simpler API client, an administrator can create a 90-day API token from **Account management**. The token is displayed once; store it securely and revoke it from the same page when no longer needed. Send it as a bearer token, which does not require the browser-session CSRF header:

```powershell
curl.exe -X POST "https://your-api.example.com/predict" `
  -H "Authorization: Bearer <API_TOKEN>" `
  -H "Content-Type: application/json" `
  --data-binary "@prediction.json"
```

The JSON file must contain a `features` object with `race`, `gender`, `age`, `admission_type_id`, `discharge_disposition_id`, `admission_source_id`, and `time_in_hospital`; see `/docs` for the full request schema. API tokens are stored as hashes, expire after 90 days, and are revoked when their account is disabled or its password is reset. Use HTTPS for external access.

The API is available at <http://127.0.0.1:8000>. Stop the containers with `docker compose down`; the named `mysql_data` volume is retained. `docker compose down -v` deletes the database volume, user accounts, sessions, and records. Records older than 30 days and expired sessions are purged at startup and daily. Do not commit `.env` or expose MySQL outside the Docker network. For full disk-level protection, also use encrypted host storage for Docker volumes.

If port 8000 is already occupied, set `API_PORT` in `.env` (for example, `API_PORT=18080`) and use that port for the API URL.

#### Inspect stored requests and audit events

Open a MySQL shell inside the Compose network; enter the `MYSQL_PASSWORD` from `.env` when prompted:

```powershell
docker compose exec mysql mysql -ureadmission_app -p readmission
```

List tables and inspect recent request metadata (payloads are encrypted blobs):

```sql
SHOW TABLES;
SELECT request_id, method, path, status_code, duration_ms, created_at
FROM api_request_records
ORDER BY created_at DESC
LIMIT 20;

SELECT request_id, event_type, occurred_at
FROM api_audit_events
ORDER BY occurred_at DESC
LIMIT 20;
```

To decrypt the newest request payloads locally, run this from the repository root after setting the same `APP_DATA_ENCRYPTION_KEY` in your environment (PowerShell loads it from `.env` only when Compose is run, so copy the value into the current shell without committing it):

```powershell
$env:APP_DATA_ENCRYPTION_KEY = '<the APP_DATA_ENCRYPTION_KEY value from .env>'
@'
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from app.data_store import request_records
import os

engine = create_engine(
    "mysql+pymysql://readmission_app:<URL-encode MYSQL_PASSWORD>@127.0.0.1:3306/readmission"
)
cipher = Fernet(os.environ["APP_DATA_ENCRYPTION_KEY"].encode("ascii"))
with engine.connect() as connection:
    rows = connection.execute(
        select(request_records.c.request_id, request_records.c.request_payload,
               request_records.c.response_payload)
        .order_by(request_records.c.created_at.desc())
        .limit(10)
    )
    for row in rows:
        print(row.request_id)
        print("request:", cipher.decrypt(row.request_payload).decode("utf-8", errors="replace"))
        print("response:", cipher.decrypt(row.response_payload).decode("utf-8", errors="replace"))
'@ | python -
```

For a host-side SQLAlchemy connection, temporarily publish MySQL by adding `"3306:3306"` under the MySQL service's `ports` in `compose.yaml`; do not expose that port on an untrusted network. URL-encode the database password when placing it in `DATABASE_URL`. The Fernet key is required to decrypt records; keep it private, back it up separately, and do not paste decrypted health data into logs or shared terminals.

API pages and endpoints:

- Form: <http://127.0.0.1:8000/>
- Swagger UI: <http://127.0.0.1:8000/docs>
- ReDoc: <http://127.0.0.1:8000/redoc>
- Health: <http://127.0.0.1:8000/health>
- Prometheus metrics: <http://127.0.0.1:8000/metrics>

## Development checks

Install the development dependencies with `python -m pip install -r requirements-dev.txt`, then run:

```powershell
python -m pytest
python -m ruff check .
```

The API is organized under `app/`: `main.py` creates the FastAPI application, `routes.py` defines endpoints, `schemas.py` defines request and response models, `prediction.py` handles model inference, `observability.py` configures metrics and audit logging, `data_store.py` persists encrypted request and audit records, and `templates/` contains the prediction form.

The prediction form groups fields into patient, admission, clinical-history, lab-result, and diabetes-medication sections. Race, gender, age, admission type, discharge disposition, admission source, and time in hospital are required in both the form and JSON API; encounter ID, patient number, and weight remain optional.

Patient-level examples scored from the source dataset are kept locally in `examples/readmission_probability_examples.json`. They are intentionally excluded from version control and Docker build contexts.

Each new training run writes train, validation, and test metrics for every candidate model to its evaluation JSON. Candidate selection remains based on validation ROC AUC, with validation F1 as the tie-breaker; the selected model's refit test metrics are also recorded at the report's top level.

## Deploy to Docker Desktop Kubernetes

The API manifest currently references `readmission-risk-api:random-forest-main-20261029-2103`. Docker Desktop Kubernetes uses containerd, so building an image in Docker alone may leave the cluster using an older cached image. First follow **Configure MySQL storage in Kubernetes** below to create credentials and deploy MySQL and the API. Then apply the monitoring manifest:

```powershell
docker build -t readmission-risk-api:random-forest-main-20261029-2103 .
$archive = Join-Path $env:TEMP 'readmission-risk-api.tar'
docker save -o $archive readmission-risk-api:random-forest-main-20261029-2103
docker cp $archive desktop-control-plane:/root/readmission-risk-api.tar
docker exec desktop-control-plane ctr -n k8s.io images import /root/readmission-risk-api.tar
docker exec desktop-control-plane rm -f /root/readmission-risk-api.tar
Remove-Item $archive

kubectl apply -f k8s/monitoring.yaml
kubectl rollout status deployment/prometheus -n monitoring
kubectl rollout status deployment/grafana -n monitoring
```

The `desktop-control-plane` image-import commands are specific to this Docker Desktop Kubernetes setup. For another Kubernetes cluster, push the image to a registry accessible by the nodes and update the image in `k8s/readmission-api.yml`.

### Configure MySQL storage in Kubernetes

Generate credentials, the application encryption key, and a bootstrap admin password, then create/update the Kubernetes Secret before applying the MySQL and API manifests:

```powershell
$dbRootPassword = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$dbPassword = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$adminPassword = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$encryptionKey = python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
$databaseUrl = "mysql+pymysql://readmission_app:$dbPassword@mysql.default.svc.cluster.local:3306/readmission?charset=utf8mb4"
kubectl create secret generic readmission-db-credentials `
  --from-literal="MYSQL_ROOT_PASSWORD=$dbRootPassword" `
  --from-literal="MYSQL_PASSWORD=$dbPassword" `
  --from-literal="DATABASE_URL=$databaseUrl" `
  --from-literal="APP_DATA_ENCRYPTION_KEY=$encryptionKey" `
  --from-literal="AUTH_BOOTSTRAP_ADMIN_USERNAME=admin" `
  --from-literal="AUTH_BOOTSTRAP_ADMIN_PASSWORD=$adminPassword" `
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f k8s/mysql.yaml
kubectl rollout status statefulset/mysql -n default
kubectl apply -f k8s/readmission-api.yml
kubectl rollout status deployment/readmission-risk-api -n default
```

The MySQL StatefulSet uses a 5 GiB persistent volume claim. Ensure the cluster has a default StorageClass or update the claim with the appropriate class. Updating the Secret adds the required bootstrap-admin keys to existing deployments. Keep the generated bootstrap password private and use it to sign in at the API `/login` page; then create named user accounts and reset/disable access through Account management. The bootstrap password initializes the admin once and is not reapplied on later restarts. The API cookie is Secure by default, so browser login requires HTTPS outside localhost. Request/response and audit payloads are application-encrypted; configure Kubernetes Secret encryption at rest and encrypted persistent volumes for protection of credentials and database metadata.

### Rebuild the API image

Use a new tag for each rebuild so Kubernetes cannot reuse an old cached tag. For example, set `$tag` to a new value, then run:

```powershell
$tag = 'metrics-v2'
docker build -t "readmission-risk-api:$tag" .
$archive = Join-Path $env:TEMP 'readmission-risk-api.tar'
docker save -o $archive "readmission-risk-api:$tag"
docker cp $archive desktop-control-plane:/root/readmission-risk-api.tar
docker exec desktop-control-plane ctr -n k8s.io images import /root/readmission-risk-api.tar
docker exec desktop-control-plane rm -f /root/readmission-risk-api.tar
Remove-Item $archive
python deploy_with_rollback.py "readmission-risk-api:$tag"
```

The deployment script waits for rollout success and attempts `kubectl rollout undo` if the new version fails. It publishes the last rollback result to Pushgateway; start the complete workflow first so the Pushgateway port-forward is available on `127.0.0.1:9092`. Update the `image` value in `k8s/readmission-api.yml` to the same new tag so future `kubectl apply` commands keep using that build.

### Gate Git pushes on local Kubernetes checks

The `.githooks/pre-push` hook blocks pushes of `master` until the local checks pass: tests, Ruff, Docker image build, Docker Desktop Kubernetes rollout with rollback, and an in-pod API health check. Pushes of `chakri`, other branches, and tags are not gated. The check requires a clean working tree and the pushed commit to be the checked-out `master` branch's `HEAD`.

Enable the hook once in the repository:

```powershell
git config core.hooksPath .githooks
```

The hook is cross-platform and requires Python 3 with development dependencies, Docker, a reachable Kubernetes cluster configured with `kubectl`, and the existing `readmission-risk-api` deployment, MySQL Secret, and database. For image import, it uses Docker Desktop's running `desktop-control-plane` container when available, or the `kind` CLI for a kind cluster. Use Bash/Git Bash for the hook on Linux, macOS, and Windows. It runs locally on every machine where configured; it does not install itself remotely or protect pushes made from other machines or through GitHub's web interface. Do not push untrusted code while using this hook, since checks build and deploy the checked-out code.

The `.github/workflows/local-ci-cd.yml` workflow separately runs tests, Ruff, and a Docker build on pushes to `master` and `chakri` using a Windows self-hosted runner registered with the upstream repository. It does not deploy; the local pre-push hook gates deployment only for `master`. Registering the Actions runner is optional if only the local push gate is needed.

## Monitoring and Grafana

Prometheus scrapes `readmission-risk-api.default.svc.cluster.local:8000/metrics` every 15 seconds. Grafana's default Prometheus datasource is provisioned by `k8s/monitoring.yaml`.

The dashboard reports the following service success metrics:

- **p95 latency:** API request latency by route and overall; the overall p95 uses the high-resolution histogram.
- **Cold-start time:** Kubernetes pod creation to readiness, captured by `capture_startup.py`.
- **SLO adherence:** Over a rolling 7-day window, at least 99.5% of requests must avoid a 5xx response and p95 latency must be at most 500 ms. 4xx responses count as available requests.
- **Rollback success:** The latest rollback attempt is shown as succeeded or failed. Deployments must use `deploy_with_rollback.py` for this event to be measured.
- **Audit-log coverage:** Terminal audit events (completed or failed) divided by started HTTP request audit events over the last 5 minutes. A failed request is covered when its failure is logged.

Rebuild and redeploy the API after changing its instrumentation so Prometheus receives the new audit and latency metrics. To load the SLO recording rules and dashboard updates, apply `k8s/monitoring.yaml`, restart Prometheus, and re-import `k8s/readmission-api-grafana-dashboard.json`:

```powershell
kubectl apply -f k8s/monitoring.yaml
kubectl rollout restart deployment/prometheus -n monitoring
kubectl rollout status deployment/prometheus -n monitoring
```

To access Grafana locally, run this in a PowerShell terminal and leave it running:

```powershell
kubectl port-forward -n monitoring svc/grafana 3001:3000
```

Open <http://127.0.0.1:3001>. The configured initial Grafana credentials are `admin` / `admin`; change the password before exposing Grafana beyond a local development environment.

Import the dashboard using **Dashboards → New → Import**, then select `k8s/readmission-api-grafana-dashboard.json`. The datasource is named `Prometheus`. Dashboard query data appears after Prometheus scrapes the app and requests have reached the API. The error-rate panel can be empty when no 4xx or 5xx requests have occurred.

The start script forwards Prometheus to port `9091`. To create the same forward manually, use:

```powershell
kubectl port-forward -n monitoring svc/prometheus 9091:9090
```

Then open <http://127.0.0.1:9091/targets> and confirm the `readmission-api` target is `UP`.

If a local port is already in use, choose another local port, for example `13001:3000` for Grafana or `19090:9090` for Prometheus.

## Useful Kubernetes Checks

```powershell
kubectl get pods,services -n default
kubectl get pods,services -n monitoring
kubectl logs -n default deployment/readmission-risk-api --tail=100
kubectl logs -n monitoring deployment/prometheus --tail=100
```

The API service is internal DNS `readmission-risk-api.default.svc.cluster.local:8000`; the Prometheus and Grafana services are in the `monitoring` namespace.

## Tests

Install `requirements-dev.txt` first, then run:

```powershell
python -m pytest -q
```

### Load test the prediction API

With the API available at `http://127.0.0.1:8001` (for example, through the workflow port-forward), start an interactive Locust session:

```powershell
$env:LOCUST_USERNAME = '<account username>'
$env:LOCUST_PASSWORD = '<account password>'
python -m locust -f locustfile.py --host http://127.0.0.1:8001
```

Open <http://127.0.0.1:8089> and set the user count and spawn rate. For a one-minute headless run with 10 users:

```powershell
python -m locust -f locustfile.py --host http://127.0.0.1:8001 --headless -u 10 -r 2 -t 1m
```

Set `LOCUST_USERNAME` and `LOCUST_PASSWORD` to an active account before either run. Each Locust user signs in, obtains a session CSRF token, and then posts a valid full-feature prediction request; non-200 responses or invalid prediction values are marked as failures. Numeric form fields have a browser minimum of zero, and the API also rejects negative integer inputs.
