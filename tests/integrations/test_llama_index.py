"""LlamaIndex tools and memory block against the real classes and a real daemon."""

import json
import unittest

from support import StoreCase, installed

if installed("llama_index"):
    from llama_index.core.base.llms.types import ChatMessage
    from llama_index.core.memory import Memory
    from llama_index.core.tools import FunctionTool

    from memoryking.integrations import llama_index as adapter


@unittest.skipUnless(installed("llama_index"), "llama-index-core is not installed")
class LlamaIndexTests(StoreCase):
    def test_tools_are_function_tools_with_the_lean_schemas(self):
        from memoryking import lean

        tools = adapter.tools(self.client)
        catalog = {item["name"]: item for item in lean.TOOLS}
        self.assertEqual(
            [tool.metadata.name for tool in tools],
            ["remember", "recall", "save_progress", "resume", "hand_off"],
        )
        for tool in tools:
            self.assertIsInstance(tool, FunctionTool)
            self.assertEqual(
                tool.metadata.get_parameters_dict(), catalog[tool.metadata.name]["inputSchema"]
            )
            openai = tool.metadata.to_openai_tool()
            self.assertEqual(openai["function"]["name"], tool.metadata.name)
            self.assertEqual(
                openai["function"]["parameters"], catalog[tool.metadata.name]["inputSchema"]
            )

    def test_call_reaches_the_store(self):
        tools = {tool.metadata.name: tool for tool in adapter.tools(self.client)}
        saved = tools["remember"].call(text="Raising every timeout broke the deadline")
        self.assertFalse(saved.is_error)
        observation = json.loads(saved.content)["observation"]
        self.assertIn(observation, tools["recall"].call(query="timeout deadline").content)
        refused = tools["resume"].call(task="never-saved")
        self.assertEqual(json.loads(refused.content)["error"], "checkpoint_unavailable")

    async def test_acall(self):
        tools = {tool.metadata.name: tool for tool in adapter.tools(self.client)}
        saved = await tools["remember"].acall(text="async finding")
        self.assertEqual(json.loads(saved.content)["position"], 1)

    async def test_memory_block_briefs_the_latest_user_message(self):
        self.client.remember("The retry deadline is 30 seconds in production.", source="runbook")
        block = adapter.MemoryKingBlock(client=self.client)
        found = await block.aget(
            [
                ChatMessage(role="user", content="Hello"),
                ChatMessage(role="assistant", content="Hi"),
                ChatMessage(role="user", content="What is the retry deadline?"),
            ]
        )
        self.assertIn("The retry deadline is 30 seconds in production.", found)
        self.assertEqual(await block.aget([ChatMessage(role="assistant", content="Hi")]), "")
        before = self.client.remember("probe").position
        await block.aput([ChatMessage(role="user", content="please store this transcript line")])
        await block.aput(
            [ChatMessage(role="user", content="and this")], from_short_term_memory=True
        )
        self.assertEqual(self.client.remember("probe two").position, before + 1)  # nothing written

    async def test_memory_block_inside_llama_index_memory(self):
        self.client.remember("The retry deadline is 30 seconds in production.", source="runbook")
        memory = Memory.from_defaults(
            session_id="s1",
            token_limit=30000,
            async_database_uri="sqlite+aiosqlite:///:memory:",
            memory_blocks=[adapter.MemoryKingBlock(client=self.client, priority=1)],
        )
        await memory.aput(ChatMessage(role="user", content="What is the retry deadline?"))
        history = await memory.aget()
        joined = "\n".join(str(message.content) for message in history)
        self.assertIn("The retry deadline is 30 seconds in production.", joined)


if __name__ == "__main__":
    unittest.main()
