"""Single-process, hashed browser sessions and one-use OAuth transactions.

Only the fixed provider's numeric identity can reach canonical session issuance.
Ephemeral state is bounded and never enters content backups or diagnostics.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.viewer_budget import request_budget
from memoryking.adapters.viewer_identity import ViewerIdentity
from memoryking.adapters.viewer_reads import ViewerSession
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import Clock, LocalError, Released, SystemClock, temporal_valid
from memoryking.domain.viewer import GITHUB_ISSUER, VerifiedViewer

SESSION_COOKIE = "__Host-memoryking-viewer"
LOGIN_COOKIE = "__Host-memoryking-login"
CALLBACK = "/viewer/auth/github/callback"
GITHUB_AUTHORIZATION_ISSUER = "https://github.com/login/oauth"
AUTHORIZE = "https://github.com/login/oauth/authorize"
StoreFactory = Callable[[float], AbstractContextManager[LocalStore]]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def opaque(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is not None


class Provider(Protocol):
    def authenticate(self, code: str, verifier: str, callback: str, deadline: float) -> str: ...


@dataclass(repr=False)
class Pending:
    nonce_hash: str
    csrf_hash: str
    utc: int
    mono: int
    fence: tuple[str, str, str, int]
    verifier: str = field(default="", repr=False)


@dataclass(repr=False)
class Session:
    subject: VerifiedViewer
    born_utc: int
    born_mono: int
    seen_utc: int
    seen_mono: int


class ViewerAuth:
    def __init__(
        self,
        config: dict[str, Any],
        store: StoreFactory,
        provider: Provider,
        *,
        clock: Clock | None = None,
    ):
        self.config, self.store, self.provider = config, store, provider
        self.clock = clock or SystemClock()
        self.guard = threading.RLock()
        self.contexts: dict[str, Pending] = {}
        self.pending: dict[str, Pending] = {}
        self.sessions: dict[str, Session] = {}
        self._csrf_key = secrets.token_bytes(32)
        self.last_utc = self.clock.utc_ms()
        self.last_mono = self.clock.monotonic_ms()

    def _now(self) -> tuple[int, int]:
        utc, mono = self.clock.utc_ms(), self.clock.monotonic_ms()
        if utc < self.last_utc or mono < self.last_mono:
            self.contexts.clear()
            self.pending.clear()
            self.sessions.clear()
            raise LocalError("clock_untrusted")
        self.last_utc, self.last_mono = utc, mono
        return utc, mono

    def _expired(self, item: Pending, utc: int, mono: int) -> bool:
        lifetime = int(self.config["login_seconds"]) * 1000
        return utc - item.utc >= lifetime or mono - item.mono >= lifetime

    def _expiry(self, item: Session, utc: int, mono: int) -> int:
        absolute, idle = (
            int(self.config["session_absolute_seconds"]) * 1000,
            int(self.config["session_idle_seconds"]) * 1000,
        )
        return min(
            item.born_utc + absolute,
            item.seen_utc + idle,
            utc + absolute - (mono - item.born_mono),
            utc + idle - (mono - item.seen_mono),
        )

    def _clean(self) -> tuple[int, int]:
        utc, mono = self._now()
        self.contexts = {k: v for k, v in self.contexts.items() if not self._expired(v, utc, mono)}
        self.pending = {k: v for k, v in self.pending.items() if not self._expired(v, utc, mono)}
        self.sessions = {k: v for k, v in self.sessions.items() if self._expiry(v, utc, mono) > utc}
        return utc, mono

    def _fence(self, store: LocalStore) -> tuple[str, str, str, int]:
        tenant = self.config["tenant"]
        if hasattr(store, "tenant") and getattr(store, "tenant") != tenant:
            raise LocalError("context_changed")
        row = store.db.execute(
            "SELECT policy_epoch FROM tenants WHERE tenant=?", (tenant,)
        ).fetchone()
        if row is None:
            raise LocalError("context_changed")
        return store.store_id, tenant, store.incarnation, int(row[0])

    def snapshot(self, deadline: float) -> tuple[str, str, str, int]:
        with self.store(deadline) as store, request_budget(store, deadline):
            with store.transaction() as decision:
                result = self._fence(store)
            store._expose()
            decision.check(store.clock)
            return result

    def context(self, old_nonce: str, deadline: float) -> tuple[dict[str, Any], str]:
        fence = self.snapshot(deadline)
        nonce, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.guard:
            utc, mono = self._clean()
            if opaque(old_nonce):
                previous = digest(old_nonce)
                self.contexts.pop(previous, None)
                self.pending = {k: v for k, v in self.pending.items() if v.nonce_hash != previous}
            if len(self.contexts) + len(self.pending) >= self.config["limits"]["pending_logins"]:
                raise LocalError("service_unavailable")
            self.contexts[digest(nonce)] = Pending(digest(nonce), digest(csrf), utc, mono, fence)
        return {
            "schema_version": 1,
            "csrf": csrf,
            "expires_ms": utc + self.config["login_seconds"] * 1000,
        }, nonce

    def start(self, nonce: str, csrf: str, old_session: str, deadline: float) -> dict[str, Any]:
        if not opaque(nonce) or not opaque(csrf):
            raise LocalError("login_failed")
        fence = self.snapshot(deadline)
        with self.guard:
            self._clean()
            item = self.contexts.get(digest(nonce))
            if (
                item is None
                or not hmac.compare_digest(item.csrf_hash, digest(csrf))
                or item.fence != fence
            ):
                raise LocalError("login_failed")
            del self.contexts[digest(nonce)]
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            item.verifier = verifier
            self.pending[digest(state)] = item
            self.sessions.pop(digest(old_session), None)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return {
            "schema_version": 1,
            "authorization_url": AUTHORIZE
            + "?"
            + urlencode(
                {
                    "client_id": self.config["github_client_id"],
                    "redirect_uri": self.config["public_origin"] + CALLBACK,
                    "scope": "",
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "prompt": "select_account",
                    "allow_signup": "false",
                }
            ),
        }

    def callback(self, nonce: str, params: dict[str, str], deadline: float) -> tuple[str, Released]:
        state = params.get("state", "")
        if not opaque(nonce) or not opaque(state):
            raise LocalError("login_failed")
        with self.guard:
            self._clean()
            item = self.pending.get(digest(state))
            if item is None or not hmac.compare_digest(item.nonce_hash, digest(nonce)):
                raise LocalError("login_failed")
            del self.pending[digest(state)]  # Consume before any provider call, including denial.
        if (
            set(params) != {"state", "code", "iss"}
            or params["iss"] != GITHUB_AUTHORIZATION_ISSUER
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", params["code"])
        ):
            raise LocalError("login_failed")
        if self.snapshot(deadline) != item.fence:
            raise LocalError("context_changed")
        user_id = self.provider.authenticate(
            params["code"], item.verifier, self.config["public_origin"] + CALLBACK, deadline
        )
        if not isinstance(user_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", user_id):
            raise LocalError("login_failed")
        token = secrets.token_urlsafe(32)
        key = digest(token)
        try:
            with self.store(deadline) as store, request_budget(store, deadline):
                with store.transaction() as decision:
                    if self._fence(store) != item.fence:
                        raise LocalError("context_changed")
                    tenant = self.config["tenant"]
                    row = store.db.execute(
                        "SELECT * FROM viewer_bindings WHERE tenant=? AND issuer=? AND user_id=? AND enabled=1",
                        (tenant, GITHUB_ISSUER, user_id),
                    ).fetchone()
                    if row is None:
                        raise LocalError("login_failed")
                    grants = store.db.execute(
                        "SELECT project,starts,expires FROM viewer_grants WHERE tenant=? AND principal=? LIMIT 1025",
                        (tenant, row["principal"]),
                    ).fetchall()
                    if len(grants) > 1024 or not any(
                        temporal_valid(decision.evaluated_ms, g["starts"], g["expires"])
                        for g in grants
                    ):
                        raise LocalError("login_failed")
                    marker = {
                        "user_id": user_id,
                        "generation": row["generation"],
                        "policy_epoch": item.fence[3],
                    }
                    credential = store._token(
                        "credential", tenant=tenant, principal=row["principal"], viewer=marker
                    )
                    subject = VerifiedViewer(
                        store.store_id,
                        tenant,
                        row["principal"],
                        user_id,
                        store.incarnation,
                        row["generation"],
                        item.fence[3],
                        credential,
                    )
                    with self.guard:
                        utc, mono = self._clean()
                        if self._expired(item, utc, mono):
                            raise LocalError("login_failed")
                        matching = [
                            k
                            for k, s in self.sessions.items()
                            if s.subject.tenant == tenant
                            and s.subject.principal == row["principal"]
                        ]
                        # Fixed upper bound: 256 canonical bindings, each with configured session ceiling.
                        if len(matching) >= self.config["limits"]["active_sessions"]:
                            oldest = min(matching, key=lambda k: self.sessions[k].born_mono)
                            del self.sessions[oldest]
                        if len(self.sessions) >= 256 * self.config["limits"]["active_sessions"]:
                            raise LocalError("service_unavailable")
                        self.sessions[key] = Session(subject, utc, mono, utc, mono)
                    store.fault("viewer_before_session_commit")
                store._expose()
                decision.check(store.clock)
                return token, Released(
                    canonical_bytes({"schema_version": 1, "status": "signed_in"}),
                    decision,
                    store.clock,
                )
        except BaseException:
            with self.guard:
                self.sessions.pop(key, None)
            raise

    def resolve(self, token: str) -> ViewerSession:
        if not opaque(token):
            raise LocalError("unauthenticated")
        with self.guard:
            utc, mono = self._clean()
            key = digest(token)
            item = self.sessions.get(key)
            if item is None:
                raise LocalError("unauthenticated")
            item.seen_utc, item.seen_mono = utc, mono
            csrf = hmac.new(self._csrf_key, key.encode(), hashlib.sha256).hexdigest()
            return ViewerSession(item.subject, key, csrf, self._expiry(item, utc, mono))

    def logout(self, token: str, csrf: str, all_sessions: bool, deadline: float) -> None:
        session = self.resolve(token)
        if not hmac.compare_digest(session.csrf, csrf):
            raise LocalError("login_failed")
        if all_sessions:
            with self.store(deadline) as store, request_budget(store, deadline):
                with store.transaction() as decision:
                    subject = session.subject
                    if self._fence(store) != (
                        subject.store,
                        subject.tenant,
                        subject.incarnation,
                        subject.policy_epoch,
                    ):
                        raise LocalError("context_changed")
                    row = ViewerIdentity(store)._binding(subject.tenant, subject.principal)
                    if (
                        row is None
                        or not row["enabled"]
                        or row["generation"] != subject.generation
                        or row["user_id"] != subject.user_id
                    ):
                        raise LocalError("context_changed")
                    # Construct one self-only durable generation operation. No owner credential.
                    ViewerIdentity(store)._mutate(
                        {"tenant": subject.tenant, "principal": subject.principal},
                        {
                            "version": 1,
                            "incarnation": subject.incarnation,
                            "key": "logout-" + session.identity,
                            "action": "logout_all",
                            "args": {
                                "principal": subject.principal,
                                "expected_version": row["version"],
                            },
                        },
                        decision.evaluated_ms,
                    )
                store._expose()
                decision.check(store.clock)
        with self.guard:
            if all_sessions:
                self.sessions = {
                    k: v
                    for k, v in self.sessions.items()
                    if (v.subject.tenant, v.subject.principal)
                    != (session.subject.tenant, session.subject.principal)
                }
            else:
                self.sessions.pop(digest(token), None)

    def forget(self, token: str) -> None:
        with self.guard:
            self.sessions.pop(digest(token), None)
