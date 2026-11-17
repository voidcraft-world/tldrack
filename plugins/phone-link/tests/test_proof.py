"""The handshake's wire format (proof.py): the README's test vector, the header shapes, and the nonce book."""

from __future__ import annotations

import json

import pytest

from link_client import sign
from phone_link import proof

# The vector in plugins/phone-link/README.md § The handshake: the Android client checks the same numbers.
TOKEN = "TEST_VECTOR_TOKEN_do_not_use_0123456789abcd"
CHALLENGE = "TEST_VECTOR_CHALLENGE_0123456789abcdefghij"
NONCE = "TEST_VECTOR_NONCE_0123456789abcdefghijklmno"
NONCE_BODY = b'{"ok":true,"v":2,"nonce":"TEST_VECTOR_NONCE_0123456789abcdefghijklmno","ttl_s":30}'
ANSWER_BODY = b'{"id":"r1","verdict":"open"}'


def test_the_readme_vector():
    assert (
        sign(TOKEN, proof.nonce_message(CHALLENGE, 200, NONCE_BODY))
        == "d08023bee422685b7cb6d7002e662d159cca9fdec5d9d7e8936a849a20800675"
    )
    assert proof.request_message(NONCE, "POST", "/plugin/laya/review/answer", ANSWER_BODY) == (
        b"phone-link/2 request\nTEST_VECTOR_NONCE_0123456789abcdefghijklmno\nPOST\n/plugin/laya/review/answer\n"
        b"d008ed8c71bdb40455d43d00250edfc100aa391c6760ca4f7fa223bc0fdefefe"
    )
    assert (
        sign(TOKEN, proof.request_message(NONCE, "POST", "/plugin/laya/review/answer", ANSWER_BODY))
        == "893bac9380754babbf40a9bfcfcc0baea347a15bb194ba7c9ed13e779a543d0d"
    )
    assert (
        sign(TOKEN, proof.request_message(NONCE, "GET", "/ledger/search?q=open+work", b""))
        == "12157b675a0412d9e46bade13cdfd47ba30ecdd51ba034127c94fe3e6d7fb9bc"
    )
    assert (
        sign(TOKEN, proof.reply_message(NONCE, 200, b'{"ok":true}'))
        == "e3c4bb9073d51afafa3dec53caf4a76f92e2fffde59f2008ccd6fd0a99b67ea6"
    )
    assert json.loads(NONCE_BODY)["nonce"] == NONCE


def test_a_request_and_a_reply_never_sign_the_same_string():
    """Domain separation: no choice of fields makes a reply's string a request's, or the other way round."""
    request = proof.request_message(NONCE, "GET", "/health", b"")
    reply = proof.reply_message(NONCE, 200, b"")
    issued = proof.nonce_message(NONCE, 200, b"")
    assert request.split(b"\n", 1)[0] == b"phone-link/2 request"
    assert reply.split(b"\n", 1)[0] == b"phone-link/2 reply"
    assert issued.split(b"\n", 1)[0] == b"phone-link/2 nonce"


def test_a_nonce_reply_is_never_a_proven_reply():
    """`c` is the caller's choice, nonce-shaped if it likes: the link's `/nonce` signature over it must not
    verify as its reply to a request carrying that nonce (the signing oracle closed on 2026-11-07)."""
    body = b'{"ok":true}'
    assert sign(TOKEN, proof.nonce_message(NONCE, 200, body)) != sign(TOKEN, proof.reply_message(NONCE, 200, body))


def test_every_signed_field_changes_the_mac():
    base = ("n" * 43, "POST", "/ledger/ingest", b"{}")
    seen = {sign(TOKEN, proof.request_message(*base))}
    for changed in (
        ("m" * 43, *base[1:]),
        (base[0], "GET", *base[2:]),
        (*base[:2], "/ledger/ingest?x=1", base[3]),
        (*base[:3], b"{} "),
    ):
        seen.add(sign(TOKEN, proof.request_message(*changed)))
    assert len(seen) == 5


@pytest.mark.parametrize(
    "header, expected",
    [
        (f"PhoneLink n={NONCE}, mac={'a' * 64}", (NONCE, "a" * 64)),
        (f"phonelink n={NONCE},mac={'0' * 64}", (NONCE, "0" * 64)),
        (f"PhoneLink n={NONCE}, mac={'A' * 64}", None),  # lowercase hex only: one spelling
        (f"Bearer {TOKEN}", None),  # the old scheme is gone
        (f"PhoneLink n={NONCE}", None),
        (f"PhoneLink mac={'a' * 64}, n={NONCE}", None),
        (f"PhoneLink n={NONCE[:-1]}, mac={'a' * 64}", None),
        (f"PhoneLink n={NONCE}, mac={'a' * 63}", None),
        (f"PhoneLink n={NONCE}, mac={'a' * 64}, x=1", None),
        ("", None),
    ],
)
def test_the_authorization_header(header, expected):
    found = proof.credential([header])
    assert (found.nonce, found.mac) == expected if expected else found is None


def test_exactly_one_authorization_header():
    one = f"PhoneLink n={NONCE}, mac={'a' * 64}"
    assert proof.credential([one, one]) is None
    assert proof.credential([]) is None


@pytest.mark.parametrize(
    "query, expected",
    [
        (f"c={CHALLENGE}", CHALLENGE),
        ("c=" + "x" * 22, "x" * 22),
        ("c=" + "x" * 21, None),
        ("c=" + "x" * 129, None),
        (f"c={CHALLENGE}&c={CHALLENGE}", None),
        (f"c={CHALLENGE}&d=1", None),
        ("", None),
        ("c=a%2Fb" + "x" * 22, None),
    ],
)
def test_the_challenge(query, expected):
    assert proof.challenge_of(query) == expected


# -- the nonce book ------------------------------------------------------------------------------------
def test_a_nonce_is_good_once():
    book = proof.NonceBook()
    issued = book.issue()
    assert proof.NONCE_RE.fullmatch(issued)
    assert book.take(issued) is True
    assert book.take(issued) is False
    assert book.take("x" * 43) is False


def test_a_nonce_expires():
    now = [100.0]
    book = proof.NonceBook(ttl_s=30.0, clock=lambda: now[0])
    early, late = book.issue(), book.issue()
    now[0] += 30.0
    assert book.take(early) is True  # at the limit, still good
    now[0] += 0.1
    assert book.take(late) is False  # past it
    book.issue()  # and the expired are pruned on the next issue
    assert len(book) == 1


def test_a_flood_of_nonces_evicts_the_oldest_and_never_blocks():
    book = proof.NonceBook(capacity=4)
    first = book.issue()
    newest = [book.issue() for _ in range(4)]
    assert len(book) == 4
    assert book.take(first) is False
    assert all(book.take(issued) for issued in newest)
