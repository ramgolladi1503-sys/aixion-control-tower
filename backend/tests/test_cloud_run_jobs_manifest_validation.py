from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from scripts.validate_cloud_run_jobs_manifest import validate_manifest_text

ROOT = Path(__file__).resolve().parents[2]
JOBS_PATH = ROOT / "deploy/gcp/cloud-run-jobs-staging.yaml"
SCHEDULER_PATH = ROOT / "deploy/gcp/cloud-scheduler-staging.tf"


def _valid_inputs() -> tuple[str, str]:
    jobs = JOBS_PATH.read_text(encoding="utf-8")
    scheduler = SCHEDULER_PATH.read_text(encoding="utf-8")
    replacements = {
        "REPLACE_WITH_GCP_PROJECT_NUMBER": "123456789012",
        "REPLACE_WITH_PROJECT_ID": "aixion-staging-123",
        "REPLACE_WITH_REGION": "asia-south1",
        "REPLACE_WITH_SCHEDULER_REGION": "asia-south1",
        "REPLACE_WITH_CLOUDSQL_INSTANCE": "aixion-staging-db",
        "REPLACE_WITH_IMMUTABLE_ARTIFACT_REGISTRY_IMAGE": (
            "asia-south1-docker.pkg.dev/aixion-staging-123/aixion/aixion-api@sha256:"
            + "a" * 64
        ),
        "REPLACE_WITH_PUSH_RUNTIME_SERVICE_ACCOUNT": "aixion-push@aixion-staging-123.iam.gserviceaccount.com",
        "REPLACE_WITH_EMAIL_RUNTIME_SERVICE_ACCOUNT": "aixion-email@aixion-staging-123.iam.gserviceaccount.com",
        "REPLACE_WITH_FIREBASE_PROJECT_ID": "aixion-staging-123",
        "REPLACE_WITH_APPROVED_SMTP_HOST": "smtp.example.org",
        "REPLACE_WITH_VERIFIED_SENDER": "verify@example.org",
        "REPLACE_WITH_PUSH_CRON": "*/5 * * * *",
        "REPLACE_WITH_EMAIL_CRON": "*/5 * * * *",
        "REPLACE_WITH_IANA_TIMEZONE": "UTC",
        "REPLACE_WITH_SCHEDULER_SERVICE_ACCOUNT_ID": "aixion-scheduler",
        "REPLACE_WITH_POSTGRES_SECRET_NAME": "aixion-postgres-dsn",
        "REPLACE_WITH_SMTP_USERNAME_SECRET_NAME": "aixion-smtp-username",
        "REPLACE_WITH_SMTP_PASSWORD_SECRET_NAME": "aixion-smtp-password",
        "REPLACE_WITH_EMAIL_OUTBOX_KEY_SECRET_NAME": "aixion-email-outbox-key",
        "REPLACE_WITH_POSTGRES_SECRET_VERSION": "1",
        "REPLACE_WITH_SMTP_USERNAME_SECRET_VERSION": "1",
        "REPLACE_WITH_SMTP_PASSWORD_SECRET_VERSION": "1",
        "REPLACE_WITH_EMAIL_OUTBOX_KEY_SECRET_VERSION": "1",
    }
    for old, new in replacements.items():
        jobs = jobs.replace(old, new)
        scheduler = scheduler.replace(old, new)
    return jobs, scheduler


def _issues(jobs: str, scheduler: str) -> list[str]:
    return validate_manifest_text(jobs, scheduler)


def test_reviewed_candidate_shape_passes_local_checks() -> None:
    jobs, scheduler = _valid_inputs()
    assert _issues(jobs, scheduler) == []


