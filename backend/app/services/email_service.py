"""Email service — sends opportunity digests over SMTP (Gmail App Password friendly)."""
from __future__ import annotations

import logging
import smtplib
from datetime import date
from html import escape
from urllib.parse import quote_plus
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings
from app.database.models import Opportunity, TeamMember
from app.services import geo_priority
from app.services.links import link_kind, resolve_link

log = logging.getLogger("scraper")


class EmailNotConfiguredError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(settings.smtp_user and settings.smtp_password)


def _deadline_cell(o: Opportunity) -> str:
    """Deadline plus urgency. A third of live opportunities are rolling and have
    no date at all — rendering those as an em dash read as missing data rather
    than as 'no closing date', which is what it actually means."""
    from datetime import date

    if o.deadline is None:
        return ('<span style="color:#059669;font-weight:600;">Ongoing</span>'
                '<div style="color:#9ca3af;font-size:11px;">no closing date</div>')
    left = (o.deadline - date.today()).days
    colour = "#dc2626" if left <= 3 else "#ea580c" if left <= 7 else \
             "#d97706" if left <= 14 else "#6b7280"
    urgency = ("closes today" if left == 0 else
               f"{left} days left" if left > 0 else "closed")
    return (f'<span style="font-weight:600;">{o.deadline.strftime("%d %b %Y")}</span>'
            f'<div style="color:{colour};font-size:11px;font-weight:600;">{urgency}</div>')


def _type_cell(o: Opportunity) -> str:
    """Research/Implementation first, then Grant/RFP/Tender.

    The category says what kind of document this is; the work type says which
    team should pick it up, and that is the decision the reader is actually
    making. So it leads, and the category becomes the supporting detail.
    """
    wt = (o.work_type or "").strip()
    colour = {"Research": "#7c3aed", "Implementation": "#0284c7"}.get(wt, "")
    lines = []
    if wt:
        lines.append(
            f'<span style="display:inline-block;background:{colour}1a;color:{colour};'
            f'border-radius:9999px;padding:2px 8px;font-size:11px;font-weight:600;">{wt}</span>'
        )
    lines.append(f'<div style="color:#6b7280;margin-top:3px;">{o.category.value}</div>')
    if (o.study_type or "").strip():
        lines.append(f'<div style="color:#9ca3af;font-size:11px;">{o.study_type}</div>')
    return "".join(lines)


# Sections appear in this order regardless of size, so the same email always
# reads the same way. Anything unrecognised falls to the end under "Other".
#
# The leading entries are no longer written here: the home region order comes
# from settings (see geo_priority), and this list supplies only the tail. Two
# hard-coded orderings — one here, one in geo_priority — would drift, and the
# first symptom would be an email whose sections disagree with the order the
# matcher chose.
_REGION_TAIL = [
    "Southeast Asia", "East Asia", "Central Asia", "Middle East",
    "Africa", "Europe", "North America", "Latin America", "Oceania", "Global",
]
_UNPLACED = "Other / unspecified"


def _region_order(priority) -> list[str]:
    """Configured home regions first, then the fixed tail, no repeats."""
    ordered = [r for r in priority.regions]
    ordered += [r for r in _REGION_TAIL if r not in ordered]
    return ordered

# Gmail clips any message over ~102 KB behind a "[Message clipped]" link, and
# other clients get slow or refuse outright. A row costs roughly 1.7 KB with its
# summary, so an unrestricted digest of everything live (~9,900 rows) renders at
# about 16 MB and would arrive unreadable.
#
# Two levers, applied only when they're needed so a small digest is unaffected:
#   * compact rows  — drop the summary and vertical chips past _COMPACT_ABOVE
#   * per-region cap — list the soonest N per region and say how many remain
#
# The cap is always stated in the email. An earlier version of this system
# silently truncated at 100, so a member with 900 matches was told "100 new" and
# given 100 with nothing to indicate the rest existed. Never again: the counts
# in the index and the section headings are the true totals.
_COMPACT_ABOVE = 40
# Tried in order until the rendered email fits _SIZE_BUDGET. Guessing a single
# constant doesn't work: a row costs ~1.4 KB, but how many rows a cap yields
# depends on how many regions the member's matches span, which varies per
# member and per day. Rendering and measuring is exact where arithmetic is a
# guess, and each attempt is only a string build.
_CAP_LADDER = (25, 15, 10, 6, 4, 2)
_SIZE_BUDGET = 95 * 1024      # Gmail clips at ~102 KB; leave headroom for MIME


