from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, UniqueConstraint

from app.db.base import Base
from app.models import (
    AmoCrmMirrorJob,
    AmocrmChatBinding,
    AmocrmCrmOauthToken,
    AmocrmEntityLink,
    AmocrmMessageProjection,
    AttachmentSpoolObject,
    CanonicalIdentity,
    Conversation,
    ConversationOpsEvent,
    EphemeralPiiValue,
    ExternalIdentityLink,
    IdentityReviewCase,
    InboxMessage,
    IngressEvent,
    ManagerMessage,
    MasterChannelBinding,
    MasterCommandPending,
    OutboxMessage,
    ReplyPlan,
    SelfBookingCreatePending,
    SelfBookingActiveOffer,
    SelfBookingPiiAdmission,
    WorkerHeartbeat,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


_EXPECTED_CHECKS_01A_STABLE = {
    "ck_conversations_channel": "channel IN ('synthetic')",
    "ck_conversations_status": "status IN ('OPEN', 'HANDOFF', 'CLOSED')",
    "ck_inbox_channel": "channel IN ('synthetic')",
    "ck_inbox_direction": "direction IN ('INBOUND')",
    "ck_inbox_message_type": "message_type IN ('TEXT')",
    "ck_inbox_processing_status": (
        "processing_status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED')"
    ),
}

_EXPECTED_UNIQUES_01A = {
    "uq_conversations_channel_external_id",
    "uq_inbox_channel_external_message_id",
    "uq_outbox_source_inbox_destination",
}

_EXPECTED_CHECKS_20 = {
    "ck_ingress_channel": "channel IN ('synthetic', 'amocrm')",
    "ck_ingress_event_type": (
        "event_type IN ('SYNTHETIC_MESSAGE', 'AMOCRM_MANAGER_MESSAGE')"
    ),
    "ck_ingress_channel_event_pairing": (
        "(channel = 'synthetic' AND event_type = 'SYNTHETIC_MESSAGE') OR "
        "(channel = 'amocrm' AND event_type = 'AMOCRM_MANAGER_MESSAGE')"
    ),
    "ck_amocrm_chat_bindings_status": "status IN ('ACTIVE', 'REVOKED')",
    "ck_amocrm_chat_bindings_chat_id_nonempty": "char_length(amocrm_chat_id) >= 1",
}

_EXPECTED_UNIQUES_20 = {
    "uq_amocrm_chat_bindings_conversation_id",
    "uq_amocrm_chat_bindings_amocrm_chat_id",
}

_EXPECTED_CHECKS_21 = {
    "ck_amocrm_message_projections_source_kind": (
        "source_kind IN ('CLIENT_INBOUND', 'BOT_OUTBOUND')"
    ),
    "ck_amocrm_message_projections_status": (
        "status IN ('PENDING', 'PROCESSING', 'PROJECTED', 'SKIPPED', "
        "'FAILED', 'DEAD')"
    ),
    "ck_amocrm_message_projections_attempt_count_nonnegative": "attempt_count >= 0",
    "ck_amocrm_message_projections_max_attempts_positive": "max_attempts > 0",
    "ck_amocrm_message_projections_lease_version_nonnegative": "lease_version >= 0",
    "ck_amocrm_message_projections_integration_msgid_format": (
        "integration_msgid ~ '^[cb][0-9a-f]{32}$'"
    ),
    "ck_amocrm_message_projections_projected_has_amo_id": (
        "(status = 'PROJECTED' AND amocrm_message_id IS NOT NULL) OR "
        "(status <> 'PROJECTED')"
    ),
}

_EXPECTED_UNIQUES_21 = {
    "uq_amocrm_message_projections_source",
    "uq_amocrm_message_projections_integration_msgid",
    "uq_amocrm_message_projections_amocrm_message_id",
}

_EXPECTED_CHECKS_22 = {
    "ck_amocrm_chat_bindings_integ_cid_nonempty": (
        "integration_conversation_id IS NULL OR "
        "char_length(integration_conversation_id) >= 1"
    ),
}

_EXPECTED_CHECKS_23 = {
    "ck_amocrm_crm_oauth_tokens_scope_len": (
        "char_length(connection_scope) BETWEEN 1 AND 64"
    ),
    "ck_amocrm_crm_oauth_tokens_crypto_version": "crypto_version = 1",
    "ck_amocrm_crm_oauth_tokens_access_nonce_len": (
        "octet_length(access_nonce) = 12"
    ),
    "ck_amocrm_crm_oauth_tokens_refresh_nonce_len": (
        "octet_length(refresh_nonce) = 12"
    ),
    "ck_amocrm_crm_oauth_tokens_access_ct_len": (
        "octet_length(access_ciphertext) >= 16"
    ),
    "ck_amocrm_crm_oauth_tokens_refresh_ct_len": (
        "octet_length(refresh_ciphertext) >= 16"
    ),
    "ck_amocrm_crm_oauth_tokens_key_id": "key_id ~ '^[A-Z0-9_]{1,64}$'",
    "ck_amocrm_crm_oauth_tokens_lease_version_nonnegative": "lease_version >= 0",
}

_EXPECTED_UNIQUES_23 = {
    "uq_amocrm_crm_oauth_tokens_connection_scope",
}

_EXPECTED_CHECKS_24 = {
    "ck_amocrm_entity_links_entity_kind": (
        "entity_kind IN ('CONTACT', 'TECHNICAL_DEAL')"
    ),
}

_EXPECTED_CHECKS_25 = {
    "ck_amocrm_entity_links_status": (
        "status IN ('ACTIVE', 'REVOKED', 'RESERVED', 'RECONCILE_REQUIRED')"
    ),
    "ck_amocrm_entity_links_external_id_state": (
        "("
        "status IN ('RESERVED', 'RECONCILE_REQUIRED') "
        "AND (external_id IS NULL OR char_length(external_id) >= 1)"
        ") OR ("
        "status IN ('ACTIVE', 'REVOKED') "
        "AND external_id IS NOT NULL AND char_length(external_id) >= 1"
        ")"
    ),
    "ck_amocrm_entity_links_lease_version_nonnegative": "lease_version >= 0",
}

_EXPECTED_CHECKS_26 = {
    "ck_identity_review_cases_status": "status IN ('OPEN', 'RESOLVED')",
    "ck_identity_review_cases_reason_code": (
        "reason_code IN ('AMBIGUOUS_RESOLVE', 'CONFLICTING_CANONICAL', "
        "'CANONICAL_NOT_ACTIVE')"
    ),
}

_EXPECTED_CHECKS_01B = {
    "ck_ingress_channel": "channel IN ('synthetic')",
    "ck_ingress_event_type": "event_type IN ('SYNTHETIC_MESSAGE')",
    "ck_ingress_status": (
        "status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED', 'DEAD')"
    ),
    "ck_ingress_attempt_count_nonnegative": "attempt_count >= 0",
    "ck_ingress_lease_version_nonnegative": "lease_version >= 0",
}

_EXPECTED_UNIQUES_01B = {
    "uq_ingress_channel_external_event_id",
}

_EXPECTED_CHECKS_01C = {
    "ck_conversations_ownership": "ownership IN ('BOT', 'MANAGER')",
    "ck_conversations_context_version_nonnegative": "context_version >= 0",
    "ck_reply_plans_plan_type": "plan_type IN ('CLIENT_REPLY', 'SERVICE_SIGNAL')",
    "ck_reply_plans_status": (
        "status IN ('PENDING', 'READY', 'PROCESSING', 'DISPATCHED', "
        "'CANCELLED', 'SUPERSEDED', 'FAILED', 'DEAD')"
    ),
    "ck_outbox_destination_type": (
        "destination_type IN ('INTERNAL_DRAFT', 'SYNTHETIC_OUTBOUND')"
    ),
    "ck_outbox_delivery_status": (
        "delivery_status IN ('PENDING', 'PROCESSING', 'DELIVERED', "
        "'FAILED', 'DEAD', 'CANCELLED')"
    ),
}

_EXPECTED_UNIQUES_01C = {
    "uq_reply_plans_conversation_context_version",
    "uq_outbox_idempotency_key",
    "uq_outbox_reply_plan_destination",
}

_EXPECTED_CHECKS_09 = {
    "ck_amocrm_mirror_job_type": (
        "job_type IN ('CLIENT_MESSAGE_RECEIVED_META', "
        "'REPLY_PLAN_STATE_CHANGED', 'MANAGER_TAKEOVER', "
        "'OUTBOUND_DELIVERED_META')"
    ),
    "ck_amocrm_mirror_subject_kind": (
        "subject_kind IN ('CONVERSATION', 'INBOX_MESSAGE', 'REPLY_PLAN', "
        "'OUTBOX_MESSAGE')"
    ),
    "ck_amocrm_mirror_status": (
        "status IN ('PENDING', 'PROCESSING', 'MIRRORED', 'SKIPPED', "
        "'FAILED', 'DEAD')"
    ),
    "ck_amocrm_mirror_attempt_count_nonnegative": "attempt_count >= 0",
    "ck_amocrm_mirror_max_attempts_positive": "max_attempts > 0",
    "ck_amocrm_mirror_lease_version_nonnegative": "lease_version >= 0",
    "ck_amocrm_mirror_context_version_nonnegative": (
        "context_version IS NULL OR context_version >= 0"
    ),
}

_EXPECTED_UNIQUES_09 = {
    "uq_amocrm_mirror_key",
}

_EXPECTED_CHECKS_10 = {
    "ck_ingress_max_attempts_positive": "max_attempts > 0",
}

_EXPECTED_CHECKS_11 = {
    "ck_conversations_handoff_state": (
        "handoff_state IN ('BOT_ACTIVE', 'HUMAN_ACTIVE', 'HUMAN_PAUSE')"
    ),
    "ck_conversations_manager_epoch_nonnegative": "manager_epoch >= 0",
    "ck_conversations_current_event_seq_nonnegative": "current_event_seq >= 0",
    "ck_conversations_manager_sequence_hwm_nonnegative": (
        "manager_sequence_hwm IS NULL OR manager_sequence_hwm >= 0"
    ),
    "ck_inbox_conversation_event_seq_positive": "conversation_event_seq > 0",
    "ck_reply_plans_manager_epoch_nonnegative": "manager_epoch >= 0",
    "ck_reply_plans_event_seq_hwm_nonnegative": "event_seq_hwm >= 0",
    "ck_outbox_manager_epoch_nonnegative": "manager_epoch >= 0",
    "ck_outbox_event_seq_hwm_nonnegative": "event_seq_hwm >= 0",
    "ck_manager_messages_channel": "channel IN ('synthetic')",
    "ck_manager_messages_status": (
        "status IN ('APPLIED', 'STALE', 'QUARANTINED')"
    ),
    "ck_manager_messages_provider_sequence_nonnegative": (
        "provider_sequence IS NULL OR provider_sequence >= 0"
    ),
    "ck_manager_messages_event_seq_positive": (
        "conversation_event_seq IS NULL OR conversation_event_seq > 0"
    ),
    "ck_manager_messages_body_length": (
        "char_length(body_text) BETWEEN 1 AND 4000"
    ),
}

_EXPECTED_UNIQUES_11 = {
    "uq_inbox_conversation_event_seq",
    "uq_manager_messages_channel_external_message_id",
    "uq_manager_messages_conversation_event_seq",
}

_EXPECTED_CHECKS_12 = {
    "ck_worker_heartbeats_loop_name": (
        "loop_name IN ('ingress', 'handoff_expiry', 'reply_plan', "
        "'outbound', 'amocrm_mirror')"
    ),
    "ck_worker_heartbeats_consecutive_failures_nonnegative": (
        "consecutive_failures >= 0"
    ),
}


def test_alembic_metadata_imports() -> None:
    assert Conversation.__tablename__ == "conversations"
    assert InboxMessage.__tablename__ == "inbox_messages"
    assert OutboxMessage.__tablename__ == "outbox_messages"
    assert IngressEvent.__tablename__ == "ingress_events"
    assert ReplyPlan.__tablename__ == "reply_plans"
    assert AmoCrmMirrorJob.__tablename__ == "amocrm_mirror_jobs"
    assert ManagerMessage.__tablename__ == "manager_messages"
    assert WorkerHeartbeat.__tablename__ == "worker_heartbeats"
    assert ConversationOpsEvent.__tablename__ == "conversation_ops_events"
    assert EphemeralPiiValue.__tablename__ == "ephemeral_pii_values"
    assert AttachmentSpoolObject.__tablename__ == "attachment_spool_objects"
    assert MasterChannelBinding.__tablename__ == "master_channel_bindings"
    assert MasterCommandPending.__tablename__ == "master_command_pendings"
    assert SelfBookingCreatePending.__tablename__ == "self_booking_create_pendings"
    assert SelfBookingActiveOffer.__tablename__ == "self_booking_active_offers"
    assert SelfBookingPiiAdmission.__tablename__ == "self_booking_pii_admissions"
    assert CanonicalIdentity.__tablename__ == "canonical_identities"
    assert ExternalIdentityLink.__tablename__ == "external_identity_links"
    assert AmocrmChatBinding.__tablename__ == "amocrm_chat_bindings"
    assert AmocrmMessageProjection.__tablename__ == "amocrm_message_projections"
    assert AmocrmCrmOauthToken.__tablename__ == "amocrm_crm_oauth_tokens"
    assert AmocrmEntityLink.__tablename__ == "amocrm_entity_links"
    assert IdentityReviewCase.__tablename__ == "identity_review_cases"
    table_names = set(Base.metadata.tables)
    assert table_names == {
        "conversations",
        "inbox_messages",
        "outbox_messages",
        "ingress_events",
        "reply_plans",
        "amocrm_mirror_jobs",
        "manager_messages",
        "worker_heartbeats",
        "conversation_ops_events",
        "ephemeral_pii_values",
        "attachment_spool_objects",
        "master_channel_bindings",
        "master_command_pendings",
        "self_booking_create_pendings",
        "self_booking_active_offers",
        "self_booking_pii_admissions",
        "canonical_identities",
        "external_identity_links",
        "amocrm_chat_bindings",
        "amocrm_message_projections",
        "amocrm_crm_oauth_tokens",
        "amocrm_entity_links",
        "identity_review_cases",
    }


def test_alembic_migration_has_upgrade_and_downgrade() -> None:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions())
    assert len(revisions) >= 7
    by_id = {rev.revision: rev for rev in revisions}
    assert "20260727_01a_foundation" in by_id
    assert "20260727_01b_ingress" in by_id
    assert "20260727_01c_reply_outbound" in by_id
    assert "20260728_09_amocrm_mirror" in by_id
    assert "20260728_10_attempt_exhaustion" in by_id
    assert "20260729_11_handoff_schema" in by_id
    assert "20260729_12_worker_runtime" in by_id
    assert by_id["20260727_01b_ingress"].down_revision == "20260727_01a_foundation"
    assert by_id["20260727_01c_reply_outbound"].down_revision == "20260727_01b_ingress"
    assert (
        by_id["20260728_09_amocrm_mirror"].down_revision
        == "20260727_01c_reply_outbound"
    )
    assert (
        by_id["20260728_10_attempt_exhaustion"].down_revision
        == "20260728_09_amocrm_mirror"
    )
    assert (
        by_id["20260729_11_handoff_schema"].down_revision
        == "20260728_10_attempt_exhaustion"
    )
    assert (
        by_id["20260729_12_worker_runtime"].down_revision
        == "20260729_11_handoff_schema"
    )
    assert "20260801_16_spool_leases" in by_id
    assert (
        by_id["20260801_16_spool_leases"].down_revision
        == "20260801_15_attachment_spool"
    )
    assert "20260807_17_master_bindings" in by_id
    assert (
        by_id["20260807_17_master_bindings"].down_revision
        == "20260801_16_spool_leases"
    )
    assert "20260808_18_master_commands" in by_id
    assert (
        by_id["20260808_18_master_commands"].down_revision
        == "20260807_17_master_bindings"
    )
    assert "20260809_19_identity_resolution" in by_id
    assert (
        by_id["20260809_19_identity_resolution"].down_revision
        == "20260808_18_master_commands"
    )
    assert "20260812_20_amocrm_mgr_ingress" in by_id
    assert (
        by_id["20260812_20_amocrm_mgr_ingress"].down_revision
        == "20260809_19_identity_resolution"
    )
    assert "20260812_21_amocrm_chat_proj" in by_id
    assert (
        by_id["20260812_21_amocrm_chat_proj"].down_revision
        == "20260812_20_amocrm_mgr_ingress"
    )
    assert "20260812_22_amo_chat_integ_cid" in by_id
    assert (
        by_id["20260812_22_amo_chat_integ_cid"].down_revision
        == "20260812_21_amocrm_chat_proj"
    )
    assert "20260813_23_amocrm_crm_oauth" in by_id
    assert (
        by_id["20260813_23_amocrm_crm_oauth"].down_revision
        == "20260812_22_amo_chat_integ_cid"
    )
    assert "20260813_24_amo_entity_links" in by_id
    assert (
        by_id["20260813_24_amo_entity_links"].down_revision
        == "20260813_23_amocrm_crm_oauth"
    )
    assert "20260813_25_amo_deal_reserve" in by_id
    assert (
        by_id["20260813_25_amo_deal_reserve"].down_revision
        == "20260813_24_amo_entity_links"
    )
    assert "20260816_26_identity_glue" in by_id
    assert (
        by_id["20260816_26_identity_glue"].down_revision
        == "20260813_25_amo_deal_reserve"
    )
    assert "20260818_27_amocrm_deal_kind" in by_id
    assert (
        by_id["20260818_27_amocrm_deal_kind"].down_revision
        == "20260816_26_identity_glue"
    )
    assert "20260820_28_self_booking_create" in by_id
    assert (
        by_id["20260820_28_self_booking_create"].down_revision
        == "20260818_27_amocrm_deal_kind"
    )
    assert "20260820_29_active_offer" in by_id
    assert (
        by_id["20260820_29_active_offer"].down_revision
        == "20260820_28_self_booking_create"
    )
    assert "20260820_30_pii_admission" in by_id
    assert (
        by_id["20260820_30_pii_admission"].down_revision
        == "20260820_29_active_offer"
    )

    for revision_id in (
        "20260727_01a_foundation",
        "20260727_01b_ingress",
        "20260727_01c_reply_outbound",
        "20260728_09_amocrm_mirror",
        "20260728_10_attempt_exhaustion",
        "20260729_11_handoff_schema",
        "20260729_12_worker_runtime",
        "20260801_16_spool_leases",
        "20260807_17_master_bindings",
        "20260808_18_master_commands",
        "20260809_19_identity_resolution",
        "20260812_20_amocrm_mgr_ingress",
        "20260812_21_amocrm_chat_proj",
        "20260812_22_amo_chat_integ_cid",
        "20260813_23_amocrm_crm_oauth",
        "20260813_24_amo_entity_links",
        "20260813_25_amo_deal_reserve",
        "20260816_26_identity_glue",
        "20260818_27_amocrm_deal_kind",
        "20260820_28_self_booking_create",
        "20260820_29_active_offer",
        "20260820_30_pii_admission",
    ):
        rev = by_id[revision_id]
        assert callable(rev.module.upgrade)
        assert callable(rev.module.downgrade)
        text = Path(rev.path).read_text(encoding="utf-8")
        assert "def upgrade()" in text
        assert "def downgrade()" in text

    # alembic_version.version_num is VARCHAR(32); overflow truncates upgrades.
    heads = script.get_heads()
    assert len(heads) == 1
    revision_ids = [rev.revision for rev in revisions]
    assert all(type(revision_id) is str and revision_id for revision_id in revision_ids)
    assert all(len(revision_id) <= 32 for revision_id in revision_ids)
    assert len(revision_ids) == len(set(revision_ids))
    assert "20260801_16_spool_leases" in revision_ids
    assert len("20260801_16_spool_leases") <= 32
    assert "20260807_17_master_bindings" in revision_ids
    assert len("20260807_17_master_bindings") <= 32
    assert "20260808_18_master_commands" in revision_ids
    assert len("20260808_18_master_commands") <= 32
    assert "20260809_19_identity_resolution" in revision_ids
    assert len("20260809_19_identity_resolution") <= 32
    assert "20260812_21_amocrm_chat_proj" in revision_ids
    assert len("20260812_21_amocrm_chat_proj") <= 32
    assert "20260812_22_amo_chat_integ_cid" in revision_ids
    assert len("20260812_22_amo_chat_integ_cid") <= 32
    assert "20260813_23_amocrm_crm_oauth" in revision_ids
    assert len("20260813_23_amocrm_crm_oauth") <= 32
    assert "20260813_24_amo_entity_links" in revision_ids
    assert len("20260813_24_amo_entity_links") <= 32
    assert "20260813_25_amo_deal_reserve" in revision_ids
    assert len("20260813_25_amo_deal_reserve") <= 32
    assert "20260816_26_identity_glue" in revision_ids
    assert len("20260816_26_identity_glue") <= 32
    assert "20260818_27_amocrm_deal_kind" in revision_ids
    assert len("20260818_27_amocrm_deal_kind") <= 32
    assert "20260820_28_self_booking_create" in revision_ids
    assert len("20260820_28_self_booking_create") <= 32
    assert "20260820_29_active_offer" in revision_ids
    assert len("20260820_29_active_offer") <= 32
    assert "20260820_30_pii_admission" in revision_ids
    assert len("20260820_30_pii_admission") <= 32
    assert heads == ["20260820_30_pii_admission"]

    foundation = Path(by_id["20260727_01a_foundation"].path).read_text(encoding="utf-8")
    assert "delivery_status IN ('PENDING', 'CANCELLED')" in foundation
    assert "'SENT'" not in foundation

    reply = Path(by_id["20260727_01c_reply_outbound"].path).read_text(encoding="utf-8")
    assert "reply_plans" in reply
    assert "SYNTHETIC_OUTBOUND" in reply
    assert "DELIVERED" in reply
    assert "'SENT'" not in reply
    assert "op.drop_table" in reply

    mirror = Path(by_id["20260728_09_amocrm_mirror"].path).read_text(encoding="utf-8")
    assert "amocrm_mirror_jobs" in mirror
    assert "op.create_table" in mirror
    assert 'op.drop_table("amocrm_mirror_jobs")' in mirror
    assert "'SENT'" not in mirror
    # CURSOR-09 ships one table only: no external-entity mapping, no direction
    # column, and no amoCRM entity semantics.
    assert mirror.count("op.create_table(") == 1
    for absent in (
        "amocrm_entity_links",
        "external_entity_id",
        'sa.Column("direction"',
    ):
        assert absent not in mirror, f"out-of-scope object in migration: {absent}"

    handoff = Path(by_id["20260729_11_handoff_schema"].path).read_text(
        encoding="utf-8"
    )
    assert "manager_messages" in handoff
    assert "'infinity'::timestamptz" in handoff
    assert "HANDOFF_SCHEMA_MIGRATION" in handoff
    assert "ADMITTED" in handoff
    assert "pre-upgrade database" in handoff

    runtime = Path(by_id["20260729_12_worker_runtime"].path).read_text(
        encoding="utf-8"
    )
    assert "worker_heartbeats" in runtime
    assert "generation_id" in runtime
    assert 'op.drop_table("worker_heartbeats")' in runtime
    for forbidden in ("body_text", "external_conversation_id", "DATABASE_URL"):
        assert forbidden not in runtime