def test_staging_region_defaults_match_owner_india_direction() -> None:
    jobs = JOBS_PATH.read_text(encoding="utf-8")
    scheduler = SCHEDULER_PATH.read_text(encoding="utf-8")
    documents = list(yaml.safe_load_all(jobs))

    assert len(documents) == 2
    assert all(
        document["metadata"]["labels"]["cloud.googleapis.com/location"]
        == "asia-south1"
        for document in documents
    )
    assert jobs.count(
        "run.googleapis.com/cloudsql-instances: "
        "REPLACE_WITH_PROJECT_ID:asia-south1:REPLACE_WITH_CLOUDSQL_INSTANCE"
    ) == 2
    assert 'default     = "asia-south1"' in scheduler
    assert 'default     = "REPLACE_WITH_SCHEDULER_REGION"' not in scheduler


def test_fcm_sender_role_is_required_and_limited_to_push_runtime_identity() -> None:
    jobs, scheduler = _valid_inputs()
    assert _issues(jobs, scheduler) == []


@pytest.mark.parametrize(
    ("role_block", "member_block"),
    [
        ('permissions = ["cloudmessaging.messages.create", "cloudmessaging.topicSubscriptions.create"]', None),
        (None, None),
        (None, 'member  = "serviceAccount:aixion-email@aixion-staging-123.iam.gserviceaccount.com"'),
    ],
)
def test_fcm_iam_contract_rejects_missing_broad_or_wrong_principal_grants(
    role_block: str | None,
    member_block: str | None,
) -> None:
    jobs, scheduler = _valid_inputs()
    if role_block is None:
        start = scheduler.index('resource "google_project_iam_custom_role" "fcm_sender"')
        end = scheduler.index('\n}', start) + 2
        scheduler = scheduler[:start] + scheduler[end:]
    else:
        scheduler = scheduler.replace(
            'permissions = ["cloudmessaging.messages.create"]', role_block
        )
    if member_block is not None:
        scheduler = scheduler.replace(
            'member  = "serviceAccount:aixion-push@aixion-staging-123.iam.gserviceaccount.com"',
            member_block,
        )
    assert any("FCM sender" in issue for issue in _issues(jobs, scheduler))


def test_checked_in_templates_fail_closed_on_unresolved_inputs() -> None:
    assert _issues(
        JOBS_PATH.read_text(encoding="utf-8"), SCHEDULER_PATH.read_text(encoding="utf-8")
    )


def test_invalid_yaml_fails_closed() -> None:
    _, scheduler = _valid_inputs()
    assert any("invalid Cloud Run Jobs YAML" in issue for issue in _issues("kind: [", scheduler))


def test_mutable_image_tag_is_rejected() -> None:
    jobs, scheduler = _valid_inputs()
    jobs = jobs.replace("@sha256:" + "a" * 64, ":latest", 1)
    assert any("immutable Artifact Registry digest" in issue for issue in _issues(jobs, scheduler))


def test_job_task_fanout_is_rejected() -> None:
    jobs, scheduler = _valid_inputs()
    documents = list(yaml.safe_load_all(jobs))
    documents[0]["spec"]["template"]["spec"]["taskCount"] = 2
    jobs = "\n---\n".join(yaml.safe_dump(doc) for doc in documents)
    assert any("exactly one task" in issue for issue in _issues(jobs, scheduler))


def test_cloud_run_task_retries_are_rejected() -> None:
    jobs, scheduler = _valid_inputs()
    documents = list(yaml.safe_load_all(jobs))
    documents[0]["spec"]["template"]["spec"]["template"]["spec"]["maxRetries"] = 1
    jobs = "\n---\n".join(yaml.safe_dump(doc) for doc in documents)
    assert any("maxRetries to 0" in issue for issue in _issues(jobs, scheduler))


def test_email_secret_is_rejected_from_push_job() -> None:
    jobs, scheduler = _valid_inputs()
    documents = list(yaml.safe_load_all(jobs))
    env = documents[0]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"]
    env.append(
        {
            "name": "AIXION_EMAIL_SMTP_PASSWORD",
            "valueFrom": {"secretKeyRef": {"name": "smtp-password", "key": "1"}},
        }
    )
    jobs = "\n---\n".join(yaml.safe_dump(doc) for doc in documents)
    assert any("provider secrets outside its secret boundary" in issue for issue in _issues(jobs, scheduler))


