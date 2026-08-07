"""Move a DevelopmentAid session between machines from the terminal.

No browser window opens on the server, and no credentials are typed anywhere.
You log in once on a machine that has a screen; only the resulting session —
cookies and localStorage — travels.

    # on your PC, after clicking "Connect account" in the dashboard
    python scripts/devaid_session.py export > devaid_session.json

    # copy that file to the server, then there:
    python scripts/devaid_session.py import devaid_session.json
    python scripts/devaid_session.py status

`import` accepts a path or reads stdin, so this also works in one line:

    ssh ubuntu@host 'cd ... && python scripts/devaid_session.py import' \
        < devaid_session.json

Why not just take an email and password here? Their login is behind reCAPTCHA,
so a scripted sign-in is blocked by design, and attempting it is what their
terms restrict — it would risk the account rather than save time. The human
step stays human; only its result is portable.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scrapers.devaid_auth import (  # noqa: E402
    SESSION_FILE,
    export_session_state,
    has_profile,
    import_session_state,
    verify_session,
)


def cmd_export(_args) -> int:
    """Print the session as JSON on stdout, so it can be piped or redirected."""
    try:
        state = export_session_state()
    except Exception as exc:
        print(f"Could not export: {exc}", file=sys.stderr)
        return 1
    json.dump(state, sys.stdout)
    print(f"\nExported {len(state['cookies'])} cookies.", file=sys.stderr)
    return 0


def cmd_import(args) -> int:
    raw = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    try:
        count = import_session_state(json.loads(raw))
    except json.JSONDecodeError:
        print("That is not valid JSON — expected the file produced by 'export'.",
              file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Installed {count} cookies -> {SESSION_FILE}")
    # Verify rather than assume: an expired session is still well-formed JSON,
    # and reporting success on one would put us right back to a dashboard that
    # claims a working account while every scrape returns nothing.
    print("Verifying against the live site…")
    if verify_session():
        print("Connected. DevelopmentAid scraping will use this session.")
        return 0
    print(
        "Installed, but the site did not confirm a signed-in session.\n"
        "Log in again on the machine with a screen, re-export, and re-import.",
        file=sys.stderr,
    )
    return 2


def cmd_status(_args) -> int:
    print(f"session file : {SESSION_FILE}")
    print(f"  exists     : {SESSION_FILE.exists()}")
    print(f"  connected  : {has_profile()}")
    print("checking the live site…")
    print(f"  signed in  : {verify_session()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export", help="print this machine's session as JSON").set_defaults(fn=cmd_export)
    imp = sub.add_parser("import", help="install a session from a file or stdin")
    imp.add_argument("file", nargs="?", help="path to devaid_session.json (default: stdin)")
    imp.set_defaults(fn=cmd_import)
    sub.add_parser("status", help="report whether this machine has a working session").set_defaults(fn=cmd_status)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