def test_model_migration_check_and_unique_parity() -> None:
    root = Path(__file__).resolve().parents[1]
    migration_01a = (
        root / "alembic" / "versions" / "20260727_01a_foundation.py"
    ).read_text(encoding="utf-8")
    migration_01b = (
        root / "alembic" / "versions" / "20260727_01b_ingress.py"
    ).read_text(encoding="utf-8")
    migration_01c = (
        root / "alembic" / "versions" / "20260727_01c_reply_outbound.py"
    ).read_text(encoding="utf-8")
    migration_09 = (
        root / "alembic" / "versions" / "20260728_09_amocrm_mirror.py"
    ).read_text(encoding="utf-8")
    migration_10 = (
        root / "alembic" / "versions" / "20260728_10_attempt_exhaustion.py"
    ).read_text(encoding="utf-8")
    migration_11 = (
        root / "alembic" / "versions" / "20260729_11_handoff_schema.py"
    ).read_text(encoding="utf-8")
    migration_12 = (
        root / "alembic" / "versions" / "20260729_12_worker_runtime.py"
    ).read_text(encoding="utf-8")
    migration_20 = (
        root / "alembic" / "versions" / "20260812_20_amocrm_mgr_ingress.py"
    ).read_text(encoding="utf-8")
    migration_21 = (
        root / "alembic" / "versions" / "20260812_21_amocrm_chat_proj.py"
    ).read_text(encoding="utf-8")
    migration_22 = (
        root / "alembic" / "versions" / "20260812_22_amo_chat_integ_cid.py"
    ).read_text(encoding="utf-8")
    migration_23 = (
        root / "alembic" / "versions" / "20260813_23_amocrm_crm_oauth.py"
    ).read_text(encoding="utf-8")
    migration_24 = (
        root / "alembic" / "versions" / "20260813_24_amo_entity_links.py"
    ).read_text(encoding="utf-8")
    migration_25 = (
        root / "alembic" / "versions" / "20260813_25_amo_deal_reserve.py"
    ).read_text(encoding="utf-8")
    migration_26 = (
        root / "alembic" / "versions" / "20260816_26_identity_glue.py"
    ).read_text(encoding="utf-8")
    migration_27 = (
        root / "alembic" / "versions" / "20260818_27_amocrm_deal_kind.py"
    ).read_text(encoding="utf-8")

    model_checks: dict[str, str] = {}
    model_uniques: set[str] = set()
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name:
                model_checks[constraint.name] = str(constraint.sqltext).strip()
            if isinstance(constraint, UniqueConstraint) and constraint.name:
                model_uniques.add(constraint.name)

    assert set(_EXPECTED_CHECKS_01A_STABLE) <= set(model_checks)
    assert _EXPECTED_UNIQUES_01A <= model_uniques
    assert set(_EXPECTED_CHECKS_01B) <= set(model_checks)
    assert _EXPECTED_UNIQUES_01B <= model_uniques
    assert set(_EXPECTED_CHECKS_01C) <= set(model_checks)
    assert _EXPECTED_UNIQUES_01C <= model_uniques
    assert set(_EXPECTED_CHECKS_09) <= set(model_checks)
    assert _EXPECTED_UNIQUES_09 <= model_uniques
    assert set(_EXPECTED_CHECKS_10) <= set(model_checks)
    assert set(_EXPECTED_CHECKS_11) <= set(model_checks)
    assert _EXPECTED_UNIQUES_11 <= model_uniques
    assert set(_EXPECTED_CHECKS_12) <= set(model_checks)
    assert set(_EXPECTED_CHECKS_20) <= set(model_checks)
    assert _EXPECTED_UNIQUES_20 <= model_uniques
    assert set(_EXPECTED_CHECKS_21) <= set(model_checks)
    assert _EXPECTED_UNIQUES_21 <= model_uniques
    assert set(_EXPECTED_CHECKS_22) <= set(model_checks)
    assert set(_EXPECTED_CHECKS_23) <= set(model_checks)
    assert _EXPECTED_UNIQUES_23 <= model_uniques
    assert set(_EXPECTED_CHECKS_24) <= set(model_checks)
    assert set(_EXPECTED_CHECKS_25) <= set(model_checks)
    assert set(_EXPECTED_CHECKS_26) <= set(model_checks)

    for name, sql in _EXPECTED_CHECKS_01A_STABLE.items():
        assert name in migration_01a
        assert sql in migration_01a
        assert model_checks[name] == sql

    # 01A historically created the narrow outbox checks; 01C replaces them.
    assert "destination_type IN ('INTERNAL_DRAFT')" in migration_01a
    assert "delivery_status IN ('PENDING', 'CANCELLED')" in migration_01a
    assert model_checks["ck_outbox_destination_type"] == _EXPECTED_CHECKS_01C[
        "ck_outbox_destination_type"
    ]
    assert "DELIVERED" in model_checks["ck_outbox_delivery_status"]
    assert "PROCESSING" in model_checks["ck_outbox_delivery_status"]
    assert "SENT" not in model_checks["ck_outbox_delivery_status"]
    assert _EXPECTED_CHECKS_01C["ck_outbox_destination_type"] in migration_01c
    assert "DELIVERED" in migration_01c
    assert "SYNTHETIC_OUTBOUND" in migration_01c

    for name in _EXPECTED_UNIQUES_01A:
        assert name in migration_01a

    for name, sql in _EXPECTED_CHECKS_01B.items():
        assert name in migration_01b
        assert sql in migration_01b
        if name in {"ck_ingress_channel", "ck_ingress_event_type"}:
            # 01B historically created the narrow ingress checks; AMO-01A replaces.
            continue
        assert model_checks[name] == sql

    for name in _EXPECTED_UNIQUES_01B:
        assert name in migration_01b

    for name, sql in _EXPECTED_CHECKS_01C.items():
        assert name in migration_01c
        if name == "ck_outbox_delivery_status":
            assert "'ADMITTED'" in model_checks[name]
        else:
            assert model_checks[name] == sql
        # Migration may split long CHECK literals across adjacent strings.
        for token in re.findall(r"'[A-Z_]+'", sql):
            assert token in migration_01c, f"{name} missing {token}"

    for name in _EXPECTED_UNIQUES_01C:
        assert name in migration_01c

    assert re.search(
        r'name=["\']uq_reply_plans_conversation_context_version["\']',
        migration_01c,
    )

    for name, sql in _EXPECTED_CHECKS_09.items():
        assert name in migration_09
        assert model_checks[name] == sql
        for token in re.findall(r"'[A-Z_]+'", sql):
            assert token in migration_09, f"{name} missing {token}"

    for name in _EXPECTED_UNIQUES_09:
        assert name in migration_09

    assert re.search(r'name=["\']uq_amocrm_mirror_key["\']', migration_09)

    for name, sql in _EXPECTED_CHECKS_10.items():
        assert name in migration_10
        assert model_checks[name] == sql
        assert sql in migration_10

    for name, sql in _EXPECTED_CHECKS_11.items():
        assert name in migration_11
        assert model_checks[name] == sql
        for token in re.findall(r"'[A-Z_]+'", sql):
            assert token in migration_11, f"{name} missing {token}"

    for name in _EXPECTED_UNIQUES_11:
        assert name in migration_11

    for name, sql in _EXPECTED_CHECKS_12.items():
        assert name in migration_12
        assert model_checks[name] == sql
        for token in re.findall(r"'[a-z_]+'", sql):
            assert token in migration_12, f"{name} missing {token}"

    for name, sql in _EXPECTED_CHECKS_20.items():
        assert name in migration_20
        assert model_checks[name] == sql
        assert sql in migration_20

    for name in _EXPECTED_UNIQUES_20:
        assert name in migration_20
    assert "amocrm_chat_bindings" in migration_20

    for name, sql in _EXPECTED_CHECKS_21.items():
        assert name in migration_21
        assert model_checks[name] == sql
        if name == "ck_amocrm_message_projections_integration_msgid_format":
            assert sql in migration_21
            continue
        # Migration may split long CHECK literals across adjacent strings.
        for token in re.findall(r"'[A-Z_]+'", sql):
            assert token in migration_21, f"{name} missing {token}"
        if "'" not in sql:
            assert sql in migration_21

    for name in _EXPECTED_UNIQUES_21:
        assert name in migration_21
    assert "amocrm_message_projections" in migration_21
    for forbidden_col in ("body_text", "message_text", "payload_text", "text_body"):
        assert forbidden_col not in migration_21

    for name, sql in _EXPECTED_CHECKS_22.items():
        assert name in migration_22
        assert model_checks[name] == sql
        for token in (
            "integration_conversation_id",
            "char_length(integration_conversation_id)",
        ):
            assert token in migration_22
    assert "integration_conversation_id" in migration_22

    for name, sql in _EXPECTED_CHECKS_23.items():
        assert name in migration_23
        assert model_checks[name] == sql
        if "'" in sql:
            for token in re.findall(r"'[^']*'", sql):
                assert token in migration_23, f"{name} missing {token}"
        else:
            assert sql in migration_23
    for name in _EXPECTED_UNIQUES_23:
        assert name in migration_23
    assert "amocrm_crm_oauth_tokens" in migration_23
    for forbidden in ("AMOCRM_CHAT", "channel_secret", "body_text"):
        assert forbidden not in migration_23

    for name, sql in _EXPECTED_CHECKS_24.items():
        assert name in migration_24
        assert model_checks[name] == sql
        for token in re.findall(r"'[A-Z_]+'", sql):
            assert token in migration_24, f"{name} missing {token}"
    assert "amocrm_entity_links" in migration_24
    assert "uq_amocrm_entity_links_active_conversation_kind" in migration_24
    assert "uq_amocrm_entity_links_active_kind_external" in migration_24
    for forbidden in ("create_contact", "create_deal", "booking"):
        assert forbidden not in migration_24

    for name, sql in _EXPECTED_CHECKS_25.items():
        assert name in migration_25
        assert model_checks[name] == sql
        for token in re.findall(r"'[A-Z_]+'", sql):
            assert token in migration_25, f"{name} missing {token}"
        if "'" not in sql:
            assert sql in migration_25
    assert "RESERVED" in migration_25
    assert "RECONCILE_REQUIRED" in migration_25
    assert "create_submitted_at" in migration_25
    assert "uq_amocrm_entity_links_open_conversation_kind" in migration_25

    for name, sql in _EXPECTED_CHECKS_26.items():
        assert name in migration_26
        assert model_checks[name] == sql
        for token in re.findall(r"'[A-Z_]+'", sql):
            assert token in migration_26, f"{name} missing {token}"
    assert "identity_review_cases" in migration_26
    assert "canonical_identity_id" in migration_26
    assert "uq_identity_review_cases_open_conversation_reason" in migration_26
    for forbidden in ("normalize_phone", "send_silent_text", "create_contact"):
        assert forbidden not in migration_26

    assert "AMOCRM_DEAL" in migration_27
    assert "uq_external_identity_links_active_amocrm_deal_role" in migration_27
    assert "('AMOCRM_DEAL', 'AMOCRM_TECHNICAL_DEAL')" in migration_27
    upgrade_27 = migration_27.split("def downgrade")[0]
    assert "('AMOCRM_BUYER_CARD', 'AMOCRM_TECHNICAL_DEAL')" not in upgrade_27
    assert "def upgrade()" in migration_27
    assert "def downgrade()" in migration_27
    assert "UPDATE " not in migration_27
    assert "DELETE FROM" not in migration_27

    for complex_check in (
        "ck_conversations_handoff_consistency",
        "ck_manager_messages_classification",
        "ck_outbox_admitted_destination",
        "ck_outbox_admitted_state",
    ):
        assert complex_check in migration_11
        assert complex_check in model_checks


