"""Canonical Docker runtime allowlist contract for default-deny build context."""

from __future__ import annotations

import ast
from pathlib import Path

EXPECTED_DOCKER_ALLOW_RULES: tuple[str, ...] = (
    "!requirements-lock.txt",
    "!alembic.ini",
    "!alembic/",
    "!alembic/env.py",
    "!alembic/script.py.mako",
    "!alembic/versions/",
    "!alembic/versions/.gitkeep",
    "!alembic/versions/20260727_01a_foundation.py",
    "!alembic/versions/20260727_01b_ingress.py",
    "!alembic/versions/20260727_01c_reply_outbound.py",
    "!alembic/versions/20260728_09_amocrm_mirror.py",
    "!alembic/versions/20260728_10_attempt_exhaustion.py",
    "!alembic/versions/20260729_11_handoff_schema.py",
    "!alembic/versions/20260729_12_worker_runtime.py",
    "!alembic/versions/20260729_13_handoff_quarantine.py",
    "!alembic/versions/20260731_14_ephemeral_pii_values.py",
    "!alembic/versions/20260801_15_attachment_spool.py",
    "!alembic/versions/20260801_16_spool_leases.py",
    "!alembic/versions/20260807_17_master_bindings.py",
    "!alembic/versions/20260808_18_master_commands.py",
    "!alembic/versions/20260809_19_identity_resolution.py",
    "!alembic/versions/20260812_20_amocrm_mgr_ingress.py",
    "!alembic/versions/20260812_21_amocrm_chat_proj.py",
    "!alembic/versions/20260812_22_amo_chat_integ_cid.py",
    "!alembic/versions/20260813_23_amocrm_crm_oauth.py",
    "!alembic/versions/20260813_24_amo_entity_links.py",
    "!alembic/versions/20260813_25_amo_deal_reserve.py",
    "!alembic/versions/20260816_26_identity_glue.py",
    "!alembic/versions/20260818_27_amocrm_deal_kind.py",
    "!alembic/versions/20260820_28_self_booking_create.py",
    "!alembic/versions/20260820_29_active_offer.py",
    "!alembic/versions/20260820_30_pii_admission.py",
    "!alembic/versions/20260821_31_sbc_exec_loop.py",
    "!alembic/versions/20260825_32_teya_req_orch.py",
    "!app/",
    "!app/__init__.py",
    "!app/channels/",
    "!app/channels/__init__.py",
    "!app/channels/vk_master_config.py",
    "!app/channels/vk_master_types.py",
    "!app/channels/vk_master_webhook.py",
    "!app/channels/vk_master_reply.py",
    "!app/channels/vk_master_http.py",
    "!app/config.py",
    "!app/core/",
    "!app/core/__init__.py",
    "!app/core/outbound_policy.py",
    "!app/core/pii_gateway.py",
    "!app/core/ephemeral_pii_types.py",
    "!app/core/ephemeral_pii_keys.py",
    "!app/core/ephemeral_pii_crypto.py",
    "!app/core/attachment_types.py",
    "!app/core/attachment_keys.py",
    "!app/core/attachment_crypto.py",
    "!app/core/attachment_mime.py",
    "!app/core/attachment_fs.py",
    "!app/core/attachment_maintenance_types.py",
    "!app/core/attachment_maintenance_heartbeat.py",
    "!app/core/booking_types.py",
    "!app/core/manager_working_hours.py",
    "!app/core/booking_dialog_policy.py",
    "!app/core/booking_eligibility_remote.py",
    "!app/core/booking_eligibility_http.py",
    "!app/core/booking_availability_remote.py",
    "!app/core/booking_availability_http.py",
    "!app/core/booking_create_remote.py",
    "!app/core/booking_create_http.py",
    "!app/core/booking_request_remote.py",
    "!app/core/booking_request_http.py",
    "!app/core/teya_request_types.py",
    "!app/core/amocrm_crm_writes_http.py",
    "!app/core/amocrm_crm_business_write_config.py",
    "!app/core/s2s_http_transport.py",
    "!app/core/s2s_http_stdlib.py",
    "!app/core/booking_eligibility_factory.py",
    "!app/core/mode_contract.py",
    "!app/core/closed_test_config.py",
    "!app/core/amocrm_chat_config.py",
    "!app/core/amocrm_chat_signature.py",
    "!app/core/amocrm_chat_egress_config.py",
    "!app/core/amocrm_chat_egress_http.py",
    "!app/core/amocrm_crm_oauth_types.py",
    "!app/core/amocrm_crm_oauth_keys.py",
    "!app/core/amocrm_crm_oauth_crypto.py",
    "!app/core/amocrm_crm_rest_config.py",
    "!app/core/amocrm_crm_rest_http.py",
    "!app/core/amocrm_crm_deal_create_config.py",
    "!app/core/amocrm_crm_leads_http.py",
    "!app/core/amocrm_crm_contacts_http.py",
    "!app/core/amocrm_identity_lookup.py",
    "!app/core/amocrm_buyer_card_discovery.py",
    "!app/core/amocrm_deal_discovery.py",
    "!app/core/amocrm_crm_buyer_card_http.py",
    "!app/core/amocrm_buyer_card_read_flow.py",
    "!app/core/amocrm_buyer_card_bind.py",
    "!app/core/amocrm_manager_ids.py",
    "!app/core/master_channel_binding.py",
    "!app/core/identity_resolution.py",
    "!app/core/identity_provider_port.py",
    "!app/core/identity_glue.py",
    "!app/core/client_ref_resolution.py",
    "!app/core/self_booking_create_types.py",
    "!app/core/self_booking_active_offer_types.py",
    "!app/core/self_booking_pii_admission_types.py",
    "!app/core/self_booking_confirm_admission_types.py",
    "!app/core/pii_admission_mac_types.py",
    "!app/core/pii_admission_mac_keys.py",
    "!app/core/pii_admission_mac.py",
    "!app/core/master_command_types.py",
    "!app/core/master_command_parser.py",
    "!app/core/master_command_remote.py",
    "!app/core/master_command_http.py",
    "!app/db/",
    "!app/db/__init__.py",
    "!app/db/base.py",
    "!app/db/clock.py",
    "!app/db/session.py",
    "!app/db/worker_lock.py",
    "!app/http_healthcheck.py",
    "!app/closed_test_router.py",
    "!app/amocrm_chat_webhook.py",
    "!app/identity_glue_ops.py",
    "!app/amocrm_buyer_card_bind_ops.py",
    "!app/integrations/",
    "!app/integrations/__init__.py",
    "!app/main.py",
    "!app/models/",
    "!app/models/__init__.py",
    "!app/models/amocrm_mirror.py",
    "!app/models/amocrm_chat_binding.py",
    "!app/models/amocrm_message_projection.py",
    "!app/models/amocrm_crm_oauth_token.py",
    "!app/models/amocrm_entity_link.py",
    "!app/models/conversation.py",
    "!app/models/conversation_ops_event.py",
    "!app/models/ephemeral_pii.py",
    "!app/models/attachment_spool.py",
    "!app/models/inbox.py",
    "!app/models/ingress.py",
    "!app/models/manager_message.py",
    "!app/models/canonical_identity.py",
    "!app/models/identity_review_case.py",
    "!app/models/master_channel_binding.py",
    "!app/models/master_command_pending.py",
    "!app/models/self_booking_create_pending.py",
    "!app/models/teya_request_pending.py",
    "!app/models/self_booking_active_offer.py",
    "!app/models/self_booking_pii_admission.py",
    "!app/models/outbox.py",
    "!app/models/reply_plan.py",
    "!app/models/worker_heartbeat.py",
    "!app/repositories/",
    "!app/repositories/__init__.py",
    "!app/repositories/amocrm_mirror.py",
    "!app/repositories/amocrm_chat_bindings.py",
    "!app/repositories/amocrm_message_projections.py",
    "!app/repositories/amocrm_crm_oauth_tokens.py",
    "!app/repositories/amocrm_entity_links.py",
    "!app/repositories/conversations.py",
    "!app/repositories/ephemeral_pii.py",
    "!app/repositories/attachment_spool.py",
    "!app/repositories/ingress.py",
    "!app/repositories/manager_messages.py",
    "!app/repositories/identity_resolution.py",
    "!app/repositories/identity_glue.py",
    "!app/repositories/master_channel_bindings.py",
    "!app/repositories/master_command_pendings.py",
    "!app/repositories/self_booking_create_pendings.py",
    "!app/repositories/teya_request_pendings.py",
    "!app/repositories/self_booking_active_offers.py",
    "!app/repositories/self_booking_pii_admissions.py",
    "!app/repositories/messages.py",
    "!app/repositories/outbound.py",
    "!app/repositories/reply_plans.py",
    "!app/repositories/worker_heartbeats.py",
    "!app/schemas/",
    "!app/schemas/__init__.py",
    "!app/schemas/inbound.py",
    "!app/schemas/ingress.py",
    "!app/schemas/manager_message.py",
    "!app/schemas/booking_input.py",
    "!app/schemas/self_booking_confirm_action.py",
    "!app/schemas/closed_test.py",
    "!app/schemas/amocrm_manager_ingress.py",
    "!app/services/",
    "!app/services/__init__.py",
    "!app/services/amocrm_adapter.py",
    "!app/services/amocrm_mirror.py",
    "!app/services/amocrm_manager_ingress.py",
    "!app/services/amocrm_chat_projection.py",
    "!app/services/amocrm_technical_deal.py",
    "!app/services/amocrm_identity_lookup.py",
    "!app/services/amocrm_buyer_card_discovery.py",
    "!app/services/amocrm_deal_discovery.py",
    "!app/services/amocrm_buyer_card_read_flow.py",
    "!app/services/amocrm_buyer_card_bind.py",
    "!app/services/amocrm_crm_mirror_adapter.py",
    "!app/services/dialog_context.py",
    "!app/services/ephemeral_pii_store.py",
    "!app/services/attachment_spool_store.py",
    "!app/services/attachment_maintenance.py",
    "!app/services/booking_eligibility_flow.py",
    "!app/services/booking_flow.py",
    "!app/services/booking_synthetic.py",
    "!app/services/outbound_reply_text.py",
    "!app/services/closed_test.py",
    "!app/services/handoff_expiry.py",
    "!app/services/inbound.py",
    "!app/services/ingress.py",
    "!app/services/manager_messages.py",
    "!app/services/identity_resolution.py",
    "!app/services/identity_glue.py",
    "!app/services/identity_glue_ops.py",
    "!app/services/master_channel_binding.py",
    "!app/services/master_command_flow.py",
    "!app/services/self_booking_create_pending.py",
    "!app/services/self_booking_create_execution.py",
    "!app/services/self_booking_active_offer.py",
    "!app/services/self_booking_pii_admission.py",
    "!app/services/self_booking_confirm_admission.py",
    "!app/services/self_booking_create_execution_worker.py",
    "!app/services/teya_request_pending.py",
    "!app/services/teya_request_crm.py",
    "!app/services/teya_request_crm_wiring.py",
    "!app/services/teya_request_contact_route.py",
    "!app/services/teya_request_orchestrator.py",
    "!app/services/teya_request_orchestrator_worker.py",
    "!app/services/vk_master_adapter.py",
    "!app/services/outbound_arbiter.py",
    "!app/services/reply_outbound.py",
    "!app/services/synthetic_outbound.py",
    "!app/services/takeover.py",
    "!app/services/worker_health.py",
    "!app/services/worker_runtime.py",
    "!app/attachment_maintenance.py",
    "!app/attachment_maintenance_healthcheck.py",
    "!app/worker.py",
    "!app/worker_healthcheck.py",
)

