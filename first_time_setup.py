import argparse
import base64
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / ".run"
CREDENTIALS_FILE = RUN_DIR / "first_time_credentials.json"
COMPOSE_ENV = ROOT / ".env"
COMPOSE_ENV_EXAMPLE = ROOT / ".env.example"
KUBERNETES_SECRET = "readmission-db-credentials"
KUBERNETES_NAMESPACE = "default"
SECRET_KEYS = (
    "MYSQL_ROOT_PASSWORD",
    "MYSQL_PASSWORD",
    "DATABASE_URL",
    "APP_DATA_ENCRYPTION_KEY",
    "AUTH_BOOTSTRAP_ADMIN_USERNAME",
    "AUTH_BOOTSTRAP_ADMIN_PASSWORD",
)


def _run(arguments: list[str], *, input_text: str | None = None) -> str:
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip()
        raise RuntimeError(
            f"Command failed ({result.returncode}): {arguments[0]}"
            + (f": {detail}" if detail else "")
        )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    return result.stdout.strip()


def _new_secret() -> str:
    return secrets.token_hex(32)


def _read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    values = {}
    for line in lines:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2).strip("\"'")
    return lines, values


def _save_credentials(environment: str, username: str, password: str) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if CREDENTIALS_FILE.exists():
        credentials = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    else:
        credentials = {}
    credentials[environment] = {"username": username, "password": password}
    CREDENTIALS_FILE.write_text(json.dumps(credentials, indent=2) + "\n", encoding="utf-8")
    try:
        CREDENTIALS_FILE.chmod(0o600)
    except OSError:
        pass


def _ensure_compose_env() -> tuple[dict[str, str], bool]:
    if not COMPOSE_ENV.exists():
        COMPOSE_ENV.write_text(COMPOSE_ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    lines, values = _read_env(COMPOSE_ENV)
    generated_password = False
    generated = {
        "MYSQL_ROOT_PASSWORD": _new_secret(),
        "MYSQL_PASSWORD": _new_secret(),
        "APP_DATA_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "AUTH_BOOTSTRAP_ADMIN_USERNAME": "admin",
        "AUTH_BOOTSTRAP_ADMIN_PASSWORD": secrets.token_urlsafe(32),
        "AUTH_COOKIE_SECURE": "true",
    }
    placeholders = ("replace-with-", "generate-a-fernet-key")
    for key, value in generated.items():
        current = values.get(key, "")
        if not current or current.startswith(placeholders):
            values[key] = value
            if key == "AUTH_BOOTSTRAP_ADMIN_PASSWORD":
                generated_password = True

    updated_lines = []
    seen = set()
    for line in lines:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in generated:
            key = match.group(1)
            updated_lines.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            updated_lines.append(line)
    for key in generated:
        if key not in seen:
            updated_lines.append(f"{key}={values[key]}")
    COMPOSE_ENV.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    return values, generated_password


def _compose_setup() -> None:
    if not shutil.which("docker"):
        raise RuntimeError("Docker CLI was not found. Install/start Docker Desktop or Docker Engine.")
    settings, generated_password = _ensure_compose_env()
    if generated_password:
        _save_credentials(
            "compose",
            settings["AUTH_BOOTSTRAP_ADMIN_USERNAME"],
            settings["AUTH_BOOTSTRAP_ADMIN_PASSWORD"],
        )
    _run(["docker", "compose", "up", "--build", "-d"])
    port = settings.get("API_PORT", "8000")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise RuntimeError("API_PORT in .env must be a port number between 1 and 65535.")
    _wait_for_url(f"http://127.0.0.1:{port}/health")
    print(f"Compose API is ready: http://127.0.0.1:{port}/login")
    if generated_password:
        print(f"New Compose admin credentials saved privately in {CREDENTIALS_FILE}.")


def _secret_values(
    existing_secret: dict | None,
    compose_settings: dict[str, str] | None,
) -> dict[str, str]:
    if existing_secret is None:
        username = (
            compose_settings.get("AUTH_BOOTSTRAP_ADMIN_USERNAME")
            if compose_settings
            else "admin"
        ) or "admin"
        password = (
            compose_settings.get("AUTH_BOOTSTRAP_ADMIN_PASSWORD")
            if compose_settings
            else None
        ) or secrets.token_urlsafe(32)
        generated = {
            "MYSQL_ROOT_PASSWORD": _new_secret(),
            "MYSQL_PASSWORD": _new_secret(),
            "APP_DATA_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
            "AUTH_BOOTSTRAP_ADMIN_USERNAME": username,
            "AUTH_BOOTSTRAP_ADMIN_PASSWORD": password,
        }
        generated["DATABASE_URL"] = (
            "mysql+pymysql://readmission_app:"
            f"{generated['MYSQL_PASSWORD']}@mysql.default.svc.cluster.local:3306/"
            "readmission?charset=utf8mb4"
        )
        return generated

    data = existing_secret.get("data", {})
    missing = [key for key in SECRET_KEYS if key not in data]
    if missing:
        raise RuntimeError(
            f"Existing Kubernetes Secret {KUBERNETES_SECRET} is missing keys: "
            + ", ".join(missing)
            + ". Add them without replacing existing database credentials."
        )
    return {}


def _secret_manifest(values: dict[str, str]) -> str:
    encoded = {
        key: base64.b64encode(value.encode("utf-8")).decode("ascii")
        for key, value in values.items()
    }
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": KUBERNETES_SECRET, "namespace": KUBERNETES_NAMESPACE},
            "type": "Opaque",
            "data": encoded,
        }
    )


