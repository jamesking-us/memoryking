#!/usr/bin/env python3
"""Install the products the long-history benchmark compares against, each into a directory of its
own under /tmp/mk-lh-<name>/ (its own virtualenv or npm prefix, its own HOME where the product
writes one). Nothing global, nothing in the repository, nothing in the real HOME.

Versions and hashes are pinned by the lock files in `locks/` (npm `integrity` entries, `uv pip
compile --generate-hashes` output). Network use: the npm and PyPI registries, plus
`huggingface.co` when a product downloads an embedding model the first time it runs.

Usage: install_systems.py [--root /tmp] [--force] [name ...]
Names: mcp-memory, basic-memory, claude-mem, mcp-memory-service, mem0, graphiti (default: all)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCKS = HERE / "locks"
UV = os.environ.get("LONGHAUL_UV", shutil.which("uv") or "uv")
PYTHON_VERSION = "3.12"


def run(cmd, **kw):
    print("+", " ".join(str(x) for x in cmd), flush=True)
    subprocess.run([str(x) for x in cmd], check=True, **kw)


def base_env(home: Path, extra: dict | None = None) -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        in {
            "PATH",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
        }
        or k.upper() in {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}
    }
    env.update(
        HOME=str(home), XDG_CACHE_HOME=str(home / ".cache"), UV_CACHE_DIR=str(home / ".cache/uv")
    )
    env.update(extra or {})
    return env


def fresh(target: Path, force: bool) -> bool:
    if target.exists():
        if not force:
            print(f"{target} exists; skipping (use --force to reinstall)")
            return False
        shutil.rmtree(target)
    target.mkdir(parents=True)
    return True


def npm_ci(lock_dir: Path, prefix: Path, home: Path) -> None:
    prefix.mkdir(parents=True, exist_ok=True)
    for f in ("package.json", "package-lock.json"):
        shutil.copy(lock_dir / f, prefix / f)
    run(
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--cache", home / ".npm"],
        cwd=prefix,
        env=base_env(home),
    )


def install_mcp_memory(root: Path, force: bool) -> None:
    t = root / "mk-lh-mcp-memory"
    if not fresh(t, force):
        return
    (t / "home").mkdir()
    npm_ci(LOCKS / "mcp-memory", t / "prefix", t / "home")
    # the server declares node >= 24; the adapter runs it with LONGHAUL_NODE24 (default /opt/node24/bin/node)
    print("note: @modelcontextprotocol/server-memory needs Node 24 or later to run")


def fetch_commit(repo: str, commit: str, dest: Path, env: dict) -> None:
    """A shallow checkout of one commit (the plugin or hook sources a product's docs install)."""
    dest.mkdir(parents=True)
    env = {**env, "GIT_TERMINAL_PROMPT": "0"}
    run(["git", "init", "-q"], cwd=dest, env=env)
    run(["git", "fetch", "-q", "--depth", "1", repo, commit], cwd=dest, env=env)
    run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest, env=env)


# Plugin and hook sources, at the release tags that match the pinned packages.
BASIC_MEMORY_REPO = "https://github.com/basicmachines-co/basic-memory.git"
BASIC_MEMORY_PLUGIN_COMMIT = "c9bd187b83baba549d5cd16bacc180f8023d51c8"  # tag v1.0.0
MMS_REPO = "https://github.com/doobidoo/mcp-memory-service.git"
MMS_HOOKS_COMMIT = "d22f074da21d18385541bf2c77061d79518557b0"  # tag v13.0.0


def install_basic_memory(root: Path, force: bool) -> None:
    t = root / "mk-lh-basic-memory"
    if not fresh(t, force):
        return
    (t / "home").mkdir()
    env = base_env(t / "home")
    run([UV, "venv", "--python", PYTHON_VERSION, t / "venv"], env=env)
    run(
        [
            UV,
            "pip",
            "install",
            "--python",
            t / "venv/bin/python",
            "--require-hashes",
            "-r",
            LOCKS / "basic-memory.lock",
        ],
        env=env,
    )
    run([t / "venv/bin/basic-memory", "--version"], env=env)
    fetch_commit(BASIC_MEMORY_REPO, BASIC_MEMORY_PLUGIN_COMMIT, t / "src", env)


