"""Unit tests for the legacy Ghost → Farm News importer (GOL-3564).

Exercises the pure, input-only logic — TSV parsing, newsletter-subscription
detection, the bounced flag, and the static eligibility classifier — with a
synthetic TSV. The XML-RPC orchestration needs a live Odoo and is covered by the
QA dry-run/apply run recorded on the issue, not here.
"""

import importlib.util
import os

_PATH = os.path.join(os.path.dirname(__file__), "..", "import_legacy_ghost_members.py")
_spec = importlib.util.spec_from_file_location("import_legacy_ghost_members", _PATH)
imp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(imp)


# A synthetic export covering every branch the classifier must decide. No real
# emails — example.com addresses only.
SYNTHETIC_TSV = (
    "\t".join(["email", "name", "status", "created_at", "email_disabled", "newsletters"])
    + "\n"
    + "\n".join(
        [
            # subscribed + deliverable  -> eligible
            "\t".join(["free1@example.com", "Free One", "free", "2024-01-01", "0", "Goldberry Grove"]),
            # paid member, subscribed + deliverable -> eligible (paid still imports its sub)
            "\t".join(["paid1@example.com", "Paid One", "paid", "2024-02-01", "0", "Goldberry Grove"]),
            # subscribed but email disabled (bounced) -> skip bounced
            "\t".join(["bounced@example.com", "Bounced", "free", "2024-03-01", "1", "Goldberry Grove"]),
            # not subscribed to the newsletter -> skip not_subscribed
            "\t".join(["nonews@example.com", "No News", "free", "2024-04-01", "0", ""]),
            # subscribed to a different newsletter only -> skip not_subscribed
            "\t".join(["other@example.com", "Other List", "free", "2024-05-01", "0", "Some Other List"]),
            # qualifier after the name + mixed case + whitespace -> still eligible
            "\t".join(
                ["qualified@example.com", "Qualified", "free", "2024-06-01", "0", " goldberry grove (subscribed) "]
            ),
            # blank email -> skip no_email
            "\t".join(["", "Nameless", "free", "2024-07-01", "0", "Goldberry Grove"]),
        ]
    )
)


def test_normalize_email_unwraps_and_lowercases():
    assert imp.normalize_email("  Foo@Example.COM ") == "foo@example.com"
    assert imp.normalize_email("Ada <ada@example.com>") == "ada@example.com"
    assert imp.normalize_email(None) == ""


def test_is_subscribed_matches_token_case_insensitively():
    assert imp.is_subscribed_to_newsletter("Goldberry Grove")
    assert imp.is_subscribed_to_newsletter("Nursery News, Goldberry Grove")
    assert imp.is_subscribed_to_newsletter(" goldberry grove (subscribed) ")
    assert not imp.is_subscribed_to_newsletter("")
    assert not imp.is_subscribed_to_newsletter("Some Other List")
    assert not imp.is_subscribed_to_newsletter(None)


def test_email_delivery_disabled_fails_safe():
    assert not imp.email_delivery_disabled("0")
    assert not imp.email_delivery_disabled("")
    assert not imp.email_delivery_disabled("false")
    assert imp.email_delivery_disabled("1")
    assert imp.email_delivery_disabled("true")
    # An unexpected value is treated as disabled (skip rather than mail).
    assert imp.email_delivery_disabled("maybe")


def test_parse_members_tsv_reads_header_and_rows():
    rows = imp.parse_members_tsv(SYNTHETIC_TSV)
    assert len(rows) == 7
    assert rows[0]["email"] == "free1@example.com"
    assert rows[0]["newsletters"] == "Goldberry Grove"


def test_parse_members_tsv_requires_columns():
    bad = "email\tname\n" + "a@example.com\tA\n"  # missing newsletters + email_disabled
    try:
        imp.parse_members_tsv(bad)
    except ValueError as exc:
        assert "newsletters" in str(exc) and "email_disabled" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing required columns")


def test_parse_members_tsv_tolerates_blank_lines_and_bom():
    text = "﻿email\tnewsletters\temail_disabled\n\n a@example.com \tGoldberry Grove\t0\n\n"
    rows = imp.parse_members_tsv(text)
    assert len(rows) == 1
    assert rows[0]["email"] == "a@example.com"


def test_classify_eligibility_decides_each_row():
    rows = imp.parse_members_tsv(SYNTHETIC_TSV)
    results = [imp.classify_eligibility(r) for r in rows]
    assert results[0] == (True, None)  # free, subscribed, deliverable
    assert results[1] == (True, None)  # paid, subscribed, deliverable
    assert results[2] == (False, "bounced")
    assert results[3] == (False, "not_subscribed")
    assert results[4] == (False, "not_subscribed")
    assert results[5] == (True, None)  # qualifier + case/space tolerant
    assert results[6] == (False, "no_email")

    eligible = sum(1 for ok, _ in results if ok)
    assert eligible == 3
