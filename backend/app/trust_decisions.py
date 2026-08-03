from __future__ import annotations

from .store import store
from .trust_models import PolicyDecision


def latest_policy_decision(action_id: str) -> PolicyDecision | None:
    decisions = [
        item
        for item in store.policy_decisions.values()
        if item.proposed_action_id == action_id
    ]
    if not decisions:
        return None
    return max(decisions, key=lambda item: (item.evaluated_at, item.id))
