from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from scripts.inventory_persistence import (
    PersistenceVisitor,
    _store_aliases,
    _store_maps,
    inventory,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
LEGACY_PERSISTENCE_CEILINGS = {
    "read": 347,
    # Includes the transactional SQLite TestRun repository's record and audit
    # writes, one audited fail-closed MCP unknown-outcome write in the legacy
    # gateway path, three approval-expiry backfill compatibility writes, two
    # local-only MCP registry compatibility projections (server and audit),
    # and the registration/credential/audit plus token-rotation/revocation
    # local SQLite lifecycle writes in the external-agent adapter.
    # Remove these allowances when those paths no longer publish through maps.
    "write": 168,
    "persist": 118,
    # SQLite external-agent auth, idea, and test-run adapters explicitly bind
    # the existing store connection factory; all remain local/test defaults.
    "direct_boundary_access": 33,
}
LEGACY_MODULE_CEILING = 56


def test_store_maps_are_discovered_from_store_initialization(tmp_path: Path) -> None:
    store_file = tmp_path / "store.py"
    store_file.write_text(
        "class Store:\n"
        "    def __init__(self):\n"
        "        self.approvals = {}\n"
        "        self.audit_events: list = []\n"
        "        self.count = 0\n",
        encoding="utf-8",
    )

    assert _store_maps(store_file) == {"approvals", "audit_events"}


def test_inventory_classifies_reads_writes_mutations_and_persist() -> None:
    source = textwrap.dedent(
        """
        from app.store import store as db
        approval = db.approvals[approval_id]
        db.approvals[approval_id] = approval
        db.approvals[approval_id].status = "APPROVED"
        db.audit_events.append(event)
        db.persist()
        db._connect()
        """
    )
    tree = ast.parse(source)
    visitor = PersistenceVisitor("app/example.py", _store_aliases(tree), {"approvals", "audit_events"})
    visitor.visit(tree)

    pairs = [(event["action"], event["resource"]) for event in visitor.events]
    assert pairs.count(("read", "approvals")) == 1
    assert pairs.count(("write", "approvals")) == 1
    assert pairs.count(("mutable_record_write", "approvals")) == 1
    assert pairs.count(("write", "audit_events")) == 1
    assert pairs.count(("persist", "store")) == 1
    assert pairs.count(("direct_boundary_access", "_connect")) == 1


def test_inventory_attaches_enclosing_route_to_nested_persistence_access() -> None:
    source = textwrap.dedent(
        """
        from app.store import store as db

        @router.post("/approvals")
        def create_approval():
            def load_project():
                return db.projects.get("project-1")
            return load_project()
        """
    )
    tree = ast.parse(source)
    visitor = PersistenceVisitor("app/routes.py", _store_aliases(tree), {"projects"})
    visitor.visit(tree)

    assert len(visitor.events) == 1
    assert visitor.events[0]["owner_chain"] == ["create_approval", "load_project"]
    assert visitor.events[0]["route"] == [{"method": "POST", "path": "/approvals"}]


def test_inventory_report_states_dependency_alias_coverage_precisely() -> None:
    report = inventory(BACKEND_ROOT)
    route_limitations = [
        limitation
        for limitation in report["limitations"]
        if limitation.startswith("Route call-graph attribution")
    ]

    assert len(route_limitations) == 1
    assert "common module-level Depends/Security aliases" in route_limitations[0]
    assert "does not resolve every dependency-factory pattern" in route_limitations[0]


def test_inventory_expands_static_api_route_methods() -> None:
    source = textwrap.dedent(
        """
        from app.store import store

        @router.api_route("/sessions", methods=["GET", "DELETE"])
        def sessions():
            return store.sessions
        """
    )
    tree = ast.parse(source)
    visitor = PersistenceVisitor("app/routes.py", _store_aliases(tree), {"sessions"})
    visitor.visit(tree)

    assert visitor.events[0]["route"] == [
        {"method": "GET", "path": "/sessions"},
        {"method": "DELETE", "path": "/sessions"},
    ]


def test_background_task_inventory_follows_imported_entrypoint_and_local_calls(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "store.py").write_text(
        "class Store:\n"
        "    def __init__(self):\n"
        "        self.notifications = {}\n"
        "store = Store()\n",
        encoding="utf-8",
    )
    (app / "main.py").write_text(
        "import asyncio\n"
        "from .worker import worker_loop\n"
        "async def lifespan(app):\n"
        "    asyncio.create_task(worker_loop())\n",
        encoding="utf-8",
    )
    (app / "worker.py").write_text(
        "import asyncio\n"
        "from .store import store\n"
        "async def worker_loop():\n"
        "    await asyncio.to_thread(dispatch_one)\n"
        "def dispatch_one():\n"
        "    store.notifications.clear()\n",
        encoding="utf-8",
    )

    report = inventory(tmp_path)

    assert report["format_version"] == "aixion-persistence-inventory-v7"
    assert report["background_tasks"] == [{
        "source_module": "app.main",
        "source_function": "lifespan",
        "line": 4,
        "mechanism": "asyncio.create_task",
        "entrypoint": "app.worker:worker_loop",
        "reachable_functions": ["app.worker:dispatch_one", "app.worker:worker_loop"],
        "persistence_access_count": 1,
        "persistence_accesses_by_action": {"write": 1},
        "persistence_accesses": report["background_tasks"][0]["persistence_accesses"],
    }]
    access = report["background_tasks"][0]["persistence_accesses"][0]
    assert access["path"] == "app/worker.py"
    assert access["owner_chain"] == ["dispatch_one"]


def test_application_lifespan_workers_are_joined_to_sqlite_accesses() -> None:
    report = inventory(BACKEND_ROOT)
    tasks = {item["entrypoint"]: item for item in report["background_tasks"]}

    assert set(tasks) == {
        "app.main:_approval_recovery_loop",
        "app.main:_approval_expiry_loop",
        "app.notifications:notification_dispatch_loop",
        "app.postgres_notification_dispatcher:postgres_notification_dispatch_loop",
        "app.email_delivery:email_dispatch_loop",
        "unresolved:to_thread",
    }
    assert tasks["app.main:_approval_recovery_loop"]["persistence_access_count"] > 0
    assert tasks["app.main:_approval_expiry_loop"]["persistence_access_count"] > 0
    assert tasks["app.notifications:notification_dispatch_loop"]["persistence_access_count"] > 0
    assert tasks["app.email_delivery:email_dispatch_loop"]["persistence_access_count"] > 0


def test_script_inventory_follows_main_guard_imported_entrypoint_and_persistence(
    tmp_path: Path,
) -> None:
    app = tmp_path / "app"
    scripts = tmp_path / "scripts"
    app.mkdir()
    scripts.mkdir()
    (app / "store.py").write_text(
        """class Store:
    def __init__(self):
        self.approvals = {}
store = Store()
""",
        encoding="utf-8",
    )
    (app / "operations.py").write_text(
        """from .store import store
def import_approvals():
    return store.approvals.values()
""",
        encoding="utf-8",
    )
    (scripts / "import_data.py").write_text(
        """from app.operations import import_approvals
if __name__ == '__main__':
    raise SystemExit(import_approvals())
    run_legacy_helper()
""",
        encoding="utf-8",
    )

    report = inventory(tmp_path)

    assert len(report["script_entrypoints"]) == 1
    script = report["script_entrypoints"][0]
    assert script["script_module"] == "scripts.import_data"
    assert script["entrypoints"] == [{
        "line": 3,
        "name": "import_approvals",
        "target": "app.operations:import_approvals",
    }]
    assert script["unresolved_calls"] == [{"line": 4, "name": "run_legacy_helper"}]
    assert script["reachable_functions"] == ["app.operations:import_approvals"]
    assert script["persistence_access_count"] == 1
    assert script["persistence_accesses"][0]["path"] == "app/operations.py"


def test_real_backend_script_inventory_lists_import_and_worker_commands() -> None:
    report = inventory(BACKEND_ROOT)
    scripts = {item["script_module"]: item for item in report["script_entrypoints"]}

    assert "scripts.import_approval_slice_to_postgres" in scripts
    assert "scripts.run_mission_control_worker" in scripts
    assert scripts["scripts.import_approval_slice_to_postgres"]["entrypoints"]


def test_route_inventory_follows_imported_persistence_helper(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "store.py").write_text(
        "class Store:\n"
        "    def __init__(self):\n"
        "        self.projects = {}\n"
        "store = Store()\n",
        encoding="utf-8",
    )
    (app / "routes.py").write_text(
        "from .services import load_project\n"
        "@router.get('/projects/{project_id}')\n"
        "def get_project(project_id):\n"
        "    return load_project(project_id)\n",
        encoding="utf-8",
    )
    (app / "services.py").write_text(
        "from .store import store\n"
        "def load_project(project_id):\n"
        "    return store.projects.get(project_id)\n",
        encoding="utf-8",
    )

    report = inventory(tmp_path)
    routes = report["route_persistence_accesses"]

    assert len(routes) == 1
    assert routes[0]["entrypoint"] == "app.routes:get_project"
    assert routes[0]["method"] == "GET"
    assert routes[0]["path"] == "/projects/{project_id}"
    assert routes[0]["reachable_functions"] == [
        "app.routes:get_project",
        "app.services:load_project",
    ]
    assert routes[0]["persistence_accesses_by_action"] == {"read": 1}
    assert routes[0]["persistence_accesses"][0]["path"] == "app/services.py"


def test_route_inventory_resolves_literal_router_prefix(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "store.py").write_text(
        "class Store:\n"
        "    def __init__(self):\n"
        "        self.projects = {}\n"
        "store = Store()\n",
        encoding="utf-8",
    )
    (app / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "from .store import store\n"
        "router = APIRouter(prefix='/v1/projects')\n"
        "@router.get('/{project_id}')\n"
        "def get_project(project_id):\n"
        "    return store.projects.get(project_id)\n",
        encoding="utf-8",
    )

    report = inventory(tmp_path)

    assert report["route_persistence_accesses"][0]["path"] == "/{project_id}"
    assert report["route_persistence_accesses"][0]["router_prefix"] == "/v1/projects"
    assert report["route_persistence_accesses"][0]["mounted_path"] == "/v1/projects/{project_id}"


def test_application_routes_report_cross_module_persistence_helpers() -> None:
    report = inventory(BACKEND_ROOT)
    approval_decision = next(
        route
        for route in report["route_persistence_accesses"]
        if route["entrypoint"] == "app.main:decide_approval"
        and route["method"] == "POST"
        and route["path"] == "/approvals/{approval_id}/decision"
    )

    assert any(
        function.startswith("app.approval_")
        for function in approval_decision["reachable_functions"]
    )
    assert "app.auth:require_user" in approval_decision["reachable_functions"]
    assert any(
        access["path"] == "app/auth.py" and access["resource"] in {"users", "sessions"}
        for access in approval_decision["persistence_accesses"]
    )
    assert approval_decision["persistence_access_count"] > 12


def test_legacy_persistence_access_does_not_grow() -> None:
    report = inventory(BACKEND_ROOT)

    for action, ceiling in LEGACY_PERSISTENCE_CEILINGS.items():
        assert report["totals"].get(action, 0) <= ceiling, (
            f"Legacy SQLite {action} access grew beyond {ceiling}; "
            "route new persistence through a repository boundary."
        )
    assert report["modules_with_persistence_access"] <= LEGACY_MODULE_CEILING, (
        "Legacy SQLite persistence has spread to more modules; "
        "extend an existing repository boundary instead."
    )
