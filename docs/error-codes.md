# Error codes

Every error reply carries `error` and a one-line `hint`. This page is generated from
the same table (`memoryking.domain.hints`) by `tools/generate_error_codes.py`, so it
cannot drift from what the service says. Codes are grouped by their first word.

## acceptance

| Code | What to do |
| --- | --- |
| `acceptance_evidence_required` | A passed acceptance criterion must cite evidence. |

## administrative

| Code | What to do |
| --- | --- |
| `administrative_operation_required` | This action needs an administrator credential with a tenant-wide grant. |

## applicability

| Code | What to do |
| --- | --- |
| `applicability_unknown` | Supply bindings for the fields the policy's selectors depend on. |

## artifact

| Code | What to do |
| --- | --- |
| `artifact_availability_invalid` | A stored artifact needs an observation and content hash; an unstored one must have neither. |
| `artifact_input_limit` | The artifact exceeds its size limit. |
| `artifact_integrity_failure` | The artifact bytes do not match their recorded hash. |
| `artifact_not_utf8` | Artifacts must be UTF-8 text. |
| `artifact_path_invalid` | Artifact paths are relative, normalized and without `..`. |
| `artifact_reference_required` | Stored artifacts must reference their observation. |
| `artifact_unavailable` | The artifact is missing or not readable. |

## atlas

| Code | What to do |
| --- | --- |
| `atlas_resync_required` | The atlas cursor is stale; start a new atlas query. |

## backup

| Code | What to do |
| --- | --- |
| `backup_archive_limit` | The backup exceeds its archive size limit. |
| `backup_invalid` | The backup manifest or files failed verification. |
| `backup_requires_new_directory` | Back up into a new directory. |
| `backup_requires_new_external_directory` | Back up into a new directory outside the store and control paths. |
| `backup_role_required` | Run backups with the backup database role. |
| `backup_row_limit` | The backup exceeds its row limit. |

## bearer

| Code | What to do |
| --- | --- |
| `bearer_required` | This endpoint needs an `Authorization: Bearer <credential>` header. |

## binding

| Code | What to do |
| --- | --- |
| `binding_coverage_incomplete` | Bindings must be 1 to 256 objects within 128 KiB; cover each selector field once. |

## blob

| Code | What to do |
| --- | --- |
| `blob_corrupt` | Stored content does not match its hash; restore from backup. |

## bootstrap

| Code | What to do |
| --- | --- |
| `bootstrap_invalid` | The one-use code is wrong, already used, expired, or issued before a permission change; request a new code. |
| `bootstrap_limit` | Too many unused sign-in codes are outstanding; wait a minute and request one again. |

## breadth

| Code | What to do |
| --- | --- |
| `breadth_review_required` | Reviews must confirm breadth_reviewed for claims of this authority. |

## budget

| Code | What to do |
| --- | --- |
| `budget_insufficient` | The required content does not fit the token budget; raise max_output_tokens or narrow scope. |

## checkpoint

| Code | What to do |
| --- | --- |
| `checkpoint_capacity` | This stream holds the maximum number of checkpoints; resume and continue from the latest. |
| `checkpoint_dependency_limit` | A checkpoint may cite at most the documented number of dependencies. |
| `checkpoint_evidence_unavailable` | Evidence cited by the checkpoint is missing or unreadable. |
| `checkpoint_input_limit` | The checkpoint exceeds its size limit; trim findings or attachments. |
| `checkpoint_integrity_failure` | The stored checkpoint does not match its hash; restore from backup. |
| `checkpoint_reconciliation_required` | The checkpoint predates a restore; reconcile its operations first. |
| `checkpoint_revision_conflict` | Someone saved a newer checkpoint; resume, merge, and save with the current expected_revision. |
| `checkpoint_unavailable` | No usable checkpoint for this task; save one or check the task identity. |
| `checkpoint_writer_audience_required` | The writer must be among the checkpoint's readers. |
| `checkpoint_writer_denied` | Only the stream's writer can save to it; reassign the writer explicitly. |

## clock

| Code | What to do |
| --- | --- |
| `clock_untrusted` | The system clock moved backwards or disagrees with monotonic time; fix the clock and retry. |

## clone

| Code | What to do |
| --- | --- |
| `clone_already_registered` | This clone alias is already registered for the repository. |

## concurrency

| Code | What to do |
| --- | --- |
| `concurrency_limit` | Four job leases are already live; finish or let them expire before claiming more. |

## constraint

| Code | What to do |
| --- | --- |
| `constraint_conflict` | Mandatory constraints conflict; a reviewer must resolve them. |
| `constraint_resolution_incomplete` | Not all mandatory constraints could be resolved; the result is blocked, not truncated. |

## context

| Code | What to do |
| --- | --- |
| `context_changed` | Identity, project or history differs from what the client bound; reconcile and create a fresh client. |
| `context_rebuild_required` | Retained context is no longer authorized; rebuild from current permissions. |

## control

| Code | What to do |
| --- | --- |
| `control_busy` | Another process holds the control lock; wait or stop it. |
| `control_must_be_independent` | Store and control directories may not contain each other. |
| `control_required` | Pass `--control PATH` or use `--profile NAME`. |

