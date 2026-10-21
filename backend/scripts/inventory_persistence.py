#!/usr/bin/env python3
"""Read-only AST inventory of application persistence access patterns.

The report is a refactoring aid, not proof that every runtime access has been
found. Dynamic access, aliases created outside imports, and mutations through
objects returned by helper functions require manual review.
"""
from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _store_maps(store_path: Path) -> set[str]:
    tree = ast.parse(store_path.read_text(encoding="utf-8"), filename=str(store_path))
    maps: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        is_collection = isinstance(value, (ast.Dict, ast.List, ast.Set))
        is_factory = (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in {"dict", "list", "set"}
        )
        if not is_collection and not is_factory:
            continue
        for target in targets:
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                if target.value.id == "self":
                    maps.add(target.attr)
    return maps


def _store_aliases(tree: ast.Module) -> set[str]:
    aliases = {"store"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.endswith("store"):
            continue
        for imported in node.names:
            if imported.name == "store":
                aliases.add(imported.asname or imported.name)
    return aliases


def _is_map_root(node: ast.AST, aliases: set[str], maps: set[str]) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases
    ):
        if node.attr in maps:
            return node.attr
    return None


def _contains_target(target: ast.AST, candidate: ast.AST) -> bool:
    return any(node is candidate for node in ast.walk(target))


def _module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_symbols(tree: ast.Module, module_name: str) -> dict[str, str]:
    symbols: dict[str, str] = {}
    package = module_name.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: max(0, len(package) - node.level + 1)]
                imported_module = ".".join([*base, *(node.module or "").split(".")]).strip(".")
            else:
                imported_module = node.module or ""
            for imported in node.names:
                if imported.name == "*":
                    continue
                alias = imported.asname or imported.name
                symbols[alias] = f"{imported_module}:{imported.name}"
        elif isinstance(node, ast.Import):
            for imported in node.names:
                alias = imported.asname or imported.name.split(".")[0]
                symbols[alias] = f"{imported.name}:"
    return symbols


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _function_nodes(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _router_prefixes(tree: ast.Module) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        else:
            continue
        if not isinstance(value, ast.Call):
            continue
        constructor = _call_name(value.func)
        if constructor != "APIRouter":
            continue
        prefix_node = next(
            (keyword.value for keyword in value.keywords if keyword.arg == "prefix"),
            None,
        )
        prefix = prefix_node.value if isinstance(prefix_node, ast.Constant) and isinstance(prefix_node.value, str) else ""
        for target in targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def _call_graph(
    trees: dict[str, ast.Module],
    module_functions: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]],
    imports: dict[str, dict[str, str]],
) -> dict[str, set[str]]:
    aliases: dict[str, dict[str, str]] = {}
    for module, tree in trees.items():
        aliases[module] = {}
        functions = module_functions[module]
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                targets = statement.targets
                value = statement.value
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
                value = statement.value
            else:
                continue
            if not isinstance(value, ast.Call):
                continue
            if isinstance(value.func, ast.Name) and value.func.id in {"Depends", "Security"}:
                reference = _call_name(value.args[0]) if value.args else None
            else:
                reference = _call_name(value.func)
            if reference is None or not (
                reference in functions or reference in imports[module]
            ):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    aliases[module][target.id] = reference

    def resolve(module: str, name: str, seen: set[tuple[str, str]] | None = None) -> str | None:
        visited = set() if seen is None else seen
        key = (module, name)
        if key in visited:
            return None
        visited.add(key)
        if name in module_functions.get(module, {}):
            return f"{module}:{name}"
        if name in aliases.get(module, {}):
            return resolve(module, aliases[module][name], visited)
        imported = imports.get(module, {}).get(name)
        if imported and ":" in imported:
            imported_module, imported_name = imported.split(":", 1)
            return resolve(imported_module, imported_name, visited)
        return None

    graph: dict[str, set[str]] = {}
    for module, functions in module_functions.items():
        for function_name, function in functions.items():
            source = f"{module}:{function_name}"
            graph[source] = set()
            references: list[str] = []
            for call in ast.walk(function):
                if not isinstance(call, ast.Call):
                    continue
                called_names = [_call_name(call.func)]
                if (
                    isinstance(call.func, ast.Name)
                    and call.func.id in {"Depends", "Security"}
                    and call.args
                ):
                    called_names.append(_call_name(call.args[0]))
                if (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "to_thread"
                    and call.args
                ):
                    # asyncio.to_thread receives the worker callable as a Name,
                    # not as a nested Call expression.
                    called_names.append(_call_name(call.args[0]))
                references.extend(name for name in called_names if name is not None)
            defaults = [*function.args.defaults, *function.args.kw_defaults]
            references.extend(
                value.id
                for value in defaults
                if isinstance(value, ast.Name)
            )
            graph[source].update(
                target
                for reference in references
                if (target := resolve(module, reference)) is not None
            )
    for module, module_aliases in aliases.items():
        for alias_name, reference in module_aliases.items():
            target = resolve(module, reference)
            if target is not None:
                graph[f"{module}:{alias_name}"] = {target}
    return graph


