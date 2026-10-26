"""M04 scoped auth probes; browser evidence is required separately."""

from mobile_viewer_probes import paired


def auth(*methods):
    return paired("test_viewer_auth", ("LocalViewerAuth", "PostgresViewerAuth"), methods)


def http(*methods):
    return paired(
        "test_viewer_auth_http", ("LocalViewerAuthHTTP", "PostgresViewerAuthHTTP"), methods
    )


def provider(*methods):
    return ["test_github_oauth.ProviderAdapterTests." + method for method in methods]


EXPECTED = {
    "AC12": auth(
        "test_success_pkce_numeric_identity_and_hashed_state",
        "test_closed_auth_dtos_and_cross_context_fence",
        "test_state_nonce_csrf_expiry_denial_and_pkce_failures",
        "test_parallel_callback_and_ambiguous_exchange_are_one_use",
    )
    + http(
        "test_actual_login_cookie_flags_private_read_and_logout",
        "test_callback_replay_denial_missing_nonce_and_logs_are_clean",
    ),
    "AC13": auth(
        "test_unknown_malformed_and_ungranted_identity_never_issues",
        "test_parallel_callback_and_ambiguous_exchange_are_one_use",
    )
    + provider(
        "test_response_limits_duplicates_redirects_scopes_and_json",
        "test_fixed_targets_empty_scopes_numeric_id_and_token_discard",
        "test_child_total_deadline_reaps_process_without_secrets_in_args",
        "test_child_strict_output_spawn_failure_and_quiet_invalid_input",
    ),
    "AC14": auth(
        "test_rotation_restart_idle_absolute_and_clock_rollback",
        "test_pending_capacity_rotation_expiry_and_active_ceiling",
        "test_logout_csrf_and_durable_logout_all_during_issuance",
        "test_grant_expires_during_exchange_and_issuance_failure_cleans_session",
        "test_evaluation_instant_allows_finish_but_expired_grant_cannot_read",
        "test_expired_release_removes_unexposed_session",
    )
    + http("test_actual_login_cookie_flags_private_read_and_logout"),
    "AC15": http(
        "test_start_csrf_origin_fetch_site_host_return_url_and_get_logout",
        "test_forged_callback_does_not_log_out_existing_session",
        "test_closed_post_shapes_callback_bounds_and_no_provider_override",
    )
    + auth(
        "test_state_nonce_csrf_expiry_denial_and_pkce_failures",
        "test_logout_csrf_and_durable_logout_all_during_issuance",
    ),
    "AC16": auth(
        "test_policy_change_during_provider_and_next_read",
        "test_account_switch_rotates_without_transferring_projects",
        "test_restore_fences_pending_and_previous_session",
        "test_grant_expires_during_exchange_and_issuance_failure_cleans_session",
        "test_evaluation_instant_allows_finish_but_expired_grant_cannot_read",
        "test_expired_release_removes_unexposed_session",
        "test_logout_csrf_and_durable_logout_all_during_issuance",
    )
    + http("test_http_revocation_forgets_session_and_provider_failure_is_clean")
    + [
        "test_viewer_auth_http.PostgresViewerAuthHTTP.test_production_internal_auth_factory_has_no_owner_capabilities"
    ],
    "AC17": provider(
        "test_private_regular_secret_files_and_no_blocking_fifo",
        "test_child_strict_output_spawn_failure_and_quiet_invalid_input",
        "test_child_total_deadline_reaps_process_without_secrets_in_args",
    )
    + http(
        "test_disabled_listener_does_not_read_secret_and_enabled_requires_private_file",
        "test_callback_replay_denial_missing_nonce_and_logs_are_clean",
    )
    + ["test_viewer_contracts.ViewerContractTests.test_closed_config_origin_tls_and_bounds"],
}
EXTERNAL = {
    "AC12": "Run tools/viewer_auth_browser_probe.py on both stores/transports; controlled GitHub page, actual app requests and cookie behavior. Real GitHub is AC32, not this phase.",
    "AC15": "Same browser probe must exercise same-site hostile origin and actual top-level callback without Origin.",
    "AC17": "Inspect packaged artifacts/config/examples and safe diagnostic output at the frozen source revision; no provider secret, token or callback value may be embedded.",
}
