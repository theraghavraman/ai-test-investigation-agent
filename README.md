# AI Code Testing & Bug Investigation Agent

Production-oriented **single-tenant** Python/FastAPI service for reviewing explicitly approved source/specification files, generating Pytest tests with the **OpenAI Responses API**, and executing those tests inside resource-limited Docker containers. The system stores immutable source snapshots, append-only generated tests and audit events, and separate **test observations** vs. **model hypotheses** in JSON and Markdown reports.

## Architecture

```text
Client --Bearer token--> FastAPI ------------------> PostgreSQL
   |                    |                              | status/report
   | ZIP + allowlist    +-- Celery job --> Redis ------+--> Worker
   |                                                       |
   |                                          OpenAI Responses API (stateless)
   |                                           read_file / write_test / run_tests
   |                                                       |
   |                                      Dedicated Docker daemon (runner host)
   |                                        - network=none; readonly workspace
   |                                        - nonroot; no new privileges
   |                                        - caps dropped; CPU/memory/PID/time limits
   |                                                       |
   +<--------------------------- JSON & Markdown evidence reports
```

### Trust boundaries

1. **API** authenticates with a single server-owned bearer key; use HTTPS termination, per-client throttling and an API gateway before exposing publicly. This implementation intentionally does **not** provide tenant/RBAC isolation.
2. **Archive ingestion** accepts only exact names enumerated by the submitting user in `approved_paths`, canonical relative UTF-8 text files, with size, member-type, duplicate, traversal, and compression-ratio checks. Symlinks and unlisted files are forbidden. Content is snapshotted and hashed.
3. **Model tools** are exactly `read_file`, `write_test`, `run_tests`. `read_file` enforces the allowlist and digests. `write_test` creates only `tests/generated/test_generated_NNN.py` using exclusive creation. No tool can edit/delete approved files or previous tests.
4. **Execution** occurs inside a dedicated Pytest image via Docker CLI with **no external network, read-only workspace, read-only root filesystem, no privileges, UID 65534, disabled plugin auto-loading and resource limits**. Only an isolated artifacts directory is writable. The Docker daemon itself remains privileged; **production should use an isolated runner VM/host and a rootless Docker daemon**. Do not mount a production host Docker socket into the API or a public-facing container.
5. **Reports** contain the hashes of generated test files, JUnit-reported outcomes, bounded stdout, run exit codes and timeouts under `test_evidence`. A distinct `model_analysis` section holds *unverified* hypotheses and recommendations. Generated tests themselves are untrusted; results can be inaccurate or manipulated by malicious code in the sandbox. Passing tests do not prove correctness.
6. **Model data handling:** `store=False` on Responses requests. Reasoning items with encrypted content are replayed for stateless tool use. Approved code and specs are sent to OpenAI **only when the model uses `read_file`**. Review organizational data policies before submitting confidential code.

## Quick start (local Docker Compose)

Preconditions: Docker Engine/CLI + Compose v2, Docker build permissions, a valid OpenAI API key (or a local model server, see below), and Linux or a Docker-compatible setup in which the worker and Docker daemon see the **same absolute host data directory**.

```bash
cp .env.example .env
# Edit .env and replace ALL example secrets.
export HOST_DATA_DIR="$(pwd)/runtime-data"
mkdir -p "$HOST_DATA_DIR"
# The web container runs UID 10001, so allow it to create project files:
sudo chown 10001:10001 "$HOST_DATA_DIR"

# Build this fixed, pre-approved test image first:
docker build -f docker/Dockerfile.runner -t ai-test-runner:local .
# Mounts Docker socket ONLY for local development. See security warning above.
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

For macOS Docker Desktop, the host filesystem and data directory must be shared into Docker Desktop. For Windows use a Linux Docker host/WSL2; POSIX UID ownership and daemon-visible mount paths differ from native Windows paths. To avoid host Docker socket exposure in production, deploy the worker on an isolated machine with a rootless daemon; supply `DOCKER_HOST` and a secure shared artifact filesystem so the daemon sees the same absolute `DATA_DIR` path. Run one Celery beat scheduler.

### Run with a local model (no API key)

The worker can talk to any OpenAI-compatible server that implements the **Responses API** (`/v1/responses`) with function calling, such as LM Studio or Ollama, instead of OpenAI. In `.env`:

```bash
OPENAI_API_KEY=
OPENAI_MODEL=<model id as listed by your local server>
LLM_BASE_URL=http://host.docker.internal:1234/v1   # LM Studio; Ollama: http://host.docker.internal:11434/v1
OPENAI_CALL_TIMEOUT_SECONDS=600
INVESTIGATION_TIMEOUT_SECONDS=1200
```

Notes: the worker runs in a container, so use `host.docker.internal`, not `127.0.0.1`, and start with the `compose.dev.yaml` overlay. With `LLM_BASE_URL` set, approved code is sent to that URL rather than OpenAI, and the OpenAI-only `reasoning.encrypted_content` option is not requested. In local mode the agent also sends `LLM_TEMPERATURE` (default 0.1), withholds the JSON schema until the final no-tools request, reminds the model up to twice if it stops before writing or running a test, and retries transient server errors. Result quality depends on the model: it must emit tool calls reliably and write valid Pytest code. Very small models (around 1–2B parameters) complete the loop but tend to write unusable tests; if the final answer is not valid JSON, the model's free text is kept and labelled as such, and test evidence is still recorded. `INVESTIGATION_TIMEOUT_SECONDS` is capped at 1200 (the Celery task limit is 1500).

### Use the API

```bash
python scripts/make_example_zip.py
export TOKEN='YOUR_API_KEY_FROM_ENV_FILE'

