import json
import os
import stat

import pytest

from finance_tool.store.secrets import (
    REDACTED,
    SECRET_KEYS,
    Secrets,
    load_secrets,
    redact,
    save_secrets,
)


def test_load_of_a_missing_file_is_empty(tmp_path):
    assert load_secrets(tmp_path / "secrets.json") == Secrets()


def test_round_trip_keeps_the_credential_but_encrypts_it_at_rest(tmp_path):
    path = tmp_path / "secrets.json"
    credential = "https://user:pw@bridge.example/access"
    save_secrets(Secrets(simplefin_access_url=credential), path)

    loaded = load_secrets(path)

    assert loaded.simplefin_access_url == credential
    stored = json.loads(path.read_text())["simplefin_access_url"]
    assert stored.startswith(("dpapi:", "plain:"))
    # The plaintext must not be lying in the file under any encoding.
    assert credential not in path.read_text()


def test_revoking_is_a_delete_and_leaves_no_residue(tmp_path):
    path = tmp_path / "secrets.json"
    save_secrets(Secrets(simplefin_access_url="https://x"), path)
    assert path.exists()

    save_secrets(Secrets(), path)

    # §13: "a removed credential leaves no residue in the store."
    assert not path.exists()


def test_a_corrupt_secrets_file_is_not_a_credential_and_is_never_logged(tmp_path, capsys):
    path = tmp_path / "secrets.json"
    path.write_text("{ not json", encoding="utf-8")

    loaded = load_secrets(path)

    assert loaded.simplefin_access_url is None
    captured = capsys.readouterr()
    assert "not json" not in captured.out + captured.err


def test_repr_never_contains_the_credential():
    secrets = Secrets(simplefin_access_url="https://user:hunter2@bridge.example/access")
    text = repr(secrets)
    assert "hunter2" not in text
    assert "bridge.example" not in text
    assert "simplefin_access_url" in text
    assert repr(Secrets()) == "Secrets(empty)"


def test_redact_hides_a_present_credential():
    assert redact("https://secret") == REDACTED
    assert redact(None) == ""
    assert redact("") == ""


def test_unknown_secret_keys_are_refused():
    secrets = Secrets()
    with pytest.raises(KeyError):
        secrets.set("bank_password", "hunter2")


def test_there_is_no_field_for_a_bank_password():
    """§13: the bank password is never stored, and never entered into this app."""
    fields = set(Secrets().__dataclass_fields__)
    assert not any("password" in name for name in fields)
    assert set(SECRET_KEYS) == {
        "simplefin_access_url",
        "llm_anthropic", "llm_openai", "llm_deepseek", "llm_openrouter",
        "feed_redbark", "feed_banksync", "app_pin_hash",
    }


def test_a_legacy_plaintext_file_is_still_read(tmp_path):
    """The file predates encryption: refusing it would silently unconfigure a sync."""
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"simplefin_access_url": "https://legacy"}), encoding="utf-8")

    assert load_secrets(path).simplefin_access_url == "https://legacy"


def test_an_undecryptable_value_is_absent_not_an_error(tmp_path):
    """Ciphertext from another Windows account is unconfigured, not a crash."""
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"llm_anthropic": "dpapi:not-base64!!"}), encoding="utf-8")

    loaded = load_secrets(path)

    assert loaded.llm_anthropic is None
    assert loaded.has("llm_anthropic") is False


def test_every_known_key_round_trips_and_can_be_revoked(tmp_path):
    path = tmp_path / "secrets.json"
    secrets = Secrets()
    for number, key in enumerate(SECRET_KEYS):
        secrets.set(key, f"value-{number}")
    save_secrets(secrets, path)

    loaded = load_secrets(path)
    for number, key in enumerate(SECRET_KEYS):
        assert getattr(loaded, key) == f"value-{number}"

    loaded.revoke("llm_openai")
    save_secrets(loaded, path)
    reread = load_secrets(path)
    assert reread.llm_openai is None
    assert reread.llm_anthropic == "value-1"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not meaningful on Windows")
def test_the_file_is_readable_only_by_its_owner(tmp_path):
    path = tmp_path / "secrets.json"
    save_secrets(Secrets(simplefin_access_url="https://x"), path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_extra_keys_survive_a_round_trip_without_being_exported_nowhere():
    secrets = Secrets(extra={"future_key": "value"})
    assert secrets.to_dict() == {"future_key": "value"}
    assert Secrets.from_dict({"future_key": "value"}).extra == {"future_key": "value"}
