from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.release_validation import (
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
    assert summary.errors == []


def test_render_backend_release_validation_markdown_contains_core_sections() -> None:
    summary = build_backend_release_validation_summary()

    markdown = render_backend_release_validation_markdown(summary)

    assert "# Backend Release Validation Summary" in markdown
    assert "Release decision: **PASS**" in markdown
    assert "## Runtime readiness" in markdown
    assert "## Database migrations" in markdown
    assert "## Recovery snapshot validation" in markdown
    assert "## Audit export and retention" in markdown
    assert "## Operator note" in markdown


def test_write_backend_release_validation_summary_creates_markdown_file(tmp_path: Path) -> None:
    output_path = tmp_path / "release" / "backend_release_validation_summary.md"

    written_path = write_backend_release_validation_summary(output_path)

    assert written_path == output_path
    assert written_path.exists()
    content = written_path.read_text(encoding="utf-8")
    assert "# Backend Release Validation Summary" in content
    assert "Release decision: **PASS**" in content