curl -fsS -X POST http://127.0.0.1:8000/v1/projects \
  -H "Authorization: Bearer $TOKEN" \
  -F 'approved_paths=["calculator.py","spec.md"]' \
  -F 'archive=@examples/calculator_project.zip'
# Copy the returned UUID as PROJECT_ID.

curl -fsS -X POST http://127.0.0.1:8000/v1/investigations \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-inv-0001' \
  -d '{"project_id":"PROJECT_ID","goal":"Generate targeted tests for divide-by-zero behavior from spec.md and investigate any observed failure."}'
# Copy returned UUID as JOB_ID. Requests are queued, not run on the API process.

curl -fsS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/v1/investigations/JOB_ID
curl -fsS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/v1/investigations/JOB_ID/report
curl -fsS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/v1/investigations/JOB_ID/report.md
```

Other endpoints: `GET /v1/projects/{id}`, `GET /v1/investigations?project_id={id}&status={status}&limit=50`, `POST /v1/investigations/{id}/cancel`, `GET /v1/diagnostics`; `GET /health/live`, `GET /health/ready`. OpenAPI docs at `/docs` and `/openapi.json` are **off by default**; set `ENABLE_DOCS=true` only for local development.

## How an investigation proceeds

1. API snapshots the exact approved files; it stores SHA-256 checksums in PostgreSQL.
2. API creates a durable `queued` record and requests a Celery task. The periodic reconciler repairs lost dispatches and stale leases. An `Idempotency-Key` avoids accidental duplicated investigations.
3. Worker claims the record, validates source hashes, copies only approved content into an attempt workspace, and initializes append-only JSONL tool events.
4. Model sees the objective plus approved relative file names. Tool calls are validated server-side with strict Pydantic types and budgets. The tool loop uses `store=False`, encrypted reasoning replay, and a strict final JSON schema.
5. Generated tests are **append only** and Pytest runs execute **all** tests generated so far. Incorrect tests cannot be edited in a running job; submit a new investigation if a correction is required.
6. Reports are written atomically to `DATA_DIR/investigations/<id>/report.json|report.md` and also stored in PostgreSQL for reliable authenticated retrieval. Completed, failed and cancelled records and their files are retained 30 days by default. Projects are retained until separately removed by an administrator.

## Operational limits (configurable via environment)

| Configuration | Default |
| --- | ---: |
| `MAX_APPROVED_FILES` | 30 |
| `MAX_FILE_BYTES` | 128,000 |
| `MAX_EXTRACTED_BYTES` | 1,000,000 |
| `MAX_ARCHIVE_BYTES` | 8,000,000 |
| `MAX_QUEUED_PER_PROJECT` | 20 |
| `MAX_TOOL_ROUNDS` / `MAX_TOOL_CALLS` | 12 / 32 |
| `MAX_GENERATED_TESTS` / `MAX_TEST_RUNS` | 8 / 4 |
| `INVESTIGATION_TIMEOUT_SECONDS` | 300 |
| `TEST_TIMEOUT_SECONDS` | 45 per run |
| `MAX_MODEL_OUTPUT_TOKENS` | 8,192 per model request |
| `DOCKER_MEMORY_MB` / `DOCKER_CPUS` / `DOCKER_PIDS_LIMIT` | 256 / 1.0 / 64 |
| `DOCKER_LOG_BYTES` | 40,000 |
| `REPORT_RETENTION_DAYS` | 30 |
| `RATE_LIMIT_PER_MINUTE` (per client, `/v1/` routes, 0 disables) | 60 |
| `ENABLE_DOCS` | false |

### Rate limiting

Every `/v1/` request is counted per client IP in a one-minute window stored in Redis, **before** the bearer token is checked, so failed token guesses are throttled too. Over the limit the API returns `429` with a `Retry-After` header; health checks are never throttled. If Redis is unreachable the limiter lets requests through and logs a warning. Behind a reverse proxy, the client IP is the proxy's unless uvicorn trusts the proxy's `X-Forwarded-For` header: change `--forwarded-allow-ips` in `docker/Dockerfile.api` to the proxy's address, otherwise all clients share one limit. Keep proxy-level throttling as well.

## Updating pinned dependencies

Base images are pinned by multi-architecture digest in the three Dockerfiles and `compose.yaml`, and Python packages are installed from hash-verified lock files (`requirements.lock` for the API and worker, `requirements-dev.lock` for CI, `docker/runner-requirements.lock` for the test runner). Nothing updates by itself, so refresh them regularly to pick up security fixes:

```bash
# Python packages (regenerate after editing pyproject.toml or docker/runner-requirements.in)
uv pip compile pyproject.toml --universal --python-version 3.12 --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 --generate-hashes -o requirements-dev.lock
uv pip compile docker/runner-requirements.in --universal --python-version 3.12 --generate-hashes -o docker/runner-requirements.lock