## counter

| Code | What to do |
| --- | --- |
| `counter_exhausted` | The commit counter reached its maximum; restore into a fresh store. |

## credential

| Code | What to do |
| --- | --- |
| `credential_audience_mismatch` | The credential was issued for a different service audience; use one issued by this deployment. |
| `credential_output_exists` | The output file already exists; choose a new private file name. |
| `credential_output_required` | A grant returns a secret; pass `--credential-output NEW_FILE` to save it privately. |
| `credential_required` | Send a credential: Bearer header on HTTP, `credential` on the socket, or `--credential` on the CLI. |
| `credential_required_path` | Pass `--credential FILE` or use `--profile NAME`. |

## csrf

| Code | What to do |
| --- | --- |
| `csrf_rejected` | The request's CSRF token does not match the session; reload the dashboard and retry. |

## cursor

| Code | What to do |
| --- | --- |
| `cursor_context_changed` | The paging cursor belongs to another session or history; start over. |

## dashboard

| Code | What to do |
| --- | --- |
| `dashboard_action_unsupported` | The dashboard command is not one of the supported browser actions. |
| `dashboard_disabled` | Start `serve` with `--dashboard-port` to enable the dashboard. |
| `dashboard_origin_required` | Configure the exact Origin the shared dashboard accepts with `--origin`. |
| `dashboard_unavailable` | The dashboard failed internally; find the request id in the daemon log. |

## database

| Code | What to do |
| --- | --- |
| `database_durability_required` | PostgreSQL must run with fsync, synchronous_commit and full_page_writes on. |
| `database_tls_required` | Remote DSNs must use sslmode=verify-full. |
| `database_transaction_required` | This read needs an open store transaction. |

## delegation

| Code | What to do |
| --- | --- |
| `delegation_depth_exceeded` | Delegated credentials cannot delegate again; request a grant from an administrator. |

## delta

| Code | What to do |
| --- | --- |
| `delta_base_mismatch` | The delta base is not the retained base; fetch a full packet. |
| `delta_binding_mismatch` | The delta's binding differs from the client's. |
| `delta_invalid` | The delta is malformed. |
| `delta_limit` | The delta exceeds its size or change-count limit; fetch a full packet. |
| `delta_reconstruction_failed` | Applying the delta did not reproduce the target hash; fetch a full packet. |
| `delta_sequence_mismatch` | Deltas must apply in order; fetch the missing ones or a full packet. |
| `delta_target_mismatch` | The reconstructed packet does not match the target. |

## denied

| Code | What to do |
| --- | --- |
| `denied` | The credential lacks the capability or project for this action; use a credential with the right grant or ask the owner for one. |

## dependency

| Code | What to do |
| --- | --- |
| `dependency_coverage_incomplete` | Not every declared dependency could be bound; supply the missing bindings. |
| `dependency_policy_conflict` | Declared dependency policies conflict; resolve them. |
| `dependency_policy_missing` | A declared dependency has no policy. |
| `dependency_policy_stale` | The dependency policy predates the current snapshot. |
| `dependency_policy_unsafe` | The dependency policy would widen access; reject or review it. |
| `dependency_unsupported` | This dependency kind is not supported. |

## diagnostic

| Code | What to do |
| --- | --- |
| `diagnostic_limit` | Retrieval diagnostics exceed 64 KiB; reduce the request's breadth. |

## duplicate

| Code | What to do |
| --- | --- |
| `duplicate_criterion` | Acceptance criteria ids must be unique within a checkpoint. |

## egress

| Code | What to do |
| --- | --- |
| `egress_audit_missing` | No egress audit record matches that permit for this principal; report only permits you consumed. |
| `egress_audit_reported` | That egress audit record already has a final outcome; a report is written once. |
| `egress_classification_denied` | A message cites content above the provider's classification ceiling; rebuild the conversation without it or raise the ceiling in the policy. |
| `egress_policy_invalid` | The egress policy file is malformed: check revision, provider names, hosts, models, classification ceilings and that plain HTTP is loopback-only. |
| `egress_provider_denied` | The provider or model is not in the daemon's egress policy; add it to the policy file and restart `serve --egress-policy`. |

## embedding

| Code | What to do |
| --- | --- |
| `embedding_batch_invalid` | The batch's items do not match the leased job. |
| `embedding_disabled` | Embeddings are not configured for this project; configure a partition first. |
| `embedding_dispatch_expired` | The embedding lease expired; claim again. |
| `embedding_fingerprint_mismatch` | The model fingerprint differs from the partition's. |
| `embedding_generation_conflict` | The partition moved to a new generation; claim again. |
| `embedding_input_limit` | Embedding inputs are at most 16 KiB each. |
| `embedding_model_checksum_mismatch` | The downloaded model does not match its manifest checksum. |
| `embedding_model_invalid` | The model manifest is malformed. |
| `embedding_model_not_installed` | Install the model with `python -m memoryking.model_install`. |
| `embedding_model_unsupported` | Only the pinned model manifest is supported. |
| `embedding_partition_limit` | Too many embedding partitions. |
| `embedding_partition_unavailable` | No partition for this project and fingerprint. |
| `embedding_provider_failure` | The embedding runtime failed; see the worker log. |
| `embedding_provider_unsupported` | Only the local ONNX runtime is supported. |
| `embedding_query_mismatch` | The query vector's dimension or fingerprint does not match the partition. |
| `embedding_release_invalid` | The embedding release is malformed. |
| `embedding_runtime_mismatch` | The installed runtime differs from the pinned version. |
| `embedding_runtime_unavailable` | Install the embeddings lock to run the local model. |
| `embedding_source_unavailable` | The observation to embed is missing or purged. |
| `embedding_vector_invalid` | Vectors must have the model's dimension and finite values. |