def test_job_without_oauth_scheduler_authentication_is_rejected() -> None:
    jobs, scheduler = _valid_inputs()
    scheduler = scheduler.replace("oauth_token {", "oidc_token {", 1)
    assert any("OAuth" in issue for issue in _issues(jobs, scheduler))


def test_project_wide_invoker_binding_is_rejected() -> None:
    jobs, scheduler = _valid_inputs()
    scheduler += '\nresource "google_project_iam_member" "broad" { role = "roles/run.invoker" }\n'
    assert any("project-wide IAM" in issue for issue in _issues(jobs, scheduler))


def test_scheduler_retry_is_rejected() -> None:
    jobs, scheduler = _valid_inputs()
    scheduler = scheduler.replace("retry_count = 0", "retry_count = 3", 1)
    assert any("zero scheduler retries" in issue for issue in _issues(jobs, scheduler))


def test_scheduler_must_remain_paused_in_prebilling_template() -> None:
    jobs, scheduler = _valid_inputs()
    scheduler = scheduler.replace("paused           = true", "paused           = false", 1)
    assert any("must remain paused" in issue for issue in _issues(jobs, scheduler))


def test_scheduler_must_use_google_jobs_run_endpoint() -> None:
    jobs, scheduler = _valid_inputs()
    scheduler = scheduler.replace("jobs/aixion-push-dispatch:run", "jobs/other:run", 1)
    assert any("push Scheduler target" in issue for issue in _issues(jobs, scheduler))


def test_scheduler_project_must_match_job_project() -> None:
    jobs, scheduler = _valid_inputs()
    scheduler = scheduler.replace(
        'variable "project_id" {\n  description = "Owner-approved dedicated staging project ID."\n  type        = string\n  default     = "aixion-staging-123"',
        'variable "project_id" {\n  description = "Owner-approved dedicated staging project ID."\n  type        = string\n  default     = "aixion-other-project"',
        1,
    )
    assert any("project_id must match the Job" in issue for issue in _issues(jobs, scheduler))


def test_scheduler_cloud_run_region_must_match_job_region() -> None:
    jobs, scheduler = _valid_inputs()
    scheduler = scheduler.replace(
        'default     = "asia-south1"', 'default     = "us-central1"', 1
    )
    assert any("cloud_run_region must match the Job" in issue for issue in _issues(jobs, scheduler))


def test_invalid_scheduler_service_account_id_is_rejected() -> None:
    jobs, scheduler = _valid_inputs()
    scheduler = scheduler.replace(
        'variable "scheduler_service_account_id" {\n  description = "Dedicated, non-human service account ID used only to invoke the two jobs."\n  type        = string\n  default     = "aixion-scheduler"',
        'variable "scheduler_service_account_id" {\n  description = "Dedicated, non-human service account ID used only to invoke the two jobs."\n  type        = string\n  default     = "Bad_ID"',
        1,
    )
    assert any("Scheduler service account ID must be 6-30" in issue for issue in _issues(jobs, scheduler))


def test_invalid_runtime_service_account_email_is_rejected() -> None:
    jobs, scheduler = _valid_inputs()
    jobs = jobs.replace(
        "aixion-push@aixion-staging-123.iam.gserviceaccount.com",
        "AIXION@aixion-staging-123.iam.gserviceaccount.com",
        1,
    )
    assert any("concrete job-specific runtime service account" in issue for issue in _issues(jobs, scheduler))


def test_scheduler_job_iam_must_be_exactly_scoped() -> None:
    jobs, scheduler = _valid_inputs()
    scheduler = scheduler.replace('name     = "aixion-push-dispatch"', 'name     = "aixion-email-dispatch"', 1)
    assert any("IAM grant must be scoped to that exact job" in issue for issue in _issues(jobs, scheduler))


def test_jobs_must_share_the_same_project_and_region() -> None:
    jobs, scheduler = _valid_inputs()
    documents = list(yaml.safe_load_all(jobs))
    documents[1]["metadata"]["labels"]["cloud.googleapis.com/location"] = "us-central1"
    jobs = "\n---\n".join(yaml.safe_dump(doc) for doc in documents)
    assert any("same project and region" in issue for issue in _issues(jobs, scheduler))