BANNED_BROAD_DOCKER_ALLOW_RULES: tuple[str, ...] = (
    "!app/**",
    "!alembic/**",
    "!app/**/*.py",
    "!alembic/**/*.py",
    "!app/**/*",
    "!alembic/**/*",
)

# CURSOR-27 runtime + migration paths that must be present in Docker build context.
CURSOR27_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "alembic/versions/20260807_17_master_bindings.py",
    "app/core/master_channel_binding.py",
    "app/models/master_channel_binding.py",
    "app/repositories/master_channel_bindings.py",
    "app/services/master_channel_binding.py",
)

# CURSOR-28 runtime + migration paths that must be present in Docker build context.
CURSOR28_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "alembic/versions/20260808_18_master_commands.py",
    "app/core/master_command_types.py",
    "app/core/master_command_parser.py",
    "app/core/master_command_remote.py",
    "app/core/master_command_http.py",
    "app/models/master_command_pending.py",
    "app/repositories/master_command_pendings.py",
    "app/services/master_command_flow.py",
)

# CURSOR-29 VK master adapter paths that must be present in Docker build context.
CURSOR29_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "app/channels/vk_master_config.py",
    "app/channels/vk_master_types.py",
    "app/channels/vk_master_webhook.py",
    "app/channels/vk_master_reply.py",
    "app/channels/vk_master_http.py",
    "app/services/vk_master_adapter.py",
)

