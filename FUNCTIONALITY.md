# Readmission Risk API: Functionality Guide

This guide describes the application's user-facing features and the supporting
model, storage, security, operations, and deployment workflows. For setup
commands, see the [README](README.md). This model is a research and decision
support tool; its output is not a diagnosis or a substitute for clinical
judgment.

## 1. Application at a glance

The project provides:

- A browser sign-in page and a clinical-data prediction form.
- A JSON prediction API that accepts either a browser session or a Bearer API
  token.
- Admin-managed accounts and revocable, expiring API tokens.
- Encrypted MySQL storage for request/response records and audit-event payloads.
- A trained scikit-learn model packaged with the FastAPI application.
- Prometheus metrics, a Grafana dashboard, pod startup measurements, and a
  Kubernetes deployment workflow with rollout rollback.
- Scripts for first-time setup, local Kubernetes port-forwards, model training,
  and deployment validation.

## 2. Browser experience and authentication

### Sign-in and sessions

- `GET /login` displays the sign-in form. `POST /login` checks the submitted
  username and password and creates a database-backed session on success.
- Login submissions use a one-time CSRF cookie/token pair. Invalid or stale
  login CSRF tokens are rejected.
- Passwords are stored as salted scrypt hashes, not plaintext. Usernames are
  normalized to lowercase and must be 3-64 characters using letters, numbers,
  periods, underscores, or hyphens. Passwords must be 12-128 characters.
- Sessions last 8 hours. The browser receives an HttpOnly, SameSite=Strict
  cookie; the session token itself is stored as a hash.
- `POST /logout` revokes the active session and clears its browser cookie. It
  requires CSRF protection.
- `AUTH_COOKIE_SECURE=true` marks the session cookie Secure. Keep HTTPS enabled
  when serving the application outside localhost.

### Prediction form

After sign-in, `GET /` displays the prediction form. It groups fields for
patient and encounter details, admission, clinical history, laboratory results,
and diabetes medications. Submitting the form posts to the internal
`POST /predict_form` route. The route requires a valid session and CSRF token,
validates inputs, and returns the prediction result to the form.

Administrators also see a link to **Account management**. Regular users can
make predictions but cannot manage accounts.

## 3. Prediction API and documentation UI

### `POST /predict`

The endpoint accepts a JSON request with a `features` object and returns a
binary prediction, the positive-class probability, a model name, and a status.
Use `/docs` while signed in to view the generated request schema and try the
endpoint interactively.

Authentication options:

1. **Browser session:** send the session cookie and an `X-CSRF-Token` header.
   The token can be obtained from `GET /auth/csrf`.
2. **Bearer API token:** send `Authorization: Bearer <API_TOKEN>`. This
   server-to-server method does not require a browser session or CSRF header.

Example request (replace the URL and token, and provide values appropriate to
your application):

```powershell
curl.exe -X POST "https://your-api.example.com/predict" `
  -H "Authorization: Bearer <API_TOKEN>" `
  -H "Content-Type: application/json" `
  --data-binary "@prediction.json"