## essential

| Code | What to do |
| --- | --- |
| `essential_context_unavailable` | A mandatory constraint is not readable by this principal; ask its owner for access. |

## evidence

| Code | What to do |
| --- | --- |
| `evidence_changed` | The evidence bytes differ from the cited hash; re-cite the current content. |
| `evidence_unavailable` | Cited evidence is missing, purged, or not readable by this principal. |

## exclusive

| Code | What to do |
| --- | --- |
| `exclusive_gate_required` | This operation needs the exclusive tenant gate, not a release-only store. |

## experience

| Code | What to do |
| --- | --- |
| `experience_actor_audience_required` | The actor must be among the record's readers. |
| `experience_capacity` | The tenant reached its experience record limit; see the limits table. |
| `experience_integrity_failure` | The record does not match its hash. |
| `experience_record_limit` | The record exceeds the per-record size limit. |
| `experience_record_unavailable` | The record is missing, purged, or not visible. |
| `experience_response_limit` | The listing exceeds the response limit; narrow the request. |
| `experience_revision_conflict` | A newer revision exists; read it and retry. |

## explicit

| Code | What to do |
| --- | --- |
| `explicit_shared_hosts_required` | Start the shared service with at least one `--host` value. |

## extension

| Code | What to do |
| --- | --- |
| `extension_administrator_required` | Enabling pgvector needs a superuser; run `enable-vector` as one. |

## family

| Code | What to do |
| --- | --- |
| `family_kind_mismatch` | A revision must keep its family's kind (guide, constraint, procedure). |
| `family_version_conflict` | expected_version is behind the family; read the current version and retry. |

## feedback

| Code | What to do |
| --- | --- |
| `feedback_reference_not_retrieved` | Feedback must reference an item delivered in a briefing to this principal. |
| `feedback_scope_mismatch` | Feedback must match the task and run of the referenced briefing. |
| `feedback_use_required` | State whether the memory was used and how useful it was. |

## git

| Code | What to do |
| --- | --- |
| `git_unavailable` | Git is not on PATH; install it or adjust PATH for the daemon. |

## handoff

| Code | What to do |
| --- | --- |
| `handoff_ack_invalid` | Acknowledge with the view stamp you received. |
| `handoff_capacity` | Too many handoffs are outstanding for this stream; acknowledge or cancel some. |
| `handoff_expired` | The handoff's ttl passed; prepare a new one. |
| `handoff_superseded` | A newer handoff replaced this one; use the latest. |
| `handoff_unavailable` | The handoff is missing, purged, or not addressed to this principal. |
| `handoff_view_capacity` | Too many handoff views are retained; let older ones expire. |
| `handoff_view_stale` | Policy or checkpoint changed since the view; preview again. |

## harness

| Code | What to do |
| --- | --- |
| `harness_already_exists` | A harness journal already exists at that path. |
| `harness_binding_changed` | The store binding changed under the harness; start again from a checkpoint. |
| `harness_capture_degraded` | Some captures failed; inspect the harness journal. |
| `harness_checkpoint_changed` | The checkpoint changed under the harness; resume first. |
| `harness_closed` | The harness is closed; start a new one. |
| `harness_configuration_invalid` | The harness configuration is malformed. |
| `harness_evidence_capacity` | The harness holds too much evidence; checkpoint and reset. |
| `harness_journal_diverged` | The harness journal differs from the store; recover from the checkpoint. |
| `harness_journal_integrity` | The harness journal is corrupt. |
| `harness_journal_invalid` | The harness journal is malformed. |
| `harness_journal_limit` | The harness journal is full; checkpoint and reset. |
| `harness_provenance_removed` | Provenance for retained content was removed; rebuild context. |
| `harness_recovery_incomplete` | Recovery did not finish; retry with the same checkpoint. |
| `harness_state_required` | Start the harness with an explicit state file. |

## history

| Code | What to do |
| --- | --- |
| `history_changed` | The store's history id changed (restore or reset); refresh with memory_status and rebuild context before retrying. |

## host

| Code | What to do |
| --- | --- |
| `host_authorization_expired` | The host's authorization for this content expired; authorize again. |
| `host_authorization_required` | Host-supplied content needs a host authorization with a source hash. |
| `host_rejected` | The Host header is not one the service was started with; pass it with `--host` or use the configured name. |

