"""Exact provider bytes and retained-state parity on native PostgreSQL repositories."""

import json

import test_inference
from test_postgres_parity import NativeFixture, reuse

from memoryking.adapters.inference import Inference
from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_ipc import StoreOperations
from memoryking.domain.local import Released
from memoryking.harness_context import AuthorizedConversation, LocalRecordingProvider
from memoryking.sdk import BINDING_FIELDS, LocalClient


class NativeClient(LocalClient):
    def __init__(self, fixture):
        self.fixture, self.project = fixture, "p"
        self.credential_file = fixture.control / fixture.tenant / "owner.credential"
        self.last_context = self._request(self._credential(), "context", {"project": "p"})
        self.binding = tuple(self.last_context[k] for k in BINDING_FIELDS)

    def _request(self, credential, operation, args):
        with self.fixture.actor(credential) as store:
            result = StoreOperations(store).execute(
                {"version": 1, "operation": operation, "args": args, "credential": credential}
            )
            if isinstance(result, Released):
                output = []
                result.emit(output.append)
                return json.loads(output[0])
            return result


class PostgresInferenceParity(NativeFixture):
    def setUp(self):
        super().setUp()
        self.client = NativeClient(self)
        self.provider = LocalRecordingProvider(self.root / "provider")
        self.context = AuthorizedConversation(self.client, self.provider)

    def call(self, action, args, credential=None):
        return self.service(Inference, {"version": 1, "action": action, "args": args}, credential)

    def test_revocation_then_explicit_identity_switch_excludes_old_provider_state(self):
        reader = self.store.grant(self.owner, "reader", "p", ["read", "inference"])
        client = NativeClient(self)
        client.credential_file = self.root / "reader.credential"
        atomic(client.credential_file, reader.encode())
        client.last_context = client._request(reader, "context", {"project": "p"})
        client.binding = tuple(client.last_context[k] for k in BINDING_FIELDS)
        self.context.rebind(client)
        self.host("restricted prior principal input")
        first = self.context.dispatch()
        self.store.revoke(self.owner, "reader", "p")
        self.fails("denied", self.context.dispatch)
        self.assertEqual(1, len(list(self.provider.directory.glob("*.json"))))
        self.context.rebind(self.client)
        self.host("new allowed input")
        second = self.context.dispatch()
        actual = self.provider.path(second["handle"]).read_text()
        self.assertNotIn("restricted prior principal input", actual)
        self.assertNotIn(first["handle"], actual)


reuse(PostgresInferenceParity, test_inference.InferenceTests, "host args fetch")
reuse(
    PostgresInferenceParity,
    test_inference.InferenceTests,
    """
test_exact_request_and_complete_derived_and_handle_closure
test_ui_originated_injection_keeps_evidence_authority_at_provider_boundary
test_permit_rejects_changed_bytes_manifest_context_and_identity
test_partial_manifest_unknown_provenance_changed_memory_and_missing_handles
test_quarantine_rebuild_excludes_original_derived_and_hidden_provider_state
test_permit_deadline_and_delayed_release_do_not_extend
test_client_elapsed_guard_counts_round_trip_and_does_not_retry
test_pre_dispatch_revocation_invalidates_permit
test_inflight_result_quarantined_after_policy_change
test_ordinary_readers_and_model_tools_cannot_authorize_inference
test_missing_provider_handle_never_silently_discards_context
test_lost_consumption_acknowledgment_cannot_reauthorize_dispatch
""",
)