```

`prediction.json` has this shape:

```json
{
  "features": {
    "race": "Caucasian",
    "gender": "Female",
    "age": "[50-60)",
    "admission_type_id": 1,
    "discharge_disposition_id": 1,
    "admission_source_id": 7,
    "time_in_hospital": 4
  }
}
```

The example omits optional fields. The complete feature list is in the next
section and the live schema appears in Swagger UI.

### Prediction request fields

The request must include `race`, `gender`, `age`, `admission_type_id`,
`discharge_disposition_id`, `admission_source_id`, and `time_in_hospital`.
Integer fields shown as nonnegative reject negative values. Other fields are
optional and may be omitted or set to `null`.

| Field | Required | Meaning |
|---|---:|---|
| `encounter_id` | No | Encounter identifier; not used as a model feature. |
| `patient_nbr` | No | Patient identifier; not used as a model feature. |
| `race` | Yes | Race category. |
| `gender` | Yes | Gender category. |
| `age` | Yes | Age band/category, such as `[50-60)`. |
| `weight` | No | Weight range/category. |
| `admission_type_id` | Yes | Admission type code. |
| `discharge_disposition_id` | Yes | Discharge disposition code. |
| `admission_source_id` | Yes | Admission source code. |
| `time_in_hospital` | Yes | Length of stay in days. |
| `payer_code` | No | Payer category/code. |
| `medical_specialty` | No | Medical specialty category. |
| `num_lab_procedures` | No | Number of lab procedures. |
| `num_procedures` | No | Number of procedures. |
| `num_medications` | No | Number of medications. |
| `number_outpatient` | No | Prior outpatient visits. |
| `number_emergency` | No | Prior emergency visits. |
| `number_inpatient` | No | Prior inpatient visits. |
| `diag_1` | No | Primary diagnosis code. |
| `diag_2` | No | Secondary diagnosis code. |
| `diag_3` | No | Tertiary diagnosis code. |
| `number_diagnoses` | No | Number of diagnoses. |
| `max_glu_serum` | No | Maximum serum glucose result/category. |
| `A1Cresult` | No | Hemoglobin A1c result/category. |
| `metformin` | No | Metformin medication status. |
| `repaglinide` | No | Repaglinide medication status. |
| `nateglinide` | No | Nateglinide medication status. |
| `chlorpropamide` | No | Chlorpropamide medication status. |
| `glimepiride` | No | Glimepiride medication status. |
| `acetohexamide` | No | Acetohexamide medication status. |
| `glipizide` | No | Glipizide medication status. |
| `glyburide` | No | Glyburide medication status. |
| `tolbutamide` | No | Tolbutamide medication status. |
| `pioglitazone` | No | Pioglitazone medication status. |
| `rosiglitazone` | No | Rosiglitazone medication status. |
| `acarbose` | No | Acarbose medication status. |
| `miglitol` | No | Miglitol medication status. |
| `troglitazone` | No | Troglitazone medication status. |
| `tolazamide` | No | Tolazamide medication status. |
| `examide` | No | Examide medication status. |
| `citoglipton` | No | Citoglipton medication status. |
| `insulin` | No | Insulin medication status. |
| `glyburide-metformin` | No | Glyburide-metformin medication status. |
| `glipizide-metformin` | No | Glipizide-metformin medication status. |
| `glimepiride-pioglitazone` | No | Glimepiride-pioglitazone medication status. |
| `metformin-rosiglitazone` | No | Metformin-rosiglitazone medication status. |
| `metformin-pioglitazone` | No | Metformin-pioglitazone medication status. |
| `change` | No | Whether diabetes medication changed. |
| `diabetesMed` | No | Whether the patient is on diabetes medication. |

The five medication-combination fields use hyphenated JSON names as shown;
their Python model aliases use underscores.

### Prediction response and status

A successful response resembles:

```json
{
  "prediction": 1,
  "probability": 0.51,
  "model_name": "readmission_risk_model",
  "status": "ok"
}
```

The label is binary. The training target maps source value `<30` to `1`
(readmitted within 30 days), and maps `NO` and `>30` to `0`. `probability` is
the model's estimated probability for class `1`; it is not a calibrated
clinical risk guarantee. A missing model artifact returns HTTP 503. Invalid
input returns HTTP 422; missing/invalid authentication returns HTTP 401, and
missing/invalid session CSRF returns HTTP 403.

### Swagger and OpenAPI access

- `GET /docs` serves Swagger UI only to a signed-in browser.
- `GET /openapi.json` serves the schema only to a signed-in browser.
- Unauthenticated requests to those two paths redirect to `/login`.
- Browser-only routes are omitted from the schema. Swagger sends the session
  cookie and fetches CSRF for mutating session-authenticated requests. Its
  **Authorize** control supports Bearer API tokens.
- The application does not enable ReDoc.

## 4. Account and API-token administration

Administrators use **Account management** (`/admin/users`) to:

- Create users with the `user` or `admin` role.
- Enable or disable accounts.
- Reset a user's password.
- Issue API tokens for active accounts and revoke active tokens.

All management actions require an administrator session and CSRF validation.
The last active administrator cannot be disabled. Disabling an account
invalidates its sessions and revokes its active API tokens. Resetting a
password also invalidates that user's existing sessions and API tokens.

API tokens:

- Are generated with the `rr_` prefix and expire after 90 days.
- Are displayed only once at creation. Copy them to a secure secret store;
  they cannot be retrieved from the database later.
- Are stored as SHA-256 hashes, not in plaintext.
- Can be individually revoked. Expired and revoked tokens cannot authenticate.
- Inherit the account's active status. A token for a disabled account cannot
  be used.

## 5. Health, request logging, storage, and retention

### Health and metrics endpoints

- `GET /health` returns `{"status":"ok"}` and emits a health-check audit event.
  Kubernetes uses this endpoint for startup, readiness, and liveness probes.
- `GET /metrics` exposes Prometheus instrumentation. It is not part of the
  OpenAPI schema.

### Request records and audit events

When `DATABASE_URL` and `APP_DATA_ENCRYPTION_KEY` are configured, the API
stores request/response records and audit events in MySQL. Request metadata
includes the request ID, method, path, status, duration, and timestamp.
Request/response bodies and audit-event payloads are Fernet-encrypted in the
application before being stored. The database still contains metadata such as
paths, status codes, and timestamps.

Structured application logs redact `patient_nbr` and `encounter_id`. The
encrypted database audit payload retains event data for authorized operational
review. Avoid copying decrypted patient data to logs or shared terminals.

The `DATA_RETENTION_DAYS` setting defaults to 30. On startup and once per day,
the application purges older request/audit records, expired sessions, and
expired API tokens. Back up the encryption key separately: without the same
key, stored encrypted payloads cannot be decrypted.

`DATABASE_URL` enables authentication and database persistence. Without it,
the API intentionally fails closed for logins and predictions. When a
database URL is set, `APP_DATA_ENCRYPTION_KEY` must also be set to a valid
Fernet key.

## 6. Model training and inference

### Training data and target

`readmission_model.py` loads the UCI diabetes readmission dataset from `data/`
when available; otherwise it downloads and extracts it. The source's `?`
missing-value marker is converted to missing data. The binary target maps
`readmitted` values as described above. Encounter and patient identifiers,
the original label, and the derived label are excluded from model features.

The training pipeline also produces a compact dataset summary and exploratory
plots for readmission-category distribution, age by readmission category, and
hospital-stay length.

### Candidate models, evaluation, and artifacts

The pipeline compares Logistic Regression, Random Forest, and Gradient
Boosting classifiers. It splits the data into 70% training, 15% validation, and
15% test subsets, stratified by target. Numeric columns are median-imputed and
scaled; categorical columns are most-frequent-imputed and one-hot encoded.

Each candidate is measured using accuracy, precision, recall, F1, and ROC AUC.
The selected candidate maximizes validation ROC AUC, with validation F1 as a
tie-breaker. The selected pipeline is refit on the training plus validation
data, then evaluated on the held-out test set. Timestamped model and
evaluation-report artifacts preserve each training run.

The API loads only
`artifacts/final_readmission_model.joblib` at startup/inference. Training a new
timestamped artifact does not automatically replace that production model or
deploy it. Promotion and deployment are separate, explicit operations. The
current Random Forest and preserved Gradient Boosting backup, where present,
are stored under `artifacts/`; the API does not automatically fail over to a
backup model.

Inference caches the loaded joblib model for the process. Rebuild and redeploy
the API image after changing the model artifact. Only load trusted joblib
artifacts.

## 7. First-time setup and local workflows

### First-time setup

Run from the repository root:

```powershell
python first_time_setup.py --target compose
python first_time_setup.py --target kubernetes
python first_time_setup.py --target both
```

Choose one target. Compose setup creates or fills missing `.env` secrets
without replacing configured values, starts MySQL and the API, and checks
`/health`. Kubernetes setup checks cluster access, creates a database Secret
only when needed, applies MySQL, builds and loads the image named in the API
manifest, deploys the API, and checks rollout and health. An existing Secret
missing required keys is reported rather than silently replaced.

Generated bootstrap credentials are saved in the ignored
`.run/first_time_credentials.json`; keep this file private. The bootstrap admin
is created only once and its password is not reset on later restarts.

### Docker Compose

Compose runs MySQL 8.4 with a persistent named `mysql_data` volume and the API.
Set the required credentials and encryption key in `.env`, then run:

```powershell
docker compose up --build -d
docker compose ps
```

The default API URL is `http://127.0.0.1:8000`. Set `API_PORT` in `.env` to
change the host port. `docker compose down` stops containers but preserves the
database volume; `docker compose down -v` deletes it and its stored data.