## idempotency

| Code | What to do |
| --- | --- |
| `idempotency_conflict` | The same key was used with different bytes; investigate rather than inventing a new key. |
| `idempotency_result_expired` | The replay body for this key expired; the identity stays consumed, so reconcile instead of repeating. |

## impact

| Code | What to do |
| --- | --- |
| `impact_downgrade_requires_new_revision` | Lowering impact needs a new revision, not an edit. |

## incarnation

| Code | What to do |
| --- | --- |
| `incarnation_required` | Send the current history id from memory_status, or let the bridge fill it. |

## index

| Code | What to do |
| --- | --- |
| `index_not_ready` | The dense index has not caught up; wait or use lexical retrieval. |

## inference

| Code | What to do |
| --- | --- |
| `inference_capacity` | Too many inference permits are outstanding; consume or let them expire. |
| `inference_context_changed` | The conversation's binding changed; rebuild context before dispatch. |
| `inference_dependency_unavailable` | A cited receipt is no longer available; rebuild context. |
| `inference_hash_mismatch` | Message bytes differ from their manifest; rebuild from the retained packets. |
| `inference_manifest_incomplete` | Every message and handle needs a manifest entry. |
| `inference_permit_consumed` | The permit was already used; authorize again. |
| `inference_permit_expired` | The permit expired; authorize again. |
| `inference_permit_mismatch` | The permit was issued for different bytes or binding. |
| `inference_provenance_unknown` | Each message needs a memory artifact or host authorization as provenance. |
| `inference_request_limit` | The conversation is full (128 messages, so 127 before a dispatch), the request exceeds 96 KiB, or the artifact bounds are exceeded. |
| `inference_result_quarantined` | The provider result was quarantined; do not reuse it. |

## inherited

| Code | What to do |
| --- | --- |
| `inherited_owner_forbidden` | A forked process may not reuse its parent's store ownership. |

## initialization

| Code | What to do |
| --- | --- |
| `initialization_requires_new_directories` | Initialize into directories that do not exist yet. |

## inspection

| Code | What to do |
| --- | --- |
| `inspection_response_limit` | The inspection result is too large; narrow the window. |

## internal

| Code | What to do |
| --- | --- |
| `internal_error` | The service failed unexpectedly; find the request id in the daemon log. |

## invalid

| Code | What to do |
| --- | --- |
| `invalid_binding` | Binding values are strings of at most 256 bytes over known fields. |
| `invalid_capabilities` | Capabilities must be a non-empty list of known names without admin; see the deployment guide. |
| `invalid_checkpoint` | The checkpoint body does not match the contract; start from the fixture shape. |
| `invalid_constraint` | Constraints need a rule key, value and effect. |
| `invalid_content_type` | Send `Content-Type: application/json`. |
| `invalid_dashboard_configuration` | The dashboard port must be between 0 and 65535. |
| `invalid_delegation` | A delegation must narrow an existing grant with a bounded ttl_ms; check the caller's own capabilities. |
| `invalid_duration` | Use a number followed by m, h or d, for example `45m`, `12h` or `30d`. |
| `invalid_evidence_span` | An evidence span needs an observation, byte range and content hash that match the text. |
| `invalid_identifier` | Identifiers are 1 to 96 ASCII letters, digits, `_` or `-`. |
| `invalid_interval` | The interval's start must precede its end. |
| `invalid_operation_id` | Operation ids are identifiers of at most 96 characters. |
| `invalid_pool_configuration` | The connection pool size must be a positive integer. |
| `invalid_port` | Choose a port between 1 and 65535. |
| `invalid_query` | The query must be a non-empty string within 2,048 characters. |
| `invalid_range` | The requested range is outside the allowed bounds; check the limits table. |
| `invalid_recorded_position` | The recorded position is not one this store can recognize; take it from a recent receipt. |
| `invalid_request` | The request body does not match its contract; validate against the schema in the reference guide. |
| `invalid_response` | The service returned something unparseable; check the daemon log for a request id. |
| `invalid_scope` | Project, task and run must be consistent identifiers for this stream. |
| `invalid_secret_file` | The secret file is missing, empty, not private, or not valid text; recreate it with mode 0600. |
| `invalid_shared_url` | Use `https://host[:port]` with no path, query, fragment or embedded credentials. |
| `invalid_token` | The token is malformed, signed by another store, or too long; obtain a fresh credential from this store. |
| `invalid_tool_arguments` | The tool arguments do not match the catalog schema; compare with the tool's description. |
| `invalid_transition` | That lifecycle transition is not allowed from the current state. |
| `invalid_usage` | Usage figures must be consistent non-negative integers. |

## journal

| Code | What to do |
| --- | --- |
| `journal_invalid` | The policy journal's chain is broken; restore using independent control evidence. |
| `journal_record_limit` | The policy action exceeds 256 KiB and was refused before writing; reduce its payload. |
| `journal_replay_required` | Unapplied journal records exist; reopen the store to replay them. |
| `journal_rollback` | The journal is shorter than the anchor records; the control state was rolled back. |
| `journal_tenant_missing` | A journal record names a tenant the database lacks; restore the matching database. |