def _reachable_functions(graph: dict[str, set[str]], entrypoint: str) -> set[str]:
    reachable: set[str] = set()
    pending = [entrypoint] if entrypoint in graph else []
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(graph.get(current, set()) - reachable)
    return reachable


def _owner_module(event: dict[str, Any]) -> str | None:
    if not event["owner_chain"]:
        return None
    return event["path"].removesuffix(".py").replace("/", ".")


def _route_persistence_accesses(
    module_functions: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]],
    module_router_prefixes: dict[str, dict[str, str]],
    graph: dict[str, set[str]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for module, functions in module_functions.items():
        for function_name, function in functions.items():
            metadata = PersistenceVisitor._route_metadata(function)
            if not metadata:
                continue
            entrypoint = f"{module}:{function_name}"
            reachable = _reachable_functions(graph, entrypoint)
            route_events = [
                event
                for event in events
                if event["owner_chain"]
                and f"{_owner_module(event)}:{event['owner_chain'][0]}" in reachable
            ]
            prefixes = module_router_prefixes.get(module, {})
            route_prefix = next(
                (
                    prefixes[decorator.func.value.id]
                    for decorator in function.decorator_list
                    if isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id in prefixes
                ),
                "",
            )
            for route in metadata:
                counts = Counter(event["action"] for event in route_events)
                local_path = route["path"]
                mounted_path = (
                    f"{route_prefix.rstrip('/')}/{local_path.lstrip('/')}"
                    if route_prefix
                    else local_path
                ) or "/"
                routes.append(
                    {
                        "entrypoint": entrypoint,
                        "router_prefix": route_prefix,
                        "mounted_path": mounted_path,
                        **route,
                        "reachable_functions": sorted(reachable),
                        "persistence_access_count": len(route_events),
                        "persistence_accesses_by_action": dict(sorted(counts.items())),
                        "persistence_accesses": route_events,
                    }
                )
    return sorted(routes, key=lambda item: (item["entrypoint"], item["method"], item["path"]))


def _scheduled_background_tasks(
    trees: dict[str, ast.Module],
    module_functions: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]],
    imports: dict[str, dict[str, str]],
    graph: dict[str, set[str]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for module, tree in trees.items():
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "create_task"
                and _call_name(call.func.value) == "asyncio"
                and call.args
                and isinstance(call.args[0], ast.Call)
            ):
                continue
            entry_name = _call_name(call.args[0].func)
            if not entry_name:
                continue
            if entry_name in module_functions.get(module, {}):
                entrypoint = f"{module}:{entry_name}"
            elif entry_name in imports.get(module, {}):
                imported_module, imported_name = imports[module][entry_name].split(":", 1)
                entrypoint = f"{imported_module}:{imported_name}"
            else:
                entrypoint = f"unresolved:{entry_name}"

            caller: str | None = None
            ancestor = parents.get(call)
            while ancestor is not None:
                if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    caller = ancestor.name
                    break
                ancestor = parents.get(ancestor)

            reachable = _reachable_functions(graph, entrypoint)
            worker_events = [
                event
                for event in events
                if event["owner_chain"]
                and f"{_owner_module(event)}:{event['owner_chain'][0]}" in reachable
            ]
            event_counts = Counter(event["action"] for event in worker_events)
            tasks.append(
                {
                    "source_module": module,
                    "source_function": caller,
                    "line": call.lineno,
                    "mechanism": "asyncio.create_task",
                    "entrypoint": entrypoint,
                    "reachable_functions": sorted(reachable),
                    "persistence_access_count": len(worker_events),
                    "persistence_accesses_by_action": dict(sorted(event_counts.items())),
                    "persistence_accesses": worker_events,
                }
            )
    return sorted(tasks, key=lambda item: (item["source_module"], item["line"]))


