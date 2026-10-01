import pytest


@pytest.fixture(autouse=True)
def isolated_match_cache(tmp_path, monkeypatch):
    """Never read or write the real ~/.musicdl/matches.json in tests."""
    import musicdl.job

    monkeypatch.setattr(musicdl.job, "DEFAULT_CACHE", tmp_path / "matches.json")


@pytest.fixture(autouse=True)
def always_online(monkeypatch):
    """Tests never probe the real network (the cloud sandbox has none)."""
    import musicdl.network

    monkeypatch.setattr(musicdl.network, "is_online", lambda timeout=3.0: True)