## knowledge

| Code | What to do |
| --- | --- |
| `knowledge_reconciliation_required` | Knowledge state changed under a restore; reconcile before reviewing. |

## legacy

| Code | What to do |
| --- | --- |
| `legacy_batch_limit` | Apply at most 500 rows per batch. |
| `legacy_batch_start` | The batch start does not follow the last applied row. |
| `legacy_context_changed` | The target store changed during import; reconcile first. |
| `legacy_incomplete` | The import is incomplete; apply the remaining batches. |
| `legacy_integrity_failed` | The snapshot's hashes do not match the plan. |
| `legacy_journal_corrupt` | The import journal is corrupt; start from a new snapshot directory. |
| `legacy_journal_plan_mismatch` | The journal belongs to a different plan. |
| `legacy_lesson_proposal_limit` | Too many lesson proposals in one batch. |
| `legacy_operator_required` | Import needs an operator credential. |
| `legacy_plan_changed` | The plan changed since the journal started. |
| `legacy_plan_exists_use_new_snapshot_directory` | A plan already exists; use a new snapshot directory. |
| `legacy_reconciliation_required` | Reconcile the import before applying more. |
| `legacy_record_too_large` | A legacy row exceeds 60 KiB. |
| `legacy_reference_missing` | A legacy row references a missing record. |
| `legacy_row_limit` | The source exceeds 10,000 rows. |
| `legacy_shared_tenant_required` | Shared import needs `--tenant`. |
| `legacy_snapshot_destination_or_source_invalid` | The snapshot source or destination path is invalid. |
| `legacy_snapshot_hash_mismatch` | The snapshot file's hash changed since inspection. |
| `legacy_snapshot_invalid` | The snapshot is not a supported SQLite profile. |
| `legacy_snapshot_not_detached` | Snapshot a detached backup, not a live database with a WAL. |
| `legacy_source_changed` | The legacy source changed since planning. |
| `legacy_sqlite_inspection_failed` | The snapshot could not be read as SQLite. |
| `legacy_table_limit` | The source has more than 128 tables. |
| `legacy_target_mismatch` | The plan targets a different store. |
| `legacy_total_limit` | The source exceeds 64 MiB of canonical row data. |
| `legacy_value_unsupported` | A legacy value type is not supported. |

## local

| Code | What to do |
| --- | --- |
| `local_io_or_configuration_error` | A file or directory could not be read or written; check the path, its permissions, and that the daemon is running. |

## loopback

| Code | What to do |
| --- | --- |
| `loopback_development_only` | `--loopback-http` works only on a loopback address without TLS flags. |

## maintenance

| Code | What to do |
| --- | --- |
| `maintenance_operation_mismatch` | Pass the operation id recorded in the anchor's maintenance_operation. |

## mcp

| Code | What to do |
| --- | --- |
| `mcp_protocol_unsupported` | The host asked for an MCP protocol version this bridge does not speak. |

## migration

| Code | What to do |
| --- | --- |
| `migration_role_required` | Run migrations with the migration database role. |

## mixed

| Code | What to do |
| --- | --- |
| `mixed_authentication` | Send either a session cookie or a bearer credential, never both. |

## model

| Code | What to do |
| --- | --- |
| `model_destination_exists` | The model destination already exists; choose a new directory. |
| `model_download_checksum_mismatch` | A downloaded file's checksum differs from the manifest; retry or check the mirror. |
| `model_download_limit` | A model file exceeds its size limit. |
| `model_download_redirect_denied` | The model download redirected; only direct downloads are allowed. |

## new

| Code | What to do |
| --- | --- |
| `new_bootstrap_output_required` | Pass a new, non-existent `--bootstrap-output` file for the one-use code. |
| `new_credential_output_required` | Pass a new, non-existent `--credential-output` file for the issued secret. |

## no

| Code | What to do |
| --- | --- |
| `no_relevant_memory` | Nothing matched the query under the current permissions; an empty result can be correct. |

## operation

| Code | What to do |
| --- | --- |
| `operation_already_recorded` | This operation already exists with the same identity. |
| `operation_capacity` | 4,096 operations are open in the tenant; resolve some to succeeded or failed. |
| `operation_identity_consumed` | The operation id was already recorded; use the recorded one or a new id. |
| `operation_limit` | Too many operations in one request. |
| `operation_reconciliation_invalid` | A reconciled transition must end in succeeded or failed with evidence. |
| `operation_reconciliation_required` | The operation's outcome is unknown after a restart; reconcile it before repeating. |
| `operation_result_evidence_required` | Succeeded and failed outcomes must cite evidence. |
| `operation_result_invalid` | The result hash or evidence is malformed. |
| `operation_revision_conflict` | The operation moved on; read its current revision and retry. |
| `operation_scope_capacity` | 32 operations are open in this task stream; resolve some before recording more. |
| `operation_transition_invalid` | That state change is not allowed from the current state. |
| `operation_unavailable` | No such operation in this stream for this principal. |

## optional