def wrike_url(opportunity_id: int) -> str:
    """Opens the dashboard's "Add opportunity to Wrike" dialog for one row.

    A link to the DASHBOARD, never one that creates the task itself. Mail
    clients and corporate link scanners fetch every URL in a message before
    anyone clicks; a GET that created a task would file tasks for every row of
    every digest on delivery. The dialog shows the destination folder, offers
    assignees and asks "Create this Wrike task?" before anything is sent.
    A fragment (#wrike=<id>), not a query parameter: the dashboard reads query
    parameters as filter links, and a fragment is never sent to the server.
    Sign-in keeps it (LoginScreen.tsx).
    """
    base = (settings.dashboard_url or "").rstrip("/")
    return f"{base}/?view=user#wrike={int(opportunity_id)}"


def _wrike_cell(o: Opportunity) -> str:
    """The per-row action, replacing the retired Approve button.

    A table rather than a styled <a> because Outlook ignores padding on inline
    anchors and would collapse the button to bare text — the same reason the
    Approve button was built this way.
    """
    return (
        '<table cellpadding="0" cellspacing="0" style="border-collapse:separate;">'
        '<tr><td style="background:#4f46e5;border-radius:6px;">'
        f'<a href="{_text(wrike_url(o.id))}" style="display:inline-block;padding:7px 12px;'
        'color:#ffffff;font-size:12px;font-weight:600;text-decoration:none;'
        'white-space:nowrap;">Add to Wrike</a>'
        '</td></tr></table>'
    )


def _show_wrike() -> bool:
    """Only offer the button where the server can actually create tasks;
    otherwise every click lands on "Wrike is not enabled on this server"."""
    return bool(getattr(settings, "wrike_enabled", False))


def _text(value: object) -> str:
    """Escape source-owned text before it is inserted into an HTML email."""
    return escape(str(value or ""), quote=True)


def _short(value: object, limit: int) -> str:
    """Collapse scraped prose and trim it without cutting the final word."""
    value = " ".join(str(value or "").split())
    if len(value) <= limit:
        return _text(value)
    shortened = value[:limit].rsplit(" ", 1)[0] or value[:limit]
    return _text(shortened + "…")


def _labels(value: str, empty: str = "Not classified") -> str:
    labels = [part.strip() for part in (value or "").split(",") if part.strip()]
    return _text(", ".join(labels) if labels else empty)


def _group_for_digest(opportunities: list[Opportunity]) -> list[tuple[str, list[Opportunity]]]:
    """Home countries first, then regions, soonest deadline inside each bucket.

    A single flat list of several hundred rows can only be read by scrolling
    through all of it, so the digest has always been sectioned. It sectioned by
    region alone, and that had a specific failure: India and Bangladesh and
    Nepal all landed in one "South Asia" block, and because each section is
    capped (see _CAP_LADDER) an Indian call could be pushed below the cap by
    Nepalese ones and never appear at all. For a team that bids in India, that
    is the worst row to hide.

    So the configured home countries are lifted out into their own sections
    above the regions. Everything else groups by region exactly as before —
    country-level sections for the whole world would replace one long list with
    a 220-entry index.
    """
    priority = geo_priority.load()
    country_names = list(priority.countries)
    # casefolded name -> the display spelling, so "india" and "INDIA" land in
    # the same bucket and the heading still reads "India".
    home = {name.casefold(): name for name in country_names}

    buckets: dict[str, list[Opportunity]] = {}
    for o in opportunities:
        key = home.get((o.country or "").strip().casefold())
        if key is None:
            key = (o.region or "").strip() or _UNPLACED
        buckets.setdefault(key, []).append(o)

    for items in buckets.values():
        # Ongoing calls (no deadline) sort last: they can be acted on any time,
        # so they shouldn't push a closing deadline down the page.
        items.sort(key=lambda x: (x.deadline is None, x.deadline or date.max))

    ordered = [(c, buckets.pop(c)) for c in country_names if c in buckets]
    ordered += [(r, buckets.pop(r)) for r in _region_order(priority) if r in buckets]
    # Anything not in the known list, then the unplaced bucket, always last.
    tail = sorted((r for r in buckets if r != _UNPLACED))
    ordered += [(r, buckets[r]) for r in tail]
    if _UNPLACED in buckets:
        ordered.append((_UNPLACED, buckets[_UNPLACED]))
    return ordered


