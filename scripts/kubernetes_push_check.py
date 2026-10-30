import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(arguments: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        check=False,
        text=True,
        capture_output=capture_output,
    )
    if not capture_output:
        if result.returncode:
            raise RuntimeError(f"Command failed with exit code {result.returncode}: {arguments[0]}")
        return result
    return result


def _run_checked(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    result = _run(arguments, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {arguments[0]}")
    return result


def _verify_repository(branch: str, commit: str) -> Path:
    repository = Path(
        _run_checked(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
    )
    current_branch = _run_checked(["git", "branch", "--show-current"]).stdout.strip()
    if current_branch != branch:
        raise RuntimeError(f"Check out local branch {branch!r} before pushing it.")

    head = _run_checked(["git", "rev-parse", "HEAD"]).stdout.strip()
    if head != commit:
        raise RuntimeError("The pushed commit must be the current HEAD.")

    status = _run_checked(
        ["git", "status", "--porcelain", "--untracked-files=normal"]
    ).stdout
    if status:
        raise RuntimeError("Commit or remove tracked and untracked changes before pushing.")
    return repository


def _load_image(image: str, archive: Path) -> None:
    control_plane = _run(
        ["docker", "inspect", "--format", "{{.State.Running}}", "desktop-control-plane"],
        capture_output=True,
    )
    if control_plane.returncode == 0 and control_plane.stdout.strip() == "true":
        try:
            _run_checked(
                [
                    "docker",
                    "cp",
                    str(archive),
                    "desktop-control-plane:/root/readmission-risk-api.tar",
                ]
            )
            _run_checked(
                [
                    "docker",
                    "exec",
                    "desktop-control-plane",
                    "ctr",
                    "-n",
                    "k8s.io",
                    "images",
                    "import",
                    "/root/readmission-risk-api.tar",
                ]
            )
        finally:
            cleanup = _run(
                [
                    "docker",
                    "exec",
                    "desktop-control-plane",
                    "rm",
                    "-f",
                    "/root/readmission-risk-api.tar",
                ],
                capture_output=True,
            )
            if cleanup.returncode:
                message = cleanup.stderr.strip() or "unknown Docker error"
                print(
                    f"Warning: could not remove temporary image archive: {message}",
                    file=sys.stderr,
                )
        return

    nodes_result = _run_checked(["kubectl", "get", "nodes", "-o", "json"])
    nodes = json.loads(nodes_result.stdout)
    provider_id = nodes["items"][0].get("spec", {}).get("providerID", "")
    if provider_id.startswith("kind://"):
        cluster_name = provider_id.rstrip("/").split("/")[-2]
        if shutil.which("kind"):
            _run_checked(["kind", "load", "docker-image", "--name", cluster_name, image])
            return

    raise RuntimeError(
        "Could not load the image into this Kubernetes cluster. "
        "Expose Docker Desktop's control-plane container or install the kind CLI "
        "for a kind-based cluster."
    )


def run_checks(branch: str, commit: str) -> None:
    repository = _verify_repository(branch, commit)
    os.chdir(repository)

    for command in ("docker", "kubectl"):
        if not shutil.which(command):
            raise RuntimeError(f"Required command {command!r} was not found on PATH.")

    python = sys.executable
    print(f"Running tests for {branch} at {commit}...")
    _run_checked([python, "-m", "pytest"])
    _run_checked([python, "-m", "ruff", "check", "."])

    _run_checked(["kubectl", "cluster-info"])
    nodes_result = _run_checked(["kubectl", "get", "nodes", "-o", "json"])
    if not json.loads(nodes_result.stdout).get("items"):
        raise RuntimeError("The active Kubernetes cluster has no nodes.")
    _run_checked(["kubectl", "get", "deployment/readmission-risk-api", "-n", "default"])

    image = f"readmission-risk-api:pre-push-{commit[:12]}"
    print(f"Building {image}...")
    _run_checked(["docker", "build", "--tag", image, "."])

    with tempfile.TemporaryDirectory(prefix="readmission-risk-api-") as directory:
        archive = Path(directory) / "image.tar"
        _run_checked(["docker", "save", "--output", str(archive), image])
        _load_image(image, archive)

    print("Deploying and waiting for Kubernetes rollout...")
    _run_checked([python, "deploy_with_rollback.py", image, "--timeout", "300s"])

    health_check = (
        "import json, urllib.request; "
        "response = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=10); "
        "body = json.load(response); "
        "assert response.status == 200 and body.get('status') == 'ok', body; "
        "print('Kubernetes API health check passed.')"
    )
    _run_checked(
        [
            "kubectl",
            "exec",
            "deployment/readmission-risk-api",
            "-n",
            "default",
            "--",
            "python",
            "-c",
            health_check,
        ]
    )
    print(f"All pre-push checks passed for {branch} at {commit}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and deploy before pushing master.")
    parser.add_argument("--branch", choices=("master",), required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    try:
        run_checks(args.branch, args.commit)
    except (OSError, RuntimeError, KeyError, ValueError, json.JSONDecodeError) as error:
        print(f"Pre-push check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
