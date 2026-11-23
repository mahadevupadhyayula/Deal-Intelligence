"""Gate 1 static checks for the Docker image and Compose stack (Spec 008).

WHAT: Parses `docker-compose.yml`, the Dockerfile, `.dockerignore` and `requirements.lock` and
asserts the security and startup-order properties the spec promises (no literal secrets,
least-privilege env wiring, hardening, health checks, localhost-only ports, ordering).
WHERE: Verifies `docker-compose.yml`, `infra/docker/Dockerfile`, `.dockerignore`,
`requirements.lock`, `workers/settings.py`.
WHY: These properties are easy to break with a one-line edit and invisible until an incident
(e.g. an admin URL leaking into the API container). Cheap static tests keep them true without
needing Docker; one extra test runs `docker compose config` when Docker is available.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]
APP_SERVICES = ["migrate", "api", "worker"]
PROVIDER_VARS = {"TAVILY_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LANGSMITH_API_KEY"}


def env_of(name: str) -> dict:
    return SERVICES[name].get("environment", {})


def test_no_env_file_and_no_literal_secrets():
    for name, svc in SERVICES.items():
        assert "env_file" not in svc, f"{name}: use explicit environment, not env_file"
    for name, svc in SERVICES.items():
        for key, val in env_of(name).items():
            if re.search(r"PASSWORD|KEY|SECRET|TOKEN", key):
                # every secret must be an interpolation, never a literal value
                assert str(val).startswith("${") or val in ("", None), f"{name}.{key} is literal"
    text = (ROOT / "docker-compose.yml").read_text()
    assert not re.search(r"sk-[A-Za-z0-9]{10,}|tvly-|lsv2_", text)


def test_required_passwords_fail_fast():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "${POSTGRES_PASSWORD:?" in text and "${APP_USER_PASSWORD:?" in text


def test_admin_url_only_in_migrate_and_tools():
    holders = {n for n in SERVICES if "ADMIN_DATABASE_URL" in env_of(n)}
    assert holders == {"migrate", "tools"}


def test_provider_keys_only_in_worker_and_tools():
    for name in SERVICES:
        has = PROVIDER_VARS & set(env_of(name))
        if name in ("worker", "tools"):
            assert has == PROVIDER_VARS, name
        else:
            assert not has, f"{name} must not receive provider keys: {has}"


def test_app_services_are_hardened():
    for name in [*APP_SERVICES, "tools"]:
        svc = SERVICES[name]
        assert svc["user"] == "10001:10001", name
        assert svc["read_only"] is True and "/tmp" in svc["tmpfs"], name
        assert svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name


def test_published_ports_are_localhost_only():
    for name, svc in SERVICES.items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} publicly"


def test_worker_is_scalable():
    w = SERVICES["worker"]
    assert "container_name" not in w and "ports" not in w
    # enough for run_job to record a cancelled job; ARQ does not drain on SIGTERM (Spec 008 notes)
    assert 10 <= int(w["stop_grace_period"].rstrip("s")) <= 60


def test_redis_never_evicts_and_persists():
    cmd = SERVICES["redis"]["command"]
    assert cmd[cmd.index("--maxmemory-policy") + 1] == "noeviction"
    assert cmd[cmd.index("--appendonly") + 1] == "yes"
    assert SERVICES["redis"]["volumes"] and SERVICES["postgres"]["volumes"]


def test_health_checks_and_startup_order():
    for name in ("postgres", "redis", "api", "worker"):
        assert "healthcheck" in SERVICES[name], name
    assert SERVICES["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    for name in ("api", "worker"):
        deps = SERVICES[name]["depends_on"]
        assert deps["migrate"]["condition"] == "service_completed_successfully"
        assert deps["redis"]["condition"] == "service_healthy"
    assert SERVICES["migrate"]["restart"] == "no"
    assert SERVICES["api"]["restart"] == "unless-stopped"


def test_only_migrate_builds_the_shared_image():
    # Two services building the same tag in parallel race (found while building this spec).
    assert "build" in SERVICES["migrate"]
    for name in ("api", "worker"):
        assert "build" not in SERVICES[name] and SERVICES[name]["pull_policy"] == "never"
        assert SERVICES[name]["image"] == SERVICES["migrate"]["image"]


def test_api_has_no_admin_or_redis_write_secrets_and_uses_app_user():
    assert env_of("api")["DATABASE_URL"].startswith("postgresql://app_user:")
    assert env_of("worker")["DATABASE_URL"].startswith("postgresql://app_user:")


def test_dockerfile_non_root_no_secrets():
    text = (ROOT / "infra/docker/Dockerfile").read_text()
    stages = re.findall(r"^FROM .* AS (\w+)", text, re.MULTILINE)
    assert stages == ["builder", "runtime", "tools"]
    assert re.search(r"^USER 10001:10001", text, re.MULTILINE)
    assert not re.search(r"COPY .*\.env", text)
    for line in text.splitlines():
        if line.startswith(("ENV", "ARG")):
            assert not re.search(r"PASSWORD|SECRET|TOKEN|API_KEY", line), line
    assert "requirements.lock" in text and "FROM python:3.11-slim" in text


def test_dockerignore_excludes_secrets_and_local_state():
    lines = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()}
    assert {".env", ".git", ".venv", "evals/reports"} <= lines


def test_lock_file_pins_core_packages():
    lock = (ROOT / "requirements.lock").read_text()
    for pkg in ("fastapi", "uvicorn", "arq", "psycopg", "psycopg-pool", "pydantic", "langgraph",
                "langchain", "langchain-core", "langsmith", "tavily-python", "langchain-openai",
                "langchain-anthropic"):
        assert re.search(rf"^{re.escape(pkg)}==\d", lock, re.MULTILINE), f"{pkg} not pinned"
    assert "pytest" not in lock  # dev tools never ship


def test_worker_health_check_interval_set():
    from workers.settings import WorkerSettings

    assert 0 < WorkerSettings.health_check_interval <= 60


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_compose_config_validates(tmp_path):
    env = tmp_path / "e.env"
    env.write_text("POSTGRES_PASSWORD=x\nAPP_USER_PASSWORD=y\nLOG_HASH_KEY=z\n")
    out = subprocess.run(
        ["docker", "compose", "--env-file", str(env), "-f", str(ROOT / "docker-compose.yml"),
         "--profile", "tools", "--profile", "observability", "config", "-q"], capture_output=True, text=True, timeout=60, check=False)
    if "Cannot connect to the Docker daemon" in out.stderr:
        pytest.skip("docker daemon not running")
    assert out.returncode == 0, out.stderr


def test_echo_delay_disabled_by_default_in_compose():
    assert "${ECHO_MAX_DELAY_SECONDS:-0}" in env_of("worker")["ECHO_MAX_DELAY_SECONDS"]
