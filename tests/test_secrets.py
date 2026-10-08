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


def test_round_trip_stores_the_credential_as_entered(tmp_path):
    path = tmp_path / "secrets.json"
    save_secrets(Secrets(simplefin_access_url="https://user:pw@bridge.example/access"), path)

    loaded = load_secrets(path)

    assert loaded.simplefin_access_url == "https://user:pw@bridge.example/access"
    assert json.loads(path.read_text())["simplefin_access_url"].startswith("https://")


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
    assert SECRET_KEYS == ("simplefin_access_url",)


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