# CURSOR-30 Identity Resolution paths that must be present in Docker build context.
CURSOR30_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "alembic/versions/20260809_19_identity_resolution.py",
    "app/core/identity_resolution.py",
    "app/core/identity_provider_port.py",
    "app/models/canonical_identity.py",
    "app/repositories/identity_resolution.py",
    "app/services/identity_resolution.py",
)

# IR-1 conversation↔canonical glue + production ops CLI.
IR1_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "alembic/versions/20260816_26_identity_glue.py",
    "app/core/identity_glue.py",
    "app/models/identity_review_case.py",
    "app/repositories/identity_glue.py",
    "app/services/identity_glue.py",
    "app/services/identity_glue_ops.py",
    "app/identity_glue_ops.py",
)

# IR-2 read-only amoCRM contact identity lookup.
IR2_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "app/core/amocrm_crm_contacts_http.py",
    "app/core/amocrm_identity_lookup.py",
    "app/services/amocrm_identity_lookup.py",
)

# IR-3 read-only amoCRM Buyer Card (Customer) candidate discovery.
IR3_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "app/core/amocrm_buyer_card_discovery.py",
    "app/core/amocrm_crm_buyer_card_http.py",
    "app/services/amocrm_buyer_card_discovery.py",
)

# Read-only amoCRM business Deal (Lead) discovery.
DEAL_DISCOVERY_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "alembic/versions/20260818_27_amocrm_deal_kind.py",
    "app/core/amocrm_deal_discovery.py",
    "app/services/amocrm_deal_discovery.py",
)

