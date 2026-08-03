"""Test fixtures.

The suite must not care whether someone has run `npm run build` — otherwise
results flip depending on whether `hub_server/www` happens to exist. Point the
frontend directory at nothing by default; the two tests that exercise bundle
hosting override it explicitly.
"""

from __future__ import annotations

import pytest

import hub_server.main as main_module


@pytest.fixture(autouse=True)
def no_frontend_bundle(tmp_path_factory, monkeypatch):
    missing = tmp_path_factory.mktemp("no-bundle") / "www"
    monkeypatch.setattr(main_module, "FRONTEND_DIR", missing)
