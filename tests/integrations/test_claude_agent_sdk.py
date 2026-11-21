"""Claude Agent SDK adapter against the real SDK classes and a real daemon; no model is called."""

import json
import unittest
from pathlib import Path

from support import StoreCase, installed

if installed("claude_agent_sdk"):
    import claude_agent_sdk

    from memoryking.integrations import claude_agent_sdk as adapter


def _failed(result):
    """`isError` in mcp 1.x, `is_error` in mcp 2.x."""
    return bool(getattr(result, "is_error", getattr(result, "isError", False)))


@unittest.skipUnless(installed("claude_agent_sdk"), "claude-agent-sdk is not installed")
class ClaudeAgentSdkTests(StoreCase):
    def test_mcp_config_is_the_lean_bridge_command(self):
        config = adapter.mcp_server_config(
            "checkout",
            socket=self.server.path,
            credential=self.credential,
            bindings={"environment": "prod"},
        )
        self.assertEqual(config["type"], "stdio")
        args = config["args"]
        self.assertEqual(args[:2], ["-m", "memoryking.adapters.stdio"])
        self.assertEqual(args[args.index("--profile") + 1], "lean")
        self.assertEqual(args[args.index("--binding") + 1], "environment=prod")
        self.assertEqual(args[args.index("--credential") + 1], str(self.credential.resolve()))
        self.assertNotIn(self.credential.read_text().strip(), json.dumps(config))
        options = claude_agent_sdk.ClaudeAgentOptions(
            mcp_servers={"memoryking": config},
            allowed_tools=adapter.allowed_tool_names(),
        )
        self.assertIn("mcp__memoryking__recall", options.allowed_tools)

    async def test_the_bridge_command_really_serves_the_lean_tools(self):
        # Run the exact command the SDK's Claude Code would, over stdio, as MCP.
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        config = adapter.mcp_server_config(
            "checkout", socket=self.server.path, credential=self.credential
        )
        params = StdioServerParameters(command=config["command"], args=config["args"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                self.assertEqual(
                    [tool.name for tool in listed.tools][:5],
                    ["remember", "recall", "save_progress", "resume", "hand_off"],
                )
                saved = await session.call_tool("remember", {"text": "bridge works"})
                self.assertFalse(_failed(saved))

    def test_profile_resolves_to_absolute_paths(self):
        root = Path(self.temp.name)
        config = root / "config.toml"
        config.write_text(
            f'[profiles.demo]\nstore = "{root / "data"}"\ncontrol = "{root / "control"}"\n'
        )
        config.chmod(0o600)
        resolved = adapter.mcp_server_config("checkout", profile="demo", config=config)
        args = resolved["args"]
        self.assertEqual(args[args.index("--socket") + 1], str(root / "data" / "service.sock"))

    async def test_sdk_tools_run_against_the_store(self):
        server = adapter.sdk_server(self.client)
        self.assertEqual(server["type"], "sdk")
        self.assertEqual(server["name"], "memoryking")
        self.assertIsNotNone(server["instance"])

        tools = {tool.name: tool for tool in adapter.sdk_tools(self.client)}
        self.assertEqual(list(tools), ["remember", "recall", "save_progress", "resume", "hand_off"])
        self.assertIsInstance(tools["recall"], claude_agent_sdk.SdkMcpTool)
        self.assertTrue(tools["recall"].annotations.model_dump(by_alias=True)["readOnlyHint"])
        self.assertIn("query", tools["recall"].input_schema["properties"])

        saved = await tools["remember"].handler(
            {"text": "Raising every timeout broke the deadline"}
        )
        self.assertNotIn("is_error", saved)
        observation = json.loads(saved["content"][0]["text"])["observation"]
        briefing = await tools["recall"].handler({"query": "timeout deadline"})
        self.assertNotIn("is_error", briefing)
        self.assertIn(observation, briefing["content"][0]["text"])
        refused = await tools["resume"].handler({"task": "never-saved"})
        self.assertTrue(refused["is_error"])
        self.assertEqual(
            json.loads(refused["content"][0]["text"])["error"], "checkpoint_unavailable"
        )
        invalid = await tools["recall"].handler({})
        self.assertTrue(invalid["is_error"])

    async def test_session_context_and_pre_compact_hook(self):
        self.client.save_progress("retry-fix", "Fix the retry deadline", "Investigating")
        context = await adapter.session_start_hook(self.client, task="retry-fix")(
            {"hook_event_name": "SessionStart", "session_id": "s1"}, None, {"signal": None}
        )
        added = context["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(context["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("Fix the retry deadline", added)
        self.assertIn(
            "Fix the retry deadline", adapter.session_context(self.client, task="retry-fix")
        )

        transcript = Path(self.temp.name) / "transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {"type": "user", "message": {"role": "user", "content": "the retry deadline fails"}}
            )
            + "\n"
        )
        registered = adapter.hooks(self.client, task="retry-fix")
        self.assertIsInstance(registered["PreCompact"][0], claude_agent_sdk.HookMatcher)
        callback = registered["PreCompact"][0].hooks[0]
        event = {
            "hook_event_name": "PreCompact",
            "session_id": "s1",
            "transcript_path": str(transcript),
            "cwd": self.temp.name,
            "trigger": "auto",
            "custom_instructions": None,
        }
        self.assertEqual(await callback(event, None, {"signal": None}), {})
        self.assertEqual(await callback(event, None, {"signal": None}), {})  # replays
        self.assertIn("retry deadline fails", self.client.recall("retry deadline").text)
        self.assertIn("retry deadline fails", self.client.resume("retry-fix--mkhook").text)

    async def test_hook_failure_is_a_message_not_an_exception(self):
        transcript = Path(self.temp.name) / "transcript.txt"
        transcript.write_text("something worth saving")
        event = {
            "hook_event_name": "PreCompact",
            "session_id": "s",
            "transcript_path": str(transcript),
            "trigger": "auto",
        }
        callback = adapter.pre_compact_hook(self.client)
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        try:
            reply = await callback(event, None, {"signal": None})
            self.assertIn("could not save before compaction", reply["systemMessage"])
        finally:
            import threading

            from memoryking.adapters.local_ipc import Server

            self.server = Server(self.store)
            self.thread = threading.Thread(target=self.server.serve_forever)
            self.thread.start()

    def test_shared_clients_are_refused_for_hooks(self):
        class Shared:  # stands in for a SharedClient, which has no local socket
            project = "checkout"

        from memoryking.domain.local import LocalError

        with self.assertRaises(LocalError):
            adapter.session_context(Shared())


if __name__ == "__main__":
    unittest.main()
