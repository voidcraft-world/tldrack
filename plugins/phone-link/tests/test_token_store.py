"""The token: minted once, kept 0600 in a 0700 folder, used only as an HMAC key, never shown."""

import hashlib
import hmac
import os
import stat

import pytest

from phone_link import token_store
from phone_link.token_store import TOKEN_RE, TokenStore


def test_minted_once_and_kept_private(tmp_path):
    store = TokenStore(tmp_path / "phone-link")
    token = store.value()
    assert token and TOKEN_RE.fullmatch(token) and store.minted
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700
    again = TokenStore(tmp_path / "phone-link")
    assert again.value() == token and not again.minted
    assert [p.name for p in store.path.parent.iterdir()] == ["token"]  # no temp file left behind


def test_a_loosened_file_is_tightened(tmp_path):
    TokenStore(tmp_path).value()
    os.chmod(tmp_path / "token", 0o644)
    TokenStore(tmp_path).value()
    assert stat.S_IMODE(os.stat(tmp_path / "token").st_mode) == 0o600


def test_a_corrupt_file_is_replaced_and_a_symlink_refused(tmp_path):
    (tmp_path / "token").write_text("not a token\n")
    store = TokenStore(tmp_path)
    assert TOKEN_RE.fullmatch(store.value() or "")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("x" * 43)
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "token").symlink_to(elsewhere)
    refused = TokenStore(linked)
    assert refused.value() is None and refused.error and "x" * 43 not in refused.error
    assert refused.sign(b"m") is None and not refused.verify(b"m", "0" * 64)


def test_sign_is_hmac_sha256_keyed_by_the_token(tmp_path):
    store = TokenStore(tmp_path)
    token = store.value()
    assert store.sign(b"message") == hmac.new(token.encode(), b"message", hashlib.sha256).hexdigest()
    assert token not in store.sign(b"message")


def test_verify_is_constant_time_and_exact(tmp_path, monkeypatch):
    store = TokenStore(tmp_path)
    good = store.sign(b"message")
    seen = []
    real = token_store.hmac.compare_digest

    def spy(a, b):
        seen.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(token_store.hmac, "compare_digest", spy)
    assert store.verify(b"message", good)
    assert not store.verify(b"message!", good)
    assert not store.verify(b"message", good[:-1] + ("0" if good[-1] != "0" else "1"))
    assert not store.verify(b"message", "")
    assert not store.verify(b"message", None)
    assert not store.verify(b"message", "é" * 64)
    assert not store.verify(b"message", store.value())  # the token itself is never a credential
    assert seen and all(kinds == (bytes, bytes) for kinds in seen)


def test_the_token_is_never_in_repr(tmp_path):
    store = TokenStore(tmp_path)
    token = store.value()
    assert token not in repr(store) and token not in str(store)


def test_an_unwritable_folder_is_an_error_not_a_crash(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    store = TokenStore(blocker / "phone-link")
    assert store.value() is None
    assert store.error
    assert store.sign(b"m") is None and not store.verify(b"m", "a" * 64)


@pytest.mark.parametrize("presented", ["Bearer", "Basic abc", " ", "a" * 300, "a" * 64])
def test_garbage_never_verifies(tmp_path, presented):
    assert not TokenStore(tmp_path).verify(b"message", presented)