# Base images: pull the tag, read its digest, and paste it after the tag (image:tag@sha256:...)
docker pull python:3.12-slim && docker inspect --format '{{index .RepoDigests 0}}' python:3.12-slim
```

## Testing

```bash
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
export API_KEY='replace_this_with_40_or_more_random_characters'
pytest -q
ruff check app tests
```

The test suite uses fake OpenAI responses and a stubbed Docker runner, so it **does not require a real API key, database server, Redis or Docker**. For an end-to-end smoke test, start Compose with the real key and use the calculator example.

## Deployment checklist

- [ ] Deploy **API + worker + Docker runner** in segregated security domains, preferably a dedicated runner VM/host with rootless Docker; restrict any Docker API/socket to that host. The `compose.dev.yaml` socket mount is **only** for a disposable local dev environment.
- [ ] Put API behind HTTPS, request-body limit (e.g., 10 MB), authentication-aware throttling, logging, WAF and a private ingress; do not expose `/docs` or PostgreSQL/Redis publicly.
- [ ] Rotate API, DB, Redis and OpenAI secrets in a secrets manager; ensure source projects do not include credentials. Worker alone receives `OPENAI_API_KEY`.
- [ ] Scan the digest-pinned base images and locked dependencies before deployment, and refresh the pins regularly (see above); do not let uploads install Python packages. Maintain separate reviewed runner images for projects requiring known dependencies.
- [ ] Mount a daemon-visible shared filesystem at the same absolute path for worker and runner Docker daemon; ensure separate directory ownership/permissions. Network-filesystem security and Docker socket configuration require environment-specific validation.
- [ ] Run `alembic upgrade head` before releasing new API/worker images, maintain PostgreSQL backup + restore drills, Redis AOF persistence, one Celery beat, and dashboards for queue depth/failed jobs/runner error rates.
- [ ] Route Docker containers through additional defense-in-depth as available: default seccomp/AppArmor (never disable them), user namespaces/rootless engine, and dedicated execution hosts. Container isolation is not the same as a hardened VM boundary.
- [ ] Add organization-specific authorization/tenant isolation and quotas before supporting multiple customers. This distribution is **single-tenant** by design.
- [ ] Use a vulnerability scanner + container runtime monitoring and run adversarial evaluations against archive uploads, tool output injection, malicious tests and model-produced tests that try to forge reports.

## Limitations and scope

This service investigates **only code that is approved and present in the snapshot**. It does not perform dependency resolution, arbitrary code checkout, live service integration, coverage-guided fuzzing, mutation testing or claim mathematical verification. Tests needing third-party libraries need a **prebuilt reviewed runner image** with those dependencies, configured by an administrator (never by a model). Many findings are hypotheses, not confirmed defects. Worker cancellation is cooperative between tool/API calls; an ongoing Docker subprocess is terminated by the per-run wall-time limit.