| Code | What to do |
| --- | --- |
| `optional_claim_conflict` | Two optional claims disagree; resolve the conflict or review one. |
| `optional_claim_sources_incomplete` | The optional claim cites sources that are not all available. |
| `optional_claim_unavailable` | The optional claim is not visible to this principal. |
| `optional_source_limit` | Too many optional sources; reduce the set. |
| `optional_source_not_text` | An optional source must be text. |
| `optional_source_unavailable` | An optional source is missing or unreadable. |

## origin

| Code | What to do |
| --- | --- |
| `origin_rejected` | The Origin header does not match the dashboard's origin; open the dashboard from its printed URL. |
| `origin_required` | Browser requests must carry an Origin header matching the dashboard's own origin. |

## output

| Code | What to do |
| --- | --- |
| `output_hash_mismatch` | The declared output hash does not match the content. |

## owner

| Code | What to do |
| --- | --- |
| `owner_busy_or_unsafe` | Another process owns the store, or the lock file is not private. |
| `owner_closed` | The store was closed; open it again. |

## plan

| Code | What to do |
| --- | --- |
| `plan_closure_evidence_required` | Closing a plan as done or abandoned needs a closure note and at least one lesson reference. |
| `plan_dependency_cycle` | Step prerequisites form a cycle; remove the circular depends_on link. |
| `plan_dependency_unresolved` | A step can be doing or done only when every prerequisite is done or skipped. |
| `plan_duplicate_id` | Phase and step ids must be unique within a plan. |
| `plan_reason_required` | A blocked or skipped step needs a note explaining why. |
| `plan_reference_invalid` | Every step must name an existing phase and existing prerequisite step ids. |
| `plan_reverification_required` | A completed step's definition or prerequisites changed; cite fresh evidence or reopen the step as todo. |
| `plan_step_unavailable` | The plan has no step with that id; read the current plan and use a stable step id. |
| `plan_stream_type_changed` | A task stream cannot switch between ordinary checkpoints and a plan; use a separate task id. |
| `plan_task_stream_required` | Plans live on the task stream; send run as null. |
| `plan_unavailable` | No plan on this task stream (or the revision does not exist); save one with plan_save or check the task id. |
| `plan_unresolved_steps` | A done plan needs at least one step, each done or skipped; finish or skip the rest first. |

## pool

| Code | What to do |
| --- | --- |
| `pool_reset_failed` | A pooled connection could not be reset; restart the service. |

## preflight

| Code | What to do |
| --- | --- |
| `preflight_rejected` | Only GET, HEAD and POST with a JSON content type are allowed; check the browser request. |

## primary

| Code | What to do |
| --- | --- |
| `primary_nonprivileged_runtime_required` | The runtime role must not be a superuser or bypass row security. |
| `primary_required` | Writes must go to the primary database, not a replica. |

## procedure

| Code | What to do |
| --- | --- |
| `procedure_case_ambiguous` | More than one procedure matches; add bindings to disambiguate. |
| `procedure_review_required` | Activate only reviewed procedures. |
| `procedure_support_required` | The procedure's supporting evidence is missing. |
| `procedure_unavailable` | The procedure was purged or is not visible. |
| `procedure_validation_required` | Bindings did not validate against the procedure's selector. |

## profile

| Code | What to do |
| --- | --- |
| `profile_config_invalid` | The profiles file must hold `[profiles.NAME]` tables with absolute store and control paths. |
| `profile_unknown` | No profile with that name; run `memoryking profile list` or `profile add`. |

## project

| Code | What to do |
| --- | --- |
| `project_required` | Pass `--project NAME` or set `MEMORYKING_PROJECT`. |

## provider

| Code | What to do |
| --- | --- |
| `provider_context_limit` | The provider's stored context exceeds its limit. |
| `provider_credential_missing` | The adapter's key or token file is missing or unreadable; check key_file, token_file or token_command in the policy. |
| `provider_handle_invalid` | The provider handle is malformed. |
| `provider_handle_unavailable` | The provider handle's file is missing or unreadable. |
| `provider_not_supported` | Only the local recorder and adapters built from the daemon's current egress policy are accepted; load the policy with `serve --egress-policy` and build the adapter from it. |
| `provider_request_invalid` | The provider request bytes are not the canonical frozen request. |
| `provider_response_invalid` | The provider's reply was not the expected shape or exceeded the size bound; nothing was admitted to the conversation. |
| `provider_unavailable` | The provider did not answer within the timeout or returned an error; the dispatch was not retried. |

## query

| Code | What to do |
| --- | --- |
| `query_term_limit` | Queries are searched on their first 32 distinct terms. |

## queue

| Code | What to do |
| --- | --- |
| `queue_full` | The tenant has 1,000 unfinished jobs; the daemon drains them between requests, so retry shortly or run a worker. |

## ranking

