"""Explicit local reference-agent commands; no autonomous or network model execution."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from memoryking.contracts.canonical import ContractError, canonical_bytes, load_strict
from memoryking.domain.local import LocalError
from memoryking.harness import LocalFileTool, ReferenceHarness
from memoryking.harness_context import LocalRecordingProvider
from memoryking.sdk import LocalClient


def state_file(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(131073)
    if len(raw) > 131072:
        raise LocalError("checkpoint_input_limit")
    value = load_strict(raw)
    if not isinstance(value, dict):
        raise LocalError("invalid_checkpoint")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("socket", "credential", "journal", "tool-root"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--run")
    parser.add_argument(
        "--reader", action="append", help="Additional checkpoint reader; start only"
    )
    commands = parser.add_subparsers(dest="action", required=True)
    start = commands.add_parser("start", help="Durably register explicit task state")
    start.add_argument("--state", required=True, type=Path)
    for name in ("status", "resume", "reconcile", "close"):
        commands.add_parser(name)
    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("--reason", required=True)
    update = commands.add_parser("update")
    update.add_argument("--state", required=True, type=Path)
    constraint = commands.add_parser("constraint")
    constraint.add_argument("--text", required=True)
    create = commands.add_parser("create-file")
    create.add_argument("--operation", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--content", required=True, help="Explicit UTF-8 contents; exclude secrets")
    artifact = commands.add_parser("store-artifact")
    artifact.add_argument("--name", required=True)
    snapshot = commands.add_parser(
        "refresh-repository",
        help="Capture an explicitly registered clone and declare the change scope",
    )
    snapshot.add_argument("--repository", required=True)
    snapshot.add_argument("--clone", required=True)
    snapshot.add_argument("--environment", required=True)
    snapshot.add_argument("--path", action="append", required=True)
    for command in (update, constraint, create, artifact, snapshot):
        command.add_argument("--approve", action="store_true", help="Explicit host authorization")
    model = commands.add_parser(
        "model-turn", help="The local recorder by default; a policy adapter with --provider"
    )
    model.add_argument("--provider-root", type=Path, help="Local recorder directory")
    model.add_argument("--egress-policy", type=Path, help="The daemon's egress policy file")
    model.add_argument("--provider", help="An adapter named in the policy, e.g. openai-chat")
    model.add_argument("--model", help="A model the policy allows for that provider")
    handoff = commands.add_parser("prepare-handoff", help="Prepare only; does not send a message")
    handoff.add_argument("--recipient", required=True)
    handoff.add_argument("--stage", required=True)
    handoff.add_argument("--role", required=True)
    args = parser.parse_args()
    harness = None
    try:
        if hasattr(args, "approve") and not args.approve:
            raise LocalError("host_authorization_required")
        if args.reader and args.action != "start":
            raise LocalError("readers_only_at_start")
        client = LocalClient(args.socket, args.credential, args.project)
        readers = (
            None
            if not args.reader
            else sorted(set([client.last_context["principal"], *args.reader]))
        )
        harness = ReferenceHarness(
            client,
            args.journal,
            LocalFileTool(args.tool_root),
            task=args.task,
            run=args.run,
            readers=readers,
            state=state_file(args.state) if args.action == "start" else None,
        )
        result: Any
        if args.action in {"start", "status"}:
            result = harness.status()
        elif args.action == "resume":
            result = harness.resume()
        elif args.action == "reconcile":
            result = harness.reconcile()
        elif args.action == "checkpoint":
            result = harness.checkpoint(args.reason)
        elif args.action == "update":
            harness.update_state(state_file(args.state))
            result = harness.status()
        elif args.action == "constraint":
            harness.accept_constraint(args.text, approved=True)
            result = harness.status()
        elif args.action == "create-file":
            result = harness.create_file(args.operation, args.name, args.content, approved=True)
        elif args.action == "store-artifact":
            result = harness.store_artifact(args.name, approved=True)
        elif args.action == "refresh-repository":
            result = harness.refresh_repository(
                args.repository, args.clone, args.environment, args.path, approved=True
            )
        elif args.action == "model-turn":
            if args.provider:
                from memoryking.adapters.egress_policy import EgressPolicy
                from memoryking.providers import adapter

                if not args.egress_policy or not args.model:
                    raise LocalError("egress_policy_invalid")
                policy = EgressPolicy.load(args.egress_policy)
                result = harness.model_turn(adapter(policy, args.provider, args.model))
            else:
                if args.provider_root is None:
                    raise LocalError("provider_handle_invalid")
                result = harness.model_turn(LocalRecordingProvider(args.provider_root))
        elif args.action == "prepare-handoff":
            result = harness.prepare_handoff(args.recipient, args.role, args.stage)
        else:
            harness.close()
            result = {"status": "closed", "checkpointed": True}
        sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
    except (LocalError, ContractError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, LocalError) else "invalid_local_input"
        sys.stderr.write(code + "\n")
        raise SystemExit(2) from None
    finally:
        if harness is not None:
            # Each mutating command captured its boundary already. Only explicit
            # close adds shutdown; inspect/resume must not supersede a handoff.
            harness.close(checkpoint=False)


if __name__ == "__main__":
    main()
