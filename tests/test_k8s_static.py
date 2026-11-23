"""Gate 1 static checks for the Kubernetes manifests (Spec 010).

WHAT: Renders every kustomization with `kubectl kustomize` and asserts the security, scaling and
wiring properties the spec promises (hardening, least-privilege secrets, probes, drain timing,
KEDA settings, label/selector consistency, no literal secrets, doc/manifest agreement).
WHERE: Verifies `infra/k8s/**`, `scripts/k8s_*.sh`, `docs/k8s-local.md`.
WHY: These properties break with one-line edits and stay invisible until an incident (a pod that
can run as root, an admin URL mounted into the API, a grace period shorter than the drain). Cheap
render-and-assert tests keep them true without a cluster; the live behaviour is Gate 3.
Skipped when `kubectl` is not installed.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "infra" / "k8s"
DIRS = ["base/namespace", "base/app", "base/migrate", "overlays/local/datastores",
        "overlays/local/app", "overlays/local/migrate"]

pytestmark = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not installed")


def render(d: str) -> list[dict]:
    out = subprocess.run(["kubectl", "kustomize", str(K8S / d)], capture_output=True, text=True,
                         check=True, timeout=60).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def all_docs() -> list[dict]:
    docs = []
    for d in ("base/namespace", "overlays/local/app", "overlays/local/migrate",
              "overlays/local/datastores"):
        docs += render(d)
    return docs


DOCS = all_docs() if shutil.which("kubectl") else []


def of_kind(kind):
    return [d for d in DOCS if d["kind"] == kind]


def pod_specs():
    for d in DOCS:
        if d["kind"] in ("Deployment", "StatefulSet") or d["kind"] == "Job":
            yield d["metadata"]["name"], d["spec"]["template"]["spec"]


def secret_names(spec) -> set[str]:
    names = set()
    for c in spec["containers"]:
        for ef in c.get("envFrom", []):
            if "secretRef" in ef:
                names.add(ef["secretRef"]["name"])
        for e in c.get("env", []):
            ref = e.get("valueFrom", {}).get("secretKeyRef")
            if ref:
                names.add(ref["name"])
    return names


@pytest.mark.parametrize("d", DIRS)
def test_every_kustomization_renders(d):
    assert render(d)


def test_namespace_enforces_restricted_pod_security():
    ns = of_kind("Namespace")[0]
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


def test_every_container_is_hardened_with_resources_and_a_pinned_tag():
    seen = 0
    for name, spec in pod_specs():
        assert spec.get("automountServiceAccountToken") is False, name
        assert spec.get("enableServiceLinks") is False, f"{name}: service links collide with settings"
        assert spec["securityContext"]["runAsNonRoot"] is True, name
        assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault", name
        for c in spec["containers"]:
            seen += 1
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False, f"{name}/{c['name']}"
            assert sc["capabilities"]["drop"] == ["ALL"], f"{name}/{c['name']}"
            assert sc["seccompProfile"]["type"] == "RuntimeDefault"
            assert c["resources"]["requests"] and c["resources"]["limits"], f"{name}: no resources"
            assert not c["image"].endswith(":latest") and ":" in c["image"], c["image"]
            if name in ("api", "worker", "migrate"):  # our own image: read-only root, numeric uid
                assert sc["readOnlyRootFilesystem"] is True and sc["runAsUser"] == 10001
    assert seen >= 5


def test_secrets_are_scoped_to_the_workloads_that_need_them():
    by_name = {n: secret_names(s) for n, s in pod_specs()}
    assert by_name["api"] == {"app-secrets"}
    assert by_name["worker"] == {"app-secrets", "provider-secrets"}
    assert by_name["migrate"] == {"admin-secrets"}
    for n, secrets in by_name.items():
        if n != "migrate":
            assert "admin-secrets" not in secrets, n
        if n != "worker":
            assert "provider-secrets" not in secrets, n
    worker = next(s for n, s in pod_specs() if n == "worker")
    optional = [e["secretRef"].get("optional") for e in worker["containers"][0]["envFrom"]
                if e.get("secretRef", {}).get("name") == "provider-secrets"]
    assert optional == [True]  # a worker without research credentials still runs echo jobs


def test_no_literal_secret_values_in_any_rendered_manifest():
    text = "\n".join(subprocess.run(["kubectl", "kustomize", str(K8S / d)], capture_output=True,
                                    text=True, check=True).stdout for d in DIRS)
    assert not re.search(r"sk-[A-Za-z0-9]{10,}|tvly-|lsv2_", text)
    assert not any(d["kind"] == "Secret" for d in DOCS), "Secrets are created at deploy time only"
    for d in of_kind("ConfigMap"):
        for key, val in d["data"].items():
            assert not re.search(r"PASSWORD|API_KEY|SECRET|TOKEN|DATABASE_URL", key), key
            assert "@" not in val, f"{key} looks like a credentialed URL"


def test_api_probes_and_pdb():
    api = next(d for d in of_kind("Deployment") if d["metadata"]["name"] == "api")
    c = api["spec"]["template"]["spec"]["containers"][0]
    assert api["spec"]["replicas"] >= 2
    assert c["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert "startupProbe" in c and "--no-access-log" in c["command"]
    assert of_kind("PodDisruptionBudget")[0]["spec"]["minAvailable"] == 1


def test_worker_starts_idle_drains_and_has_a_grace_period_longer_than_the_drain():
    from workers.settings import WorkerSettings

    w = next(d for d in of_kind("Deployment") if d["metadata"]["name"] == "worker")
    spec = w["spec"]["template"]["spec"]
    assert w["spec"]["replicas"] == 0
    assert spec["terminationGracePeriodSeconds"] > WorkerSettings.job_completion_wait
    assert spec["containers"][0]["livenessProbe"]["exec"]["command"][:2] == ["arq", "--check"]
    cfg = {d["metadata"]["name"]: d for d in of_kind("ConfigMap")}["app-config"]["data"]
    assert int(cfg["JOB_TIMEOUT_SECONDS"]) == WorkerSettings.job_completion_wait


def test_scaledobject_scales_worker_on_the_outstanding_gauge_with_a_fallback():
    so = of_kind("ScaledObject")[0]["spec"]
    assert so["scaleTargetRef"]["name"] == "worker"
    assert so["minReplicaCount"] == 0 and 1 < so["maxReplicaCount"] <= 5
    assert so["fallback"]["replicas"] >= 1 and so["fallback"]["failureThreshold"] >= 1
    t = so["triggers"][0]
    assert t["type"] == "metrics-api" and t["metricType"] == "AverageValue"  # fallback needs it
    assert t["metadata"]["valueLocation"] == "di_queue_outstanding"
    assert t["metadata"]["format"] == "prometheus"
    assert re.fullmatch(r"http://api-metrics\.deal-intel\.svc\.cluster\.local:9101/metrics",
                        t["metadata"]["url"])


def test_services_and_policies_select_real_workloads():
    labels = []
    for d in DOCS:
        if d["kind"] in ("Deployment", "StatefulSet", "Job"):
            labels.append(d["spec"]["template"]["metadata"]["labels"])
    names = {lb["app.kubernetes.io/name"] for lb in labels} | {"admin-task"}  # admin Jobs are ad hoc
    for svc in of_kind("Service"):
        assert svc["spec"]["selector"]["app.kubernetes.io/name"] in names, svc["metadata"]["name"]
    for pol in of_kind("NetworkPolicy"):
        sel = pol["spec"]["podSelector"].get("matchLabels", {})
        if sel:
            assert sel["app.kubernetes.io/name"] in names, pol["metadata"]["name"]
    pol_names = {p["metadata"]["name"] for p in of_kind("NetworkPolicy")}
    assert {"default-deny", "allow-dns", "api", "worker", "postgres", "redis"} <= pol_names
    deny = next(p for p in of_kind("NetworkPolicy") if p["metadata"]["name"] == "default-deny")
    assert deny["spec"]["podSelector"] == {} and set(deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    # Metrics ports are reachable only from KEDA's namespace / labelled scrapers, never everyone.
    api_pol = next(p for p in of_kind("NetworkPolicy") if p["metadata"]["name"] == "api")
    metrics_rule = next(r for r in api_pol["spec"]["ingress"] if r["ports"][0]["port"] == 9101)
    assert any("namespaceSelector" in f for f in metrics_rule["from"])


def test_migrate_job_is_the_only_holder_of_the_admin_secret_and_uses_the_loaded_image():
    job = of_kind("Job")[0]
    assert job["metadata"]["name"] == "migrate"
    c = job["spec"]["template"]["spec"]["containers"][0]
    assert c["command"] == ["python", "-m", "agents.migrate"] and c["image"] == "deal-intelligence:local"
    assert c["imagePullPolicy"] == "IfNotPresent"  # kind-loaded image: never pull


def test_keda_install_is_pinned_and_checksummed():
    ver = (K8S / "keda" / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", ver)
    assert re.fullmatch(r"[0-9a-f]{64}  keda\.yaml", (K8S / "keda" / "SHA256").read_text().strip())
    deploy = (ROOT / "scripts" / "k8s_deploy.sh").read_text()
    assert "shasum -a 256 -c" in deploy and "--server-side" in deploy


def test_deploy_script_never_puts_secret_values_in_arguments():
    deploy = (ROOT / "scripts" / "k8s_deploy.sh").read_text()
    assert "--from-env-file=/dev/stdin" in deploy
    assert "--from-literal" not in deploy
    for script in ("k8s_deploy.sh", "k8s_smoke.sh", "k8s_admin.sh"):
        assert "set -x" not in (ROOT / "scripts" / script).read_text()


def test_docs_variable_table_matches_the_manifests():
    doc = (ROOT / "docs" / "k8s-local.md").read_text()
    rows = dict(re.findall(r"^\| `([A-Z_]+)` \| (Secret|ConfigMap)[^|]*\|", doc, re.MULTILINE))
    cfg = {d["metadata"]["name"]: d for d in of_kind("ConfigMap")}["app-config"]["data"]
    for key in cfg:
        assert rows.get(key) == "ConfigMap", f"{key} missing/misclassified in docs/k8s-local.md"
    for key in ("DATABASE_URL", "LOG_HASH_KEY", "ADMIN_DATABASE_URL", "APP_USER_PASSWORD",
                "POSTGRES_PASSWORD", "TAVILY_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                "LANGSMITH_API_KEY"):
        assert rows.get(key) == "Secret", f"{key} missing/misclassified in docs/k8s-local.md"