def install_claude_mem(root: Path, force: bool) -> None:
    t = root / "mk-lh-claude-mem"
    if not fresh(t, force):
        return
    home = t / "home"
    home.mkdir()
    npm_ci(LOCKS / "claude-mem", t / "prefix", home)
    cli = t / "prefix/node_modules/claude-mem/dist/npx-cli/index.js"
    env = base_env(home, {"CLAUDE_MEM_ONLINE_OPTIN": "false"})
    # The documented installer, run under the isolated HOME. `--provider claude` is the form that
    # needs no claude-mem account (it uses the Claude login the host already has); it makes no
    # model call and `--no-auto-start` keeps it from starting a worker. The installer also tries
    # to reach cmem.ai for an optional sign-in; that fails harmlessly where the host is blocked.
    run(
        ["node", cli, "install", "--ide", "claude-code", "--provider", "claude", "--no-auto-start"],
        env=env,
        stdin=subprocess.DEVNULL,
    )
    run(["node", cli, "telemetry", "disable"], env=env, stdin=subprocess.DEVNULL)


def install_mcp_memory_service(root: Path, force: bool) -> None:
    t = root / "mk-lh-mcp-memory-service"
    if not fresh(t, force):
        return
    (t / "home").mkdir()
    env = base_env(t / "home")
    run([UV, "venv", "--python", PYTHON_VERSION, t / "venv"], env=env)
    run(
        [
            UV,
            "pip",
            "install",
            "--python",
            t / "venv/bin/python",
            "--require-hashes",
            "-r",
            LOCKS / "mcp-memory-service.lock",
        ],
        env=env,
    )
    fetch_commit(MMS_REPO, MMS_HOOKS_COMMIT, t / "src", env)


def install_mem0(root: Path, force: bool) -> None:
    t = root / "mk-lh-mem0"
    if not fresh(t, force):
        return
    (t / "home").mkdir()
    env = base_env(t / "home")
    run([UV, "venv", "--python", PYTHON_VERSION, t / "venv"], env=env)
    # mem0ai 2.2.1 with its [nlp] extra (BM25 and entity boosting), FastEmbed for the local
    # embedding model, and the spaCy model the extra needs, all pinned with hashes.
    run(
        [
            UV,
            "pip",
            "install",
            "--python",
            t / "venv/bin/python",
            "--require-hashes",
            "-r",
            LOCKS / "mem0.lock",
        ],
        env=env,
    )
    run([t / "venv/bin/python", "-c", "import mem0, spacy; spacy.load('en_core_web_sm')"], env=env)


GRAPHITI_REPO = "https://github.com/getzep/graphiti.git"
GRAPHITI_COMMIT = (
    "9b8a4630092084734b080f2e6f409c2c23bdcc3a"  # mcp_server 1.1.3 on graphiti-core 0.31.x
)


def install_graphiti(root: Path, force: bool) -> None:
    t = root / "mk-lh-graphiti"
    if not fresh(t, force):
        return
    (t / "home").mkdir()
    env = base_env(
        t / "home", {"GIT_TERMINAL_PROMPT": "0", "UV_PROJECT_ENVIRONMENT": str(t / "venv")}
    )
    src = t / "src"
    src.mkdir()
    run(["git", "init", "-q"], cwd=src, env=env)
    run(["git", "fetch", "-q", "--depth", "1", GRAPHITI_REPO, GRAPHITI_COMMIT], cwd=src, env=env)
    run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=src, env=env)
    # The reference MCP server and graphiti-core, exactly as the project's own uv.lock pins them
    # (hashes included); `providers` brings sentence-transformers, the documented local embedder.
    run(
        [UV, "sync", "--frozen", "--python", PYTHON_VERSION, "--extra", "providers"],
        cwd=src / "mcp_server",
        env=env,
    )
    # The embedded graph database (a FalkorDB server binary shipped inside the wheel), no container.
    run(
        [
            UV,
            "pip",
            "install",
            "--python",
            t / "venv/bin/python",
            "--require-hashes",
            "--no-deps",
            "-r",
            LOCKS / "falkordblite.lock",
        ],
        env=env,
    )


INSTALLERS = {
    "mcp-memory": install_mcp_memory,
    "basic-memory": install_basic_memory,
    "claude-mem": install_claude_mem,
    "mcp-memory-service": install_mcp_memory_service,
    "mem0": install_mem0,
    "graphiti": install_graphiti,
}


def register(name: str, fn) -> None:
    INSTALLERS[name] = fn


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("names", nargs="*")
    ap.add_argument(
        "--root", default="/tmp", help="parent directory of the mk-lh-<name> directories"
    )
    ap.add_argument("--force", action="store_true", help="delete and reinstall")
    a = ap.parse_args()
    names = a.names or list(INSTALLERS)
    for n in names:
        if n not in INSTALLERS:
            sys.exit(f"unknown product {n!r}; choose from {', '.join(INSTALLERS)}")
    for n in names:
        print(f"== {n}", flush=True)
        INSTALLERS[n](Path(a.root), a.force)


if __name__ == "__main__":
    main()