### Kubernetes and local port-forwards

Kubernetes uses a MySQL StatefulSet with a 5 GiB persistent-volume claim, a
single API Deployment, and an internal API Service. Database credentials,
encryption key, and bootstrap-admin credentials are read from the
`readmission-db-credentials` Secret in the `default` namespace. The API has
startup, readiness, and liveness probes against `/health`.

After the API and monitoring services are running, `start_services.py`:

- Starts the readmission model training script if it is not already running.
- Creates local port-forwards for the API (`8001`), Grafana (`3001`),
  Prometheus (`9091`), and Pushgateway (`9092`).
- Starts the Kubernetes pod startup-time capture process.
- Leaves matching processes already running untouched and records processes
  it starts under `.run/`, with logs under `.run/logs/`.

`stop_services.py` stops only processes recorded as started by that script; it
does not stop unrelated processes. Stopping a training run early discards
unfinished training work.

## 8. Monitoring and performance testing

### Prometheus and Grafana

Prometheus scrapes the API's `/metrics` endpoint every 15 seconds. The
monitoring manifests install Prometheus, Grafana, and Pushgateway, with a
provisioned Prometheus datasource. The dashboard includes:

- API latency, including p95 latency by route and overall.
- Kubernetes pod creation-to-readiness time for the API, Prometheus, and
  Grafana. `capture_startup.py` records these measurements in
  `.run/logs/cold_starts.jsonl` and publishes them to Pushgateway. This measures
  pod readiness, not model loading or first-prediction latency.