def _script_entrypoints(
    trees: dict[str, ast.Module],
    module_functions: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]],
    imports: dict[str, dict[str, str]],
    graph: dict[str, set[str]],
    events: list[dict[str, Any]],
    script_modules: set[str],
) -> list[dict[str, Any]]:
    """Inventory direct calls made by executable backend scripts.

    This deliberately recognizes only the conventional ``__main__`` guard.
    Shell wrappers, task schedulers, dynamic imports, and arbitrary top-level
    side effects remain explicit limitations rather than inferred coverage.
    """
    reports: list[dict[str, Any]] = []
    builtin_calls = set(dir(__builtins__)) | {"SystemExit"}
    for module in sorted(script_modules):
        tree = trees[module]
        for guard in ast.walk(tree):
            if not isinstance(guard, ast.If):
                continue
            test = guard.test
            is_main_guard = (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "__main__"
            )
            if not is_main_guard:
                continue
            calls: dict[tuple[int, str], str] = {}
            unresolved_calls: set[tuple[int, str]] = set()
            for call in ast.walk(guard):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                    continue
                name = call.func.id
                imported = imports.get(module, {}).get(name)
                if name in module_functions.get(module, {}):
                    target = f"{module}:{name}"
                elif imported and ":" in imported:
                    imported_module, imported_name = imported.split(":", 1)
                    target = f"{imported_module}:{imported_name}"
                else:
                    if name not in builtin_calls:
                        unresolved_calls.add((call.lineno, name))
                    continue
                calls[(call.lineno, name)] = target

            if not calls:
                reports.append({
                    "script_module": module,
                    "guard_line": guard.lineno,
                    "entrypoints": [],
                    "unresolved_calls": [
                        {"line": line, "name": name}
                        for line, name in sorted(unresolved_calls)
                    ],
                    "reachable_functions": [],
                    "persistence_access_count": 0,
                    "persistence_accesses": [],
                })
                continue

            targets = sorted(set(calls.values()))
            reachable: set[str] = set()
            for target in targets:
                reachable.update(_reachable_functions(graph, target))
            script_path = module.replace(".", "/") + ".py"
            script_events = [
                event
                for event in events
                if (
                    event["owner_chain"]
                    and f"{_owner_module(event)}:{event['owner_chain'][0]}" in reachable
                )
                or (event["path"] == script_path and not event["owner_chain"])
            ]
            reports.append({
                "script_module": module,
                "guard_line": guard.lineno,
                "entrypoints": [
                    {"line": line, "name": name, "target": target}
                    for (line, name), target in sorted(calls.items())
                ],
                "unresolved_calls": [
                    {"line": line, "name": name}
                    for line, name in sorted(unresolved_calls)
                ],
                "reachable_functions": sorted(reachable),
                "persistence_access_count": len(script_events),
                "persistence_accesses": script_events,
            })
    return reports


