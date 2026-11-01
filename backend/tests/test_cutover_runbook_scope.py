from pathlib import Path

from scripts.import_approval_slice_to_postgres import SCOPE


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_cutover_runbook_documents_current_import_scope():
    runbook = (REPOSITORY_ROOT / "docs/POSTGRES_SQLITE_CUTOVER_RUNBOOK.md").read_text(
        encoding="utf-8"
    )

    assert f"current scope identifier is `{SCOPE}`." in runbook
