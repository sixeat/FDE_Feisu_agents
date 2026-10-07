import pytest

from scripts.meeting_draft_review_export import attach_evidence, short_hash


def review_payload():
    return {
        "document_id_hash": short_hash("doc-1"),
        "revision_id": "5",
        "todos": [{"evidence_block_ids": [2, 3]}],
    }


def test_review_export_attaches_only_cited_source_blocks():
    payload = review_payload()
    attach_evidence(
        payload, document_id="doc-1", revision_id="5",
        blocks=[
            {"index": 1, "text": "unrelated"},
            {"index": 2, "text": "assigned work"},
            {"index": 3, "text": "deadline"},
        ],
    )
    assert payload["todos"][0]["evidence"] == [
        {"block_id": 2, "text": "assigned work"},
        {"block_id": 3, "text": "deadline"},
    ]


@pytest.mark.parametrize(
    ("document_id", "revision_id", "blocks", "message"),
    [
        ("doc-2", "5", [{"index": 2, "text": "work"}], "source document"),
        ("doc-1", "6", [{"index": 2, "text": "work"}], "revision changed"),
        ("doc-1", "5", [{"index": 2, "text": "work"}], "no longer readable"),
    ],
)
def test_review_export_rejects_stale_or_incomplete_evidence(document_id, revision_id, blocks, message):
    with pytest.raises(ValueError, match=message):
        attach_evidence(review_payload(), document_id=document_id, revision_id=revision_id, blocks=blocks)
