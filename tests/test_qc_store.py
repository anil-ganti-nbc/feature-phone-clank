from __future__ import annotations

import pytest

from feature_phone_clank.providers.qc_store import DECISIONS, InvalidDecisionError, QcArchiveStore


@pytest.fixture
def qc(tmp_path):
    s = QcArchiveStore(str(tmp_path / "qc.db"))
    yield s
    s.close()


def _submit(qc, event_id=1, decision="USEFUL", **kw):
    return qc.submit_review(
        event_id=event_id, source_key="hmd-nokia", event_type="NEW_PRODUCT",
        decision=decision, product_key="hmd-nokia:widget-1", manufacturer="HMD",
        model="Widget 1", model_number="W1", url="https://example.test/widget-1",
        changed_fields=[{"field": "price", "old_value": None, "new_value": "10"}],
        meta={"note": "test"}, detected_at="2026-08-27T00:00:00+00:00",
        run_id=42, run_started_at="2026-08-27T00:00:00+00:00", **kw,
    )


def test_all_four_decisions_are_valid_vocabulary():
    assert DECISIONS == {"USEFUL", "NOT_USEFUL", "FALSE_POSITIVE", "OUT_OF_STOCK"}


def test_submit_review_archives_full_item_and_provenance(qc):
    result = _submit(qc, reason="looks legit")
    assert result["event_id"] == 1
    assert result["decision"] == "USEFUL"
    assert result["reason"] == "looks legit"
    assert result["product_key"] == "hmd-nokia:widget-1"
    assert result["run_id"] == 42
    assert result["is_corrected"] == 0


def test_invalid_decision_rejected(qc):
    with pytest.raises(InvalidDecisionError):
        _submit(qc, decision="MAYBE")


def test_reviewed_event_ids_reflects_archive(qc):
    assert qc.reviewed_event_ids() == set()
    _submit(qc, event_id=7)
    assert qc.reviewed_event_ids() == {7}


def test_second_submission_for_same_event_corrects_in_place_not_duplicate(qc):
    _submit(qc, event_id=5, decision="USEFUL")
    _submit(qc, event_id=5, decision="NOT_USEFUL")
    rows = qc.db.execute("SELECT * FROM qc_reviews WHERE event_id=5").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["decision"] == "NOT_USEFUL"
    assert row["is_corrected"] == 1
    import json
    history = json.loads(row["review_metadata_json"])["correction_history"]
    assert history[0]["previous_decision"] == "USEFUL"


def test_same_decision_resubmitted_is_not_flagged_as_corrected(qc):
    _submit(qc, event_id=9, decision="USEFUL")
    _submit(qc, event_id=9, decision="USEFUL")
    row = qc.review_for_event(9)
    assert row["is_corrected"] == 0


def test_recent_reviews_newest_first(qc):
    _submit(qc, event_id=1)
    _submit(qc, event_id=2)
    _submit(qc, event_id=3)
    ids = [r["event_id"] for r in qc.recent_reviews(limit=10)]
    assert ids == [3, 2, 1]


def test_archive_persists_across_reopen(tmp_path):
    path = tmp_path / "qc.db"
    s1 = QcArchiveStore(str(path))
    _submit(s1, event_id=1)
    s1.close()
    s2 = QcArchiveStore(str(path))
    try:
        assert s2.reviewed_event_ids() == {1}
        assert len(s2.recent_reviews()) == 1
    finally:
        s2.close()


def test_counts_by_decision(qc):
    _submit(qc, event_id=1, decision="USEFUL")
    _submit(qc, event_id=2, decision="USEFUL")
    _submit(qc, event_id=3, decision="OUT_OF_STOCK")
    counts = qc.counts_by_decision()
    assert counts["USEFUL"] == 2
    assert counts["OUT_OF_STOCK"] == 1
