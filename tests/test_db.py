import pytest

from khipumaq import db
from khipumaq.db import get_database


def test_get_database_connects_as_scoped_user():
    db = get_database()
    assert db.name == "llm_memory"
    # proves auth + access actually work (not just object construction)
    assert isinstance(db.collections(), list)


def test_config_path_environment_override_is_exclusive(tmp_path, monkeypatch):
    missing = tmp_path / "explicit" / "missing.ini"
    xdg_config = tmp_path / "xdg" / "khipumaq" / "db-config.ini"
    xdg_config.parent.mkdir(parents=True)
    xdg_config.write_text("[database]\n", encoding="utf-8")
    monkeypatch.setenv("KHIPUMAQ_CONFIG", str(missing))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(db, "_CHECKOUT_CONFIG", tmp_path / "checkout.ini")

    with pytest.raises(FileNotFoundError) as raised:
        db.config_path()

    assert str(missing) in str(raised.value)
    assert str(xdg_config) not in str(raised.value)


def test_config_path_returns_existing_environment_override(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit" / "db.ini"
    xdg_config = tmp_path / "xdg" / "khipumaq" / "db-config.ini"
    checkout_config = tmp_path / "checkout" / "db-config.ini"
    for path in (explicit, xdg_config, checkout_config):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[database]\n", encoding="utf-8")
    monkeypatch.setenv("KHIPUMAQ_CONFIG", str(explicit))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(db, "_CHECKOUT_CONFIG", checkout_config)

    assert db.config_path() == explicit


def test_config_path_prefers_xdg_config_over_checkout(tmp_path, monkeypatch):
    xdg_config = tmp_path / "xdg" / "khipumaq" / "db-config.ini"
    checkout_config = tmp_path / "checkout" / "db-config.ini"
    xdg_config.parent.mkdir(parents=True)
    checkout_config.parent.mkdir(parents=True)
    xdg_config.write_text("[database]\n", encoding="utf-8")
    checkout_config.write_text("[database]\n", encoding="utf-8")
    monkeypatch.delenv("KHIPUMAQ_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(db, "_CHECKOUT_CONFIG", checkout_config)

    assert db.config_path() == xdg_config


def test_config_path_falls_back_to_checkout(tmp_path, monkeypatch):
    checkout_config = tmp_path / "checkout" / "db-config.ini"
    checkout_config.parent.mkdir(parents=True)
    checkout_config.write_text("[database]\n", encoding="utf-8")
    monkeypatch.delenv("KHIPUMAQ_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(db, "_CHECKOUT_CONFIG", checkout_config)

    assert db.config_path() == checkout_config


def test_config_path_error_names_every_path_checked(tmp_path, monkeypatch):
    xdg_config = tmp_path / "xdg" / "khipumaq" / "db-config.ini"
    checkout_config = tmp_path / "checkout" / "db-config.ini"
    monkeypatch.delenv("KHIPUMAQ_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(db, "_CHECKOUT_CONFIG", checkout_config)

    with pytest.raises(FileNotFoundError) as raised:
        db.config_path()

    message = str(raised.value)
    assert str(xdg_config) in message
    assert str(checkout_config) in message
