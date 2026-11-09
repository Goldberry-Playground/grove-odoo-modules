{
    "name": "Grove Calendar",
    "version": "19.0.1.1.0",
    "category": "Marketing/Calendar",
    "summary": "Public calendar events to Discord: weekly 'This week at the Grove' digest (GOL-3830)",
    "description": """
        The `grove_calendar` module (GOL-3830). Source of truth:
        docs/superpowers/specs/2026-11-07-discord-calendar-design.md, approved in
        chat with Josh 2026-11-06/07 (revised 2026-11-08).

        Odoo is the source of truth for the farm's public events; Discord is a
        publish target. Phase B (this release) ships a weekly "This week at the
        Grove" digest posted through a channel webhook — no new credentials.

          - res.users.grove_discord_publisher: an admin-only per-user switch
            (default off) gating whose Public events reach Discord. On for Josh
            only at launch.
          - calendar.event._grove_public_domain(window_days): active, Privacy ==
            Public, organizer publishes, not yet ended, starting within the
            horizon. One definition shared by the digest, phase D and the tests.
          - grove.calendar.digest: an ir.cron (Monday 12:00 UTC) that posts the
            coming Monday-to-Sunday week's public events to
            DISCORD_EVENTS_WEBHOOK_URL (falling back to DISCORD_OPS_WEBHOOK_URL).
            Times in America/New_York, a Location matching a voice channel shown
            as 🔊 <room>, allowed_mentions disarmed, cut at 2,000 chars, empty
            week posts nothing.
          - Discord bookkeeping fields (discord_event_id, discord_payload_hash,
            discord_url) on both calendar.event and calendar.recurrence, shown
            read-only on the event form. Filled by the native-events sync in
            phase D (Train #6).

        Depends on `calendar` ONLY. It must NOT depend on `google_calendar`, so
        QA and the test runner install it without Google. The Google side is
        Odoo's own google_calendar module, paired by Josh out of band.

          - Website tag + public feed (phase F, GOL-3842): a Public event also
            carrying the grove_calendar.tag_website tag is served read-only on
            GET /grove/api/v1/events (auth public, Cache-Control 5 min) via
            calendar.event._grove_website_domain, and reads as its own calendar
            through a "Website" search filter. Never attendees or organizer.

        Non-goals: no RSVP/ticketing (event/website_event stay uninstalled), no
        Discord-to-Odoo direction, no grove-sites page rendering the feed yet
        (that waits for the visit-page work, GOL-1585).
    """,
    "author": "Gathering at the Grove",
    "website": "https://goldberrygrove.farm",
    "license": "LGPL-3",
    "depends": [
        "calendar",
    ],
    # Additive only: a cron (AbstractModel, no table/ACL), form-view inherits
    # exposing the read-only Discord group and the admin publish switch. The new
    # fields live on existing models (calendar.event, calendar.recurrence,
    # res.users), so no new ACL is needed.
    "data": [
        "data/calendar_event_type_data.xml",
        "data/calendar_digest_cron.xml",
        "views/calendar_event_views.xml",
        "views/res_users_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