# IR-4 read-only Buyer Card orchestration (lookup + discovery + reconcile).
IR4_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "app/core/amocrm_buyer_card_read_flow.py",
    "app/services/amocrm_buyer_card_read_flow.py",
)

# IR-5 offline/manual Buyer Card bind (local identity write only).
IR5_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "app/core/amocrm_buyer_card_bind.py",
    "app/services/amocrm_buyer_card_bind.py",
    "app/amocrm_buyer_card_bind_ops.py",
)

# AMO-01A durable amoCRM manager ingress paths.
AMO01A_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "alembic/versions/20260812_20_amocrm_mgr_ingress.py",
    "app/core/amocrm_chat_config.py",
    "app/core/amocrm_chat_signature.py",
    "app/core/amocrm_manager_ids.py",
    "app/amocrm_chat_webhook.py",
    "app/models/amocrm_chat_binding.py",
    "app/repositories/amocrm_chat_bindings.py",
    "app/schemas/amocrm_manager_ingress.py",
    "app/services/amocrm_manager_ingress.py",
)

# AMO-01B1 Chat projection paths.
AMO01B1_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "alembic/versions/20260812_21_amocrm_chat_proj.py",
    "alembic/versions/20260812_22_amo_chat_integ_cid.py",
    "app/core/amocrm_chat_egress_config.py",
    "app/core/amocrm_chat_egress_http.py",
    "app/models/amocrm_message_projection.py",
    "app/repositories/amocrm_message_projections.py",
    "app/services/amocrm_chat_projection.py",
)

# AMO-01B2 CRM REST OAuth foundation paths.
AMO01B2_DOCKER_RUNTIME_PATHS: tuple[str, ...] = (
    "alembic/versions/20260813_23_amocrm_crm_oauth.py",
    "alembic/versions/20260813_24_amo_entity_links.py",
    "alembic/versions/20260813_25_amo_deal_reserve.py",
    "app/core/amocrm_crm_oauth_types.py",
    "app/core/amocrm_crm_oauth_keys.py",
    "app/core/amocrm_crm_oauth_crypto.py",
    "app/core/amocrm_crm_rest_config.py",
    "app/core/amocrm_crm_rest_http.py",
    "app/core/amocrm_crm_deal_create_config.py",
    "app/core/amocrm_crm_leads_http.py",
    "app/models/amocrm_crm_oauth_token.py",
    "app/models/amocrm_entity_link.py",
    "app/repositories/amocrm_crm_oauth_tokens.py",
    "app/repositories/amocrm_entity_links.py",
    "app/services/amocrm_technical_deal.py",
    "app/services/amocrm_crm_mirror_adapter.py",
)


