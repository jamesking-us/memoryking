"""LlamaIndex: MemoryKing tools and a memory block, with no model.

Needs `llama-index-core` (tested with 0.14.26). The script calls the tools directly and pulls the
memory block the way `Memory` does before each model call.
"""

import asyncio

from demo_store import demo_store
from llama_index.core.base.llms.types import ChatMessage
from llama_index.core.memory import Memory

from memoryking.integrations import llama_index as memoryking_llama_index


async def main():
    with demo_store() as (client, _server, _credential):
        tools = {tool.metadata.name: tool for tool in memoryking_llama_index.tools(client)}
        print("tools:", ", ".join(tools))
        saved = tools["remember"].call(text="The retry deadline is 30 seconds in production.")
        print("a model would see:", saved.content)

        memory = Memory.from_defaults(
            session_id="demo",
            token_limit=30000,
            async_database_uri="sqlite+aiosqlite:///:memory:",
            memory_blocks=[memoryking_llama_index.MemoryKingBlock(client=client, priority=1)],
        )
        await memory.aput(ChatMessage(role="user", content="What is the retry deadline?"))
        for message in await memory.aget():
            print(message.role, "|", str(message.content).replace("\n", " ")[:110])

        # Where a real agent plugs in (this needs an LLM):
        #   from llama_index.core.agent.workflow import FunctionAgent
        #   agent = FunctionAgent(tools=list(tools.values()), llm=llm)
        #   await agent.run("What is the retry deadline?", memory=memory)
    print("LlamaIndex example passed.")


if __name__ == "__main__":
    asyncio.run(main())
