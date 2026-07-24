"""Hot-path applicator: append approved rules, cache, and degrade safely."""

from types import SimpleNamespace

import pytest

from app.services.refinement import store


def _settings(**over):
    base = dict(
        refinement_enabled=True,
        refinement_scope="global",
        refinement_cache_ttl_seconds=300,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _clear_cache():
    store.invalidate()
    yield
    store.invalidate()


def test_no_rules_returns_system_unchanged(monkeypatch):
    monkeypatch.setattr(store, "get_settings", lambda: _settings())
    monkeypatch.setattr(store.instructions_db, "list_for_scope", lambda scope: [])
    assert store.augment_system("PersonalInfoAgent", "SYSTEM") == "SYSTEM"


def test_active_rules_are_appended(monkeypatch):
    monkeypatch.setattr(store, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        store.instructions_db, "list_for_scope",
        lambda scope: [{
            "agent": "PersonalInfoAgent", "status": "active",
            "rules": ["Strip credential suffixes from full_name.", "Keep email verbatim."],
        }],
    )
    out = store.augment_system("PersonalInfoAgent", "SYSTEM")
    assert "SYSTEM" in out
    assert "LEARNED RULES" in out
    assert "Strip credential suffixes from full_name." in out
    assert "Keep email verbatim." in out
    # A different agent gets nothing.
    assert store.augment_system("WorkExperienceAgent", "S2") == "S2"


def test_pending_and_disabled_rules_are_not_applied(monkeypatch):
    monkeypatch.setattr(store, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        store.instructions_db, "list_for_scope",
        lambda scope: [
            {"agent": "A", "status": "pending", "rules": ["r1"]},
            {"agent": "B", "status": "disabled", "rules": ["r2"]},
        ],
    )
    assert store.augment_system("A", "S") == "S"
    assert store.augment_system("B", "S") == "S"


def test_disabled_feature_is_a_noop(monkeypatch):
    called = {"n": 0}

    def _list(scope):
        called["n"] += 1
        return [{"agent": "PersonalInfoAgent", "status": "active", "rules": ["r"]}]

    monkeypatch.setattr(store, "get_settings", lambda: _settings(refinement_enabled=False))
    monkeypatch.setattr(store.instructions_db, "list_for_scope", _list)
    assert store.augment_system("PersonalInfoAgent", "SYSTEM") == "SYSTEM"
    assert called["n"] == 0  # never even hits the DB when disabled


def test_snapshot_is_cached_within_ttl(monkeypatch):
    calls = {"n": 0}

    def _list(scope):
        calls["n"] += 1
        return [{"agent": "PersonalInfoAgent", "status": "active", "rules": ["r"]}]

    monkeypatch.setattr(store, "get_settings", lambda: _settings())
    monkeypatch.setattr(store.instructions_db, "list_for_scope", _list)

    for _ in range(5):
        store.augment_system("PersonalInfoAgent", "S")
    assert calls["n"] == 1  # one load, then served from cache

    store.invalidate()
    store.augment_system("PersonalInfoAgent", "S")
    assert calls["n"] == 2  # reloads after invalidation


def test_db_error_degrades_to_no_rules(monkeypatch):
    def _boom(scope):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(store, "get_settings", lambda: _settings())
    monkeypatch.setattr(store.instructions_db, "list_for_scope", _boom)
    # Must never raise on the parse hot path.
    assert store.augment_system("PersonalInfoAgent", "SYSTEM") == "SYSTEM"
