from __future__ import annotations

from pathlib import Path

from aixion_relay.contracts import ActionProposal, CapabilityActionType
from aixion_relay.normalization import normalize_action_proposal


def test_absolute_workspace_path_becomes_repository_relative(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    target = workspace / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("pass\n", encoding="utf-8")

    normalized = normalize_action_proposal(
        ActionProposal(
            action_type=CapabilityActionType.MODIFY_FILES,
            paths=[str(target)],
        ),
        workspace_path=str(workspace),
    )

    assert normalized.paths == ["backend/app.py"]
    assert normalized.metadata["outside_workspace_paths"] == []
    assert normalized.metadata["original_paths"] == [str(target)]


def test_outside_workspace_path_is_preserved_and_marked(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"

    normalized = normalize_action_proposal(
        ActionProposal(
            action_type=CapabilityActionType.MODIFY_FILES,
            paths=[str(outside)],
        ),
        workspace_path=str(workspace),
    )

    assert normalized.paths == [outside.resolve().as_posix()]
    assert normalized.metadata["outside_workspace_paths"] == [str(outside)]


def test_relative_parent_escape_is_marked(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    normalized = normalize_action_proposal(
        ActionProposal(
            action_type=CapabilityActionType.READ_REPOSITORY,
            paths=["../outside.txt"],
        ),
        workspace_path=str(workspace),
    )

    assert normalized.paths == ["../outside.txt"]
    assert normalized.metadata["outside_workspace_paths"] == ["../outside.txt"]


def test_network_urls_are_reduced_to_unique_lowercase_domains(tmp_path: Path) -> None:
    normalized = normalize_action_proposal(
        ActionProposal(
            action_type=CapabilityActionType.ACCESS_NETWORK,
            network_domains=[
                "https://API.GITHUB.COM/repos/owner/repo",
                "api.github.com",
                "example.com.",
            ],
        ),
        workspace_path=str(tmp_path),
    )

    assert normalized.network_domains == ["api.github.com", "example.com"]
