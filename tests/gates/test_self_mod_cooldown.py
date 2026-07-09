"""Tests for the persistent per-file self-modification cooldown."""
from gates.self_mod_cooldown import SelfModCooldown


def test_fresh_file_not_cooling(tmp_path):
    c = SelfModCooldown(db_path=tmp_path / "cooldown.db", cooldown_hours=1.0)
    assert c.is_cooling("some/file.py") is False
    assert c.remaining_seconds("some/file.py") == 0.0


def test_recorded_file_is_cooling_until_window_elapses(tmp_path):
    c = SelfModCooldown(db_path=tmp_path / "cooldown.db", cooldown_hours=1.0)
    c.record("some/file.py", now=1000.0)
    assert c.is_cooling("some/file.py", now=1000.0) is True
    assert c.remaining_seconds("some/file.py", now=1000.0) == 3600.0
    assert c.is_cooling("some/file.py", now=1000.0 + 3601) is False


def test_cooldown_survives_a_new_instance_same_db(tmp_path):
    db_path = tmp_path / "cooldown.db"
    SelfModCooldown(db_path=db_path, cooldown_hours=2.0).record("a.py", now=500.0)
    # Fresh instance, same db — restart-durable.
    c2 = SelfModCooldown(db_path=db_path, cooldown_hours=2.0)
    assert c2.is_cooling("a.py", now=500.0) is True