def _load_image(image: str, archive: Path) -> None:
    control_plane = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}}",
            "desktop-control-plane",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if control_plane.returncode == 0 and control_plane.stdout.strip() == "true":
        remote_archive = "/root/readmission-risk-api-setup.tar"
        try:
            _run(["docker", "cp", str(archive), f"desktop-control-plane:{remote_archive}"])
            _run(
                [
                    "docker",
                    "exec",
                    "desktop-control-plane",
                    "ctr",
                    "-n",
                    "k8s.io",
                    "images",
                    "import",
                    remote_archive,
                ]
            )
        finally:
            subprocess.run(
                ["docker", "exec", "desktop-control-plane", "rm", "-f", remote_archive],
                capture_output=True,
                text=True,
                check=False,
            )
        return

    nodes = json.loads(_run(["kubectl", "get", "nodes", "-o", "json"]))
    provider_id = nodes["items"][0].get("spec", {}).get("providerID", "")
    if provider_id.startswith("kind://") and shutil.which("kind"):
        cluster_name = provider_id.rstrip("/").split("/")[-2]
        _run(["kind", "load", "docker-image", "--name", cluster_name, image])
        return
    raise RuntimeError(
        "Cannot load the image into this cluster. Use Docker Desktop Kubernetes "
        "or a kind cluster with the kind CLI installed."
    )


def _wait_for_url(url: str, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, URLError) as error:
            last_error = str(error)
        time.sleep(2)
    raise RuntimeError(f"Service did not become ready at {url}: {last_error}")


def _kubernetes_setup(compose_settings: dict[str, str] | None = None) -> None:
    for command in ("docker", "kubectl"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command {command!r} was not found on PATH.")
    _run(["kubectl", "cluster-info"])
    secret_result = subprocess.run(
        [
            "kubectl",
            "get",
            "secret",
            KUBERNETES_SECRET,
            "-n",
            KUBERNETES_NAMESPACE,
            "-o",
            "json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if secret_result.returncode == 0:
        existing_secret = json.loads(secret_result.stdout)
        values = _secret_values(existing_secret, compose_settings)
        if values:
            _run(["kubectl", "apply", "-f", "-"], input_text=_secret_manifest(values))
    else:
        if "NotFound" not in secret_result.stderr:
            raise RuntimeError(
                f"Could not inspect Kubernetes Secret {KUBERNETES_SECRET}: "
                f"{secret_result.stderr.strip()}"
            )
        values = _secret_values(None, compose_settings)
        _run(["kubectl", "apply", "-f", "-"], input_text=_secret_manifest(values))
        _save_credentials(
            "kubernetes",
            values["AUTH_BOOTSTRAP_ADMIN_USERNAME"],
            values["AUTH_BOOTSTRAP_ADMIN_PASSWORD"],
        )
        print(f"New Kubernetes admin credentials saved privately in {CREDENTIALS_FILE}.")

    _run(["kubectl", "apply", "-f", "k8s/mysql.yaml"])
    _run(["kubectl", "rollout", "status", "statefulset/mysql", "-n", "default", "--timeout=300s"])

    manifest = (ROOT / "k8s" / "readmission-api.yml").read_text(encoding="utf-8")
    image_match = re.search(r"^\s+image:\s*([^\s#]+)", manifest, re.MULTILINE)
    if image_match is None:
        raise RuntimeError("Could not find the API image tag in k8s/readmission-api.yml.")
    image = image_match.group(1)
    _run(["docker", "build", "--tag", image, "."])
    with tempfile.TemporaryDirectory(prefix="readmission-risk-api-setup-") as directory:
        archive = Path(directory) / "image.tar"
        _run(["docker", "save", "--output", str(archive), image])
        _load_image(image, archive)
    _run(["kubectl", "apply", "-f", "k8s/readmission-api.yml"])
    _run(
        [
            "kubectl",
            "rollout",
            "status",
            "deployment/readmission-risk-api",
            "-n",
            "default",
            "--timeout=300s",
        ]
    )
    _run(
        [
            "kubectl",
            "exec",
            "deployment/readmission-risk-api",
            "-n",
            "default",
            "--",
            "python",
            "-c",
            (
                "import json, urllib.request; "
                "r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=10); "
                "assert r.status == 200 and json.load(r).get('status') == 'ok'"
            ),
        ]
    )
    print("Kubernetes API is ready at /health; run start_services.py for the local port-forward.")
    if compose_settings is not None:
        print("Kubernetes bootstrap credentials match the local Compose .env values.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Initialize the Readmission Risk API with Compose, Kubernetes, or both."
    )
    parser.add_argument(
        "--target",
        required=True,
        choices=("compose", "kubernetes", "both"),
        help="Which environment to set up.",
    )
    args = parser.parse_args()
    try:
        compose_settings = None
        if args.target in {"compose", "both"}:
            _compose_setup()
            compose_settings = _read_env(COMPOSE_ENV)[1]
        if args.target in {"kubernetes", "both"}:
            _kubernetes_setup(compose_settings)
        print("First-time setup completed.")
        return 0
    except (OSError, RuntimeError, KeyError, ValueError, json.JSONDecodeError) as error:
        print(f"First-time setup failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
