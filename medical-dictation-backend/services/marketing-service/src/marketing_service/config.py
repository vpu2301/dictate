"""Typed configuration. The ONLY module allowed to read the environment."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "marketing-service"
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    testing: bool = Field(default=False, alias="TESTING")

    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317",
        alias="OTEL_EXPORTER_OTLP_ENDPOINT",
    )
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    # The marketing site is a different origin from the app, and this
    # service is called by the browser directly, so CORS is load-bearing
    # rather than incidental.
    cors_allowed_origins: str = Field(default="", alias="CORS_ALLOWED_ORIGINS")

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    # ── Database ─────────────────────────────────────────────────────
    db_app_role_dsn: str = Field(
        default="postgresql://app_role:app_role@localhost:5432/medical_dictation",
        alias="DB_APP_ROLE_DSN",
    )
    db_pool_min_size: int = Field(default=1, alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=5, alias="DB_POOL_MAX_SIZE")

    # ── Email (outbound) ─────────────────────────────────────────────
    #
    # Defaults are Mailpit so a plain `make dev-up` still shows the mail.
    # Production points at Google Workspace; see docs/runbooks/demo-booking.md
    # for why the password must be an App Password and not the account one.
    email_provider: str = Field(default="mock", alias="MDX_EMAIL_PROVIDER")  # smtp | mock
    email_from: str = Field(default="sales@klarnote.com", alias="MDX_MARKETING_EMAIL_FROM")
    email_from_name: str = Field(default="Klarnote", alias="MDX_MARKETING_EMAIL_FROM_NAME")
    # Replies go to a human, not to the sending mailbox's automation. Same
    # address today; a separate one the day sending moves to a relay.
    email_reply_to: str = Field(default="sales@klarnote.com", alias="MDX_MARKETING_REPLY_TO")
    # Where a contact-form enquiry is forwarded. Its own setting rather than
    # reusing email_reply_to: those two answer different questions ("where does
    # a prospect's reply land" vs "who gets told a prospect wrote"), and the
    # day the first moves to a shared relay the second must not follow it.
    # Blank switches the forward off — the acknowledgement to the visitor and
    # the CRM write are unaffected.
    sales_notify_to: str = Field(
        default="sales@klarnote.com", alias="MDX_MARKETING_SALES_NOTIFY_TO"
    )
    smtp_host: str = Field(default="localhost", alias="MDX_MARKETING_SMTP_HOST")
    smtp_port: int = Field(default=1025, alias="MDX_MARKETING_SMTP_PORT")
    smtp_use_tls: bool = Field(default=False, alias="MDX_MARKETING_SMTP_USE_TLS")
    smtp_username: str = Field(default="", alias="MDX_MARKETING_SMTP_USERNAME")
    # Never logged, never returned by an endpoint, never in /readyz.
    smtp_password: str = Field(default="", alias="MDX_MARKETING_SMTP_PASSWORD")

    # ── Booking watcher (inbound) ────────────────────────────────────
    #
    # Google's appointment pages have no webhook. What they do have is an
    # invitation email to the organiser mailbox carrying a text/calendar
    # part — so the mailbox IS the event feed, and one credential covers
    # both directions.
    booking_watch_enabled: bool = Field(default=False, alias="MDX_DEMO_WATCH_ENABLED")
    imap_host: str = Field(default="imap.gmail.com", alias="MDX_DEMO_IMAP_HOST")
    imap_port: int = Field(default=993, alias="MDX_DEMO_IMAP_PORT")
    imap_username: str = Field(default="", alias="MDX_DEMO_IMAP_USERNAME")
    imap_password: str = Field(default="", alias="MDX_DEMO_IMAP_PASSWORD")
    imap_folder: str = Field(default="INBOX", alias="MDX_DEMO_IMAP_FOLDER")
    imap_poll_interval_s: float = Field(default=120.0, alias="MDX_DEMO_IMAP_INTERVAL_S")
    # How far back a poll looks. Generous enough to survive a restart or a
    # short outage, short enough that a cold start does not re-read a year
    # of calendar traffic.
    imap_lookback_days: int = Field(default=3, alias="MDX_DEMO_IMAP_LOOKBACK_DAYS")

    # Confirm bookings from people who never filled the form in. OFF by
    # default: every other invitation on the sales mailbox — a supplier
    # call, an internal standup — also arrives as a calendar invite, and
    # mailing those attendees a Klarnote demo confirmation is a worse
    # failure than missing a self-booked demo. The organiser is told about
    # the unmatched booking in the logs either way.
    confirm_unmatched_bookings: bool = Field(
        default=False, alias="MDX_DEMO_CONFIRM_UNMATCHED"
    )
    # Only invitations whose summary contains this (case-insensitive) are
    # treated as demo bookings. Empty disables the check.
    booking_summary_match: str = Field(default="demo", alias="MDX_DEMO_SUMMARY_MATCH")

    # ── Delivery worker ──────────────────────────────────────────────
    background_jobs_enabled: bool = Field(default=True, alias="MDX_BACKGROUND_JOBS")
    delivery_poll_interval_s: float = Field(
        default=5.0, alias="MDX_MARKETING_DELIVERY_INTERVAL_S"
    )
    delivery_batch_size: int = Field(default=25, alias="MDX_MARKETING_DELIVERY_BATCH")
    delivery_max_attempts: int = Field(default=5, alias="MDX_MARKETING_DELIVERY_MAX_ATTEMPTS")
    delivery_backoff_base_s: float = Field(
        default=60.0, alias="MDX_MARKETING_DELIVERY_BACKOFF_BASE_S"
    )

    # ── Links inside the emails ──────────────────────────────────────
    #
    # The Google appointment page the acknowledgement mail sends people to.
    booking_url: str = Field(
        default="https://calendar.app.google/336HQrX67TV9UNJr8",
        alias="MDX_DEMO_BOOKING_URL",
    )
    # Public marketing origin — unsubscribe and privacy links.
    public_base_url: str = Field(default="https://klarnote.com", alias="MDX_PUBLIC_BASE_URL")
    # Whether `public_base_url` actually SERVES the unsubscribe endpoint.
    #
    # Off by default, and that default is the safe one. RFC 8058 one-click is a
    # promise: `List-Unsubscribe-Post` tells Gmail and Yahoo they may POST to
    # the URL and have the recipient removed. A promise the origin cannot keep
    # — no route, a 404, or a TLS handshake that fails outright — is read as a
    # forged unsubscribe path, which is one of the strongest bulk-sender
    # penalties there is: the relay still answers 250 and the mail is then
    # dropped or junked at the far end, so every outbox row reads `sent` while
    # nobody receives anything. Until /unsubscribe is deployed and reachable
    # over HTTPS, the letters carry a mailto: unsubscribe instead, which is
    # equally valid and cannot be broken by a dead web origin.
    unsubscribe_one_click: bool = Field(
        default=False, alias="MDX_MARKETING_UNSUBSCRIBE_ONE_CLICK"
    )
    # Whose name signs the mail. The only person-specific string in the
    # templates, so it lives here rather than in three translated files
    # that would all have to be edited when the seat changes hands. The
    # job title and the postal address stay literal in the templates —
    # they are translated prose, not configuration.
    host_name: str = Field(default="Andrii Kovalchuk", alias="MDX_DEMO_HOST_NAME")
    demo_minutes: int = Field(default=40, alias="MDX_DEMO_MINUTES")

    # ── Abuse control ────────────────────────────────────────────────
    #
    # The endpoint is unauthenticated by necessity. Per-IP because a
    # per-email cap is trivially defeated and would also let an attacker
    # lock a real prospect out of requesting a demo.
    rate_limit_per_ip_per_hour: int = Field(default=10, alias="MDX_DEMO_RATE_LIMIT_HOUR")
    # Salt for the stored IP hash. MUST be set outside dev — an unsalted
    # hash of an IPv4 address is reversible by brute force in seconds.
    ip_hash_salt: str = Field(default="dev-marketing-salt", alias="MDX_DEMO_IP_HASH_SALT")

    @property
    def is_production(self) -> bool:
        return self.environment in {"production", "staging"}


settings = Settings()