| Code | What to do |
| --- | --- |
| `ranking_case_invalid` | Each ranking case needs a brief request and disjoint relevant and forbidden sets. |
| `ranking_duplicate_vote` | One vote per actor per item. |
| `ranking_evaluation_required` | Evaluate the configuration and pass its gates before activating. |
| `ranking_feedback_superseded` | Newer feedback exists for this item; vote on the current revision. |
| `ranking_limit` | The ranking request exceeds its bound; reduce cases or votes. |
| `ranking_not_previously_active` | Rollback needs a previously active configuration. |
| `ranking_policy_unsupported` | Only the documented ranking policy version is supported. |
| `ranking_shadow_stale` | The project changed since the evaluation; evaluate again. |
| `ranking_version_conflict` | expected_version is behind the ranking state; read it and retry. |

## readers

| Code | What to do |
| --- | --- |
| `readers_only_at_start` | Readers are fixed when a stream starts; start a new stream to change them. |

## recovery

| Code | What to do |
| --- | --- |
| `recovery_admin_missing` | No tenant-wide admin grant exists to receive the recovered credential. |
| `recovery_checkpoint_invalid` | The supplied journal head does not match the journal. |
| `recovery_required` | The store is fenced or inconsistent; stop and follow the recovery guide. |

## redirect

| Code | What to do |
| --- | --- |
| `redirect_forbidden` | The shared service URL redirected; use the final HTTPS URL directly. |

## release

| Code | What to do |
| --- | --- |
| `release_expired` | More than five seconds passed between authorization and delivery; request again. |

## replacement

| Code | What to do |
| --- | --- |
| `replacement_out_of_range` | A partial replacement must select within the predecessor's selector. |

## replay

| Code | What to do |
| --- | --- |
| `replay_denied` | The principal may no longer read a source of the artifact. |
| `replay_expired` | The artifact expired or was evicted after 30 days or at capacity; request a fresh briefing. |
| `replay_integrity_failure` | The retained bytes do not match their hash; restore from backup. |
| `replay_missing` | No artifact with that receipt for this principal and project. |
| `replay_purged` | A source of the artifact was purged; request a fresh briefing. |

## repository

| Code | What to do |
| --- | --- |
| `repository_audience_conflict` | Readers differ from the registered repository audience. |
| `repository_audience_invalid` | Repository readers must be identifiers or null. |
| `repository_capture_configuration_missing` | Register a capture configuration before capturing. |
| `repository_capture_limit` | The snapshot exceeds 2,048 files, 16 MiB or 12 seconds; narrow the capture. |
| `repository_changed_during_capture` | Files changed while Git was running; capture again. |
| `repository_clone_changed` | The clone directory was replaced; register it again. |
| `repository_context_mismatch` | The binding's repository context does not match the snapshot. |
| `repository_data_unavailable` | Repository data for this snapshot is missing. |
| `repository_generation_conflict` | A newer snapshot generation exists; capture again. |
| `repository_identity_invalid` | The repository identity does not match its registration. |
| `repository_path_invalid` | The clone path must be an absolute, existing directory and not a symlink. |
| `repository_paths_unsupported` | Paths outside the clone or with unsupported characters cannot be captured. |
| `repository_projection_limit` | The repository projection exceeds 4 MiB. |
| `repository_root_required` | The path must be the repository's top-level directory. |
| `repository_snapshot_changed` | The snapshot differs from the one the claim was validated against; revalidate. |

## request

| Code | What to do |
| --- | --- |
| `request_key_required` | Mutations need a stable `key` so retries replay instead of duplicating. |
| `request_too_large` | The request exceeds 256 KiB; reduce the payload. |

## response

| Code | What to do |
| --- | --- |
| `response_limit` | The plan view exceeds the dashboard response limit; open one plan at a time or page with the cursor. |

## retention

| Code | What to do |
| --- | --- |
| `retention_capacity` | A single artifact exceeds the 128 MiB retention cap; reduce the output budget. |
| `retention_preview_stale` | The purge preview no longer matches the store; request a new preview. |

## revalidation

| Code | What to do |
| --- | --- |
| `revalidation_limit` | Too many claims to revalidate in one request; narrow the set. |

## route

| Code | What to do |
| --- | --- |
| `route_unavailable` | No such dashboard route; check the reference guide's endpoint list. |

## runtime

| Code | What to do |
| --- | --- |
| `runtime_table_owner_forbidden` | The runtime role must not own tables; use the migration role for schema work. |

## schema

| Code | What to do |
| --- | --- |
| `schema_unsupported` | The database schema version is newer or older than this release supports; migrate or upgrade. |

## scope

| Code | What to do |
| --- | --- |
| `scope_mismatch` | The request names a different project than the client is bound to; create a client for that project. |

## selector

| Code | What to do |
| --- | --- |
| `selector_empty` | A selector needs at least one clause. |
| `selector_limit_exceeded` | Selectors allow 16 clauses, 8 atoms per clause and 16 KiB in all. |
| `selector_unsupported` | Only selector_version 1 with any_of/all_of clauses is supported. |

## service

| Code | What to do |
| --- | --- |
| `service_busy` | The service is at its concurrency bound; back off briefly and retry. |
| `service_unavailable` | The service failed internally; find the request id in the daemon log. |

## session

