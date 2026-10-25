"""Reuse product assertions against native PostgreSQL; no SQL/authorization mocks.

The selected cases are listed explicitly below. This is a growing parity suite,
not a claim that every local, transport or recovery scenario has been ported.
"""

import hashlib
import json
import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
import test_handoff
import test_knowledge
import test_repository
import test_retrieval
import test_working

from memoryking.adapters.handoff import Handoff
from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.postgres_admin import initialize_tenant, migrate, provision_roles
from memoryking.adapters.postgres_pool import create_pool
from memoryking.adapters.postgres_store import PostgresStore
from memoryking.adapters.repository_store import Repository
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.working import Working
from tools.postgres_dev_cluster import DevCluster

BIN = Path(os.environ.get("MEMORYKING_PG_BIN", "tmp/postgres-dev/runtime/bin")).resolve()


class InspectionSQL:
    """Test-only fixture inspection/corruption, still under a real exclusive DB gate."""

    def __init__(self, store, delegate):
        self.store, self.delegate = store, delegate

    def execute(self, query, args=()):
        if self.store.active:
            return self.delegate.execute(query, args)
        with self.store.transaction():
            return self.delegate.execute(query, args)

    def commit(self):
        pass  # The preceding fixture change already committed in its explicit transaction.


class NativeFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (BIN / "pg_ctl").is_file():
            raise unittest.SkipTest("native PostgreSQL runtime not configured")
        cls.cluster = DevCluster(BIN)
        try:
            with psycopg.connect(cls.cluster.dsn(), autocommit=True) as admin:
                provision_roles(admin)
            with psycopg.connect(cls.cluster.dsn("mk_migration"), autocommit=True) as migration:
                migrate(migration)
            cls.pool = create_pool(cls.cluster.dsn("mk_runtime"))
        except BaseException:
            cls.cluster.close()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()
        cls.cluster.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-pg-parity-", dir="/tmp")
        self.root = Path(self.temp.name)
        self.data, self.control = self.root / "data", self.root / "control"
        self.tenant = "t_" + uuid.uuid4().hex
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as migration:
            self.owner = initialize_tenant(
                migration, self.data, self.control, self.tenant
            ).read_text()
        self.store = PostgresStore(self.pool, self.data, self.control, self.tenant, self.owner)
        self.store.db = InspectionSQL(self.store, self.store.db)
        self.k = Knowledge(self.store)
        self.n = 0
        self.ns = self.store.namespace(self.owner, "p")
        self.raw = "Observed timeout=30. A result, not a universal rule."
        self.obs = self.store.capture(
            self.owner,
            self.ns,
            "e",
            {
                "version": 1,
                "project": "p",
                "source": "test",
                "source_event": "e",
                "text": self.raw,
                "readers": None,
                "classification": "internal",
                "observed_at_ms": 1,
            },
        )
        self.span = {
            "observation": self.obs["observation"],
            "start_byte": 0,
            "end_byte": len(self.raw.encode()),
            "content_hash": hashlib.sha256(self.raw.encode()).hexdigest(),
        }

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    @contextmanager
    def actor(self, credential):
        if credential is None or credential == self.owner:
            yield self.store
        else:
            with PostgresStore(
                self.pool,
                self.data,
                self.control,
                self.tenant,
                credential,
                clock=self.store.clock,
                fault=self.store.fault,
            ) as store:
                yield store

    def service(self, adapter, command, credential=None, raw=False):
        with self.actor(credential) as store:
            output = []
            adapter(store).execute(credential or self.owner, command).emit(output.append)
            return output[0] if raw else json.loads(output[0])

    def run_k(self, action, args, credential=None, key=None):
        self.n += 1
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": args,
        }
        if action in {"propose", "review", "attest", "promote"}:
            command["key"] = key or f"k{self.n}"
        return self.service(Knowledge, command, credential)

    def run_r(self, action="brief", args=None, credential=None, raw=False):
        return self.service(
            Retrieval,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": action,
                "args": args or self.brief_args(),
            },
            credential,
            raw,
        )

    def execute(self, command, credential=None, raw=False):
        return self.service(Working, command, credential, raw)

    def run_h(self, action, args, credential=None, key=None, raw=False):
        self.n += 1
        return self.service(
            Handoff,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": action,
                "args": args,
                "key": key or f"h{self.n}",
            },
            credential,
            raw,
        )

    def restart(self):
        clock = self.store.clock
        self.store.close()
        self.store = PostgresStore(
            self.pool, self.data, self.control, self.tenant, self.owner, clock=clock
        )
        self.store.db = InspectionSQL(self.store, self.store.db)
        self.k = Knowledge(self.store)

    def replay_result(self, credential, command):
        return self.service(Retrieval, command, credential)

    def replay(self, receipt, credential=None):
        return self.run_r(
            "replay", {"project": "p", "receipt": receipt}, credential or self.recipient, raw=True
        )

    def run_repo(self, action, credential=None, key=None, **args):
        self.n += 1
        command = {
            "version": 1,
            "incarnation": self.store.incarnation,
            "action": action,
            "args": {"project": "p", **args},
        }
        if action not in {"inspect", "impact", "coverage", "pending", "history"}:
            command["key"] = key or f"repo{self.n}"
        return self.service(Repository, command, credential)