def test_scheduler_schedule_placeholders_are_required_inputs() -> None:
    jobs, scheduler = _valid_inputs()
    scheduler = scheduler.replace('default     = "*/5 * * * *"', 'default     = "REPLACE_WITH_PUSH_CRON"', 1)
    assert any("unresolved" in issue for issue in _issues(jobs, scheduler))


def test_explicit_effective_terraform_overrides_resolve_placeholders() -> None:
    from scripts.validate_cloud_run_jobs_manifest import validate_manifest_text

    jobs, scheduler = _valid_inputs()
    scheduler = scheduler.replace(
        'default     = "aixion-staging-123"',
        'default     = "REPLACE_WITH_PROJECT_ID"',
        1,
    ).replace(
        'default     = "asia-south1"',
        'default     = "REPLACE_WITH_REGION"',
        1,
    )
    issues = validate_manifest_text(
        jobs,
        scheduler,
        terraform_overrides={
            "project_id": ["aixion-staging-123"],
            "cloud_run_region": ["asia-south1"],
            "scheduler_region": ["asia-south1"],
            "push_schedule": ["*/5 * * * *"],
            "email_schedule": ["*/5 * * * *"],
            "scheduler_timezone": ["UTC"],
            "scheduler_service_account_id": ["aixion-scheduler"],
        },
    )
    assert issues == []


def test_effective_terraform_project_override_must_match_jobs() -> None:
    from scripts.validate_cloud_run_jobs_manifest import validate_manifest_text

    jobs, scheduler = _valid_inputs()
    issues = validate_manifest_text(
        jobs,
        scheduler,
        terraform_overrides={"project_id": ["aixion-other-123"]},
    )
    assert any("project_id must match the Job" in issue for issue in issues)


def test_later_terraform_override_takes_precedence() -> None:
    from scripts.validate_cloud_run_jobs_manifest import validate_manifest_text

    jobs, scheduler = _valid_inputs()
    issues = validate_manifest_text(
        jobs,
        scheduler,
        terraform_overrides={"project_id": ["aixion-other-123", "aixion-staging-123"]},
    )
    assert not any("project_id must match the Job" in issue for issue in issues)


def test_tfvars_non_literal_string_fails_closed() -> None:
    from scripts.validate_cloud_run_jobs_manifest import _terraform_string_assignments

    try:
        _terraform_string_assignments('project_id = var.shared_project\n', "project_id")
    except ValueError as error:
        assert "must be a literal string" in str(error)
    else:
        raise AssertionError("non-literal tfvars values must fail closed")


def test_cli_resolves_explicit_tfvars_without_changing_template_files(tmp_path: Path) -> None:
    from scripts.validate_cloud_run_jobs_manifest import main

    jobs, scheduler = _valid_inputs()
    scheduler = scheduler.replace(
        'default     = "aixion-staging-123"',
        'default     = "REPLACE_WITH_PROJECT_ID"',
        1,
    ).replace(
        'default     = "asia-south1"',
        'default     = "REPLACE_WITH_REGION"',
        1,
    )
    jobs_path = tmp_path / "jobs.yaml"
    scheduler_path = tmp_path / "scheduler.tf"
    tfvars_path = tmp_path / "candidate.tfvars"
    jobs_path.write_text(jobs, encoding="utf-8")
    scheduler_path.write_text(scheduler, encoding="utf-8")
    tfvars_path.write_text(
        '\n'.join(
            (
                'project_id = "aixion-staging-123"',
                'cloud_run_region = "asia-south1"',
                'scheduler_region = "asia-south1"',
                'push_schedule = "*/5 * * * *"',
                'email_schedule = "*/5 * * * *"',
                'scheduler_timezone = "UTC"',
                'scheduler_service_account_id = "aixion-scheduler"',
            )
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "--jobs", str(jobs_path),
            "--scheduler", str(scheduler_path),
            "--tfvars", str(tfvars_path),
        ]
    )

    assert result == 0
