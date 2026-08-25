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


def cmd_push(args) -> int:
    """export -> scp -> remote import, in one step.

    The three-command dance (export, scp, ssh import) is the part people get
    wrong or skip, and a half-done handoff looks exactly like a working one
    until a scrape returns nothing. Doing it as a single command means the
    session either lands and verifies, or says why it did not.

    The session file is a live credential, so the local copy is written to a
    temporary file with owner-only permissions and deleted afterwards — on both
    machines, whether or not the transfer succeeded.
    """
    import subprocess
    import tempfile

    try:
        state = export_session_state()
    except Exception as exc:
        print(f"Nothing to push — could not export this machine's session: {exc}",
              file=sys.stderr)
        print("Connect the account here first (dashboard -> Connect account).",
              file=sys.stderr)
        return 1

    remote_tmp = "/tmp/devaid_session_push.json"
    fd, local = tempfile.mkstemp(prefix="devaid_session_", suffix=".json")
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        try:
            Path(local).chmod(0o600)
        except OSError:
            pass

        print(f"Exported {len(state.get('cookies', []))} cookies. "
              f"Copying to {args.host}…")
        scp = subprocess.run(["scp", "-q", local, f"{args.host}:{remote_tmp}"])
        if scp.returncode != 0:
            print("scp failed — check the host and your SSH key.", file=sys.stderr)
            return scp.returncode

        # Run the remote import through the server's own venv, then remove the
        # copied credential whatever the import did.
        remote_cmd = (
            f"cd {args.remote} && "
            f"{args.python} scripts/devaid_session.py import {remote_tmp}; "
            f"rc=$?; rm -f {remote_tmp}; exit $rc"
        )
        print("Installing and verifying on the remote…")
        rc = subprocess.run(["ssh", args.host, remote_cmd]).returncode
        if rc == 0:
            print("\nDone — the server is using your session.")
        else:
            print("\nThe remote import did not confirm a signed-in session "
                  "(see its output above).", file=sys.stderr)
        return rc
    finally:
        try:
            Path(local).unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export", help="print this machine's session as JSON").set_defaults(fn=cmd_export)
    imp = sub.add_parser("import", help="install a session from a file or stdin")
    imp.add_argument("file", nargs="?", help="path to devaid_session.json (default: stdin)")
    imp.set_defaults(fn=cmd_import)
    sub.add_parser("status", help="report whether this machine has a working session").set_defaults(fn=cmd_status)
    push = sub.add_parser("push", help="export from here and install on the server, in one step")
    push.add_argument("--host", default="ubuntu@15.207.68.78", help="ssh target")
    push.add_argument("--remote", default="~/Deployment/lead-opportunity-platform/backend",
                      help="backend directory on the server")
    push.add_argument("--python", default="./.venv/bin/python",
                      help="python to use on the server")
    push.set_defaults(fn=cmd_push)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
