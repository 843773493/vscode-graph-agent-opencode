from datetime import UTC, datetime

from tests.support.canonical_id_at import (
    session_id_for_name_at,
    thread_id_for_name_at,
    uuid7_hex_from_name,
    uuid7_hex_from_name_at,
)


def test_name_based_fixture_ids_keep_stable_uuid7_vectors() -> None:
    moment = datetime(2026, 6, 1, 12, 34, 56, 789000, tzinfo=UTC)

    assert uuid7_hex_from_name("fixture-vector-α") == (
        "019b8f7eac80793588f71964db447044"
    )
    assert uuid7_hex_from_name_at("fixture-vector-β", moment) == (
        "019e832e18957664879b8e9acce3b70a"
    )
    assert session_id_for_name_at("fixture-vector-γ", moment) == (
        "ses_019e832e189571888ed608cf404139ab"
    )
    assert thread_id_for_name_at("fixture-vector-δ", moment) == (
        "thr_019e832e189575a288234f7c9cf08d3f"
    )
