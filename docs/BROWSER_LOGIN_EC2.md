# DevelopmentAid login from the dashboard

An administrator clicks Connect in Site Logins or Expert Pool. A tab shows
ordinary Google Chrome running on EC2, launched directly in the same way as
the working TigerVNC command. Sign in yourself, then click **Done — save login**.
The platform closes Chrome to release and flush its persistent profile, then
runs a separate scraper-session verification process. Only verified sign-in
saves a fresh session backup and marks the account connected.

No VPN, SSH tunnel or desktop VNC client is needed after setup. Authentication
and human verification on DevelopmentAid remain manual. A site may expire its
session later; Connect renews it but cannot guarantee indefinite sign-in.

## One-time EC2 setup

1. Deploy this code and build the frontend using the existing deployment process.
2. From the repository root, run `bash deploy/setup-browser-login.sh`.
3. In the **existing HTTPS server block** for `leads.catalysts.org`, add the
   `location /api/remote-login/` block from `deploy/nginx-leads.conf`. Preserve
   the certificate and other existing configuration. Do not replace Certbot's
   installed HTTPS configuration with the repository's HTTP-only template.
4. Run `sudo nginx -t` and, only if valid, `sudo systemctl reload nginx`.
5. Keep individual dashboard sign-in enabled (`LOP_PERSONAL_LOGIN=true`) and
   the existing single API worker. No security-group or VNC ports need opening.

The API starts its own Xvfb and loopback-only x11vnc processes for each login.
Google Chrome must be installed (`google-chrome --version`). The interactive
browser uses only `--user-data-dir=<devaid_profile> --no-first-run <login-url>`;
Playwright is not attached to the browser while the person signs in. Its later
verification process reads that dedicated profile explicitly, even if an older
uploaded session file exists. Verification can still encounter a site security
check; a saved native profile alone is not reported as a verified connection.
It does not connect to, replace, or reveal an existing VNC desktop. It needs no
DISPLAY setting or persistent desktop service. Use the dedicated DevelopmentAid
profile; personal Chrome profile overrides on EC2 are rejected with an error.
If a manual Chrome still owns that profile, close it before Connect. Lock files
are never removed by this feature.

## Verification after deployment

- Open the HTTPS dashboard as an admin during 08:00–22:00 Asia/Kolkata (or the
  configured scrape window). Click Connect and allow the new browser tab.
- Confirm DevelopmentAid is visible and the WebSocket returns 101 in browser
  developer tools. A 404 indicates an old backend; a failed WebSocket usually
  indicates the nginx upgrade route is missing.
- Complete the site's login yourself, click Done, and confirm both tabs report
  connected. Run the usual manual DevelopmentAid scrape to check reuse of the
  EC2 session. This step requires the real account and EC2 environment.
- Verify a non-admin cannot open the viewer. Cancel or close an unfinished
  viewer and confirm a later Connect works after cleanup.

The login expires after at most 15 minutes total, even if the tab is abandoned.
It closes earlier before the nightly scrape boundary. Connect is refused during
the configured nightly window or while any scrape is active; scrape starts and
other DevelopmentAid profile opens are refused while a remote login is active.
Both HTTP and WebSocket access check the administrator's live role and session
ownership. The WebSocket also checks Origin and periodically rechecks the role.
No credentials or bearer tokens are put into viewer URLs. Session cookies are
reused from the dashboard. The viewer loads no external scripts.

This implementation does not change nightly sign-in detection, NGOBOX speed,
brand searches, or send expiry notification emails. Those are separate tasks.
Windows retains the existing local Chrome login and session-transfer options.
Docker images need the same OS packages added before using remote login.
