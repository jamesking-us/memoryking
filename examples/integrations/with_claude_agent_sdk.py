"""Claude Agent SDK: MCP configuration, in-process tools, session start and pre-compaction hooks.

Needs `claude-agent-sdk` (tested with 0.2.187). No model is called: the script builds the
options a real agent would use and drives the tools and hooks itself.
"""

import asyncio
import json
import tempfile
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions
from demo_store import demo_store

from memoryking.integrations import claude_agent_sdk as memoryking_sdk


async def main():
    with demo_store() as (client, server, credential):
        client.save_progress("retry-fix", "Fix the retry deadline", "Investigating")

        # 1. The lean bridge as an MCP server the SDK's Claude Code process starts itself.
        bridge = memoryking_sdk.mcp_server_config(
            "checkout", socket=server.path, credential=credential
        )
        print("bridge command:", bridge["args"][:2], "... --profile lean")

        # 2. Or the same five tools in-process, bound to this client; no bridge needed.
        tools = {tool.name: tool for tool in memoryking_sdk.sdk_tools(client)}
        reply = await tools["remember"].handler(
            {"text": "Raising every timeout broke the deadline."}
        )
        print("a model would see:", reply["content"][0]["text"])
        reply = await tools["resume"].handler({"task": "no-such-task"})
        print("a refusal:", reply["is_error"], reply["content"][0]["text"][:80])

        # 3. Load the task's state when the session starts: append it to the system prompt.
        context = memoryking_sdk.session_context(client, task="retry-fix")
        print("session context starts:", context.splitlines()[0])

        # 4. Save what a compaction would drop. The SDK calls this with the hook's input.
        with tempfile.TemporaryDirectory(prefix="mk-demo-", dir="/tmp") as directory:
            transcript = Path(directory) / "transcript.jsonl"
            turn = {
                "type": "user",
                "message": {"role": "user", "content": "The deadline still fails."},
            }
            transcript.write_text(json.dumps(turn) + "\n")
            options = ClaudeAgentOptions(
                # In-process tools; or mcp_servers={"memoryking": bridge}.
                mcp_servers={"memoryking": memoryking_sdk.sdk_server(client)},
                allowed_tools=memoryking_sdk.allowed_tool_names(),
                system_prompt={"type": "preset", "preset": "claude_code", "append": context},
                hooks=memoryking_sdk.hooks(client, task="retry-fix"),
            )
            compact = options.hooks["PreCompact"][0].hooks[0]
            event = {
                "hook_event_name": "PreCompact",
                "session_id": "demo",
                "transcript_path": str(transcript),
                "trigger": "auto",
                "custom_instructions": None,
            }
            print("pre-compact hook returned:", await compact(event, None, {"signal": None}))
            print(
                "saved by the hook:",
                "The deadline still fails." in client.recall("deadline fails").text,
            )

        # Where a real agent loop plugs in (this needs Claude Code and credentials):
        #   from claude_agent_sdk import query
        #   async for message in query(prompt="Fix the retry deadline", options=options):
        #       print(message)
    print("Claude Agent SDK example passed.")


if __name__ == "__main__":
    asyncio.run(main())
