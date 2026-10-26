"""M03 automated acceptance map; real-browser evidence remains a separate gate."""


def paired(module, classes, methods):
    return [f"{module}.{backend}.{method}" for backend in classes for method in methods]


def http(*methods):
    return paired("test_viewer_http", ("LocalViewerHTTP", "PostgresViewerHTTP"), methods)


def reads(*methods):
    return paired("test_viewer_reads", ("LocalViewerReads", "PostgresViewerReads"), methods)


EXPECTED = {
    "AC06": http(
        "test_route_headers_mutation_boundary_and_non_disclosure",
        "test_default_resolver_never_authenticates_and_capacity_recovers",
    ),
    "AC07": http(
        "test_actual_get_head_private_reads_and_no_origin_in_both_modes",
        "test_route_headers_mutation_boundary_and_non_disclosure",
        "test_raw_duplicate_headers_and_oversized_line_close_bounded",
    )
    + [
        "test_viewer_startup.SharedViewerStartup.test_shared_cli_omitted_disabled_enabled_and_shutdown",
        "test_viewer_startup.LocalViewerStartup.test_documented_local_example",
    ],
    "AC08": reads(
        "test_no_snapshot_or_hidden_task_fallback",
        "test_memory_private_absent_search_and_session_handles",
        "test_unknown_constraint_blocks_without_hidden_identity",
    )
    + ["test_viewer_matrix.ViewerDifferential.test_identical_http_eligibility_and_coverage"],
    "AC09": http("test_restore_rejects_old_session_cursor_and_cache_conditions")
    + reads(
        "test_cursor_binds_session_query_frontier_and_scope",
        "test_current_session_and_project_intersection",
        "test_deadline_and_expired_session",
        "test_persisted_instant_grant_expiry_and_display_lease",
        "test_constraint_activation_and_verification_expiry_cut_lease",
    ),
    "AC10": http(
        "test_utf8_detail_configured_bounds_and_disconnect_recover",
        "test_query_deadline_interrupts_work_and_connection_recovers",
        "test_slow_header_total_deadline_and_recovery",
        "test_store_admission_pressure_uses_request_budget",
        "test_default_resolver_never_authenticates_and_capacity_recovers",
        "test_raw_duplicate_headers_and_oversized_line_close_bounded",
    )
    + [
        "test_viewer_matrix.ViewerGraphBounds.test_graph_and_equivalent_list_honor_configured_bounds",
        "test_viewer_http.PostgresViewerHTTP.test_postgres_checkout_validation_uses_total_deadline",
    ],
    "AC11": ["test_viewer_matrix.ViewerDifferential.test_identical_http_eligibility_and_coverage"],
    "AC45": http(
        "test_report_http_full_reader_image_private_catalog_and_revocation",
        "test_report_revocation_serializes_after_release_decision",
        "test_route_headers_mutation_boundary_and_non_disclosure",
    ),
}

EXTERNAL = {
    "AC07": "Run tools/viewer_browser_probe.py with installed desktop Chromium against both stores and both viewer transport modes; immutable job must also preserve existing shared transport regressions. Desktop evidence is not phone/OAuth/Tailscale qualification."
}
