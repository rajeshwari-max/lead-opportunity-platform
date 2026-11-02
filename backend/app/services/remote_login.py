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
        pdir = devaid_auth.PROFILE_DIR
        # Never remove a lock owned by another Chrome (including manual VNC).
        if any((pdir / name).exists() or (pdir / name).is_symlink()
               for name in ("SingletonLock", "lockfile")):
            raise RuntimeError("The DevelopmentAid profile is locked. Close its existing Chrome window first.")
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
        # Match the working TigerVNC command. No Playwright driver, debugging
        # port, injected scripts or extra browser flags during human sign-in.
        chrome = await asyncio.create_subprocess_exec(
            "google-chrome", f"--user-data-dir={devaid_auth.PROFILE_DIR}",
            "--no-first-run", devaid_auth.LOGIN_URL,
            env={**os.environ, "DISPLAY": f":{number}"}, start_new_session=True,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
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
        s.state, s.message = "ready", "Sign in, then click Done to save the session."
        while not s.finish.is_set() and not s.cancel.is_set() and time.monotonic() < s.deadline:
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
        elif not s.finish.is_set():
            s.state, s.message = "expired", "The login window ended. Click Connect to try again."
        else:
            s.state, s.message = "saving", "Checking and saving your login."
            # Close Chrome before reading the same profile. SIGTERM first;
            # forced group cleanup is reserved for an unresponsive process.
            await _stop_process(chrome, group=True, graceful=True)
            if s.cancel.is_set():
                s.state, s.message = "cancelled", "Login cancelled."
                return
            # Separate process: the API keeps its scrape reservation throughout
            # verification, and cancellation can also stop Playwright children.
            verifier = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.services.remote_login", "--verify",
                cwd=str(devaid_auth.PROFILE_DIR.parent.parent),
                env={**os.environ, "DISPLAY": f":{number}"}, start_new_session=True,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            while verifier.returncode is None and not s.cancel.is_set():
                await asyncio.sleep(0.25)
            if s.cancel.is_set():
                s.state, s.message = "cancelled", "Login cancelled."
            elif verifier.returncode == 0:
                s.state, s.message = "connected", "DevelopmentAid connected. You can close this tab."
            else:
                s.state, s.message = "unverified", (
                    "Chrome saved its profile, but the scrape-session check could not confirm sign-in. "
                    "A security check can prevent verification even after a successful login. "
                    "Your profile has been kept; check the scraper log for details.")
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
    from app.scrapers.devaid_auth import verify_session
    setup_logging()
    raise SystemExit(0 if verify_session(dedicated_profile=True, save=True) else 3)
