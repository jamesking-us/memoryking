"""LangChain tools and the LangGraph store against the real classes and a real daemon."""

import json
import unittest

from support import StoreCase, installed

HAVE_CORE = installed("langchain_core")
HAVE_GRAPH = installed("langgraph")
if HAVE_CORE:
    from langchain_core.tools import StructuredTool

    from memoryking.integrations import langchain as adapter
if HAVE_GRAPH:
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode
    from langgraph.store.base import BaseStore, InvalidNamespaceError, SearchItem


@unittest.skipUnless(HAVE_CORE, "langchain-core is not installed")
class LangChainToolTests(StoreCase):
    def test_tools_are_structured_tools_with_the_lean_schemas(self):
        from memoryking import lean

        tools = adapter.tools(self.client)
        self.assertEqual(
            [tool.name for tool in tools],
            ["remember", "recall", "save_progress", "resume", "hand_off"],
        )
        catalog = {item["name"]: item for item in lean.TOOLS}
        for tool in tools:
            self.assertIsInstance(tool, StructuredTool)
            self.assertEqual(tool.args_schema, catalog[tool.name]["inputSchema"])
            self.assertEqual(tool.description, catalog[tool.name]["description"])

    def test_invoke_reaches_the_store(self):
        tools = {tool.name: tool for tool in adapter.tools(self.client)}
        saved = json.loads(
            tools["remember"].invoke({"text": "Raising every timeout broke the deadline"})
        )
        self.assertEqual(saved["position"], 1)
        self.assertIn(saved["observation"], tools["recall"].invoke({"query": "timeout deadline"}))
        refused = json.loads(tools["resume"].invoke({"task": "never-saved"}))
        self.assertEqual(refused["error"], "checkpoint_unavailable")
        invalid = json.loads(tools["recall"].invoke({"query": "x", "budget": 1}))
        self.assertEqual(invalid["error"], "invalid_tool_arguments")

    async def test_ainvoke(self):
        tools = {tool.name: tool for tool in adapter.tools(self.client)}
        saved = json.loads(await tools["remember"].ainvoke({"text": "async finding"}))
        self.assertIn(saved["observation"], await tools["recall"].ainvoke({"query": "async"}))

    @unittest.skipUnless(HAVE_GRAPH, "langgraph is not installed")
    async def test_tool_node_runs_a_tool_call_message(self):
        from langchain_core.messages import AIMessage

        node = ToolNode(adapter.tools(self.client))
        message = AIMessage(
            content="",
            tool_calls=[{"name": "remember", "args": {"text": "from a tool node"}, "id": "call-1"}],
        )
        graph = StateGraph(MessagesState)
        graph.add_node("tools", node)
        graph.add_edge(START, "tools")
        graph.add_edge("tools", END)
        result = await graph.compile().ainvoke({"messages": [message]})
        reply = result["messages"][-1]
        self.assertEqual(reply.tool_call_id, "call-1")
        self.assertEqual(json.loads(reply.content)["position"], 1)


@unittest.skipUnless(HAVE_GRAPH, "langgraph is not installed")
class LangGraphStoreTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.memory = adapter.MemoryKingStore(self.client)
        self.root = (self.project,)

    def test_it_is_a_base_store(self):
        self.assertIsInstance(self.memory, BaseStore)
        self.assertIs(adapter.MemoryKingStore, type(self.memory))

    def test_put_is_a_capture_and_search_is_a_briefing(self):
        namespace = (*self.root, "preferences", "alice")
        self.memory.put(namespace, "editor", {"likes": "vim, never emacs"})
        found = self.memory.search(self.root, query="vim emacs")
        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertIsInstance(item, SearchItem)
        self.assertEqual(item.namespace, self.root)
        self.assertEqual(item.key, item.value["receipt"])
        self.assertIn("vim, never emacs", item.value["briefing"])
        self.assertIn('"key": "editor"', item.value["briefing"])
        source = item.value["sources"][0]
        self.assertEqual(source["kind"], "observation")
        # The source of the memory is the namespace below the project.
        self.assertIn("preferences-alice", item.value["briefing"])

    def test_put_never_overwrites_and_replays_identical_puts(self):
        namespace = (*self.root, "notes")
        self.memory.put(namespace, "deadline", {"value": "30 seconds"})
        self.memory.put(namespace, "deadline", {"value": "30 seconds"})  # same day, replays
        self.memory.put(namespace, "deadline", {"value": "45 seconds"})  # a new version
        text = self.memory.search(self.root, query="deadline")[0].value["briefing"]
        self.assertIn("45 seconds", text)
        position = self.client.remember("probe").position
        self.assertEqual(position, 3)  # two observations before the probe, not three

    def test_delete_is_a_retirement_request_by_memory_id(self):
        namespace = (*self.root, "notes")
        self.memory.put(namespace, "k", {"value": "a stale claim about retries"})
        item = self.memory.search(self.root, query="stale retries")[0]
        memory_id = item.value["sources"][0]["id"]
        self.memory.delete(namespace, memory_id)
        gone = self.memory.search(self.root, query="stale retries")[0]
        self.assertNotIn("a stale claim about retries", gone.value["briefing"])
        # The same through a put of None.
        self.memory.put(namespace, "k2", {"value": "another stale claim"})
        other = self.memory.search(self.root, query="another stale")[0].value["sources"][0]["id"]
        self.memory.put(namespace, other, None)

    def test_unhonourable_operations_raise_not_implemented(self):
        namespace = (*self.root, "notes")
        self.memory.put(namespace, "k", {"value": "x"})
        with self.assertRaises(NotImplementedError):
            self.memory.get(namespace, "k")
        with self.assertRaises(NotImplementedError):
            self.memory.list_namespaces()
        with self.assertRaises(NotImplementedError):
            self.memory.search(self.root)  # no query
        with self.assertRaises(NotImplementedError):
            self.memory.search(self.root, query="x", filter={"a": 1})
        with self.assertRaises(NotImplementedError):
            self.memory.search(self.root, query="x", offset=5)
        with self.assertRaises(NotImplementedError):
            self.memory.search(namespace, query="x")  # a briefing is project-wide
        with self.assertRaises(NotImplementedError):
            self.memory.put(namespace, "k", {"value": "x"}, ttl=5)
        self.assertEqual(self.memory.search(self.root, query="x", limit=0), [])

    def test_bad_namespaces_and_values_are_refused(self):
        with self.assertRaises(InvalidNamespaceError):
            self.memory.put(("other-project", "notes"), "k", {"a": 1})
        with self.assertRaises(InvalidNamespaceError):
            self.memory.put((*self.root, "has space"), "k", {"a": 1})
        with self.assertRaises(InvalidNamespaceError):
            self.memory.put((), "k", {"a": 1})
        with self.assertRaises(ValueError):
            self.memory.put((*self.root, "notes"), "k", {"a": {1, 2}})

    async def test_async_methods(self):
        await self.memory.aput((*self.root, "notes"), "k", {"value": "async deadline note"})
        found = await self.memory.asearch(self.root, query="async deadline")
        self.assertIn("async deadline note", found[0].value["briefing"])
        with self.assertRaises(NotImplementedError):
            await self.memory.aget((*self.root, "notes"), "k")


if __name__ == "__main__":
    unittest.main()
