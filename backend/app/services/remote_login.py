"""Short-lived DevelopmentAid desktop. Nothing listens on a public interface.

One API worker owns the desktop (as required by deploy/gunicorn.conf.py).
The API authenticates and relays RFB bytes; no separate websockify service,
public VNC port, SSH tunnel, or access to the server's existing desktop.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.config import settings

NOVNC = Path("/usr/share/novnc")
PROFILE_SETTLE_SECONDS = 2

# The verifier is a separate process so it can open the same profile only after
# the visible Chrome window has released it. Its exit code tells the visible
# login flow what the site actually showed.
VERIFY_SIGNED_OUT = 3
VERIFY_CHALLENGE = 4
VERIFY_UNCLEAR = 5


def available() -> bool:
    return (sys.platform.startswith("linux") and NOVNC.joinpath("core/rfb.js").is_file()
            and bool(shutil.which("Xvfb")) and bool(shutil.which("x11vnc"))
            and bool(shutil.which("google-chrome")))


def login_seconds(now: datetime | None = None) -> float:
    """Never hold the scraper's profile into its configured nightly window."""
    start, end = settings.scrape_window_start_at, settings.scrape_hard_stop_at
    if not start or not end:
        return 900
    now = now or datetime.now(ZoneInfo(settings.scrape_window_timezone))
    now = now.astimezone(ZoneInfo(settings.scrape_window_timezone))
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    minute, a, b = now.hour * 60 + now.minute, sh * 60 + sm, eh * 60 + em
    blocked = (minute >= a or minute < b) if a >= b else a <= minute < b
    if blocked:
        raise RuntimeError(f"Connect is unavailable during the {start}–{end} "
                           f"scrape window ({settings.scrape_window_timezone}).")
    next_start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if next_start <= now:
        next_start += timedelta(days=1)
    # Reserve a minute for Chrome to flush and release the profile.
    seconds = min(900, (next_start - now).total_seconds() - 60)
    if seconds < 30:
        raise RuntimeError("The nightly scrape starts shortly. Connect after the scrape window.")
    return seconds


@dataclass
class Desktop:
    owner: str
    id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    state: str = "starting"
    message: str = "Opening the login window…"
    port: int = 0
    attached: bool = False
    finish: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    deadline: float = 0

    def public(self):
        return {"id": self.id, "state": self.state, "message": self.message,
                "viewer_url": f"{settings.api_prefix}/remote-login/{self.id}/viewer"}


desktop: Desktop | None = None


def active() -> bool:
    return desktop is not None and desktop.task is not None and not desktop.task.done()


def require_profile_free() -> None:
    if active():
        raise RuntimeError("A DevelopmentAid login window is open. Finish or cancel it first.")


