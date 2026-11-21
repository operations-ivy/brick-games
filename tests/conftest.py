"""Tests never reach the internet: a source left unstubbed fails loudly
instead of quietly downloading (the manuals' first tests fetched from the
Internet Archive, 2026-11-18). The web tests' own server on 127.0.0.1 is
allowed."""

import urllib.parse
import urllib.request

import pytest

LOCAL = {"127.0.0.1", "localhost"}


@pytest.fixture(autouse=True)
def no_internet(monkeypatch):
    real = urllib.request.urlopen
    reached = []

    def urlopen(url, *args, **kwargs):
        full = url.full_url if isinstance(url, urllib.request.Request) else url
        if urllib.parse.urlsplit(full).hostname not in LOCAL:
            reached.append(full)
            raise OSError(f"a test reached the internet: {full}")
        return real(url, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    yield
    # Checked here, not only raised: the scan swallows source errors by design.
    assert not reached, f"a test reached the internet: {reached}"


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    """Wikidata's pacing (guides.PAUSE) is for the real service, not tests."""
    from brick_games import guides

    monkeypatch.setattr(guides, "PAUSE", 0)
