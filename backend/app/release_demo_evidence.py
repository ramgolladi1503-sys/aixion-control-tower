from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from .demo_data import DEMO_APPROVAL_ID, DEMO_PROJECT_ID

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SMOKE_REPORT_PATH = REPO_ROOT / "docs" / "release_reports" / "backend_demo_smoke_summary.md"
DEMO_DATA_DOC_PATH = "docs/DEMO_DATA_SEEDING.md"
DEMO_SMOKE_DOC_PATH = "docs/DEMO_SMOKE_VALIDATION.md"
DEMO_READINESS_RUNBOOK_PATH = "docs/DEMO_READINESS_RUNBOOK.md"
KNOWN_LIMITATIONS_PATH = "docs/KNOWN_LIMITATIONS.md"


class DemoReleaseEvidence(BaseModel):
    demo_seed_available: bool = True
    demo_seed_project_id: str = DEMO_PROJECT_ID
    demo_seed_approval_id: str = DEMO_APPROVAL_ID
    demo_seed_doc_path: str = DEMO_DATA_DOC_PATH
    demo_smoke_report_path: str = str(DEFAULT_SMOKE_REPORT_PATH.relative_to(REPO_ROOT))
    demo_smoke_report_available: bool = False
    demo_smoke_report_decision: str | None = None
    demo_smoke_doc_path: str = DEMO_SMOKE_DOC_PATH
    demo_readiness_runbook_path: str = DEMO_READINESS_RUNBOOK_PATH
    known_limitations_path: str = KNOWN_LIMITATIONS_PATH
    warnings: list[str]


def _path_exists(relative_path: str) -> bool:
    return (REPO_ROOT / relative_path).exists()


def _smoke_report_decision(path: Path = DEFAULT_SMOKE_REPORT_PATH) -> str | None:
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    if "Smoke decision: **PASS**" in content:
        return "PASS"
    if "Smoke decision: **FAIL**" in content:
        return "FAIL"
    return "UNKNOWN"


def build_demo_release_evidence() -> DemoReleaseEvidence:
    warnings: list[str] = []
    smoke_decision = _smoke_report_decision()
    if smoke_decision is None:
        warnings.append("Demo smoke report not found; run scripts/run_demo_smoke_validation.py before demo sign-off.")
    elif smoke_decision != "PASS":
        warnings.append(f"Demo smoke report decision is {smoke_decision}; review before demo sign-off.")

    for path in [
        DEMO_DATA_DOC_PATH,
        DEMO_SMOKE_DOC_PATH,
        DEMO_READINESS_RUNBOOK_PATH,
        KNOWN_LIMITATIONS_PATH,
    ]:
        if not _path_exists(path):
            warnings.append(f"Demo/release documentation missing: {path}")

    return DemoReleaseEvidence(
        demo_smoke_report_available=smoke_decision is not None,
        demo_smoke_report_decision=smoke_decision,
        warnings=warnings,
    )
