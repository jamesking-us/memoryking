"""Loopback HTTP/TLS login flow using real stores and a controlled provider."""

import http.client
import json
import socket
import ssl
import threading
import unittest
from contextlib import contextmanager
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import test_knowledge as knowledge
from test_postgres_parity import NativeFixture
from test_viewer_auth import AuthCases
from test_viewer_http import certificate

from memoryking.adapters.viewer_auth import CALLBACK, LOGIN_COOKIE, SESSION_COOKIE, ViewerAuth
from memoryking.adapters.viewer_http import ViewerListener, ViewerServer
from memoryking.domain.local import LocalError


class HttpAuthCases:
    command = AuthCases.command
    policy = AuthCases.policy
    bind = AuthCases.bind
    grant_viewer = AuthCases.grant_viewer
    actor_store = AuthCases.actor_store
    auth_store = AuthCases.auth_store
    setup_auth = AuthCases.setup_auth
    deadline = AuthCases.deadline

    @contextmanager
    def listener(self, tls=False, public_origin=None):
        self.setup_auth()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.settings["listen_port"] = port
        self.settings["public_origin"] = public_origin or f"https://viewer.example.invalid:{port}"
        self.tls_context = None
        if tls:
            cert, key = certificate(self.root)
            self.settings.update(
                transport="direct_tls", tls_cert_file=str(cert), tls_key_file=str(key)
            )
            self.tls_context = ssl.create_default_context(cafile=str(cert))
        self.auth = ViewerAuth(
            self.settings, self.auth_store, self.provider, clock=self.store.clock
        )
        self.server = ViewerServer(
            self.settings,
            lambda session, deadline: self.actor_store(session.subject.credential),
            auth=self.auth,
        )
        self.port, self.cookies = port, {}
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield
        finally:
            self.server.shutdown()
            self.server.server_close()
            thread.join()

    def request(self, target, method="GET", body=None, extra=None, origin=True):
        headers = {"Host": urlsplit(self.settings["public_origin"]).netloc}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if method == "POST":
            headers["Content-Type"] = "application/json"
            if origin:
                headers["Origin"] = self.settings["public_origin"]
            body = json.dumps(body or {}).encode()
        headers.update(extra or {})
        connection = (
            http.client.HTTPSConnection("127.0.0.1", self.port, context=self.tls_context, timeout=5)
            if self.tls_context
            else http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        )
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            raw, pairs = response.read(), response.getheaders()
            for name, value in pairs:
                if name.lower() == "set-cookie":
                    cookie = SimpleCookie(value)
                    for key, item in cookie.items():
                        if item["max-age"] == "0":
                            self.cookies.pop(key, None)
                        else:
                            self.cookies[key] = item.value
            return response.status, pairs, raw
        finally:
            connection.close()

    def start_http(self):
        status, headers, raw = self.request("/viewer/auth/context")
        self.assertEqual(status, 200)
        context = json.loads(raw)
        status, headers, raw = self.request(
            "/viewer/auth/github/start", "POST", {"csrf": context["csrf"]}
        )
        self.assertEqual(status, 200)
        url = json.loads(raw)["authorization_url"]
        self.assertEqual(urlsplit(url).netloc, "github.com")
        query = parse_qs(urlsplit(url).query)
        self.provider.challenge = query["code_challenge"][0]
        return query["state"][0]

    def login_http(self):
        state = self.start_http()
        status, headers, raw = self.request(
            CALLBACK + "?state=" + state + "&code=controlled-code",
            extra={
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
        )
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["Location"], "/viewer/")
        self.assertEqual(raw, b"")
        self.assertIn(SESSION_COOKIE, self.cookies)
        return headers

    def session_http(self):
        return self.request("/viewer/api/v1/session")

    def test_actual_login_cookie_flags_private_read_and_logout(self):
        with self.listener(tls=True):
            headers = self.login_http()
            cookie = [
                value
                for name, value in headers
                if name.lower() == "set-cookie" and value.startswith(SESSION_COOKIE + "=")
            ][0]
            for flag in ("Secure", "HttpOnly", "Path=/", "SameSite=Lax", "Max-Age=28800"):
                self.assertIn(flag, cookie)
            self.assertNotIn("Domain", cookie)
            self.assertNotIn(LOGIN_COOKIE, self.cookies)
            status, headers, raw = self.session_http()
            self.assertEqual(status, 200)
            session = json.loads(raw)
            self.assertEqual(session["data"]["projects"], ["p"])
            self.assertEqual(dict(headers)["Cache-Control"], "no-store")
            csrf = session["data"]["csrf"]
            self.assertEqual(self.request("/viewer/auth/logout", "POST", {"csrf": csrf})[0], 200)
            self.assertEqual(self.session_http()[0], 401)

    def test_start_csrf_origin_fetch_site_host_return_url_and_get_logout(self):
        with self.listener():
            status, headers, raw = self.request("/viewer/auth/context")
            csrf = json.loads(raw)["csrf"]
            for extra, origin, value, target in (
                ({}, False, csrf, "/viewer/auth/github/start"),
                (
                    {"Origin": "https://evil.example.invalid"},
                    True,
                    csrf,
                    "/viewer/auth/github/start",
                ),
                ({"Sec-Fetch-Site": "same-site"}, True, csrf, "/viewer/auth/github/start"),
                ({"Host": "evil.example.invalid"}, True, csrf, "/viewer/auth/github/start"),
                ({}, True, "x" * 43, "/viewer/auth/github/start"),
                ({}, True, csrf, "/viewer/auth/github/start?return=https%3A%2F%2Fevil.invalid"),
            ):
                with self.subTest(extra=extra, target=target):
                    self.assertGreaterEqual(
                        self.request(target, "POST", {"csrf": value}, extra=extra, origin=origin)[
                            0
                        ],
                        400,
                    )
            self.login_http()
            session = json.loads(self.session_http()[2])
            for endpoint in ("logout", "logout-all"):
                self.assertEqual(self.request("/viewer/auth/" + endpoint)[0], 400)
                self.assertGreaterEqual(
                    self.request(
                        "/viewer/auth/" + endpoint,
                        "POST",
                        {"csrf": session["data"]["csrf"]},
                        origin=False,
                    )[0],
                    400,
                )
            self.assertEqual(self.session_http()[0], 200)

    def test_callback_replay_denial_missing_nonce_and_logs_are_clean(self):
        with self.listener():
            state = self.start_http()
            nonce = self.cookies.pop(LOGIN_COOKIE)
            result = self.request(CALLBACK + "?state=" + state + "&code=controlled-code")
            self.assertEqual(result[0], 303)
            self.assertFalse(self.provider.calls)
            self.cookies[LOGIN_COOKIE] = nonce
            result = self.request(
                CALLBACK
                + "?state="
                + state
                + "&error=access_denied&error_description=SYNTHETIC_PRIVATE_MARKER"
            )
            self.assertEqual(result[0], 303)
            self.assertNotIn(b"SYNTHETIC_PRIVATE_MARKER", result[2])
            self.assertEqual(dict(result[1])["Location"], "/viewer/")
            self.cookies[LOGIN_COOKIE] = nonce
            self.assertEqual(
                self.request(CALLBACK + "?state=" + state + "&code=controlled-code")[0], 303
            )
            self.assertFalse(self.provider.calls)
            self.assertFalse(self.auth.sessions)
            self.assertNotIn(state, repr(self.server.counts))
            self.assertTrue(
                all(k.isdecimal() or k.endswith("_rejected") for k in self.server.counts)
            )

    def test_http_revocation_forgets_session_and_provider_failure_is_clean(self):
        with self.listener():
            self.login_http()
            self.assertEqual(self.session_http()[0], 200)
            self.policy("disable", {"principal": "phone", "expected_version": 2})
            status, headers, raw = self.session_http()
            self.assertEqual(status, 409)
            self.assertNotIn(b'"projects"', raw)
            self.assertFalse(self.auth.sessions)
            self.assertEqual(self.session_http()[0], 401)
            state = self.start_http()
            self.provider.fail = True
            status, headers, raw = self.request(
                CALLBACK + "?state=" + state + "&code=controlled-code"
            )
            self.assertEqual(status, 303)
            self.assertEqual(dict(headers)["Location"], "/viewer/")
            self.assertEqual(raw, b"")
            self.assertNotIn(SESSION_COOKIE, self.cookies)

    def test_disabled_listener_does_not_read_secret_and_enabled_requires_private_file(self):
        self.setup_auth()
        self.settings["enabled"] = False
        listener = ViewerListener(self.settings, lambda s, d: self.auth_store(d), self.auth_store)
        listener.start()
        self.assertIsNone(listener.server)
        listener.stop()
        self.settings["enabled"] = True
        listener = ViewerListener(self.settings, lambda s, d: self.auth_store(d), self.auth_store)
        with self.assertRaisesRegex(LocalError, "invalid_viewer_secret"):
            listener.start()
        self.assertIsNone(listener.server)

    def test_closed_post_shapes_callback_bounds_and_no_provider_override(self):
        from memoryking.adapters.viewer_config import validate_config

        with self.listener():
            status, headers, raw = self.request("/viewer/auth/context")
            csrf = json.loads(raw)["csrf"]
            for body in (
                {"csrf": csrf, "return": "https://evil.invalid"},
                {"csrf": "a" * 600},
                {"csrf": "a" * 50},
                {"csrf": "z" * 64},
                {"csrf": None},
                {"csrf": "x"},
            ):
                self.assertEqual(self.request("/viewer/auth/github/start", "POST", body)[0], 400)
            self.assertEqual(self.request(CALLBACK + "?state=" + "a" * 513)[0], 400)
            for key in (
                "github_token_url",
                "github_authorize_url",
                "provider_url",
                "allow_insecure_tls",
                "callback_url",
            ):
                with self.assertRaises(LocalError):
                    validate_config({**self.settings, key: "https://evil.invalid"})
            self.assertFalse(self.provider.calls)

    def test_forged_callback_does_not_log_out_existing_session(self):
        with self.listener():
            self.login_http()
            token = self.cookies[SESSION_COOKIE]
            calls = len(self.provider.calls)
            status, headers, raw = self.request(
                CALLBACK + "?state=" + "z" * 43 + "&code=controlled-code",
                extra={
                    "Sec-Fetch-Site": "cross-site",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                },
            )
            self.assertEqual(status, 303)
            self.assertEqual(dict(headers)["Location"], "/viewer/")
            self.assertFalse(any(k.lower() == "set-cookie" for k, _ in headers))
            self.assertEqual(self.cookies[SESSION_COOKIE], token)
            self.assertEqual(self.session_http()[0], 200)
            self.assertEqual(len(self.provider.calls), calls)


class LocalViewerAuthHTTP(HttpAuthCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresViewerAuthHTTP(HttpAuthCases, NativeFixture):
    def test_production_internal_auth_factory_has_no_owner_capabilities(self):
        from memoryking.adapters.shared_service import SharedService
        from memoryking.adapters.viewer_identity import ViewerIdentity

        self.setup_auth()
        service = SharedService(self.pool, self.data, self.control)
        with service.viewer_auth_store(self.tenant, self.deadline()) as store:
            with store.transaction() as decision:
                with self.assertRaises(LocalError):
                    store._auth(store.credential, "admin", "*", decision.evaluated_ms)
        self.auth.store = lambda deadline: service.viewer_auth_store(self.tenant, deadline)
        nonce, state, _ = AuthCases.begin(self)
        token = AuthCases.finish(self, nonce, state)
        self.auth.logout(token, self.auth.resolve(token).csrf, True, self.deadline())
        with self.store.transaction():
            self.assertEqual(
                ViewerIdentity(self.store)._binding(self.tenant, "phone")["generation"], 3
            )
