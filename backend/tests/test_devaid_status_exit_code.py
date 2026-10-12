"""`devaid_session.py status` must fail when the account is not signed in.

deploy/update.sh prints "DevelopmentAid session is ready" whenever the status
command exits 0. It always exited 0, so the deploy reported a ready session
directly under "signed in  : False".
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "devaid_session.py"


def _load():
    spec = importlib.util.spec_from_file_location("devaid_session_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_status_fails_when_not_signed_in(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "verify_session", lambda: False)
    monkeypatch.setattr(mod, "has_profile", lambda: False)
    assert mod.cmd_status(None) != 0


def test_status_succeeds_when_signed_in(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "verify_session", lambda: True)
    monkeypatch.setattr(mod, "has_profile", lambda: True)
    assert mod.cmd_status(None) == 0
