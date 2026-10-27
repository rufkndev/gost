from app.db.models import Base

SPEC_TABLES = {
    "users": {
        "tg_id",
        "username",
        "first_name",
        "email",
        "terms_version",
        "terms_accepted_at",
        "is_beta",
        "is_banned",
        "created_at",
    },
    "orders": {
        "id",
        "short_id",
        "user_id",
        "profile_id",
        "status",
        "file_name",
        "file_hash",
        "src_path",
        "result_path",
        "pages_estimate",
        "price_rub",
        "analysis",
        "ir",
        "report",
        "error_code",
        "keep_for_debug_until",
        "created_at",
        "updated_at",
    },
    "payments": {
        "id",
        "order_id",
        "yookassa_payment_id",
        "idempotence_key",
        "status",
        "amount",
        "confirmation_url",
        "raw_last",
        "created_at",
        "updated_at",
    },
    "refunds": {
        "id",
        "payment_id",
        "yookassa_refund_id",
        "amount",
        "reason",
        "status",
        "created_at",
    },
    "questions": {"id", "order_id", "block_id", "options", "answer", "asked_at", "answered_at"},
    "llm_calls": {
        "id",
        "order_id",
        "model",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "latency_ms",
        "ok",
        "created_at",
    },
    "events": {"id", "order_id", "type", "payload", "created_at"},
    "feedback": {"id", "order_id", "rating", "comment", "created_at"},
}


def test_tables_cover_spec() -> None:
    tables = Base.metadata.tables
    assert set(tables) == set(SPEC_TABLES)
    for name, columns in SPEC_TABLES.items():
        missing = columns - set(tables[name].columns.keys())
        assert not missing, f"{name}: missing {missing}"


def test_idempotence_keys_unique() -> None:
    t = Base.metadata.tables
    assert t["payments"].c.idempotence_key.unique
    assert t["payments"].c.yookassa_payment_id.unique
    assert t["refunds"].c.idempotence_key.unique
