"""Explicit automated M02 evidence; actual report QA/transfer/mail remain external gates."""


def both(module, classes, *methods):
    return [f"{module}.{backend}.{method}" for backend in classes for method in methods]


STORES = ("LocalReports", "NativeReports")
DELIVERY = ("LocalDelivery", "NativeDelivery")
IMPORT = ("LocalImport", "NativeImport")
CATALOG = ("LocalCatalog", "NativeCatalog")

EXPECTED = {
    "AC37": both(
        "test_report_store",
        STORES,
        "test_real_bundle_qa_publish_reader_and_revocation",
        "test_exact_qa_and_corrupted_artifact_block_publication",
    )
    + [
        "test_report_document.ReportDocumentTests.test_long_unicode_code_and_tables_survive_bounded_reader_fragments",
        "test_report_document.ReportDocumentTests.test_missing_duplicate_out_of_order_sections_and_identity_fail",
        "test_report_document.ReportDocumentTests.test_raw_html_is_data_and_qa_is_not_fabricated",
        "test_report_sandbox.ReportSandboxTests.test_real_worker_builds_complete_artifacts",
    ],
    "AC38": both(
        "test_report_store",
        STORES,
        "test_unmapped_external_input_cannot_be_submitted",
        "test_real_bundle_qa_publish_reader_and_revocation",
        "test_withdrawal_fences_worker_and_purge_erases_all_derived_refs",
    )
    + both(
        "test_report_catalog",
        CATALOG,
        "test_catalog_cursor_contains_only_visible_identity_and_revocation_fences_it",
        "test_image_read_requires_whole_report_authority_and_is_not_raw_export",
    )
    + both(
        "test_report_delivery",
        DELIVERY,
        "test_recipient_source_revocation_before_release_sends_no_content",
        "test_expired_release_deadline_after_data_prompt_requeues_without_content",
    ),
    "AC39": both(
        "test_report_store",
        STORES,
        "test_scoped_upload_immutable_submit_and_control_authority",
        "test_chunked_input_conflict_and_scoped_discard",
    )
    + [
        "test_report_document.ReportDocumentTests.test_figure_references_are_identifiers_not_paths_or_urls",
        "test_report_sandbox.ReportSandboxTests.test_no_outside_files_network_child_process_environment_or_inherited_descriptor",
        "test_report_sandbox.ReportSandboxTests.test_unsafe_layout_symlink_and_unavailable_platform_fail_closed",
        "test_report_worker.ConfigurationTests.test_secret_paths_are_explicit_private_and_outside_renderer_roots",
    ],
    "AC40": both(
        "test_report_store",
        STORES,
        "test_manifest_failure_does_not_publish_partial_bundle",
        "test_renderer_releases_request_gate_and_duplicate_worker_is_excluded",
        "test_withdrawal_fences_worker_and_purge_erases_all_derived_refs",
    )
    + both(
        "test_report_delivery",
        DELIVERY,
        "test_failed_publication_journal_intent_cannot_send_an_unpublished_report",
        "test_crash_after_provider_reply_recovers_to_unknown",
    )
    + [
        "test_report_sandbox.ReportSandboxTests.test_deadline_and_memory_monitor_stop_worker",
        "test_report_sandbox.ReportSandboxTests.test_exit_between_resource_sample_and_waitpid_is_reaped",
    ],
    "AC41": both(
        "test_report_delivery",
        DELIVERY,
        "test_reconciliation_requires_evidence_and_records_a_durable_receipt",
        "test_stale_reconciliation_cannot_match_a_later_authorized_cycle",
        "test_reconciled_cycle_keeps_three_attempt_budget_without_reusing_identity",
        "test_ambiguous_experimental_reset_history_is_not_guessed",
        "test_proven_nonsubmission_requires_explicit_operator_reconciliation_before_retry",
        "test_provider_acceptance_is_durable_and_duplicate_attempt_does_not_send",
        "test_proven_pre_data_transients_have_bounded_backoff_and_three_attempts",
        "test_crash_after_provider_reply_recovers_to_unknown",
    ),
    "AC42": both(
        "test_report_delivery",
        DELIVERY,
        "test_unknown_survives_older_content_restore_and_never_retries",
        "test_accepted_survives_older_content_restore_and_never_retries",
    )
    + both(
        "test_report_store",
        STORES,
        "test_restore_keeps_current_source_policy_and_retires_report_history",
        "test_withdrawal_fences_worker_and_purge_erases_all_derived_refs",
    )
    + both(
        "test_report_import", IMPORT, "test_import_survives_older_restore_and_reserves_revision"
    ),
    "AC43": both(
        "test_report_delivery",
        DELIVERY,
        "test_kindle_only_policy_queues_delivery_without_app_visibility",
        "test_proven_pre_data_transients_have_bounded_backoff_and_three_attempts",
    )
    + both(
        "test_report_store",
        STORES,
        "test_real_bundle_qa_publish_reader_and_revocation",
        "test_scoped_upload_immutable_submit_and_control_authority",
    ),
    "AC44": both(
        "test_report_import",
        IMPORT,
        "test_import_reserves_identity_without_app_content_or_email",
        "test_import_is_operator_only_requires_disabled_sender_and_exact_evidence",
        "test_changed_payload_or_title_alias_cannot_duplicate_legacy_identity",
        "test_import_survives_older_restore_and_reserves_revision",
    ),
}

EXTERNAL = {
    "AC37": "Inspect every actual phase PDF page and EPUB; exact QA/evidence and content equivalence.",
    "AC39": "Linux container sandbox negative tests at the frozen product revision.",
    "AC43": "Actual phase app publication and authorized Gmail acceptance; device arrival separate.",
    "AC44": "Cross-repository crash/concurrency tests, preserved baseline, actual transfer receipt and workflow skill.",
}
