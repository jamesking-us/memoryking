"""OpenAI Agents SDK adapter against real SDK classes and a scripted model; no provider is called."""

import json
import unittest

from support import StoreCase, installed

if installed("agents"):
    from agents import Agent, FunctionTool, RunConfig, Runner
    from agents.testing import ScriptedModel, assistant_message, function_call
    from agents.tool_context import ToolContext

    from memoryking.integrations import openai_agents as adapter


def invoke(tool, arguments):
    text = json.dumps(arguments)
    context = ToolContext(
        context=None, tool_name=tool.name, tool_call_id="call-1", tool_arguments=text
    )
    return tool.on_invoke_tool(context, text)


@unittest.skipUnless(installed("agents"), "openai-agents is not installed")
class OpenAIAgentsTests(StoreCase):
    def test_tools_are_function_tools_with_the_lean_schemas(self):
        from memoryking import lean

        tools = adapter.tools(self.client)
        self.assertEqual(
            [tool.name for tool in tools],
            ["remember", "recall", "save_progress", "resume", "hand_off"],
        )
        catalog = {item["name"]: item for item in lean.TOOLS}
        for tool in tools:
            self.assertIsInstance(tool, FunctionTool)
            self.assertEqual(tool.params_json_schema, catalog[tool.name]["inputSchema"])
            self.assertFalse(tool.strict_json_schema)
        self.assertEqual(len(adapter.tools(self.client, ("recall",))), 1)

    async def test_invoking_a_tool_reaches_the_store(self):
        tools = {tool.name: tool for tool in adapter.tools(self.client)}
        saved = json.loads(
            await invoke(tools["remember"], {"text": "Raising every timeout broke the deadline"})
        )
        briefing = await invoke(tools["recall"], {"query": "timeout deadline"})
        self.assertIn(saved["observation"], briefing)
        refused = json.loads(await invoke(tools["resume"], {"task": "never-saved"}))
        self.assertEqual(refused["error"], "checkpoint_unavailable")

    async def test_a_real_agent_loop_with_a_scripted_model(self):
        model = ScriptedModel(
            [
                [
                    function_call(
                        "remember",
                        {"text": "Raising every timeout broke the deadline", "source": "pytest"},
                        call_id="c1",
                    )
                ],
                [function_call("recall", {"query": "timeout deadline"}, call_id="c2")],
                [assistant_message("Saved and briefed.")],
            ]
        )
        agent = Agent(name="demo", model=model, tools=adapter.tools(self.client))
        result = await Runner.run(
            agent,
            "Remember the timeout finding, then recall it.",
            run_config=RunConfig(tracing_disabled=True),
        )
        model.assert_complete()
        self.assertEqual(result.final_output, "Saved and briefed.")
        outputs = [
            str(item.output) for item in result.new_items if item.type == "tool_call_output_item"
        ]
        self.assertEqual(len(outputs), 2)
        self.assertEqual(json.loads(outputs[0])["position"], 1)
        self.assertIn("Raising every timeout broke the deadline", outputs[1])
        self.assertIn(
            "Raising every timeout broke the deadline", self.client.recall("timeout").text
        )


if __name__ == "__main__":
    unittest.main()
