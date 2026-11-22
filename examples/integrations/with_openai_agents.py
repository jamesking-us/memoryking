"""OpenAI Agents SDK: MemoryKing as function tools, run by a real agent loop with a scripted model.

Needs `openai-agents` (tested with 0.25.0). No provider key and no network: the SDK's own
`ScriptedModel` plays the model. To use a real model, replace `model=` with a model name and
give the SDK your key; everything else stays.
"""

import asyncio

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message, function_call
from demo_store import demo_store

from memoryking.integrations import openai_agents


async def main():
    with demo_store() as (client, _server, _credential):
        model = ScriptedModel(
            [
                [
                    function_call(
                        "remember",
                        {"text": "Raising every timeout broke the deadline."},
                        call_id="c1",
                    )
                ],
                [function_call("recall", {"query": "timeout deadline"}, call_id="c2")],
                [assistant_message("Saved the finding and read it back.")],
            ]
        )
        agent = Agent(
            name="engineer",
            instructions="Call recall before you start and remember what you learn.",
            model=model,  # a real agent: model="gpt-5" (or your provider's name)
            tools=openai_agents.tools(client),
        )
        result = await Runner.run(
            agent, "Note the timeout finding.", run_config=RunConfig(tracing_disabled=True)
        )
        for item in result.new_items:
            if item.type == "tool_call_output_item":
                print("a model would see:", str(item.output).splitlines()[0][:100])
        print("final output:", result.final_output)
    print("OpenAI Agents example passed.")


if __name__ == "__main__":
    asyncio.run(main())
