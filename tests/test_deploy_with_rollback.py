import subprocess

import deploy_with_rollback


def _result(returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")


def test_successful_deployment_does_not_record_a_rollback(monkeypatch):
    results = iter([_result(), _result()])
    monkeypatch.setattr(
        deploy_with_rollback.subprocess,
        "run",
        lambda *args, **kwargs: next(results),
    )
    published = []
    monkeypatch.setattr(
        deploy_with_rollback,
        "_publish_rollback_metrics",
        lambda *args: published.append(args),
    )

    result = deploy_with_rollback.deploy("readmission-risk-api:test")

    assert result == 0
    assert published == []


def test_failed_rollout_records_successful_rollback(monkeypatch):
    results = iter([_result(), _result(returncode=1), _result(), _result()])
    monkeypatch.setattr(
        deploy_with_rollback.subprocess,
        "run",
        lambda *args, **kwargs: next(results),
    )
    published = []
    monkeypatch.setattr(
        deploy_with_rollback,
        "_publish_rollback_metrics",
        lambda *args: published.append(args),
    )

    result = deploy_with_rollback.deploy("readmission-risk-api:test")

    assert result == 1
    assert published == [
        ("readmission-risk-api", True, "http://127.0.0.1:9092")
    ]


def test_failed_rollback_records_failure(monkeypatch):
    results = iter([_result(), _result(returncode=1), _result(returncode=1)])
    monkeypatch.setattr(
        deploy_with_rollback.subprocess,
        "run",
        lambda *args, **kwargs: next(results),
    )
    published = []
    monkeypatch.setattr(
        deploy_with_rollback,
        "_publish_rollback_metrics",
        lambda *args: published.append(args),
    )

    result = deploy_with_rollback.deploy("readmission-risk-api:test")

    assert result == 1
    assert published == [
        ("readmission-risk-api", False, "http://127.0.0.1:9092")
    ]