- Rolling 7-day SLO indicators: at least 99.5% of requests avoid a 5xx
  response, and p95 latency is at most 500 ms. 4xx responses count as
  available.
- The latest deployment rollback result when deployments use
  `deploy_with_rollback.py`.
- Audit coverage: completed or failed terminal audit events divided by
  started HTTP request audit events over the last five minutes.

Dashboard panels require the services to be running, Prometheus to scrape the
API, and requests to have occurred. Error panels can be empty when no errors
have occurred.

### Load testing

`locustfile.py` exercises the authenticated prediction flow. Set
`LOCUST_USERNAME` and `LOCUST_PASSWORD` to an active account; each virtual user
signs in, obtains CSRF, then submits a valid prediction request. Locust treats
non-200 responses and invalid prediction values as failures. The README
contains interactive and headless Locust commands.

### Automated checks

The test suite covers model/data behavior, auth and token lifecycle, API
endpoints, setup, and deployment rollback behavior. Run:

```powershell
python -m pytest
python -m ruff check .
```

## 9. Deployment and rollback

### First-time and routine deployment

The first-time Kubernetes workflow builds and loads the image specified in
`k8s/readmission-api.yml`. For Docker Desktop Kubernetes, the image is imported
into the control-plane container's containerd store; for kind, the kind image
loader is used. Other clusters need an image registry reachable by their nodes.

For routine deployment, build a uniquely tagged image, load or publish it to
the cluster, update the manifest image tag, and use
`deploy_with_rollback.py <image:tag>`. The script updates the Deployment and
waits for rollout. If rollout fails, it attempts `kubectl rollout undo`, waits
for the prior rollout, and publishes the rollback result to Pushgateway.

### Git push gate and CI

The `.githooks/pre-push` hook can be enabled with:

```powershell
git config core.hooksPath .githooks
```

When configured, pushes of local `master` are gated by tests, Ruff, image
build, Kubernetes rollout with rollback, and an in-pod API health check. Other
branches and tags are not gated. The check requires a clean working tree and
the checked-out commit to be the pushed `master` HEAD. The GitHub Actions
workflow runs tests, Ruff, and Docker build on a Windows self-hosted runner; it
does not perform deployment.

## 10. Configuration and security responsibilities

| Setting | Purpose |
|---|---|
| `DATABASE_URL` | SQLAlchemy connection string. Enables database-backed auth, storage, and retention. |
| `MYSQL_ROOT_PASSWORD` | MySQL root password used by the Compose/Kubernetes database. |
| `MYSQL_PASSWORD` | MySQL application-user password. |
| `APP_DATA_ENCRYPTION_KEY` | Fernet key for encrypted request/response and audit payloads. |
| `DATA_RETENTION_DAYS` | Age cutoff for stored request/audit data; defaults to 30. |
| `AUTH_BOOTSTRAP_ADMIN_USERNAME` | Initial administrator name, used only when creating the bootstrap account. |
| `AUTH_BOOTSTRAP_ADMIN_PASSWORD` | Initial administrator password, used only when creating the bootstrap account. |
| `AUTH_COOKIE_SECURE` | Whether browser session cookies require HTTPS; defaults to `true`. |
| `API_PORT` | Compose host port for the API; defaults to `8000`. |
| `LOCUST_USERNAME` / `LOCUST_PASSWORD` | Active account credentials used by the Locust load test. |

Keep `.env`, Kubernetes Secrets, generated bootstrap credentials, API tokens,
and the encryption key private. Use HTTPS for network access; do not expose
MySQL or monitoring administration interfaces to untrusted networks. Back up
the encryption key securely and separately from the database. Admins should
revoke exposed tokens and issue replacements.