def test_alembic_env_fileconfig_call_site_disables_existing_loggers_false() -> None:
    """AST contract: production env.py must pass disable_existing_loggers=False.

    Protects the real call-site (not comments/strings). Subprocess behavioral
    tests cover runtime fileConfig semantics without touching pytest logging.
    """
    source = (_REPO_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="alembic/env.py")
    matches: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "fileConfig":
            matches.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "fileConfig":
            matches.append(node)
    assert len(matches) == 1, f"expected exactly one fileConfig call, got {len(matches)}"
    call = matches[0]
    keyword = next(
        (
            kw
            for kw in call.keywords
            if kw.arg == "disable_existing_loggers"
        ),
        None,
    )
    assert keyword is not None, "disable_existing_loggers keyword is required"
    value = keyword.value
    assert isinstance(value, ast.Constant), "disable_existing_loggers must be a literal"
    assert value.value is False, "disable_existing_loggers must be literal False"


def test_fileconfig_false_preserves_preexisting_logger_in_subprocess() -> None:
    """Runtime semantics for disable_existing_loggers=False (isolated process)."""
    script = r"""
import logging
import sys
from logging.config import fileConfig
from pathlib import Path

root = Path.cwd()
name = "bot_tv.fileconfig_isolation.probe"
logger = logging.getLogger(name)
assert logger.disabled is False

captured: list[str] = []

class MemoryHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        captured.append(record.getMessage())

handler = MemoryHandler()
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

fileConfig(str(root / "alembic.ini"), disable_existing_loggers=False)
assert logger.disabled is False
assert handler in logger.handlers

logger.info("probe_one")
assert captured == ["probe_one"], captured

fileConfig(str(root / "alembic.ini"), disable_existing_loggers=False)
assert logger.disabled is False
assert handler in logger.handlers
logger.info("probe_two")
assert captured == ["probe_one", "probe_two"], captured
print("ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"subprocess failed rc={completed.returncode} "
        f"stderr={completed.stderr!r} stdout={completed.stdout!r}"
    )
    assert "ok" in completed.stdout
    blob = completed.stdout + completed.stderr
    assert "password" not in blob.lower()
    assert "postgresql+" not in blob.lower()