def reuse(target, source, names):
    for name in names.split():
        setattr(target, name, getattr(source, name))


reuse(NativeFixture, test_knowledge.KnowledgeTests, "body propose review query constraints")
reuse(NativeFixture, test_retrieval.RetrievalTests, "brief_args receipt clock fails constraint")
reuse(
    NativeFixture,
    test_working.WorkingTests,
    "scope command save resume assign intent transition operations",
)
reuse(NativeFixture, test_handoff.HandoffTests, "prepare view ack")


class PostgresKnowledgeParity(NativeFixture):
    pass


reuse(
    PostgresKnowledgeParity,
    test_knowledge.KnowledgeTests,
    """
test_march_history_quarantine_no_resurrection test_partial_selector_replacement
test_concurrent_review_optimistic_version test_breadth_legacy_authority_and_span_validation
test_no_curator_episode_and_proposal test_constraints_complete_private_unknown_conflict_and_expiry
test_policy_selector_unknown_and_explicit_absence
test_attestations_and_promotion_keep_authority_and_dependencies
test_mutation_retry_identical_and_mismatched test_current_policy_expiry_uses_one_persisted_instant
test_purged_policy_blocks_until_explicit_authorized_deprecation
test_registry_limit_and_cross_binding_conflicts test_transitive_visibility_on_promoted_evidence
test_tenant_workspace_policy_cannot_be_silently_omitted
test_promotion_requires_destination_capture_and_policy_retry_reauthorizes
""",
)


class PostgresRetrievalParity(NativeFixture):
    pass


reuse(
    PostgresRetrievalParity,
    test_retrieval.RetrievalTests,
    """
test_mandatory_survives_zero_similarity_and_top_k_zero
test_budget_insufficient_is_explicit_and_no_truncated_mandatory_text
test_exact_final_envelope_unicode_and_special_token_literals
test_unknown_tokenizer_is_disclosed_and_no_provider_required
test_abstention_and_top_k_do_not_claim_absence test_empty_cache_new_claim_acceptance_and_membership
test_time_boundary_recomputes_policy_and_optional_eligibility
test_attestation_expiry_rechecks_freshness_on_cached_requests
test_conflict_expansion_does_not_compete_for_top_k test_hidden_policy_blocks_without_identifiers_or_counts
test_replay_original_bytes_and_separate_new_access_receipt
test_revocation_denies_cached_compilation_and_replay test_source_purge_removes_retained_bytes_and_never_regenerates
test_expired_missing_and_corrupt_are_distinct test_release_deadline_rolls_back_materialization
test_evaluation_instant_and_first_byte_deadline test_watermark_and_required_channel_degradation
test_bounded_fetch_and_explicit_rerun test_hidden_conflict_endpoint_is_generic_incomplete
test_restore_rejects_old_history_and_retained_artifacts
test_optional_excerpt_finds_late_relevance_and_preserves_bounds
""",
)


class PostgresWorkingParity(NativeFixture):
    pass


