"""Real local HTTP traffic against PostgreSQL, with distinct signed agent identities."""

import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import psycopg
import uvicorn

from memoryking.adapters.local_files import atomic
from memoryking.adapters.shared_http import create_app
from memoryking.adapters.shared_service import SharedService
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError, SystemClock
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.shared_sdk import SharedClient
from tests import test_postgres_store
from tests.test_local_store import observation


class SharedHTTPTests(unittest.TestCase):
    clock_factory = SystemClock
    setUpClass = classmethod(test_postgres_store.PostgresStoreTests.setUpClass.__func__)
    tearDownClass = classmethod(test_postgres_store.PostgresStoreTests.tearDownClass.__func__)
    store = test_postgres_store.PostgresStoreTests.store

    def setUp(self):
        test_postgres_store.PostgresStoreTests.setUp(self)
        self.tmp = tempfile.TemporaryDirectory(prefix="mk-http-", dir="/tmp")
        self.files = Path(self.tmp.name)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.service = SharedService(self.pool, self.root, self.control)
        app = create_app(
            self.service,
            hosts=[f"127.0.0.1:{self.port}"],
            origins=[self.url],
            loopback_http=True,
            plan_root=Path(__file__).resolve().parents[1]
            if getattr(self, "dashboard", False)
            else None,
            dashboard_tenant=self.tenant if getattr(self, "dashboard", False) else None,
        )
        self.server = uvicorn.Server(
            uvicorn.Config(app, log_level="critical", access_log=False, lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.listener]})
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started:
            if time.monotonic() >= deadline:
                raise RuntimeError("local HTTP start timed out")
            time.sleep(0.01)
        self.credential_path = self.files / "owner.credential"
        atomic(self.credential_path, self.credential.encode())
        self.client = SharedClient(
            self.url, self.tenant, self.credential_path, "demo", loopback_http=True
        )

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(5)
        self.listener.close()
        self.tmp.cleanup()

    def raw(self, body, headers=None):
        request = urllib.request.Request(
            self.url + f"/v1/tenants/{self.tenant}/operations",
            canonical_bytes(body),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.credential,
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def command(self, operation, args, credential=None):
        return self.client._request(credential or self.credential, operation, args)

    def test_plans_sdk_over_actual_http_and_native_postgres(self):
        from tools.plans_demo import exercise

        self.assertEqual(exercise(self.client)["work_percent"], 50)

    def test_experience_sdk_real_http_and_actual_provider_input(self):
        from tools.experience_demo import exercise

        self.assertTrue(exercise(self.client, self.files)["passed"])

    def test_embedding_sdk_real_http_and_native_store(self):
        captured = self.client.capture("embedding", observation())

        def execute(action, args, **extra):
            return self.client.embedding(
                {
                    "version": 1,
                    "incarnation": self.client.binding[-1],
                    "action": action,
                    "args": {"project": "demo", **args},
                    **extra,
                }
            )

        config = execute(
            "configure",
            {"model": "minilm-l6-v2-onnx", "state": "experimental", "expected_generation": 0},
            key="config",
        )
        job = execute("claim", {"fingerprint": config["fingerprint"]})
        self.assertEqual(job["observation"], captured["observation"])
        self.assertEqual(job["egress"], "local_cpu_only")
        result = execute(
            "complete",
            {
                "lease": job["lease"],
                "fingerprint": job["fingerprint"],
                "input_hash": job["input_hash"],
                "vector": [1000000] + [0] * 383,
                "input_tokens": 10,
                "truncated": False,
                "duration_ms": 5,
            },
        )
        self.assertEqual(result["status"], "complete")
        self.assertTrue(execute("status", {"fingerprint": config["fingerprint"]})["ready"])

    def test_capture_context_distinct_principals_and_host_origin_rules(self):
        first = self.client.capture("one", observation())
        reader = self.command(
            "grant", {"principal": "reviewer", "project": "demo", "capabilities": ["read"]}
        )["credential"]
        reader_path = self.files / "reader.credential"
        atomic(reader_path, reader.encode())
        client = SharedClient(self.url, self.tenant, reader_path, "demo", loopback_http=True)
        self.assertEqual(client.context()["principal"], "reviewer")
        result = client._request(
            reader, "read", {"project": "demo", "observation": first["handle"]}
        )
        self.assertEqual("durable memory", result["items"][0]["text"])
        with self.assertRaises(LocalError):
            client.capture("bad", observation(2))
        read = {"version": 1, "operation": "context", "args": {"project": "demo"}}
        self.assertEqual(200, self.raw(read)[0])  # Missing Origin is legitimate bearer traffic.
        self.assertEqual(403, self.raw(read, {"Origin": "https://hostile.invalid"})[0])
        self.assertEqual(421, self.raw(read, {"Host": "hostile.invalid"})[0])
        self.assertEqual(
            403, self.raw(read, {"Authorization": "", "Cookie": "session=anything"})[0]
        )
        self.assertEqual("invalid_request", self.raw({**read, "credential": reader})[1]["error"])

    def test_delegation_live_intersection_expiry_audience_and_context_switch(self):
        delegator = self.command(
            "grant",
            {"principal": "planner", "project": "demo", "capabilities": ["read", "capture"]},
        )["credential"]
        recipient = self.command(
            "grant",
            {"principal": "implementer", "project": "demo", "capabilities": ["read", "capture"]},
        )["credential"]
        delegated = self.command(
            "delegate",
            {
                "principal": "implementer",
                "project": "demo",
                "capabilities": ["read"],
                "ttl_ms": 60000,
            },
            delegator,
        )["credential"]
        context = self.command("context", {"project": "demo"}, delegated)
        self.assertEqual(["read"], context["capabilities"])
        self.assertEqual(1, len(context["delegation_chain"]))
        path = self.files / "delegate.credential"
        atomic(path, delegated.encode())
        client = SharedClient(self.url, self.tenant, path, "demo", loopback_http=True)
        atomic(path, recipient.encode())
        with self.assertRaises(LocalError) as caught:
            client.context()
        self.assertEqual("context_changed", caught.exception.code)
        self.command("revoke", {"principal": "planner", "project": "demo"})
        with self.assertRaises(LocalError):
            self.command("context", {"project": "demo"}, delegated)
        with self.service.store(self.tenant, self.credential) as store:
            body = store.control.verify(recipient)
            body["audience"] = "another-service"
            wrong = store.control.sign(body)
        with self.assertRaises(LocalError) as caught:
            self.command("context", {"project": "demo"}, wrong)
        self.assertEqual("credential_audience_mismatch", caught.exception.code)

    def test_actual_provider_request_rebuild_after_revocation(self):
        captured = self.client.capture("evidence", observation(text="restricted original"))
        context = AuthorizedConversation(
            self.client, LocalRecordingProvider(self.files / "provider")
        )
        packet = self.client.retrieval(
            {
                "version": 1,
                "incarnation": self.client.binding[-1],
                "action": "fetch",
                "args": {
                    "project": "demo",
                    "kind": "observation",
                    "reference": captured["observation"],
                    "start_byte": 0,
                    "end_byte": len("restricted original"),
                    "max_output_tokens": 6000,
                    "tokenizer": "cl100k_base",
                },
            }
        )
        # The harness keeps exact server artifact bytes and rechecks current authorization.
        context.add_memory(packet)
        self.command("purge", {"observation": captured["observation"]})
        context.add_host(
            "new safe input",
            authorization_id="input",
            classification="internal",
            expires_ms=int(time.time() * 1000) + 60000,
            approved=True,
        )
        context.dispatch()
        requests = list((self.files / "provider").glob("*"))
        self.assertTrue(requests)
        self.assertTrue(
            all(b"restricted original" not in p.read_bytes() for p in requests if p.is_file())
        )

    def add_host(self, context, text):
        context.add_host(
            text,
            authorization_id="accepted_input",
            classification="internal",
            expires_ms=int(time.time() * 1000) + 60000,
            approved=True,
        )

    def test_provider_grant_expiry_blocks_without_notification_and_rebuilds(self):
        expires = int(time.time() * 1000) + 5000
        reader = self.command(
            "grant",
            {
                "principal": "reader",
                "project": "demo",
                "capabilities": ["read", "inference"],
                "expires": expires,
            },
        )["credential"]
        path = self.files / "reader.credential"
        atomic(path, reader.encode())
        client = SharedClient(self.url, self.tenant, path, "demo", loopback_http=True)
        provider = LocalRecordingProvider(self.files / "provider")
        context = AuthorizedConversation(client, provider)
        self.add_host(context, "restricted expiry fixture")
        first = context.dispatch()
        second = context.dispatch()
        self.assertIn(first["handle"], provider.path(second["handle"]).read_text())
        time.sleep(max(0, (expires + 1 - int(time.time() * 1000)) / 1000))
        with self.assertRaises(LocalError) as caught:
            context.dispatch()
        self.assertEqual("denied", caught.exception.code)
        self.assertEqual(2, len(list(provider.directory.glob("*.json"))))
        self.assertEqual([], context._messages)
        self.assertEqual([], context._handles)
        self.command(
            "grant",
            {"principal": "reader", "project": "demo", "capabilities": ["read", "inference"]},
        )
        self.add_host(context, "fresh currently authorized input")
        fresh = context.dispatch()
        actual = provider.path(fresh["handle"]).read_text()
        self.assertNotIn("restricted expiry fixture", actual)
        self.assertNotIn(first["handle"], actual)
        self.assertNotIn(second["handle"], actual)

    def test_provider_tenant_switch_drops_original_derived_and_handle_state(self):
        from memoryking.adapters.postgres_admin import initialize_tenant

        provider = LocalRecordingProvider(self.files / "provider")
        context = AuthorizedConversation(self.client, provider)
        self.add_host(context, "restricted original tenant")
        first = context.dispatch()
        second = context.dispatch()
        tenant = self.tenant + "_other"
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            credential = initialize_tenant(connection, self.root, self.control, tenant)
        other = SharedClient(self.url, tenant, credential, "demo", loopback_http=True)
        context.rebind(other)
        self.add_host(context, "permitted new tenant input")
        current = context.dispatch()
        actual = provider.path(current["handle"]).read_text()
        self.assertNotIn("restricted original tenant", actual)
        self.assertNotIn(first["handle"], actual)
        self.assertNotIn(second["handle"], actual)
        self.assertEqual(tenant, other.context()["tenant"])

    def test_provider_restore_and_permission_service_outage_fail_closed(self):
        from memoryking.adapters.postgres_recovery import backup, restore

        provider = LocalRecordingProvider(self.files / "provider")
        context = AuthorizedConversation(self.client, provider)
        self.add_host(context, "restricted previous history")
        first = context.dispatch()
        archive = self.files / "archive"
        with (
            self.store() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            backup(store, connection, archive)
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            credential = restore(connection, self.root, self.control, self.tenant, archive)
        with self.assertRaises(LocalError) as caught:
            context.dispatch()
        self.assertEqual("history_changed", caught.exception.code)
        self.assertEqual(1, len(list(provider.directory.glob("*.json"))))
        current_client = SharedClient(self.url, self.tenant, credential, "demo", loopback_http=True)
        context.rebind(current_client)
        self.add_host(context, "permitted replacement history input")
        current = context.dispatch()
        actual = provider.path(current["handle"]).read_text()
        self.assertNotIn("restricted previous history", actual)
        self.assertNotIn(first["handle"], actual)
        self.server.should_exit = True
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        with self.assertRaises(LocalError) as caught:
            context.dispatch()
        self.assertEqual("transport_unavailable", caught.exception.code)
        self.assertEqual(2, len(list(provider.directory.glob("*.json"))))
        self.assertEqual([], context._messages)
        self.assertEqual([], context._handles)

    def test_official_mcp_sdk_initialize_list_call_and_reauthorization(self):
        import anyio
        import httpx2
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def exercise():
            endpoint = self.url + f"/v1/tenants/{self.tenant}/projects/demo/mcp"
            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + self.credential}, timeout=10
            ) as http:
                async with streamable_http_client(
                    endpoint, http_client=http, terminate_on_close=False
                ) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=10) as session:
                        initialized = await session.initialize()
                        self.assertEqual("2025-11-25", initialized.protocol_version)
                        listed = await session.list_tools()
                        self.assertEqual(9, len(listed.tools))
                        status = await session.call_tool("memory_status", {"project": "demo"})
                        self.assertFalse(status.is_error)
                        body = json.loads(status.content[0].text)
                        self.assertEqual(self.tenant, body["tenant"])
                        captured = await session.call_tool(
                            "memory_capture", {"key": "mcp", "request": observation(8)}
                        )
                        self.assertFalse(captured.is_error)
                        outcome = json.loads(
                            (
                                Path(__file__).resolve().parents[1]
                                / "fixtures/contracts/experience-vectors.json"
                            ).read_text()
                        )[0]["request"]
                        outcome["incarnation"] = body["incarnation"]
                        feedback = await session.call_tool("memory_feedback", outcome)
                        self.assertFalse(feedback.is_error)
                        self.assertEqual(json.loads(feedback.content[0].text)["kind"], "outcome")
                        forbidden = await session.call_tool("memory_status", {"project": "other"})
                        self.assertTrue(forbidden.is_error)
                        self.assertEqual(
                            "scope_mismatch", json.loads(forbidden.content[0].text)["error"]
                        )

        anyio.run(exercise)

    def test_delegation_expiry_and_no_self_escalation(self):
        from memoryking.adapters.postgres_store import PostgresStore
        from tests.test_local_store import FakeClock

        recipient = self.command(
            "grant", {"principal": "short", "project": "demo", "capabilities": ["read", "capture"]}
        )["credential"]
        clock = FakeClock()
        clock.utc = int(time.time() * 1000)
        with PostgresStore(
            self.pool, self.root, self.control, self.tenant, self.credential, clock=clock
        ) as owner:
            delegated = owner.delegate("short", "demo", ["read"], 1000)
        with PostgresStore(
            self.pool, self.root, self.control, self.tenant, delegated, clock=clock
        ) as subject:
            context = json.loads(subject.context(delegated, "demo").body)
            self.assertEqual(["read"], context["capabilities"])
            with self.assertRaises(LocalError):
                subject.namespace(delegated, "demo")
            with self.assertRaises(LocalError):
                subject.delegate("short", "demo", ["read"], 500)
            clock.advance(1000)
            with self.assertRaises(LocalError):
                subject.context(delegated, "demo")
        self.assertNotEqual(recipient, delegated)

    def test_mcp_listing_requires_current_grant_and_supported_protocol(self):
        reader = self.command(
            "grant", {"principal": "mcp-reader", "project": "demo", "capabilities": ["read"]}
        )["credential"]
        endpoint = self.url + f"/v1/tenants/{self.tenant}/projects/demo/mcp"
        body = canonical_bytes({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})

        def send(version="2025-11-25"):
            request = urllib.request.Request(
                endpoint,
                body,
                headers={
                    "Authorization": "Bearer " + reader,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": version,
                },
            )
            try:
                response = urllib.request.urlopen(request, timeout=5)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                return response.status, response.read()

        self.assertEqual(200, send()[0])
        self.assertEqual(403, send("unsupported")[0])
        self.command("revoke", {"principal": "mcp-reader", "project": "demo"})
        code, raw = send()
        self.assertEqual(403, code)
        self.assertEqual("denied", json.loads(raw)["error"])
        self.assertNotIn(b"memory_capture", raw)


class ReleaseBoundaryTests(unittest.TestCase):
    def test_delay_between_headers_and_body_never_emits_expired_content(self):
        import anyio

        from memoryking.adapters.shared_http import ReleaseResponse
        from memoryking.domain.local import Decision, Released
        from tests.test_local_store import FakeClock

        clock = FakeClock()
        packet = Released(
            b'{"secret":"restricted"}', Decision(clock.utc, clock.mono, clock.utc + 5000), clock
        )
        sent = []

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.start":
                clock.advance(5001)

        async def receive():
            return {"type": "http.disconnect"}

        async def exercise():
            await ReleaseResponse(packet)({"type": "http"}, receive, send)

        anyio.run(exercise)
        self.assertEqual(b"", sent[-1]["body"])