class PersistenceVisitor(ast.NodeVisitor):
    def __init__(self, source: str, aliases: set[str], maps: set[str]) -> None:
        self.source = source
        self.aliases = aliases
        self.maps = maps
        self.parents: dict[ast.AST, ast.AST] = {}
        self.events: list[dict[str, Any]] = []
        self.owner_stack: list[dict[str, Any]] = []

    def visit(self, node: ast.AST) -> Any:
        for child in ast.iter_child_nodes(node):
            self.parents[child] = node
        return super().visit(node)

    def _event(self, node: ast.AST, action: str, resource: str) -> None:
        route = next(
            (owner["route"] for owner in reversed(self.owner_stack) if owner.get("route")),
            None,
        )
        self.events.append(
            {
                "path": self.source,
                "line": getattr(node, "lineno", 1),
                "column": getattr(node, "col_offset", 0) + 1,
                "action": action,
                "resource": resource,
                "owner_chain": [owner["name"] for owner in self.owner_stack],
                "route": route,
            }
        )

    @staticmethod
    def _route_metadata(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict[str, str]]:
        routes: list[dict[str, str]] = []
        http_methods = {"get", "post", "put", "patch", "delete", "options", "head"}
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            method = decorator.func.attr.lower()
            if method not in http_methods | {"api_route"}:
                continue
            path_node = decorator.args[0] if decorator.args else next(
                (keyword.value for keyword in decorator.keywords if keyword.arg == "path"),
                None,
            )
            if not isinstance(path_node, ast.Constant) or not isinstance(path_node.value, str):
                continue
            if method == "api_route":
                methods_node = next(
                    (keyword.value for keyword in decorator.keywords if keyword.arg == "methods"),
                    None,
                )
                if not isinstance(methods_node, (ast.List, ast.Tuple)):
                    continue
                methods = [
                    value.value.upper()
                    for value in methods_node.elts
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                ]
            else:
                methods = [method.upper()]
            routes.extend({"method": value, "path": path_node.value} for value in methods)
        return routes

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.owner_stack.append({"name": node.name, "route": self._route_metadata(node)})
        self.generic_visit(node)
        self.owner_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        map_name = _is_map_root(node, self.aliases, self.maps)
        if map_name:
            parent = self.parents.get(node)
            action = "read"
            ancestor = node
            traversed_model_attribute = False
            while isinstance(
                (ancestor_parent := self.parents.get(ancestor)),
                (ast.Subscript, ast.Attribute),
            ):
                if isinstance(ancestor_parent, ast.Attribute):
                    if ancestor_parent.value is not ancestor:
                        break
                    traversed_model_attribute = True
                elif ancestor_parent.value is not ancestor:
                    break
                ancestor = ancestor_parent
            assignment = self.parents.get(ancestor)
            if isinstance(assignment, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
                targets = (
                    assignment.targets
                    if isinstance(assignment, (ast.Assign, ast.Delete))
                    else [assignment.target]
                )
                if any(_contains_target(target, node) for target in targets):
                    action = "mutable_record_write" if traversed_model_attribute else "write"
            elif isinstance(parent, ast.Attribute) and parent.value is node:
                call = self.parents.get(parent)
                if isinstance(call, ast.Call) and call.func is parent:
                    if parent.attr in {"append", "extend", "insert", "pop", "popitem", "clear", "update", "setdefault", "remove"}:
                        action = "write"
            self._event(node, action, map_name)
        elif isinstance(node.value, ast.Name) and node.value.id in self.aliases:
            if node.attr == "persist":
                parent = self.parents.get(node)
                if isinstance(parent, ast.Call) and parent.func is node:
                    self._event(node, "persist", "store")
            elif node.attr in {"_connect", "atomic"}:
                self._event(node, "direct_boundary_access", node.attr)
        self.generic_visit(node)


def inventory(root: Path) -> dict[str, Any]:
    app_root = root / "app"
    store_path = app_root / "store.py"
    if not store_path.is_file():
        raise FileNotFoundError(f"expected store module at {store_path}")
    maps = _store_maps(store_path)
    events: list[dict[str, Any]] = []
    module_counts: dict[str, Counter[str]] = {}
    trees: dict[str, ast.Module] = {}
    module_functions: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    module_router_prefixes: dict[str, dict[str, str]] = {}
    imports: dict[str, dict[str, str]] = {}
    app_paths = sorted(app_root.rglob("*.py"))
    scripts_root = root / "scripts"
    script_paths = sorted(scripts_root.rglob("*.py")) if scripts_root.is_dir() else []
    script_modules: set[str] = set()
    script_events: list[dict[str, Any]] = []
    for path in [*app_paths, *script_paths]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(root).as_posix()
        module = _module_name(path, root)
        trees[module] = tree
        module_functions[module] = _function_nodes(tree)
        module_router_prefixes[module] = _router_prefixes(tree)
        imports[module] = _imported_symbols(tree, module)
        visitor = PersistenceVisitor(relative_path, _store_aliases(tree), maps)
        visitor.visit(tree)
        if path.is_relative_to(scripts_root):
            script_modules.add(module)
            script_events.extend(visitor.events)
        else:
            events.extend(visitor.events)
        counts = module_counts.setdefault(relative_path, Counter())
        if not path.is_relative_to(scripts_root):
            counts.update(event["action"] for event in visitor.events)

    totals = Counter(event["action"] for event in events)
    modules = {
        name: dict(sorted(counts.items()))
        for name, counts in sorted(module_counts.items())
        if counts
    }
    graph = _call_graph(trees, module_functions, imports)
    route_accesses = _route_persistence_accesses(
        module_functions, module_router_prefixes, graph, events
    )
    background_tasks = _scheduled_background_tasks(
        trees, module_functions, imports, graph, events
    )
    script_entrypoints = _script_entrypoints(
        trees,
        module_functions,
        imports,
        graph,
        [*events, *script_events],
        script_modules,
    )
    return {
        "format_version": "aixion-persistence-inventory-v7",
        "source_root": app_root.as_posix(),
        "store_maps": sorted(maps),
        "totals": dict(sorted(totals.items())),
        "modules_with_persistence_access": len(modules),
        "modules": modules,
        "accesses": sorted(events, key=lambda item: (item["path"], item["line"], item["column"], item["action"])),
        "route_persistence_accesses": route_accesses,
        "background_tasks": background_tasks,
        "script_entrypoints": script_entrypoints,
        "limitations": [
            "AST inventory is static and may miss dynamic attribute access or indirect mutations.",
            "A map read followed by mutation of a returned object may appear as a read unless represented as a direct assignment target.",
            "Route metadata resolves literal APIRouter prefixes in the route's module; mounted sub-app prefixes, included-router prefixes, dynamic prefixes, and dynamically generated routes still require manual review.",
            "Route call-graph attribution resolves direct imported and same-module calls plus common module-level Depends/Security aliases; it does not resolve every dependency-factory pattern, callbacks, dynamic imports, or router prefixes.",
            "Background call graphs resolve direct imports and same-module calls only; dynamic scheduling and callbacks require manual review.",
            "Script entrypoints include only direct imported or same-module calls under a conventional __main__ guard; unresolved bare calls are reported but not resolved. Shell wrappers, task schedulers, package entrypoint metadata, dynamic imports, and arbitrary top-level side effects require manual review.",
            "Review raw SQL, computed aliases, callbacks, and runtime-loaded modules separately.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="backend directory containing app/store.py (default: this script's backend)",
    )
    args = parser.parse_args()
    print(json.dumps(inventory(args.backend_root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
