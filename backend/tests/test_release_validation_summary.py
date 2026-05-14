from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.release_demo_evidence import build_demo_release_evidence
from app.release_validation import (
    DEFAULT_RELEASE_REPORT_PATH,
    build_backend_release_validation_summary,
    render_backend_release_validation_markdown,
    write_backend_release_validation_summary,
)
from app.store import store


def setup_function() -> None:
    store.reset()


def test_build_backend_release_validation_summary_includes_operational_gates() -> None:
    summary = build_backend_release_validation_summary()

    assert summary.decision == "PASS"
    assert summary.profile in {"local", "demo", "test", "production"}
    assert summary.db_reachable is True
    assert summary.migrations_applied is True
    assert summary.recovery_snapshot_available is True
    assert summary.recovery_snapshot_valid is True
    assert summary.recovery_format_version == "aixion-control-tower-recovery-v1"
    assert summary.audit_retention_policy_version == "aixion-audit-retention-v1"
    assert summary.audit_export_max_limit == 1000
    assert summary.demo_evidence.demo_seed_available is True
    assert summary.demo_evidence.demo_seed_project_id == "project_demo_aixion_control"
    assert summary.demo_evidence.demo_seed_approval_id == "approval_demo_runtime_guard"
    assert summary.demo_evidence.demo_seed_doc_path == "docs/DEMO_DATA_SEEDING.md"
    assert summary.demo_evidence.demo_smoke_doc_path == "docs/DEMO_SMOKE_VALIDATION.md"
    assert summary.demo_evidence.demo_readiness_runbook_path == "docs/DEMO_READINESS_RUNBOOK.md"
    assert summary.demo_evidence.known_limitations_path == "docs/KNOWN_LIMITATIONS.md"
    assert summary.errors == []


def test_demo_release_evidence_reports_missing_smoke_report_as_warning() -> None:
    evidence = build_demo_release_evidence()

    assert evidence.demo_seed_available is True
    assert evidence.demo_smoke_report_path == "docs/release_reports/backend_demo_smoke_summary.md"
    assert any("Demo smoke report not found" in warning for warning in evidence.warnings)


def test_render_backend_release_validation_markdown_contains_core_sections() -> None:
    summary = build_backend_release_validation_summary()

    markdown = render_backend_release_validation_markdown(summary)

    assert "# Backend Release Validation Summary" in markdown
    assert "Release decision: **PASS**" in markdown
    assert "## Runtime readiness" in markdown
    assert "## Database migrations" in markdown
    assert "## Recovery snapshot validation" in markdown
    assert "## Audit export and retention" in markdown
    assert "## Demo readiness evidence" in markdown
    assert "Demo seed tooling available" in markdown
    assert "Demo smoke report decision" in markdown
    assert "## Operator note" in markdown


def test_write_backend_release_validation_summary_creates_markdown_file(tmp_path: Path) -> None:
    output_path = tmp_path / "release" / "backend_release_validation_summary.md"

    written_path = write_backend_release_validation_summary(output_path)

    assert written_path == output_path
    assert written_path.exists()
    content = written_path.read_text(encoding="utf-8")
    assert "# Backend Release Validation Summary" in content
    assert "Release decision: **PASS**" in content
    assert "## Demo readiness evidence" in content


def test_default_release_report_path_stays_under_docs_release_reports() -> None:
    assert DEFAULT_RELEASE_REPORT_PATH.name == "backend_release_validation_summary.md"
    assert DEFAULT_RELEASE_REPORT_PATH.parent.name == "release_reports"
    assert DEFAULT_RELEASE_REPORT_PATH.parent.parent.name == "docs"