def dockerignore_lines(repo_root: Path | None = None) -> list[str]:
    root = repo_root or Path(__file__).resolve().parents[1]
    return (root / ".dockerignore").read_text(encoding="utf-8").splitlines()


def _normalize_repo_rel_path(rel_path: str) -> str:
    return rel_path.replace("\\", "/").lstrip("./")


def _docker_pattern_matches(pattern: str, rel_path: str) -> bool:
    """Match a single .dockerignore pattern against a repo-relative path.

    Covers the patterns used by this repository's default-deny allowlist
    (exact paths, ``**``, directory trailing slash, and limited globs).
    """

    import fnmatch

    path = _normalize_repo_rel_path(rel_path)
    pat = pattern.replace("\\", "/")
    if pat == "**":
        return True
    if pat.endswith("/"):
        # Directory exception only re-includes the directory entry itself
        # (for traversal). Children stay excluded unless separately allowlisted.
        base = pat[:-1]
        return path == base
    if "**" in pat or "*" in pat or "?" in pat or "[" in pat:
        # Docker treats ``**`` as matching across path segments.
        regex_friendly = pat.replace("**/", "*").replace("**", "*")
        return fnmatch.fnmatchcase(path, regex_friendly) or fnmatch.fnmatchcase(
            path.split("/")[-1], regex_friendly
        )
    return path == pat


def is_included_in_docker_build_context(
    rel_path: str,
    lines: list[str] | None = None,
    *,
    repo_root: Path | None = None,
) -> bool:
    """Return whether ``rel_path`` would be sent in the Docker build context.

    Last matching .dockerignore rule wins (Docker semantics). Default before any
    match is included; this repo's ``**`` then excludes everything until ``!``
    allow rules re-include exact runtime paths.
    """

    dockerignore = lines if lines is not None else dockerignore_lines(repo_root)
    path = _normalize_repo_rel_path(rel_path)
    included = True
    for raw in dockerignore:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = line[1:] if negate else line
        if _docker_pattern_matches(pattern, path):
            included = negate
    return included


def assert_canonical_docker_runtime_allowlist(
    lines: list[str] | None = None,
    *,
    repo_root: Path | None = None,
) -> None:
    dockerignore = lines if lines is not None else dockerignore_lines(repo_root)
    for required in (".git", ".env", ".env.*", ".venv", "tests", "docs"):
        assert required in dockerignore
    assert "**" in dockerignore

    allow_rules = [line for line in dockerignore if line.startswith("!")]
    for banned in BANNED_BROAD_DOCKER_ALLOW_RULES:
        assert banned not in dockerignore
    assert allow_rules == list(EXPECTED_DOCKER_ALLOW_RULES)
    for rule in allow_rules:
        path = rule[1:]
        assert "*" not in path
        assert "?" not in path
        assert "[" not in path


def _module_name_to_repo_paths(module_name: str, *, repo_root: Path) -> list[Path]:
    """Map ``app.foo.bar`` to candidate filesystem paths under the repo."""

    rel = Path(*module_name.split("."))
    return [repo_root / f"{rel}.py", repo_root / rel / "__init__.py"]


def collect_app_import_graph_modules(
    entry_modules: tuple[str, ...],
    *,
    repo_root: Path | None = None,
) -> frozenset[str]:
    """Return repo-relative ``app/**/*.py`` modules reachable via ``app.*`` imports.

    Walks static Import/ImportFrom edges only (no dynamic imports). Used to
    assert Docker default-deny includes the real factory runtime closure, not
    just a hand-maintained CURSOR-28 path list.
    """

    root = repo_root or Path(__file__).resolve().parents[1]
    pending = list(entry_modules)
    seen_modules: set[str] = set()
    files: set[str] = set()

    while pending:
        mod = pending.pop()
        if mod in seen_modules:
            continue
        seen_modules.add(mod)
        if not mod.startswith("app"):
            continue
        path: Path | None = None
        for candidate in _module_name_to_repo_paths(mod, repo_root=root):
            if candidate.is_file():
                path = candidate
                break
        if path is None:
            continue
        rel = path.relative_to(root).as_posix()
        files.add(rel)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if name == "app" or name.startswith("app."):
                        pending.append(name)
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    continue
                if node.level and node.level > 0:
                    continue
                if node.module == "app" or node.module.startswith("app."):
                    pending.append(node.module)
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        pending.append(f"{node.module}.{alias.name}")
    return frozenset(files)