def _opportunity_email_html(
    member: TeamMember,
    opportunities: list[Opportunity],
    *,
    reminder_days: int | None = None,
) -> str:
    """Render digests and reminders with one responsive, size-safe UI."""
    groups = _group_for_digest(opportunities)
    n = len(opportunities)
    if n <= _COMPACT_ABOVE:
        html = _render_digest(
            member, groups, n, compact=False, cap=None,
            reminder_days=reminder_days,
        )
        if len(html) <= _SIZE_BUDGET:
            return html

    for cap in _CAP_LADDER:
        html = _render_digest(
            member, groups, n, compact=True, cap=cap,
            reminder_days=reminder_days,
        )
        if len(html) <= _SIZE_BUDGET:
            return html
    return html          # smallest cap still over budget — send it anyway


def _digest_html(member: TeamMember, opportunities: list[Opportunity]) -> str:
    """Grouped digest, rendered small enough that no mail client clips it."""
    return _opportunity_email_html(member, opportunities)


def _reminder_html(
    member: TeamMember, opportunities: list[Opportunity], days_before: int
) -> str:
    """A deadline reminder using the same UI and detail set as the digest."""
    return _opportunity_email_html(
        member, opportunities, reminder_days=days_before,
    )


def _render_digest(
    member: TeamMember,
    groups: list[tuple[str, list[Opportunity]]],
    n: int,
    compact: bool,
    cap: int | None,
    reminder_days: int | None = None,
) -> str:

    # Contents strip: counts per region, each an anchor link. Gives the reader
    # the shape of the email before any scrolling, and a way to jump straight to
    # the region they own.
    # Each chip jumps to that section further down the same email — no new tab,
    # no leaving the inbox.
    #
    # The target is an <a name="..."> immediately before each heading, not an
    # id on the heading itself. Gmail strips class and id from every element but
    # preserves href, style, title and the name attribute on anchors, so an
    # id-based jump silently does nothing there. `name` is deprecated HTML and
    # is used here for exactly that reason.
    chips = []
    for i, (region, items) in enumerate(groups):
        pill = ("display:inline-block;border-radius:9999px;padding:4px 11px;"
                "font-size:12px;margin:0 6px 6px 0;text-decoration:none;")
        chips.append(
            f'<a href="#r{i}" style="{pill}background:#eef2ff;color:#3730a3;">'
            f'{_text(region)} <b>{len(items)}</b></a>'
        )
    index = " ".join(chips)

    capped = cap is not None

    sections = []
    for i, (region, items) in enumerate(groups):
        shown = items[:cap] if cap is not None else items
        hidden = len(items) - len(shown)
        more = (
            f'<tr><td colspan="{4 if _show_wrike() else 3}" style="padding:10px;background:#f8fafc;color:#475569;'
            f'font-size:12px;">+{hidden} more in {_text(region)}, soonest deadlines shown first — '
            f'<a href="{_text(settings.dashboard_url)}" style="color:#4f46e5;">see them all in the '
            f'dashboard</a></td></tr>'
            if hidden > 0 else ""
        )
        sections.append(
            f'<a name="r{i}"></a>'
            f'<h3 id="r{i}" style="color:#111827;font-size:15px;margin:26px 0 8px;'
            f'padding-bottom:6px;border-bottom:2px solid #4f46e5;">'
            f'{_text(region)} <span style="color:#6b7280;font-weight:normal;font-size:13px;">'
            f'· {len(items)} {"opportunity" if len(items) == 1 else "opportunities"}</span></h3>'
            '<table style="border-collapse:collapse;width:100%;background:#ffffff;">'
            '<tr style="text-align:left;color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:.03em;">'
            '<th style="padding:8px 10px;">Opportunity</th>'
            '<th style="padding:8px 10px;">Work / Archetype</th>'
            '<th style="padding:8px 10px;">Deadline</th>'
            + ('<th style="padding:8px 10px;">Action</th>' if _show_wrike() else '')
            + '</tr>'
            f'{"".join(_digest_rows(member, shown, compact))}{more}</table>'
        )

    if reminder_days is not None:
        when = "tomorrow" if reminder_days == 1 else f"in {reminder_days} days"
        banner_label = "Deadline reminder"
        introduction = (
            f'A quick reminder — <b>{n}</b> '
            f'{"opportunity closes" if n == 1 else "opportunities close"} '
            f'<b>{when}</b>.'
        )
    else:
        banner_label = "New funding opportunities"
        interests = _text(member.keywords or member.verticals or "all topics")
        introduction = (
            f'Here {"is" if n == 1 else "are"} <b>{n}</b> '
            f'{"opportunity" if n == 1 else "opportunities"} matching your '
            f'interests ({interests}), grouped by region:'
        )

    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;max-width:760px;margin:auto;">
      <div style="background:#312e81;border-radius:10px;padding:18px 20px;margin-bottom:18px;">
        <div style="color:#c7d2fe;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;">
          {banner_label}
        </div>
        <h2 style="color:#ffffff;margin:5px 0 0;font-size:22px;">Hi {_text(member.name)},</h2>
      </div>
      <p style="color:#374151;margin-top:0;">{introduction}</p>
      {f'<p style="color:#6b7280;font-size:13px;margin:0 0 10px;">The counts below are '
       f'complete. To keep this email from being clipped by your mail client, each region '
       f'lists its {cap} soonest deadlines — the rest are in the '
       f'<a href="{_text(settings.dashboard_url)}" style="color:#4f46e5;">dashboard</a>.</p>'
       if capped else ''}
      <p style="margin:12px 0 4px;">
        <a href="{_text(settings.dashboard_url)}" style="display:inline-block;
        background:#4f46e5;color:#ffffff;border-radius:6px;padding:9px 16px;
        font-size:13px;font-weight:600;text-decoration:none;">Open the dashboard</a>
      </p>
      <div style="margin:14px 0 4px;">{index}</div>
      {''.join(sections)}
      <p style="color:#9ca3af;font-size:12px;margin-top:22px;">Sent by Lead Scanning Platform</p>
    </div>"""


def _digest_rows(
    member: TeamMember, opportunities: list[Opportunity], compact: bool = False
) -> list[str]:
    rows = []
    for o in opportunities:
        # Everything scraped that helps a decision, so the mail is actionable
        # without opening the dashboard.
        meta = [x for x in (
            o.organization or o.source_website,
            o.location or o.country,
        ) if x]
        # The description is NOT dropped. It used to be, and that made the
        # digest inconsistent in a way that looked like a bug: whether you got
        # descriptions depended on how many matches you happened to have that
        # day — under 40 and they appeared, over 40 and every row lost them.
        # A shortened description costs ~150 bytes and is the single most
        # useful line in the row, so compact mode shortens it instead.
        limit = 110 if compact else 220
        summary = _short(o.summary, limit) or "No summary provided by the source."
        eligibility = _short(o.eligibility, 120 if compact else 280) or \
            "Not specified by the source."
        # Only the opportunity's own URL is offered as the title link. Falling
        # back to o.website here is what made "the link opens the homepage" a
        # recurring complaint: the title looked like a link to the call and
        # silently wasn't. Where no direct link exists, the title is plain text
        # and the source site is offered separately, labelled for what it is.
        # Every title is a link. Where no per-call URL exists, resolve_link
        # returns a search on the source site that will find it — far more use
        # in an inbox than plain text plus a homepage, which left the reader to
        # retype the title into the site's own search box.
        href, kind = resolve_link(o.opportunity_url, o.website, o.source_website, o.title)
        title = (f'<a href="{_text(href)}" style="color:#4f46e5;font-weight:600;'
                 f'text-decoration:none;">{_text(o.title)}</a>')
        if kind == "search":
            title += ('<div style="color:#9ca3af;font-size:11px;margin-top:2px;">'
                      f'no direct link published — opens a search on {_text(o.source_website)}</div>')
        elif link_kind(o.opportunity_url) == "listing":
            # Lands on an index rather than the call itself. Best available, but
            # the reader shouldn't be surprised by it.
            title += ('<div style="color:#9ca3af;font-size:11px;margin-top:2px;">'
                      'opens a listing page — find it there</div>')
        rows.append(f"""
        <tr>
          <td style="padding:10px;border-bottom:1px solid #eee;vertical-align:top;">
            {title}
            <div style="color:#6b7280;font-size:12px;margin-top:3px;">{_text(' · '.join(meta))}</div>
            <div style="color:#4b5563;font-size:12px;margin-top:7px;line-height:1.45;"><b>Summary:</b> {summary}</div>
            <div style="color:#4b5563;font-size:12px;margin-top:5px;line-height:1.45;"><b>Source:</b> {_text(o.source_website or 'Not specified')}</div>
            <div style="color:#4b5563;font-size:12px;margin-top:5px;line-height:1.45;"><b>Opportunity size:</b> {_text(o.funding_amount or 'Not specified')}</div>
            <div style="color:#4b5563;font-size:12px;margin-top:5px;line-height:1.45;"><b>Eligibility:</b> {eligibility}</div>
            <div style="color:#4b5563;font-size:12px;margin-top:5px;line-height:1.45;"><b>Brand:</b> {_labels(o.brands)}</div>
            <div style="color:#4b5563;font-size:12px;margin-top:3px;line-height:1.45;"><b>Vertical:</b> {_labels(o.verticals)}</div>
            <div style="color:#4b5563;font-size:12px;margin-top:3px;line-height:1.45;"><b>Archetype:</b> {_text(o.category.value)}</div>
          </td>
          <td style="padding:10px;border-bottom:1px solid #eee;font-size:12px;vertical-align:top;">{_type_cell(o)}</td>
          <td style="padding:10px;border-bottom:1px solid #eee;font-size:12px;vertical-align:top;">{_deadline_cell(o)}</td>
          {f'<td style="padding:10px;border-bottom:1px solid #eee;vertical-align:top;">{_wrike_cell(o)}</td>' if _show_wrike() else ''}
        </tr>""")
    return rows


def send_digest(member: TeamMember, opportunities: list[Opportunity]) -> None:
    """Blocking SMTP send — call via asyncio.to_thread from async contexts."""
    if not is_configured():
        raise EmailNotConfiguredError(
            "SMTP is not configured. Set LOP_SMTP_USER and LOP_SMTP_PASSWORD "
            "(see backend/.env.example)."
        )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{len(opportunities)} funding opportunit{'y' if len(opportunities) == 1 else 'ies'} for you"
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_user}>"
    msg["To"] = member.email
    msg.attach(MIMEText(_digest_html(member, opportunities), "html", "utf-8"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    log.info("Emailed %s opportunities to %s <%s>", len(opportunities), member.name, member.email)


def send_reminder(member: TeamMember, opportunities: list[Opportunity],
                  days_before: int) -> None:
    """Deadline nudge for opportunities this member already received."""
    if not is_configured():
        raise EmailNotConfiguredError("SMTP is not configured.")
    when = "tomorrow" if days_before == 1 else f"in {days_before} days"
    n = len(opportunities)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = (f"Reminder: {n} opportunit{'y' if n == 1 else 'ies'} "
                      f"clos{'es' if n == 1 else 'e'} {when}")
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_user}>"
    msg["To"] = member.email

    msg.attach(MIMEText(
        _reminder_html(member, opportunities, days_before), "html", "utf-8"
    ))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    log.info("Reminder (%sd) sent to %s <%s> for %s opportunity(ies)",
             days_before, member.name, member.email, n)


def send_alert(subject: str, body: str, to: str | None = None) -> None:
    """Blocking SMTP send for a plain-text operational alert (e.g. a scraper's
    saved login session has expired). Separate from send_digest because it
    isn't tied to a TeamMember or a list of Opportunity rows.

    Logs and returns quietly if SMTP isn't configured yet, rather than raising —
    callers (scrapers) shouldn't fail a scrape just because alerting is unset up.
    """
    if not is_configured():
        log.warning("[alert] SMTP not configured — skipping alert: %s", subject)
        return
    recipient = to or settings.smtp_user
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_user}>"
    msg["To"] = recipient
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    log.info("[alert] sent '%s' to %s", subject, recipient)
