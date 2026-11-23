# syntax=docker/dockerfile:1
#
# WHAT:  One image for every Deal Intelligence process (api, worker, migrate) plus a `tools`
#        target that also carries eval datasets/fixtures for operator and eval commands.
# WHERE: Built by docker-compose.yml (Spec 008) and reused unchanged by the local Kubernetes
#        manifests (Spec 010) and the Terraform/EKS plan (Spec 011).
# WHY:   Same code for every role means one build, one artifact to scan and version; the role
#        is chosen by the command. Dependencies come from requirements.lock (exact versions) so
#        the image validated locally is the one that later ships to the cloud. Multi-stage keeps
#        build tooling out of the final image; the runtime runs as a numeric non-root user.
#        Nothing secret is built in: configuration arrives as environment variables at runtime
#        and `.env` is excluded from the build context (see .dockerignore).

# ---- builder: resolve and install pinned dependencies into a relocatable venv -------------
FROM python:3.11-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
# --no-deps would skip transitive resolution, but the lock already lists every package, so a
# plain install is both fast and fails loudly if the lock is inconsistent.
RUN /opt/venv/bin/pip install -r /tmp/requirements.lock

# ---- runtime: api, worker, migrate ---------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# Fixed numeric uid: Kubernetes `runAsNonRoot` can verify a number, not a name (Spec 010).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
# Explicit allow-list of what ships: application code and SQL migrations only.
COPY agents/ agents/
COPY api/ api/
COPY workers/ workers/
COPY migrations/ migrations/
USER 10001:10001
EXPOSE 8000
# Compose/Kubernetes supply the real command per role; the default just says what to do.
CMD ["python", "-c", "print('Specify a command: uvicorn api.main:app | arq workers.settings.WorkerSettings | python -m agents.migrate')"]

# ---- tools: runtime + eval datasets and golden fixtures ------------------------------------
FROM runtime AS tools
COPY evals/ evals/
COPY tests/fixtures/ tests/fixtures/