def _profile_processes(profile: Path, proc_root: Path = Path("/proc")) -> list[int]:
    """Linux profile owners, using exact arguments rather than a process regex.

    An unreadable process owned by this service user is uncertain, not proof
    that Chrome is absent. Never clear a lock when inspection is incomplete.
    """
    owners = []
    try:
        entries = list(proc_root.iterdir())
        uid = os.getuid()
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                args = [os.fsdecode(a) for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
            except (FileNotFoundError, ProcessLookupError):
                continue  # Process exited while inspecting it.
            if not args or not any(n in Path(args[0]).name.lower() for n in ("chrome", "chromium")):
                continue
            for index, arg in enumerate(args):
                raw = arg.partition("=")[2] if arg.startswith("--user-data-dir=") else (
                    args[index + 1] if arg == "--user-data-dir" and index + 1 < len(args) else "")
                if raw and Path(raw).resolve() == profile.resolve():
                    owners.append(int(entry.name))
                    break
    except (OSError, AttributeError) as exc:
        raise RuntimeError("Could not check whether Chrome owns the profile. "
                           "Its lock files were kept; check the server processes.") from exc
    # /proc iteration order is filesystem-dependent. Stable PID ordering keeps
    # diagnostics and the Linux deployment test deterministic.
    return sorted(owners)


def _recover_profile_locks(profile: Path) -> None:
    """Remove only stale Chrome lock entries after verifying the owner is gone."""
    names = ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile")
    locked = [profile / name for name in names
              if (profile / name).exists() or (profile / name).is_symlink()]
    if not locked:
        return
    try:
        snapshots = {entry: entry.lstat() for entry in locked}
    except OSError as exc:
        raise RuntimeError("Profile lock files changed during inspection. Try reconnecting again.") from exc
    owners = _profile_processes(profile)
    if owners:
        raise RuntimeError("The DevelopmentAid profile is open in Chrome (PID "
                           + ", ".join(map(str, owners))
                           + "). Close that Chrome window before reconnecting.")
    singleton = profile / "SingletonLock"
    if singleton.exists() or singleton.is_symlink():
        try:
            host, raw_pid = os.readlink(singleton).rsplit("-", 1)
            pid = int(raw_pid)
            if pid <= 0 or host != socket.gethostname():
                raise ValueError("Unrecognised owner")
        except (OSError, ValueError) as exc:
            raise RuntimeError("The profile lock's owner could not be verified. "
                               "The lock was kept; inspect SingletonLock on EC2.") from exc
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass  # Recorded browser owner has exited.
        except OSError as exc:
            raise RuntimeError("The profile lock's process could not be checked. Lock kept.") from exc
        else:
            raise RuntimeError(f"The profile lock names a live process (PID {pid}). "
                               "The lock was kept; check that process first.")
    # Recheck immediately before removal. Normal Connect/scrape starts are
    # already serialized by the API's shared start lock.
    if _profile_processes(profile):
        raise RuntimeError("Chrome opened the DevelopmentAid profile. Close it before reconnecting.")
    try:
        for entry in locked:
            try:
                current = entry.lstat()
            except FileNotFoundError:
                continue
            before = snapshots[entry]
            if (current.st_ino, current.st_mtime_ns, current.st_size) != (
                    before.st_ino, before.st_mtime_ns, before.st_size):
                raise RuntimeError("The profile lock changed during inspection. It was kept.")
            entry.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError("Could not remove a stale profile lock. Check its ownership on EC2.") from exc
    import logging
    logging.getLogger("scraper").info("[devaid] removed stale Chrome lock entries; profile kept")


async def start(owner: str) -> Desktop:
    global desktop
    from app.services.scraper_manager import manager
    from app.scrapers import devaid_auth

    # Same lock as manual/scheduled scrape starts: neither can slip between
    # the other's idle check and reservation.
    async with manager._start_lock:
        require_profile_free()
        if manager.state != "idle":
            raise RuntimeError("A scrape is running. Connect after it finishes.")
        seconds = login_seconds()
        if not available():
            raise RuntimeError("Browser login is not installed. Run deploy/setup-browser-login.sh on EC2.")
        if devaid_auth._own_chrome_dir():
            raise RuntimeError("Remote login requires the dedicated DevelopmentAid profile. "
                               "Remove the personal Chrome profile override on this server first.")
        await asyncio.to_thread(_recover_profile_locks, devaid_auth.PROFILE_DIR)
        desktop = Desktop(owner=owner, deadline=time.monotonic() + seconds)
        desktop.task = asyncio.create_task(_run(desktop))
        return desktop


async def _stop_process(proc, *, group: bool = False, graceful: bool = False):
    if proc is None:
        return
    def send(sig):
        try:
            if group and not graceful and os.name == "posix":
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except ProcessLookupError:
            pass
    if group and not graceful:
        # The parent may already have exited while leaving a browser helper.
        send(signal.SIGTERM)
    if proc.returncode is not None:
        return
    if not group or graceful:
        send(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        # Chrome was launched in its own group, so stuck helper processes can
        # be stopped as well. This never targets another user's browser.
        if group and os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            proc.kill()
        await proc.wait()


async def _run(s: Desktop):
    try:
        async with asyncio.timeout(max(1, s.deadline - time.monotonic())):
            await _run_desktop(s)
    except TimeoutError:
        s.state, s.message = "expired", "The login window ended. Click Connect to try again."


async def _run_desktop(s: Desktop):
    from app.scrapers import devaid_auth
    xvfb = vnc = chrome = verifier = None
    try:
        xvfb = await asyncio.create_subprocess_exec(
            "Xvfb", "-displayfd", "1", "-screen", "0", "1400x900x24", "-nolisten", "tcp",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        number = (await asyncio.wait_for(xvfb.stdout.readline(), 10)).decode().strip()
        if not number.isdigit():
            raise RuntimeError("Could not start the temporary display.")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            s.port = sock.getsockname()[1]
        vnc = await asyncio.create_subprocess_exec(
            "x11vnc", "-display", f":{number}", "-localhost", "-rfbport", str(s.port),
            "-nopw", "-forever", "-shared", "-quiet",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        devaid_auth.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        for _ in range(50):
            if vnc.returncode is not None:
                raise RuntimeError("The remote viewer could not start.")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", s.port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("The remote viewer did not become ready.")
        prompt = "Sign in fully, wait until your account page appears, then click Done."
        checks = 0
        while not s.cancel.is_set() and time.monotonic() < s.deadline:
            s.finish.clear()
            # Match the working TigerVNC command. No Playwright driver, debugging
            # port, injected scripts or extra browser flags during human sign-in.
            chrome = await asyncio.create_subprocess_exec(
                "google-chrome", f"--user-data-dir={devaid_auth.PROFILE_DIR}",
                "--no-first-run", devaid_auth.LOGIN_URL,
                env={**os.environ, "DISPLAY": f":{number}"}, start_new_session=True,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            s.state, s.message = "ready", prompt

            while (not s.finish.is_set() and not s.cancel.is_set()
                   and time.monotonic() < s.deadline):
                if xvfb.returncode is not None or vnc.returncode is not None:
                    raise RuntimeError("The temporary desktop stopped unexpectedly.")
                if chrome.returncode is not None:
                    if chrome.returncode != 0:
                        raise RuntimeError("Chrome exited before login could be saved.")
                    s.finish.set()  # Native window closed: verify, never assume login.
                    break
                await asyncio.sleep(0.25)

            if s.cancel.is_set():
                s.state, s.message = "cancelled", "Login cancelled."
                break
            if not s.finish.is_set():
                s.state, s.message = "expired", "The login window ended. Click Connect to try again."
                break

            checks += 1
            s.state, s.message = "saving", "Waiting for Chrome to save, then checking the signed-in session…"
            await _stop_process(chrome, group=True, graceful=True)
            chrome = None
            if s.cancel.is_set():
                s.state, s.message = "cancelled", "Login cancelled."
                break
            # Chrome writes cookies and preferences asynchronously even after
            # its window disappears. Let those writes settle before verification.
            await asyncio.sleep(PROFILE_SETTLE_SECONDS)

            verifier = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.services.remote_login", "--verify",
                cwd=str(devaid_auth.PROFILE_DIR.parent.parent),
                env={**os.environ, "DISPLAY": f":{number}"}, start_new_session=True,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            while verifier.returncode is None and not s.cancel.is_set():
                await asyncio.sleep(0.25)
            if s.cancel.is_set():
                s.state, s.message = "cancelled", "Login cancelled."
                break
            result = verifier.returncode
            await _stop_process(verifier, group=True)
            verifier = None
            if result == 0:
                s.state, s.message = "connected", (
                    "DevelopmentAid sign-in was verified and saved. The scraper can now use it.")
                break

            # Reopen the same visible profile for security/MFA/sign-in pages.
            # A person completes those controls; the app never bypasses them.
            if result == VERIFY_CHALLENGE:
                prompt = (f"Security verification appeared during check {checks}. Complete it in "
                          "DevelopmentAid, wait for the signed-in account page, then click Done again.")
            elif result == VERIFY_SIGNED_OUT:
                prompt = (f"DevelopmentAid still showed Sign in after check {checks}. Finish the "
                          "email/password and any verification step, wait for your account page, "
                          "then click Done again.")
            else:
                prompt = (f"Check {checks} could not confirm the page. Wait for DevelopmentAid to "
                          "finish loading or complete any verification shown, then click Done again.")
    except asyncio.CancelledError:
        s.state, s.message = "cancelled", "Login closed."
        raise
    except Exception:
        import logging
        logging.getLogger("scraper").exception("Remote DevelopmentAid login failed")
        s.state, s.message = "error", "Could not open or save the login window. Check the API logs."
    finally:
        # Keep DISPLAY alive until browsers are stopped. Each process group is
        # dedicated to this login; never terminate unrelated Chrome processes.
        await asyncio.gather(_stop_process(verifier, group=True), return_exceptions=True)
        await asyncio.gather(_stop_process(chrome, group=True), return_exceptions=True)
        await asyncio.gather(_stop_process(vnc), _stop_process(xvfb), return_exceptions=True)


async def shutdown():
    if active():
        desktop.task.cancel()
        await asyncio.gather(desktop.task, return_exceptions=True)


if __name__ == "__main__":
    # Used only by the post-login child; ordinary imports never launch Chrome.
    if sys.argv[1:] != ["--verify"]:
        raise SystemExit(2)
    from app.core.logging_config import setup_logging
    from app.scrapers.devaid_auth import LAST_VERIFY, verify_session
    setup_logging()
    if verify_session(dedicated_profile=True, save=True):
        raise SystemExit(0)
    reason = LAST_VERIFY.get("reason")
    raise SystemExit({"guest": VERIFY_SIGNED_OUT,
                      "challenge": VERIFY_CHALLENGE,
                      "blank": VERIFY_UNCLEAR}.get(reason, VERIFY_UNCLEAR))
