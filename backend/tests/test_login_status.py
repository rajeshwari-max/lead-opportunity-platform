"""The sign-in diagnostic must agree with the endpoint it explains.

`POST /api/login` answers every failure with the same sentence on purpose —
"Incorrect email or password, or account unavailable" — so that a stranger
cannot use it to find out which addresses exist. That is right for the
endpoint and useless for the person who owns the machine, which is why
scripts/login_status.py exists.

The risk in having a second copy of the rule is that it drifts: someone adds a
check to accounts.py::login and the script keeps cheerfully printing CAN SIGN
IN for an account that cannot. These tests pin the four states the script
distinguishes to the four conditions the endpoint actually tests, so a drift
shows up here rather than as a wrong answer during an outage.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.login_status import verdict                          # noqa: E402


class Member:
    def __init__(self, email="a@b.org", name="A", active=True):
        self.email, self.name, self.active = email, name, active


class Credential:
    def __init__(self, password_hash="", is_admin=False,
                 invitation_hash=None, invitation_expires=None):
        self.owner = "a@b.org"
        self.password_hash = password_hash
        self.is_admin = is_admin
        self.invitation_hash = invitation_hash
        self.invitation_expires = invitation_expires


HASH = "salt:deadbeef"


# ------------------------------------------------------------- can sign in

def test_an_active_member_with_a_password_can_sign_in():
    assert verdict(Member(), Credential(HASH)).startswith("CAN SIGN IN")


def test_an_administrator_is_named_as_one():
    """The single most common question after "why can't I sign in" is "why
    can't I see the admin panel", and it has a different answer."""
    assert "administrator" in verdict(Member(), Credential(HASH, is_admin=True))


# ------------------------------------------------------------ cannot, and why

def test_no_credential_row_at_all():
    """The state a fresh database is in. bootstrap_account.py is the fix, and
    the script has to say so or the reader is stuck."""
    assert verdict(Member(), None).startswith("CANNOT SIGN IN")


def test_a_credential_with_an_empty_hash_cannot_sign_in():
    """verify_password("", "") is false, so the endpoint rejects this — but it
    rejects it with the same sentence as a wrong password, which sends people
    hunting for a typo that isn't there."""
    assert verdict(Member(), Credential("")).startswith("CANNOT SIGN IN")


def test_an_inactive_member_cannot_sign_in_even_with_a_good_password():
    """`not member.active` is checked in accounts.py::login. A password that is
    correct and an account that is switched off look identical in the browser."""
    assert "inactive" in verdict(Member(active=False), Credential(HASH))


def test_a_credential_with_no_member_row_cannot_sign_in():
    """The endpoint requires BOTH. This pair can be produced by deleting a team
    member without deleting their credential."""
    assert verdict(None, Credential(HASH)).startswith("CANNOT SIGN IN")


# --------------------------------------------------------------- invitations

def test_a_pending_invitation_is_reported_as_pending_not_as_broken():
    """This is not a fault — the person simply has not opened their link yet.
    Reporting it as CANNOT SIGN IN would send an admin to re-invite someone who
    already has a valid link sitting in their inbox."""
    later = datetime.utcnow() + timedelta(hours=1)
    answer = verdict(Member(), Credential("", invitation_hash="x",
                                          invitation_expires=later))
    assert "NOT YET ACTIVATED" in answer


def test_an_expired_invitation_says_so():
    """activate() rejects an expired invitation with "Invitation expired or
    already used", which the user only sees after finding the link. Saying it
    here saves that round trip."""
    past = datetime.utcnow() - timedelta(hours=1)
    answer = verdict(Member(), Credential("", invitation_hash="x",
                                          invitation_expires=past))
    assert "EXPIRED" in answer


def test_an_activated_account_is_never_reported_as_invited():
    """Guards the ordering inside verdict(). activate() clears invitation_hash,
    but a stale row could carry both; a set password always wins."""
    later = datetime.utcnow() + timedelta(hours=1)
    answer = verdict(Member(), Credential(HASH, invitation_hash="x",
                                          invitation_expires=later))
    assert answer.startswith("CAN SIGN IN")


# ------------------------------------------------- it must not leak the hash

@pytest.mark.parametrize("cred", [
    Credential(HASH),
    Credential(HASH, is_admin=True),
    Credential("", invitation_hash="secret-token-value"),
])
def test_the_verdict_never_contains_the_hash_or_the_token(cred):
    """The script is meant to be pasted into a chat window when something is
    broken. Nothing it prints may be reusable as a credential."""
    answer = verdict(Member(), cred)
    assert HASH not in answer
    assert "deadbeef" not in answer
    assert "secret-token-value" not in answer
