"""Actual login transactions and canonical authority on SQLite and PostgreSQL."""

import base64
import hashlib
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from urllib.parse import parse_qs, urlsplit

import test_knowledge as knowledge
from recovery_helpers import backup_fixture, restore_fixture
from test_local_store import FakeClock
from test_postgres_parity import NativeFixture
from test_viewer_contracts import config
from test_viewer_identity import ViewerCases

from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.viewer_auth import (
    CALLBACK,
    GITHUB_AUTHORIZATION_ISSUER,
    ViewerAuth,
    digest,
)
from memoryking.adapters.viewer_reads import ViewerReads
from memoryking.domain.local import LocalError


class ControlledProvider:
    def __init__(self):
        self.calls = []
        self.user_id = "12345"
        self.before = lambda: None
        self.challenge = None
        self.fail = False

    def authenticate(self, code, verifier, callback, deadline):
        self.calls.append((code, verifier, callback))
        actual = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        if self.fail or actual != self.challenge or code != "controlled-code":
            raise LocalError("login_failed")
        self.before()
        return self.user_id


class AuthCases:
    command = ViewerCases.command
    policy = ViewerCases.policy
    bind = ViewerCases.bind
    grant_viewer = ViewerCases.grant_viewer
    actor_store = ViewerCases.actor_store

    def auth_store(self, deadline):
        if hasattr(self, "pool"):
            return PostgresStore(
                self.pool,
                self.data,
                self.control,
                self.tenant,
                self.owner,
                clock=self.store.clock,
                fault=self.store.fault,
                request_deadline=deadline,
            )
        return nullcontext(self.store)

    def setup_auth(self, *, grant=True, fake_clock=False):
        if fake_clock:
            self.clock = FakeClock()
            self.store.clock = self.clock
        self.bind()
        if grant:
            self.grant_viewer()
        self.settings = {**config(), "enabled": True, "tenant": self.tenant}
        self.provider = ControlledProvider()
        self.auth = ViewerAuth(
            self.settings, self.auth_store, self.provider, clock=self.store.clock
        )

    def deadline(self):
        return time.monotonic() + 5

    def begin(self, old_session=""):
        context, nonce = self.auth.context("", self.deadline())
        start = self.auth.start(nonce, context["csrf"], old_session, self.deadline())
        query = parse_qs(urlsplit(start["authorization_url"]).query, keep_blank_values=True)
        self.provider.challenge = query["code_challenge"][0]
        return nonce, query["state"][0], query

    def finish(self, nonce, state):
        token, released = self.auth.callback(
            nonce,
            {"state": state, "code": "controlled-code", "iss": GITHUB_AUTHORIZATION_ISSUER},
            self.deadline(),
        )
        emitted = []
        released.emit(emitted.append)
        self.assertEqual(json.loads(emitted[0])["status"], "signed_in")
        return token

    def login(self, old_session=""):
        nonce, state, _ = self.begin(old_session)
        return self.finish(nonce, state)

    def private_read(self, token):
        session = self.auth.resolve(token)
        with self.actor_store(session.subject.credential) as store:
            released = ViewerReads(store, self.settings).read(session, "session", None, {})
            emitted = []
            released.emit(emitted.append)
            return json.loads(emitted[0])

    def test_success_pkce_numeric_identity_and_hashed_state(self):
        self.setup_auth()
        nonce, state, query = self.begin()
        self.assertEqual(query["scope"], [""])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], [self.settings["public_origin"] + CALLBACK])
        self.assertNotIn(state, self.auth.pending)
        self.assertNotIn(nonce, self.auth.contexts)
        token = self.finish(nonce, state)
        self.assertEqual(set(self.auth.sessions), {digest(token)})
        self.assertNotIn(token, repr(self.auth.sessions))
        self.assertEqual(self.private_read(token)["data"]["projects"], ["p"])
        self.assertEqual(len(self.provider.calls), 1)

    def test_state_nonce_csrf_expiry_denial_and_pkce_failures(self):
        self.setup_auth(fake_clock=True)
        ctx, nonce = self.auth.context("", self.deadline())
        with self.assertRaises(LocalError):
            self.auth.start(nonce, "x" * 43, "", self.deadline())
        nonce, state, _ = self.begin()
        for bad_nonce, bad_state in (("", state), ("x" * 43, state), (nonce, "x" * 43)):
            with self.assertRaises(LocalError):
                self.finish(bad_nonce, bad_state)
        self.assertFalse(self.provider.calls)
        with self.assertRaises(LocalError):
            self.auth.callback(nonce, {"state": state, "error": "access_denied"}, self.deadline())
        with self.assertRaises(LocalError):
            self.finish(nonce, state)
        nonce, state, _ = self.begin()
        self.provider.challenge = "incorrect-pkce"
        with self.assertRaises(LocalError):
            self.finish(nonce, state)
        nonce, state, _ = self.begin()
        self.clock.advance(300000)
        with self.assertRaises(LocalError):
            self.finish(nonce, state)
        self.assertFalse(self.auth.sessions)

    def test_parallel_callback_and_ambiguous_exchange_are_one_use(self):
        self.setup_auth()
        nonce, state, _ = self.begin()

        def attempt():
            try:
                return self.finish(nonce, state)
            except LocalError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.provider.calls), 1)
        nonce, state, _ = self.begin()
        self.provider.fail = True
        with self.assertRaises(LocalError):
            self.finish(nonce, state)
        self.provider.fail = False
        with self.assertRaises(LocalError):
            self.finish(nonce, state)
        self.assertEqual(len(self.provider.calls), 2)

    def test_authorization_issuer_is_exact_and_failure_consumes_state(self):
        self.setup_auth()
        for issuer in (
            None,
            "",
            "https://github.com",
            "https://github.com/login/oauth/",
            "https://GITHUB.com/login/oauth",
            "https://evil.invalid",
        ):
            nonce, state, _ = self.begin()
            params = {"state": state, "code": "controlled-code"}
            if issuer is not None:
                params["iss"] = issuer
            with self.assertRaises(LocalError):
                self.auth.callback(nonce, params, self.deadline())
            with self.assertRaises(LocalError):
                self.finish(nonce, state)
        self.assertFalse(self.provider.calls)
        self.assertFalse(self.auth.sessions)

    def test_unknown_malformed_and_ungranted_identity_never_issues(self):
        self.setup_auth(grant=False)
        for user_id in ("12345", "98765", "012345", True, None, "user-name"):
            self.provider.user_id = user_id
            nonce, state, _ = self.begin()
            with self.assertRaises(LocalError):
                self.finish(nonce, state)
            self.assertFalse(self.auth.sessions)

    def test_rotation_restart_idle_absolute_and_clock_rollback(self):
        self.setup_auth(fake_clock=True)
        first = self.login()
        second = self.login(first)
        self.assertNotEqual(first, second)
        with self.assertRaises(LocalError):
            self.auth.resolve(first)
        fresh = ViewerAuth(self.settings, self.auth_store, self.provider, clock=self.clock)
        with self.assertRaises(LocalError):
            fresh.resolve(second)
        self.clock.advance(1800000)
        with self.assertRaises(LocalError):
            self.auth.resolve(second)
        third = self.login()
        for _ in range(28):
            self.clock.advance(1000000)
            self.auth.resolve(third)
        self.clock.advance(800000)
        with self.assertRaises(LocalError):
            self.auth.resolve(third)
        fourth = self.login()
        self.clock.utc -= 1
        with self.assertRaisesRegex(LocalError, "clock_untrusted"):
            self.auth.resolve(fourth)
        self.assertFalse(self.auth.sessions)
        self.assertFalse(self.auth.pending)

    def test_pending_capacity_rotation_expiry_and_active_ceiling(self):
        self.setup_auth(fake_clock=True)
        self.settings["limits"] = {
            **self.settings["limits"],
            "pending_logins": 2,
            "active_sessions": 2,
        }
        ctx, nonce = self.auth.context("", self.deadline())
        self.auth.context("", self.deadline())
        with self.assertRaisesRegex(LocalError, "service_unavailable"):
            self.auth.context("", self.deadline())
        self.auth.context(nonce, self.deadline())
        self.assertEqual(len(self.auth.contexts), 2)
        self.clock.advance(300000)
        first = self.login()
        self.clock.advance(1)
        second = self.login()
        self.clock.advance(1)
        third = self.login()
        with self.assertRaises(LocalError):
            self.auth.resolve(first)
        self.auth.resolve(second)
        self.auth.resolve(third)
        self.assertEqual(len(self.auth.sessions), 2)

    def test_logout_csrf_and_durable_logout_all_during_issuance(self):
        self.setup_auth()
        first, second = self.login(), self.login()
        with self.assertRaises(LocalError):
            self.auth.logout(first, self.auth.resolve(second).csrf, False, self.deadline())
        self.auth.logout(first, self.auth.resolve(first).csrf, False, self.deadline())
        with self.assertRaises(LocalError):
            self.auth.resolve(first)
        nonce, state, _ = self.begin()
        self.provider.before = lambda: self.auth.logout(
            second, self.auth.resolve(second).csrf, True, self.deadline()
        )
        with self.assertRaisesRegex(LocalError, "context_changed"):
            self.finish(nonce, state)
        self.assertFalse(self.auth.sessions)
        with self.store.transaction():
            row = self.store.db.execute(
                "SELECT generation FROM viewer_bindings WHERE tenant=? AND principal='phone'",
                (self.tenant,),
            ).fetchone()
            self.assertEqual(row[0], 3)

    def test_policy_change_during_provider_and_next_read(self):
        self.setup_auth()
        token = self.login()
        nonce, state, _ = self.begin()
        self.provider.before = lambda: self.policy(
            "disable", {"principal": "phone", "expected_version": 2}
        )
        with self.assertRaisesRegex(LocalError, "context_changed"):
            self.finish(nonce, state)
        with self.assertRaises(LocalError):
            self.private_read(token)
        self.assertEqual(
            len(self.auth.sessions), 1
        )  # Only HTTP invalidation removes old local cache.

    def test_restore_fences_pending_and_previous_session(self):
        self.setup_auth()
        token = self.login()
        nonce, state, _ = self.begin()
        archive = backup_fixture(self)
        restore_fixture(self, archive)
        with self.assertRaisesRegex(LocalError, "context_changed"):
            self.finish(nonce, state)
        with self.assertRaises(LocalError):
            self.private_read(token)

    def test_grant_expires_during_exchange_and_issuance_failure_cleans_session(self):
        self.setup_auth(fake_clock=True)
        self.grant_viewer(version=2, expires=self.clock.utc + 1000)
        nonce, state, _ = self.begin()
        self.provider.before = lambda: self.clock.advance(1000)
        with self.assertRaises(LocalError):
            self.finish(nonce, state)
        self.assertFalse(self.auth.sessions)
        self.grant_viewer(version=3)
        self.provider.before = lambda: None
        nonce, state, _ = self.begin()

        def failure(point):
            if point == "viewer_before_session_commit":
                raise RuntimeError("synthetic-issuance-failure")

        self.store.fault = failure
        with self.assertRaisesRegex(RuntimeError, "synthetic-issuance-failure"):
            self.finish(nonce, state)
        self.assertFalse(self.auth.sessions)
        self.store.fault = lambda _: None
        self.private_read(self.login())

    def test_closed_auth_dtos_and_cross_context_fence(self):
        from memoryking.contracts.viewer import validate_viewer

        self.setup_auth()
        context, nonce = self.auth.context("", self.deadline())
        validate_viewer("ViewerAuthContext", context)
        validate_viewer("ViewerAuthPost", {"csrf": context["csrf"]})
        start = self.auth.start(nonce, context["csrf"], "", self.deadline())
        validate_viewer("ViewerAuthStart", start)
        with self.assertRaises(LocalError):
            validate_viewer(
                "ViewerAuthPost", {"csrf": context["csrf"], "return": "https://evil.invalid"}
            )
        self.settings["tenant"] = "wrong_tenant"
        with self.assertRaises(LocalError):
            self.auth.context("", self.deadline())

    def test_evaluation_instant_allows_finish_but_expired_grant_cannot_read(self):
        self.setup_auth(fake_clock=True)
        self.grant_viewer(version=2, expires=self.clock.utc + 1000)
        nonce, state, _ = self.begin()

        def cross_expiry(point):
            if point == "viewer_before_session_commit":
                self.clock.advance(1001)

        self.store.fault = cross_expiry
        token = self.finish(nonce, state)
        self.store.fault = lambda _: None
        self.assertEqual(self.private_read(token)["data"]["projects"], [])
        session = self.auth.resolve(token)
        with self.actor_store(session.subject.credential) as store:
            with self.assertRaises(LocalError):
                ViewerReads(store, self.settings).read(session, "overview", "p", {})

    def test_expired_release_removes_unexposed_session(self):
        self.setup_auth(fake_clock=True)
        nonce, state, _ = self.begin()

        def cross_deadline(point):
            if point == "viewer_before_session_commit":
                self.clock.advance(6000)

        self.store.fault = cross_deadline
        with self.assertRaisesRegex(LocalError, "release_expired"):
            self.finish(nonce, state)
        self.assertFalse(self.auth.sessions)
        self.store.fault = lambda _: None

    def test_account_switch_rotates_without_transferring_projects(self):
        self.setup_auth()
        self.bind("second", "54321")
        self.policy(
            "grant",
            {
                "principal": "second",
                "project": "separate",
                "expected_version": 1,
                "starts": None,
                "expires": None,
            },
        )
        first = self.login()
        self.provider.user_id = "54321"
        second = self.login(first)
        with self.assertRaises(LocalError):
            self.auth.resolve(first)
        self.assertEqual(self.private_read(second)["data"]["projects"], ["separate"])
        session = self.auth.resolve(second)
        self.assertEqual(session.subject.principal, "second")
        with self.actor_store(session.subject.credential) as store:
            with self.assertRaises(LocalError):
                ViewerReads(store, self.settings).read(session, "memories", "p", {})


class LocalViewerAuth(AuthCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresViewerAuth(AuthCases, NativeFixture):
    pass