reuse(
    PostgresWorkingParity,
    test_working.WorkingTests,
    """
test_full_checkpoint_current_policy_and_exact_budget
test_essential_set_never_truncated_and_capture_gaps_explicit
test_compare_and_swap_run_boundaries_and_writer_reassignment test_competing_cas_writers_have_one_winner
test_current_retry_hash_and_grants_before_retry test_complete_evidence_closure_and_replay_reauthorization
test_purge_erases_checkpoint_copies_and_retires_retries test_dirty_artifact_manifest_does_not_claim_durable_bytes
test_unsafe_artifact_paths_evidence_free_success_and_duplicates_reject
test_hidden_policy_blocks_and_expiry_uses_persisted_instant test_release_deadline_rolls_back_before_commit
test_operation_state_machine_retry_and_no_automatic_redispatch
test_stale_writer_cannot_dispatch_and_new_writer_sees_old_ambiguity
test_capacity_rejects_before_a_stream_becomes_unresumable
test_expired_retained_resumes_are_collected_without_losing_checkpoint
""",
)


class PostgresHandoffParity(NativeFixture):
    def setUp(self):
        super().setUp()
        self.recipient = self.store.grant(self.owner, "reviewer", "p", ["read"])
        self.other = self.store.grant(self.owner, "other", "p", ["read"])
        self.saved = self.save(readers=["owner", "reviewer"])


reuse(
    PostgresHandoffParity,
    test_handoff.HandoffTests,
    """
test_prepare_is_opaque_and_does_not_transfer_ownership
test_recipient_gets_complete_state_and_ack_does_not_complete_task
test_role_name_and_opaque_handle_do_not_authorize_another_identity
test_checkpoint_audience_and_dependency_closure_must_allow_recipient
test_recipient_only_policy_never_leaks_through_preview_or_prepare
test_hidden_recipient_policy_blocks_complete_delivery
 test_full_preview_uses_intersection_and_is_not_acknowledgeable
test_exact_duplicate_materialization_is_released_once_per_request_but_not_recompiled
test_permissions_rechecked_before_duplicate_delivery_and_ack
test_time_activated_policy_invalidates_old_view_without_epoch_change
test_attestation_expiry_invalidates_old_materialization
test_new_checkpoint_writer_or_preparation_supersedes_old_handle
test_cancel_blocks_replay_and_retries_without_erasure_claim
test_handoff_expiry_is_half_open_and_auth_instant_allows_bounded_finish
test_release_deadline_rolls_back_materialization
test_budget_failure_never_silently_drops_essential_state
test_missing_artifact_invalidates_prior_view_and_new_packet_reports_gap
test_operation_result_changes_require_fresh_materialization
""",
)


class PostgresRepositoryParity(NativeFixture):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "code"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.repo / "a.py").write_text("def value():\n    return 1\n")
        (self.repo / "b.py").write_text("other = 2\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.rid = self.run_repo(
            "register", remote="local:fixture", clone="first", path=str(self.repo), readers=None
        )["repository"]
        self.first = self.capture()
        self.claim = self.propose()
        self.review(self.claim)


reuse(
    PostgresRepositoryParity,
    test_repository.RepositoryTests,
    "git capture binding bind verify worker",
)
reuse(
    PostgresRepositoryParity,
    test_repository.RepositoryTests,
    """
test_paused_workers_unseen_and_changed_snapshot_block_guidance
test_unchanged_hashes_need_trusted_comparison_and_environment
test_generation_fence_policy_cas_and_exact_capture_retry
test_missing_dependencies_and_unsupported_symbols_stay_unknown
test_trusted_ancestry_future_valid_and_unrelated_branch
test_scope_coverage_and_impact_are_read_only_and_bounded
test_revoked_evidence_and_expiry_block_materialization
test_capabilities_and_private_repo_visibility test_mandatory_snapshot_policy_blocks_brief
test_retained_briefing_requires_current_validation_proof
test_multi_snapshot_scope_never_emits_partially_validated_claim
test_return_to_old_snapshot_advances_barrier_and_fences_worker
test_symbol_impact_has_authorized_endpoints_and_never_validates
test_snapshot_verification_uses_persisted_authorization_instant
test_constraint_dependency_policy_needs_policy_authority
test_restore_retires_snapshot_validation_and_request_namespace
""",
)