| Code | What to do |
| --- | --- |
| `session_changed` | Permissions or history changed since sign-in and the view was cleared; sign in again. |
| `session_expired` | The dashboard session passed its 15-minute limit; sign in again with a new one-use code. |
| `session_limit` | Sixteen dashboard sessions are already open; sign out of one or wait for expiry. |
| `session_required` | Sign in to the dashboard first; the request carried no session cookie. |

## shared

| Code | What to do |
| --- | --- |
| `shared_backup_command_required` | Use the shared backup command for PostgreSQL tenants. |
| `shared_migration_command_required` | Use the shared migrate command for PostgreSQL. |
| `shared_restore_command_required` | Use the shared restore command for PostgreSQL tenants. |
| `shared_tls_required` | The shared service requires HTTPS; pass `--tls-cert` and `--tls-key`, or `--loopback-http` for local testing only. |

## snapshot

| Code | What to do |
| --- | --- |
| `snapshot_unchecked` | The snapshot has not been validated; run the validation job first. |

## socket

| Code | What to do |
| --- | --- |
| `socket_path_too_long` | The socket path must fit in 100 bytes; use a shorter store path. |
| `socket_required` | Pass `--socket PATH` or use `--profile NAME`. |

## source

| Code | What to do |
| --- | --- |
| `source_conflict` | The same source event was captured with different text; keep source identities stable. |
| `source_manifest_mismatch` | The source manifest hash differs from the retained one. |
| `source_retired` | This source event was purged and cannot be recaptured. |

## stale

| Code | What to do |
| --- | --- |
| `stale_lease` | The job lease expired or was fenced; claim again and discard stale output. |
| `stale_source` | The job's source changed or was purged; claim again. |

## store

| Code | What to do |
| --- | --- |
| `store_required` | Pass `--store PATH` or use `--profile NAME`. |

## summary

| Code | What to do |
| --- | --- |
| `summary_source_invalid` | A provider-written summary must be the exact reply recorded in a delivered egress audit for this principal and project; pass the permit and the unmodified text. |
| `summary_unavailable` | No session summary is visible for that checkpoint; derive one with `summarize` or check the readers. |

## temporal

| Code | What to do |
| --- | --- |
| `temporal_requirement_failed` | A claim's validity window does not cover the requested time. |

## tokenizer

| Code | What to do |
| --- | --- |
| `tokenizer_integrity_failure` | The bundled vocabulary file is corrupt; reinstall the package. |
| `tokenizer_unavailable` | Install the tokenizers lock for exact counts, or request `utf8-byte-estimate-v1`. |
| `tokenizer_version_unsupported` | The installed tiktoken version differs from the pinned one; install the tokenizers lock. |

## tool

| Code | What to do |
| --- | --- |
| `tool_input_limit` | Tool input exceeds the frame limit; shorten it. |
| `tool_outside_declared_scope` | The tool touched a path outside its declared root. |
| `tool_path_invalid` | Tool paths must be relative and normalized. |
| `tool_readback_mismatch` | The file read back differs from what was written. |
| `tool_target_exists` | The target file already exists. |

## transition

| Code | What to do |
| --- | --- |
| `transition_invalid` | That state change is not allowed. |

## transport

| Code | What to do |
| --- | --- |
| `transport_unavailable` | The daemon socket is unreachable; is `memoryking serve` running? |

## unavailable

| Code | What to do |
| --- | --- |
| `unavailable` | The record is missing, purged, or not visible to this principal. |

## unknown

| Code | What to do |
| --- | --- |
| `unknown_tool` | No tool with that name; call tools/list for the catalog. |

## unrelated

| Code | What to do |
| --- | --- |
| `unrelated_replacement` | Only a proposed successor in the same family can replace a claim. |

## unsafe

| Code | What to do |
| --- | --- |
| `unsafe_permissions` | Directories must be mode 0700 and files 0600, owned by this user. |
| `unsafe_socket` | The socket path exists and is not a socket owned by this user. |

## unsupported

| Code | What to do |
| --- | --- |
| `unsupported_database_statement` | The statement cannot be translated for PostgreSQL. |

## usage

| Code | What to do |
| --- | --- |
| `usage_attempt_conflict` | The usage attempt id was reused with different provenance. |
| `usage_attempt_duplicate` | The usage attempt was already recorded. |
| `usage_cohort_invalid` | The usage cohort identifier is invalid. |
| `usage_cohort_limit` | The usage cohort is full; start a new cohort. |
| `usage_record_invalid` | The usage record is malformed. |
| `usage_retention_limit` | 100,000 usage attempts are retained; expired rows are pruned automatically, otherwise wait for retention to pass. |

## vector

| Code | What to do |
| --- | --- |
| `vector_extension_mismatch` | The pgvector extension version differs from the pinned one. |

## verification

| Code | What to do |
| --- | --- |
| `verification_evidence_required` | A passed verification must cite evidence. |
| `verification_expired` | The verification's validity window ended; verify again. |

## waiver

| Code | What to do |
| --- | --- |
| `waiver_decision_required` | A waived criterion must record who decided and why. |

[Back to the reference](reference.md#troubleshooting)
