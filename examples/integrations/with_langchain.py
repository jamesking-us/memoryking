"""LangChain tools and the LangGraph store, with no model.

Needs `langchain-core` (1.8.4) and `langgraph` (1.2.24). The script invokes the tools the way an
agent's tool node does and shows which store operations MemoryKing honours and which it refuses.
"""

from demo_store import demo_store

from memoryking.integrations import langchain as memoryking_langchain


def main():
    with demo_store() as (client, _server, _credential):
        tools = {tool.name: tool for tool in memoryking_langchain.tools(client)}
        print("tools:", ", ".join(tools))
        saved = tools["remember"].invoke({"text": "Raising every timeout broke the deadline."})
        print("a model would see:", saved)
        print(tools["recall"].invoke({"query": "timeout deadline"}).splitlines()[0])

        # The store: put is a capture, search is a briefing, delete asks for retirement.
        store = memoryking_langchain.MemoryKingStore(client)
        project = ("checkout",)
        store.put((*project, "notes"), "deadline", {"value": "the retry deadline is 30 seconds"})
        store.put((*project, "notes"), "deadline", {"value": "the retry deadline is 45 seconds"})
        item = store.search(project, query="retry deadline")[0]
        print("briefing status:", item.value["status"], "| sources:", len(item.value["sources"]))
        for call in (
            lambda: store.get((*project, "notes"), "deadline"),
            lambda: store.search(project, query="x", filter={"kind": "note"}),
        ):
            try:
                call()
            except NotImplementedError as refusal:
                print("refused:", refusal)
        print("retire request for", item.value["sources"][0]["id"][:12], "...")
        store.delete((*project, "notes"), item.value["sources"][0]["id"])

        # Where a real agent plugs in (this needs a chat model):
        #   from langgraph.prebuilt import create_react_agent
        #   agent = create_react_agent(model, list(tools.values()), store=store)
    print("LangChain example passed.")


if __name__ == "__main__":
    main()
