import argparse
import os
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def _run_kubectl(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["kubectl", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    return result


def _publish_rollback_metrics(
    deployment: str,
    succeeded: bool,
    pushgateway_url: str,
) -> None:
    encoded_deployment = quote(deployment, safe="")
    endpoint = (
        f"{pushgateway_url.rstrip('/')}/metrics/job/deployment-rollback"
        f"/deployment/{encoded_deployment}"
    )
    success_value = 1 if succeeded else 0
    body = (
        "# HELP deployment_rollback_last_attempt_success Whether the last rollback completed successfully.\n"
        "# TYPE deployment_rollback_last_attempt_success gauge\n"
        f"deployment_rollback_last_attempt_success {success_value}\n"
        "# HELP deployment_rollback_last_attempt_timestamp_seconds Unix time of the last rollback attempt.\n"
        "# TYPE deployment_rollback_last_attempt_timestamp_seconds gauge\n"
        f"deployment_rollback_last_attempt_timestamp_seconds {time.time()}\n"
    ).encode("utf-8")
    request = Request(endpoint, data=body, method="PUT")
    with urlopen(request, timeout=5) as response:
        if response.status not in (200, 202):
            raise RuntimeError(f"Pushgateway returned HTTP {response.status}.")


def deploy(
    image: str,
    deployment: str = "readmission-risk-api",
    container: str = "readmission-risk-api",
    namespace: str = "default",
    timeout: str = "180s",
    pushgateway_url: str = "http://127.0.0.1:9092",
) -> int:
    set_image = _run_kubectl(
        [
            "set",
            "image",
            f"deployment/{deployment}",
            f"{container}={image}",
            "-n",
            namespace,
        ]
    )
    if set_image.returncode != 0:
        return set_image.returncode

    rollout = _run_kubectl(
        [
            "rollout",
            "status",
            f"deployment/{deployment}",
            "-n",
            namespace,
            f"--timeout={timeout}",
        ]
    )
    if rollout.returncode == 0:
        return 0

    print(
        f"Deployment rollout failed; attempting rollback for {namespace}/{deployment}.",
        file=sys.stderr,
    )
    undo = _run_kubectl(
        ["rollout", "undo", f"deployment/{deployment}", "-n", namespace]
    )
    rollback_succeeded = False
    if undo.returncode == 0:
        rollback_status = _run_kubectl(
            [
                "rollout",
                "status",
                f"deployment/{deployment}",
                "-n",
                namespace,
                f"--timeout={timeout}",
            ]
        )
        rollback_succeeded = rollback_status.returncode == 0

    try:
        _publish_rollback_metrics(deployment, rollback_succeeded, pushgateway_url)
    except (OSError, RuntimeError, URLError) as error:
        print(f"Could not publish rollback metrics: {error}", file=sys.stderr)

    if rollback_succeeded:
        print(f"Rollback completed successfully for {namespace}/{deployment}.")
    else:
        print(f"Rollback failed for {namespace}/{deployment}.", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy an API image and roll back if its rollout fails."
    )
    parser.add_argument("image", help="Container image and tag to deploy")
    parser.add_argument("--deployment", default="readmission-risk-api")
    parser.add_argument("--container", default="readmission-risk-api")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--timeout", default="180s")
    parser.add_argument(
        "--pushgateway-url",
        default=os.environ.get("PUSHGATEWAY_URL", "http://127.0.0.1:9092"),
        help="Pushgateway base URL (default: PUSHGATEWAY_URL or localhost:9092)",
    )
    args = parser.parse_args()

    try:
        return deploy(
            image=args.image,
            deployment=args.deployment,
            container=args.container,
            namespace=args.namespace,
            timeout=args.timeout,
            pushgateway_url=args.pushgateway_url,
        )
    except OSError as error:
        print(f"Deployment command failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
