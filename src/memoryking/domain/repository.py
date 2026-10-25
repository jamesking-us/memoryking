"""Passive, bounded source projections. Never imports or executes inspected source."""

from __future__ import annotations

import ast
import hashlib
import json
import tomllib
from typing import Any

PARSER = "passive-python-ast-v1"
MAX_FILE_BYTES = 1024 * 1024
MAX_NODES = 20000
MAX_PROJECTIONS = 512


def project_source(path: str, raw: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "hash": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "parser": PARSER,
        "coverage": "unsupported",
        "symbols": [],
        "imports": [],
        "calls": [],
        "tests": [],
        "dependencies": [],
        "dynamic_relationships": "unknown",
        "truncated": False,
    }
    if len(raw) > MAX_FILE_BYTES:
        result["coverage"] = "size_limit"
        return result
    try:
        if path.endswith(".py"):
            tree = ast.parse(raw, filename="<repository-data>")
            nodes = []
            for node in ast.walk(tree):
                nodes.append(node)
                if len(nodes) > MAX_NODES:
                    result["coverage"] = "node_limit"
                    return result
            result["coverage"] = "python_syntax"
            for node in nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    # Structural identity includes source location; moves are changes,
                    # never guessed symbol renames. Hash covers decorators and body.
                    ident = f"{path}:{node.name}:{node.lineno}"
                    result["symbols"].append(
                        {
                            "id": ident,
                            "name": node.name,
                            "line": node.lineno,
                            "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                            "hash": hashlib.sha256(
                                ast.dump(node, include_attributes=False).encode()
                            ).hexdigest(),
                        }
                    )
                    if node.name.startswith("test_"):
                        result["tests"].append({"symbol": ident, "status": "discovered_not_run"})
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = (
                        [a.name for a in node.names]
                        if isinstance(node, ast.Import)
                        else ["." * node.level + (node.module or "")]
                    )
                    result["imports"].extend(
                        {"target": name, "line": node.lineno, "resolution": "syntactic_only"}
                        for name in names
                    )
                elif isinstance(node, ast.Call):
                    name = ast.unparse(node.func)
                    result["calls"].append(
                        {"target": name[:256], "line": node.lineno, "resolution": "unresolved"}
                    )
        elif path.rsplit("/", 1)[-1] == "package.json":
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            for group in (
                "dependencies",
                "devDependencies",
                "peerDependencies",
                "optionalDependencies",
            ):
                deps = value.get(group, {})
                if not isinstance(deps, dict):
                    raise ValueError
                result["dependencies"].extend(
                    {
                        "name": str(k)[:256],
                        "declared": str(v)[:256],
                        "group": group,
                        "resolved": False,
                    }
                    for k, v in deps.items()
                )
            result["coverage"] = "manifest_declarations"
        elif path.rsplit("/", 1)[-1] == "pyproject.toml":
            value = tomllib.loads(raw.decode())
            project = value.get("project", {})
            if not isinstance(project, dict):
                raise ValueError
            deps = project.get("dependencies", [])
            if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
                raise ValueError
            result["dependencies"] = [{"declared": d[:512], "resolved": False} for d in deps]
            result["coverage"] = "manifest_declarations"
    except (SyntaxError, ValueError, TypeError, UnicodeError, RecursionError):
        result.update(
            coverage="parse_error", symbols=[], imports=[], calls=[], tests=[], dependencies=[]
        )
    for key in ("symbols", "imports", "calls", "tests", "dependencies"):
        if len(result[key]) > MAX_PROJECTIONS:
            result["truncated"] = True
            result[key] = result[key][:MAX_PROJECTIONS]
    return result
