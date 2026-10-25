"""Fixed real-product acceptance suites, never a fixture-answer adapter.

Each listed TestCase opens disposable actual stores/processes. Scenario booleans
are derived from executed assertions; skipped/empty/failing suites cannot pass.
Symbolic fixture labels are replaced by these concrete executable cases, including
real generated positions/identities. No model, email, live database or Docker use.
"""

import hashlib
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Match unittest discovery without inheriting an ambient project's test modules.
for directory in (ROOT, ROOT / "tools", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

PROBES = {
    "S04": (
        "test_qualification_security.LocalQualificationSecurity.test_foreign_project_read_graph_cache_export_and_receipts_are_denied",
        "test_qualification_security.NativeQualificationSecurity.test_foreign_project_read_graph_cache_export_and_receipts_are_denied",
        "test_local_store.LocalTests.test_cross_store_tokens_and_tenant_qualified_foreign_keys",
        "test_postgres_database.PostgresDatabaseTests.test_rls_default_deny_and_transaction_scope",
        "test_postgres_database.PostgresDatabaseTests.test_tenant_foreign_key_and_forced_owner_rls",
        "test_shared_http.SharedHTTPTests.test_capture_context_distinct_principals_and_host_origin_rules",
        "test_atlas.AtlasTests.test_hidden_claim_has_no_node_edge_label_count_or_focus_signal",
        "test_postgres_atlas.PostgresAtlasTests.test_hidden_claim_has_no_node_edge_label_count_or_focus_signal",
        "test_inspection.LocalInspectionTests.test_historical_projection_scope_reopening_and_current_source_denial",
        "test_inspection.NativeInspectionTests.test_historical_projection_scope_reopening_and_current_source_denial",
        "test_retrieval.RetrievalTests.test_revocation_denies_cached_compilation_and_replay",
        "test_postgres_parity.PostgresRetrievalParity.test_revocation_denies_cached_compilation_and_replay",
        "test_local_store.LocalTests.test_real_socket_protocol_and_export_reauthorization",
    ),
    "S11": (
        "test_qualification_security.LocalQualificationSecurity.test_hostile_source_cannot_change_policy_or_promote_itself",
        "test_qualification_security.NativeQualificationSecurity.test_hostile_source_cannot_change_policy_or_promote_itself",
        "test_experience.ProcedureTests.test_roles_and_source_text_cannot_activate_or_bypass_review",
        "test_postgres_experience.NativeProcedureTests.test_roles_and_source_text_cannot_activate_or_bypass_review",
        "test_knowledge.KnowledgeTests.test_breadth_legacy_authority_and_span_validation",
        "test_postgres_parity.PostgresKnowledgeParity.test_breadth_legacy_authority_and_span_validation",
    ),
    "S19": (
        "test_local_store.LocalTests.test_real_socket_protocol_and_export_reauthorization",
        "test_local_store.LocalTests.test_owner_permissions_and_tampered_handles",
        "test_dashboard.DashboardTests.test_host_origin_fetch_metadata_and_mutation_guards",
        "test_dashboard.DashboardTests.test_expired_single_use_codes_and_current_session_epoch",
        "test_dashboard.DashboardTests.test_no_scoped_principal_bootstrap_or_path_traversal",
        "test_dashboard.DashboardTests.test_browser_command_auth_receipt_origin_and_epoch",
        "test_shared_dashboard.SharedDashboardTests.test_browser_commands_use_current_cookie_authority_and_csrf",
        "test_shared_dashboard.SharedDashboardTests.test_navigation_assets_bootstrap_get_head_csrf_and_single_use",
        "test_shared_http.SharedHTTPTests.test_capture_context_distinct_principals_and_host_origin_rules",
    ),
    "S12": (
        "test_embedding_jobs.LocalEmbeddingJobs.test_reconfigure_purge_and_restore_fence_old_derived_work",
        "test_postgres_embedding_jobs.NativeEmbeddingJobs.test_reconfigure_purge_and_restore_fence_old_derived_work",
        "test_embedding_jobs.LocalEmbeddingJobs.test_revoked_worker_read_cannot_commit_previously_leased_output",
        "test_postgres_embedding_jobs.NativeEmbeddingJobs.test_revoked_worker_read_cannot_commit_previously_leased_output",
        "test_optional_migrations.LocalOptionalMigrations.test_purge_removes_vectors_and_retained_adapter_bases",
        "test_postgres_optional.NativeOptionalRecovery.test_purge_removes_vectors_and_retained_adapter_bases",
        "test_processing.LocalProcessingTests.test_targeted_source_loss_denies_summary_replay_and_leaves_unrelated_claim",
        "test_postgres_processing.NativeProcessingTests.test_targeted_source_loss_denies_summary_replay_and_leaves_unrelated_claim",
        "test_experience.ProcedureTests.test_retry_is_bound_to_current_authority_and_history",
        "test_postgres_experience.NativeProcedureTests.test_retry_is_bound_to_current_authority_and_history",
        "test_legacy_migration.LocalMigrationTests.test_purge_mapping_cannot_be_resurrected_by_repeat_import",
        "test_postgres_migration.NativeMigrationTests.test_purge_mapping_cannot_be_resurrected_by_repeat_import",
        "test_legacy_migration.LocalMigrationTests.test_restore_rejects_old_import_plan_and_preserves_later_purge",
        "test_postgres_migration.NativeMigrationTests.test_restore_rejects_old_import_plan_and_preserves_later_purge",
    ),
    "S09": (
        "test_processing_repository.LocalProcessingRepositoryTests.test_code_change_omits_dependent_summary_without_removing_unrelated_knowledge",
        "test_postgres_processing.NativeProcessingRepositoryTests.test_code_change_omits_dependent_summary_without_removing_unrelated_knowledge",
        "test_processing.LocalProcessingTests.test_targeted_source_loss_denies_summary_replay_and_leaves_unrelated_claim",
        "test_postgres_processing.NativeProcessingTests.test_targeted_source_loss_denies_summary_replay_and_leaves_unrelated_claim",
    ),
    "S10": (
        "test_embedding_worker.EmbeddingWorkerTests.test_provider_failure_is_one_attempt_and_usage_unknown",
        "test_processing_transport.SharedProcessingTransportTests.test_shared_provider_outage_keeps_lexical_path_and_unknown_usage",
        "test_embedding_worker.EmbeddingWorkerTests.test_expired_dispatch_does_not_send_and_records_known_zero",
        "test_dense_retrieval.DenseRetrievalTests.test_native_engine_availability_is_explicit_and_lexical_still_works",
        "test_postgres_dense.NativeDenseRetrieval.test_native_engine_availability_is_explicit_and_lexical_still_works",
    ),
    "S30": (
        "test_processing.LocalProcessingTests.test_harmful_synthesis_falls_back_and_all_critical_state_is_verbatim",
        "test_postgres_processing.NativeProcessingTests.test_harmful_synthesis_falls_back_and_all_critical_state_is_verbatim",
        "test_processing.LocalProcessingTests.test_optional_budget_never_replaces_essential_state",
        "test_postgres_processing.NativeProcessingTests.test_optional_budget_never_replaces_essential_state",
        "test_processing_transport.LocalProcessingTransportTests.test_harmful_candidate_cannot_rewrite_actual_provider_input",
        "test_processing_transport.SharedProcessingTransportTests.test_harmful_candidate_cannot_rewrite_actual_provider_input",
    ),
    "S08": (
        "test_delta.LocalDeltaTests.test_full_delta_reconstruction_and_both_artifacts_replay_exactly",
        "test_postgres_delta.NativeDeltaTests.test_full_delta_reconstruction_and_both_artifacts_replay_exactly",
        "test_delta.LocalDeltaTests.test_missing_hash_schema_sequence_and_expired_bases_fall_back_current",
        "test_postgres_delta.NativeDeltaTests.test_missing_hash_schema_sequence_and_expired_bases_fall_back_current",
        "test_resume_adapter.ResumeAdapterTests.test_missing_client_base_reconstructs_full_and_counts_both_transfers",
        "test_shared_resume_adapter.SharedResumeAdapterTests.test_missing_base_shared_sdk_reconstructs_and_counts_full_fallback",
        "test_resume_adapter.ResumeAdapterTests.test_wrong_target_hash_refetches_once_and_never_passes_corrupt_state",
    ),
    "S24": (
        "test_resume_adapter.ResumeAdapterTests.test_model_reset_retained_adapter_state_restores_every_essential_in_actual_request",
        "test_shared_resume_adapter.SharedResumeAdapterTests.test_model_reset_with_retained_base_has_full_essentials_in_actual_provider_request",
        "test_delta.LocalDeltaTests.test_model_context_policy_change_and_revision_gap_force_new_full_state",
        "test_postgres_delta.NativeDeltaTests.test_model_context_policy_change_and_revision_gap_force_new_full_state",
        "test_resume_adapter.ResumeAdapterTests.test_permission_epoch_rebuild_excludes_old_memory_and_derived_handles",
    ),
    "S03": (
        "test_working.WorkingTests.test_competing_cas_writers_have_one_winner",
        "test_postgres_concurrency.PostgresIndependentRequests.test_checkpoint_cas_across_independent_requests",
    ),
    "S17": (
        "test_local_store.LocalTests.test_projection_gap_cancel_reindex_and_stale_fence",
        "test_postgres_foundation_parity.PostgresFoundationParity.test_projection_gap_cancel_reindex_and_stale_fence",
        "test_retrieval.RetrievalTests.test_watermark_and_required_channel_degradation",
        "test_postgres_parity.PostgresRetrievalParity.test_watermark_and_required_channel_degradation",
        "test_postgres_database.PostgresDatabaseTests.test_real_read_replica_cannot_authorize_release",
    ),
    "S18": (
        "test_shared_acceptance_matrix.SQLiteReleaseMatrix.test_release_races_compile_cache_replay_export",
        "test_shared_acceptance_matrix.PostgresReleaseMatrix.test_release_races_compile_cache_replay_export",
    ),
    "S23": (
        "test_knowledge.KnowledgeTests.test_march_history_quarantine_no_resurrection",
        "test_postgres_parity.PostgresKnowledgeParity.test_march_history_quarantine_no_resurrection",
        "test_knowledge.KnowledgeTests.test_concurrent_review_optimistic_version",
        "test_postgres_concurrency.PostgresIndependentRequests.test_review_cas_across_independent_requests",
        "test_knowledge.KnowledgeTests.test_evidence_revocation_purge_and_restore",
    ),
    "S28": (
        "test_handoff.HandoffTests.test_prepare_is_opaque_and_does_not_transfer_ownership",
        "test_handoff.HandoffTests.test_role_name_and_opaque_handle_do_not_authorize_another_identity",
        "test_handoff.HandoffTests.test_recipient_only_policy_never_leaks_through_preview_or_prepare",
        "test_handoff.HandoffTests.test_full_preview_uses_intersection_and_is_not_acknowledgeable",
        "test_handoff.HandoffTests.test_checkpoint_audience_and_dependency_closure_must_allow_recipient",
        "test_handoff.HandoffTests.test_permissions_rechecked_before_duplicate_delivery_and_ack",
        "test_postgres_parity.PostgresHandoffParity.test_prepare_is_opaque_and_does_not_transfer_ownership",
        "test_postgres_parity.PostgresHandoffParity.test_role_name_and_opaque_handle_do_not_authorize_another_identity",
        "test_postgres_parity.PostgresHandoffParity.test_recipient_only_policy_never_leaks_through_preview_or_prepare",
        "test_postgres_parity.PostgresHandoffParity.test_full_preview_uses_intersection_and_is_not_acknowledgeable",
        "test_postgres_parity.PostgresHandoffParity.test_checkpoint_audience_and_dependency_closure_must_allow_recipient",
        "test_postgres_parity.PostgresHandoffParity.test_permissions_rechecked_before_duplicate_delivery_and_ack",
    ),
    "S32": (
        "test_local_store.LocalTests.test_restore_old_history_all_handles_and_current_journal",
        "test_local_store.LocalTests.test_lost_anchor_requires_independent_current_journal_checkpoint",
        "test_local_store.LocalTests.test_interrupted_activation_stays_unavailable_then_explicit_retry",
        "test_postgres_store.PostgresStoreTests.test_native_backup_restore_retires_history_and_replays_revocation",
        "test_postgres_store.PostgresStoreTests.test_lost_anchor_requires_independently_retained_current_journal_head",
        "test_postgres_store.PostgresStoreTests.test_restore_interruption_stays_fenced_then_explicit_retry_recovers",
        "test_handoff.HandoffTests.test_purge_erases_handoff_copies_and_restore_retires_old_history",
        "test_working.WorkingTests.test_backup_restore_retains_independent_operation_identity",
        "test_working.WorkingTests.test_restore_retains_safe_operation_identity_when_result_source_was_lost",
        "test_postgres_operation_recovery.PostgresOperationRecovery.test_restored_dispatched_operation_requires_explicit_reconciliation",
        "test_postgres_operation_recovery.PostgresOperationRecovery.test_restore_retires_handoff_materialization_ack_and_resume",
        "test_postgres_operation_recovery.PostgresOperationRecovery.test_missing_result_evidence_keeps_identity_but_requires_reconciliation",
        "test_postgres_operation_recovery.PostgresOperationRecovery.test_actual_process_death_after_journal_replays_without_redispatch",
        "test_retrieval.RetrievalTests.test_restore_rejects_old_history_and_retained_artifacts",
        "test_postgres_parity.PostgresRetrievalParity.test_restore_rejects_old_history_and_retained_artifacts",
        "test_repository.RepositoryTests.test_restore_retires_snapshot_validation_and_request_namespace",
        "test_postgres_parity.PostgresRepositoryParity.test_restore_retires_snapshot_validation_and_request_namespace",
    ),
    "S33": (
        "test_inference.InferenceTests.test_exact_request_and_complete_derived_and_handle_closure",
        "test_inference.InferenceTests.test_quarantine_rebuild_excludes_original_derived_and_hidden_provider_state",
        "test_inference.InferenceTests.test_revocation_blocks_dispatch_and_explicit_identity_switch_starts_empty",
        "test_inference.InferenceTests.test_explicit_switch_to_another_tenant_store_discards_old_context",
        "test_inference.InferenceTests.test_time_expiry_without_epoch_notice_blocks_host_and_grants",
        "test_inference.InferenceTests.test_inflight_result_quarantined_after_policy_change",
        "test_inference.InferenceTests.test_restore_retires_old_binding_and_permit_even_with_replacement_context",
        "test_inference.InferenceTests.test_unavailable_authorization_never_uses_cached_permit",
        "test_postgres_inference.PostgresInferenceParity.test_quarantine_rebuild_excludes_original_derived_and_hidden_provider_state",
        "test_postgres_inference.PostgresInferenceParity.test_revocation_then_explicit_identity_switch_excludes_old_provider_state",
        "test_postgres_inference.PostgresInferenceParity.test_inflight_result_quarantined_after_policy_change",
        "test_shared_http.SharedHTTPTests.test_actual_provider_request_rebuild_after_revocation",
        "test_shared_http.SharedHTTPTests.test_provider_grant_expiry_blocks_without_notification_and_rebuilds",
        "test_shared_http.SharedHTTPTests.test_provider_tenant_switch_drops_original_derived_and_handle_state",
        "test_shared_http.SharedHTTPTests.test_provider_restore_and_permission_service_outage_fail_closed",
    ),
    "S35": (
        "test_shared_acceptance_matrix.SQLiteReleaseMatrix.test_each_expiry_predicate_at_4999_and_5001_ms",
        "test_shared_acceptance_matrix.SQLiteReleaseMatrix.test_export_cursor_expiry_and_backward_clock",
        "test_shared_acceptance_matrix.PostgresReleaseMatrix.test_each_expiry_predicate_at_4999_and_5001_ms",
        "test_shared_acceptance_matrix.PostgresReleaseMatrix.test_export_cursor_expiry_and_backward_clock",
        "test_inference.InferenceTests.test_permit_deadline_and_delayed_release_do_not_extend",
        "test_postgres_inference.PostgresInferenceParity.test_permit_deadline_and_delayed_release_do_not_extend",
        "test_shared_http.ReleaseBoundaryTests.test_delay_between_headers_and_body_never_emits_expired_content",
    ),
    "S36": (
        "test_shared_acceptance_matrix.SQLiteReleaseMatrix.test_selector_overlap_unknown_and_rejected_predicates",
        "test_shared_acceptance_matrix.SQLiteReleaseMatrix.test_durable_replacement_inventory_cannot_exhaust_proof_silently",
        "test_shared_acceptance_matrix.PostgresReleaseMatrix.test_selector_overlap_unknown_and_rejected_predicates",
        "test_shared_acceptance_matrix.PostgresReleaseMatrix.test_durable_replacement_inventory_cannot_exhaust_proof_silently",
        "test_repository_git.RepositoryGitTests.test_branch_divergence_rebase_and_shallow_history_are_distinct",
        "test_repository.RepositoryTests.test_trusted_ancestry_future_valid_and_unrelated_branch",
        "test_postgres_parity.PostgresRepositoryParity.test_trusted_ancestry_future_valid_and_unrelated_branch",
    ),
}


class EvidenceResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def suite_passed(result, expected):
    return (
        expected > 0
        and result.wasSuccessful()
        and not result.skipped
        and not result.expectedFailures
        and len(result.passed) == expected
        and result.testsRun == expected
    )


class SharedScenarios:
    name = "sqlite-postgresql-product-suites-v1"
    supported_scenarios = frozenset(PROBES)

    def __init__(self):
        self.evidence = []

    def close(self):
        pass  # Each real TestCase owns and removes its isolated state.

    def reset(self, seed):
        if seed["classification"] != "synthetic":
            raise ValueError("only isolated synthetic fixtures are supported")

    def execute(self, operation, arguments):
        if operation != "run_shared_product_suite" or set(arguments) != {"scenario"}:
            raise ValueError("unsupported shared scenario operation")
        names = PROBES[arguments["scenario"]]
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromNames(names)
        result = EvidenceResult()
        start = time.monotonic()
        suite.run(result)
        passed = suite_passed(result, len(names))
        self.evidence.append(
            {
                "scenario": arguments["scenario"],
                "seconds": round(time.monotonic() - start, 3),
                "expected_tests": list(names),
                "passed_tests": result.passed,
                "tests_run": result.testsRun,
                "skipped_tests": [test.id() for test, _ in result.skipped],
                "failed_tests": [test.id() for test, _ in result.failures + result.errors],
                "source_sha256": {
                    name.split(".")[0]: hashlib.sha256(
                        (ROOT / "tests" / (name.split(".")[0] + ".py")).read_bytes()
                    ).hexdigest()
                    for name in names
                },
            }
        )
        print(arguments["scenario"] + (" passed" if passed else " failed"), flush=True)
        return {
            "all_product_assertions_passed": passed,
            "skipped_tests": len(result.skipped),
            "tests_executed": result.testsRun,
        }
