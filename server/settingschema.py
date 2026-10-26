"""The one table of owner settings.

Every key data/config.json may carry is declared here once: its group, its
type, its default, the words the owner sees for it, and the help text that
config.example.json and the Config window both print. settings.DEFAULTS,
executive.DEFAULT_CONFIG, suggest.DEFAULTS and the public template are all
derived from this table, so a key cannot exist in one place and be missing
from another (2026-10-13: 32 keys were in DEFAULTS but not the template,
20 were read from config and declared nowhere, and the template's session
default disagreed with the code's).

Validation lives here too (`validate`, `validate_many`), and settings.save
is the one writer that applies it. A module that needs its own extra rule
(the assistant's work-hours cross-check, intake's route normalisation)
runs that rule first and then calls settings.save.

Field notes:
  - `advanced` keys appear only under Config > Advanced settings.
  - `control` names a dedicated UI that owns the key (a calendar picker,
    a data connection card, the provider cards, the intake form); generic
    forms skip those keys so the same value never has two editors.
  - `nullable` allows an explicit null (fixture_mode's "auto").

CLI:  python -m server.settingschema example           # print the template
      python -m server.settingschema example --write   # rewrite it in place
      python -m server.settingschema check             # template parity
"""
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = ROOT / "config.example.json"

# Group id, title, one-line blurb. Order is the order the owner sees.
GROUPS = [
    ("you", "You", "Who Vira works for, and when."),
    ("assistant", "Assistant",
     "The background worker that reads new messages for commitments and keeps reminders current."),
    ("notifications", "Notifications",
     "The iMessage channel Vira texts you on, and which features may use it."),
    ("calendar", "Calendar",
     "Where the assistant files events and the hours it plans around."),
    ("mail", "Mail", "Mail accounts and the email index."),
    ("contacts", "Contacts", "Contact storage and who may update profiles."),
    ("brain", "Brain", "Vaults, Reader folders, and what each one exposes."),
    ("ai", "AI", "Providers, models, and app-wide model defaults."),
    ("workers", "Background work", "Sweeps and watchers that run on a cadence."),
    ("sessions", "Agent sessions", "Live coding sessions Vira dispatches."),
    ("atlas", "Contact Atlas", "The relationship graph."),
    ("channels", "Phone and WhatsApp", "The phone companion and the WhatsApp sidecar."),
    ("career", "Applications", "Career record, job boards, and eligibility."),
    ("lessons", "Lessons and review", "The corrections ledger and review queue."),
    ("intake", "Intake", "Filing messages into vault folders."),
    ("system", "System", "Service names, runtime limits, and external tools."),
]
GROUP_TITLES = {gid: title for gid, title, _ in GROUPS}

TYPES = ("bool", "int", "float", "str", "tz", "path", "enum", "list", "json")
STR_MAX = 1000


class Setting(dict):
    """One row of the table; a dict so it serialises as-is for the API."""
    __slots__ = ()

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def S(key, group, type, default, label, help="", *, advanced=False,
      range=None, choices=None, nullable=False, control=None):
    assert type in TYPES, (key, type)
    assert group in GROUP_TITLES, (key, group)
    return Setting(key=key, group=group, type=type, default=default,
                   label=label, help=help, advanced=advanced, range=range,
                   choices=choices, nullable=nullable, control=control)


SCHEMA = [
    # ---------------------------------------------------------------- you
    S("owner_name", "you", "str", "", "Your first name",
      "Used in the cockpit greeting and the daily-brief narrative."),
    S("assistant_timezone", "you", "tz", "", "Time zone",
      "An IANA zone such as America/New_York. Empty uses this computer's local "
      "time. Quiet hours, deadlines, the admin inbox and calendar planning all "
      "read it."),
    S("assistant_kids_names", "you", "str", "", "Children's first names",
      "Comma-separated. A calendar proposal naming one of them files under the "
      "kids calendar when the model states no lane."),

    # ---------------------------------------------------------- assistant
    S("assistant_enabled", "assistant", "bool", False,
      "Run the assistant in the background",
      "Scans new texts and mail for commitments and deadlines, keeps the "
      "reminders list current, and reconciles recent indexed messages after "
      "downtime. Every other assistant setting depends on it."),
    S("assistant_catchup_days", "assistant", "int", 14,
      "Days to check when first enabled",
      "How far back the first scan looks in the message index.",
      range=(1, 90)),
    S("assistant_stale_days", "assistant", "int", 3,
      "Flag an undated commitment after this many days", range=(1, 90)),
    S("assistant_due_soon_hours", "assistant", "int", 24,
      "Remind me this many hours before a deadline", range=(1, 168)),
    S("assistant_debounce_s", "assistant", "int", 90, "Debounce seconds",
      "Wait this long after a message before processing it, so a conversation "
      "in progress is read once.", advanced=True, range=(0, 3600)),
    S("assistant_batch_size", "assistant", "int", 3, "Contacts per pass",
      "How many contacts one background pass processes.", advanced=True,
      range=(1, 10)),
    S("coverage_enabled", "assistant", "bool", False,
      "Watch for childcare coverage gaps",
      "Uses the routine in the Childcare window: a caregiver cancelling in "
      "iMessage, a half day, or a closure on a day nobody covers puts a "
      "pickup block on the calendar and asks you once who is taking it. "
      "Needs the assistant running."),
    S("coverage_calendar_blocks", "assistant", "bool", True,
      "Put pickup blocks on the calendar for coverage gaps",
      "Off keeps each block as a suggestion in Attention instead. Blocks go "
      "on the family calendar and never invite anyone."),
    S("coverage_lookahead_days", "assistant", "int", 14,
      "Days of school calendar to check for half days and closures",
      range=(1, 60)),
    S("coverage_block_minutes", "assistant", "int", 30,
      "Pickup block length (minutes)",
      "A child's own block length in the routine overrides this.",
      advanced=True, range=(5, 180)),

    # ------------------------------------------------------ notifications
    S("notify_enabled", "notifications", "bool", True,
      "Send notifications over iMessage",
      "The channel every alert uses. Off silences all of them."),
    S("notify_handle", "notifications", "str", "", "iMessage handle for pings",
      "Your own self-thread number, e.g. +12125551234. Empty leaves every "
      "notification dormant."),
    S("notify_tier", "notifications", "str", "active",
      "Contact tier that triggers new-mail pings",
      "Inbound email from a contact at this tier (profile or master tier) "
      "pings you. Throttled to one per sender per 6 hours and 20 a day.",
      advanced=True),
    S("assistant_notify", "notifications", "bool", False,
      "Text me when a commitment needs attention",
      "Overdue and due-soon reminders. Respects quiet hours and the daily cap."),
    S("assistant_quiet_start", "notifications", "int", 22,
      "Quiet hours start (0-23)", range=(0, 23)),
    S("assistant_quiet_end", "notifications", "int", 8,
      "Quiet hours end (0-23)", range=(0, 23)),
    S("assistant_notify_daily_cap", "notifications", "int", 3,
      "Maximum reminder texts per day", range=(1, 20)),
    S("ai_health_notify", "notifications", "bool", True,
      "Text me when the AI login goes down or recovers",
      "A green-to-red edge and the recovery. Dormant unless a handle is set."),
    S("subs_notify_threshold_usd", "notifications", "float", 100,
      "Renewal ping floor (USD per cycle)",
      "Text when a renewal is within 7 days and the per-cycle amount clears "
      "this. Annual renewals always ping. Deduped per merchant and date.",
      range=(0, 1000000)),
    S("imessage_reply_enabled", "notifications", "bool", True,
      "Read replies in the self-thread as instructions",
      "Vira acts on reversible in-app things immediately and confirms once "
      "before anything that leaves this machine (server/inbound.py). Dormant "
      "unless a handle is set."),
    S("inbound_ignore_prefixes", "notifications", "list", [],
      "Machine sender prefixes to ignore",
      "Prefixes of machine-sent texts that also land in the self-thread and "
      "must never be read as you talking. Vira's own sends are already "
      "covered by the \"Vira: \" prefix and the send ledger.", advanced=True),
    S("sms_fallback", "notifications", "bool", True,
      "Re-send an undelivered iMessage as a text",
      "For Android or iMessage-less recipients. Needs Text Message Forwarding "
      "to the paired iPhone so Messages owns an SMS account. A contact can "
      "also be marked SMS on their person page."),
    S("send_verify_seconds", "notifications", "int", 8,
      "Seconds to watch for a delivery error",
      "How long Vira watches chat.db for an iMessage delivery error before "
      "giving up. 0 disables the reactive check.", advanced=True,
      range=(0, 120)),

    # ----------------------------------------------------------- calendar
    S("assistant_calendar_name", "calendar", "str", "",
      "Calendar for my own events (name)",
      "Chosen with the calendar picker in Config > Calendar.",
      control="calendar"),
    S("assistant_calendar_id", "calendar", "str", "",
      "Calendar for my own events (id)", control="calendar"),
    S("assistant_calendar_kids_name", "calendar", "str", "",
      "Calendar for the kids (name)",
      "School, classes, playdates and childcare that never need you, filed "
      "for your information. Falls back to the personal calendar.",
      control="calendar"),
    S("assistant_calendar_kids_id", "calendar", "str", "",
      "Calendar for the kids (id)", control="calendar"),
    S("assistant_calendar_family_name", "calendar", "str", "",
      "Calendar for anything that needs me (name)",
      "Family plans, parent evenings, RSVP'd or not. Falls back to the "
      "personal calendar.", control="calendar"),
    S("assistant_calendar_family_id", "calendar", "str", "",
      "Calendar for anything that needs me (id)", control="calendar"),
    S("assistant_calendar_work_start", "calendar", "int", 9,
      "Weekday work hours start (0-23)", range=(0, 23)),
    S("assistant_calendar_work_end", "calendar", "int", 17,
      "Weekday work hours end (1-24)", range=(1, 24)),
    S("assistant_calendar_block_minutes", "calendar", "int", 30,
      "Personal task block length (minutes)",
      "Vira reserves free personal work time before a task's deadline in "
      "blocks this long, inside the work hours above.", range=(15, 240)),
    S("assistant_calendar_auto_create", "calendar", "bool", False,
      "Create events automatically when I am the only attendee",
      "Otherwise every proposed event waits for your approval in Attention."),
    S("family_calendars", "calendar", "list", [],
      "Family calendar names in the brief",
      "Calendar names as they appear in Calendar.app, tagged as family in the "
      "daily brief, e.g. [\"Family\", \"Kids\"]."),
    S("brief_remote_events", "calendar", "list", [], "Remote event words",
      "Event-title substrings (case-insensitive) treated as remote or virtual "
      "in the daily brief, e.g. [\"Weekly Standup\"]. A remote event is never "
      "flagged as an overlap against a same-time in-person event (two remote "
      "events still conflict). Microsoft 365 events also auto-detect via the "
      "Graph isOnlineMeeting flag."),

    # --------------------------------------------------------------- mail
    S("graph_email", "mail", "str", "", "Default Microsoft 365 account",
      "The account the Connect M365 button and the cockpit banner use."),
    S("mail_body_index", "mail", "bool", False,
      "Index full email bodies and sent replies",
      "Puts every email, including ones you sent, into the searchable message "
      "index that Find and the assistant read. Off means the index holds texts "
      "only. The background sweep indexes 100 messages every 3 minutes; the "
      "one-time history walk is `python -m server.textindex backfill mail "
      "--full`."),
    S("msgraph_client_id", "mail", "str", "",
      "Microsoft app registration client id",
      "Config > Mail > Microsoft 365 starts recovery or sign-in automatically "
      "and fills this in. Browser login needs a Mobile and desktop redirect "
      "http://localhost/api/mail/graph/browser/callback and delegated "
      "User.Read, Mail.ReadWrite, Mail.Send and Calendars.Read. Enable public "
      "client flows for device login.", advanced=True),
    S("msgraph_tenant", "mail", "str", "", "Microsoft tenant",
      "The tenant id or domain of the app registration; filled in by the "
      "Microsoft 365 setup.", advanced=True),

    # ----------------------------------------------------------- contacts
    S("crm_root", "contacts", "path", "~/.vira/crm", "Contact storage folder",
      "Directory holding people.json, optional master.json and profiles/. "
      "Config > Contacts > Contact storage (advanced) validates an existing "
      "CRM or selects a fresh import destination. Connecting preserves person "
      "IDs and files; switching an established CRM requires compatible "
      "identities. Until people.json exists there, Vira boots in fixture mode "
      "with one demo contact (Vira themself).", control="connection"),
    S("self_record", "contacts", "path", "", "Career record folder",
      "The folder holding canon/MASTER_HISTORY.md (career evidence), your "
      "resume and application work. Connect it in Config > Contacts > Contact "
      "storage (advanced) > Career record storage. Connecting does not grant "
      "Brain or model access. Empty = <crm_root>/self.", control="connection"),
    S("assistant_contact_updates", "contacts", "bool", True,
      "Let the assistant update profiles from new messages",
      "Adds durable facts and refreshes the relationship summary on People "
      "cards as conversations happen. Off keeps the assistant to commitments "
      "only. Does nothing while the assistant is off."),
    S("apple_contacts_push", "contacts", "bool", True,
      "Update Apple Contacts when a card is saved",
      "macOS only. Company, birthday, email and phone order and labels sync "
      "to the phone."),

    # -------------------------------------------------------------- brain
    S("vault_root", "brain", "path", "", "Primary vault folder",
      "Notes vault the Brain window indexes (Obsidian or any folder of "
      "markdown; FTS plus local nomic embeddings via Ollama; nothing leaves "
      "the machine at index time). Set it from Config > Brain. Empty = the "
      "Brain stays dormant.", control="connection"),
    S("vault_sources", "brain", "json", [], "Connected vaults",
      "Each entry has stable id, name, root and optional dirs, read_enabled, "
      "write_enabled, model_exposure, model_exclude_dirs, exclude_dirs, "
      "purpose, contexts, capture_dir, write_scope, write_dirs, "
      "protected_dirs, allow_publish. Edited per vault in Config > Brain.",
      control="connection"),
    S("vault_primary", "brain", "json", {}, "Primary vault policy",
      "Policy and name overrides for the primary vault, using the same fields "
      "as vault_sources. Empty preserves legacy single-vault read/write "
      "behavior. The plan rendering hook is off unless allow_publish is true.",
      control="connection"),
    S("vault_default_destination", "brain", "str", "", "Default vault for saves",
      "Stable source id for otherwise unqualified saves. Explicit destinations "
      "and context routes take precedence. Empty asks when more than one vault "
      "is writable.", control="connection"),
    S("vault_dirs", "brain", "list", [], "Primary vault folders to index",
      "Vault subdirectories to index (recursive), plus root-level .md files "
      "always. Empty = the default set (wiki, Briefs, Sessions, retros, "
      "brain-retros).", advanced=True),
    S("vault_exclude_dirs", "brain", "list", [],
      "Folder names never indexed, in every vault",
      "Directory NAMES skipped at any depth in every connected vault, on top "
      "of qocha's own defaults (.git, .obsidian, .venv). Each vault's own "
      "\"Folders never indexed\" list in Config > Brain is the per-vault "
      "version; this one applies everywhere.", advanced=True),
    S("library_machine_dirs", "brain", "json", {}, "Machine-output folders",
      "Library: which folders of each vault are machine output (generated "
      "reports, session logs, bulk captures), hidden from the subject map "
      "until shown. Keyed by vault id; each entry is a vault-relative folder, "
      "or \"!folder\" to keep a folder inside a machine folder as knowledge. "
      "Edited per vault in Config > Brain and from the Library map.",
      control="connection"),
    S("atlas_vaults", "brain", "json", [], "Image Atlas vaults",
      "Extra Image Atlas vaults as [{id, name, root}]; the primary vault is "
      "always included. Toggled per vault in Config > Brain.",
      control="connection"),
    S("reader_sources", "brain", "json", [], "Reader folders",
      "Folders outside Vira connected to the Reader, as [{path, kind, glob, "
      "label}]. Each is read and never written or copied. kind is dossier | "
      "plan | retro | brief | walkthrough; glob is a filename pattern inside "
      "the folder (default *.html). Edited in Config > Brain > Reader "
      "document folders.", control="connection"),
    S("answer_source_policy", "brain", "json", {}, "Answer source policy",
      "Per-kind overrides for what the answering model may read: {kind: "
      "{read_enabled, model_exposure}}. Empty allows every approved source.",
      advanced=True),

    # ----------------------------------------------------------------- ai
    S("ai_provider", "ai", "str", "anthropic", "Go-to provider",
      "anthropic | openai | google | xai. Chosen from the provider cards in "
      "Config > AI.", control="ai"),
    S("ai_backend", "ai", "enum", "cli", "Backend",
      "\"cli\" uses the provider's local CLI login (a subscription); \"api\" "
      "uses an API key from the environment variable named below.",
      choices=["cli", "api"], control="ai"),
    S("cli_model", "ai", "str", "sonnet", "Default CLI model",
      "An ALIAS the CLI resolves to the newest model of that tier: sonnet | "
      "opus | haiku | fable. Never a full model id; an id here names a "
      "generation and starts lying the week the next one ships.",
      control="ai"),
    S("api_model", "ai", "str", "", "Default API model",
      "Empty = let Vira pick, at call time, the newest model of the cli_model "
      "tier from the list your API key can reach. Set a full id only to pin "
      "one deliberately.", control="ai"),
    S("openai_cli_model", "ai", "str", "", "OpenAI CLI model",
      "Empty runs whatever ~/.codex/config.toml is set to.", control="ai"),
    S("openai_api_model", "ai", "str", "", "OpenAI API model", control="ai"),
    S("google_api_model", "ai", "str", "", "Google API model", control="ai"),
    S("xai_cli_model", "ai", "str", "", "xAI CLI model",
      "The model the Grok Build CLI runs on your Grok subscription. Empty "
      "runs the CLI's own default (the one `grok models` marks).",
      control="ai"),
    S("xai_api_model", "ai", "str", "", "xAI API model", control="ai"),
    S("model_roster", "ai", "list", [], "Model roster",
      "The model ids enabled for pickers across every provider. Empty means "
      "everything the catalog offers, so a future model arrives enabled "
      "unless you have curated. Edited in Config > Advanced AI settings.",
      control="ai"),
    S("providers_disabled", "ai", "list", [], "Providers switched off",
      "Provider ids (anthropic | openai | google | xai) that are never "
      "drafted on and never run a session: a dispatch pinned to one, a go-to "
      "set to one, and the drafting ladder all REFUSE by name instead of "
      "rerouting. Toggle per provider under Config > AI.", control="ai"),
    S("module_models", "ai", "json", {}, "Per-module model picks",
      "{module id: {provider, backend, model}} chosen with the model picker "
      "beside a module's work. Empty rows inherit the defaults above.",
      control="ai"),
    S("chat_model", "ai", "str", "", "Chat with Vira model",
      "The model its session runs on. Empty = the session default "
      "(cli_model), which is what every other owner session uses."),
    S("judge_model", "ai", "str", "opus", "Judge model",
      "Model for fresh-eyes judge sessions (circuit judge stages and the "
      "Judge button on job history). Accepts sonnet | opus | haiku | fable."),
    S("chat_effort", "ai", "str", "", "Chat reasoning effort",
      "Passed to the chat session as its effort level when set. Empty = the "
      "model's default.", advanced=True),
    S("model_alias_overrides", "ai", "json", {}, "Model alias overrides",
      "Corrections for aliases this machine's CLI resolves WRONG, e.g. "
      "{\"opus\": \"claude-opus-5\"} when --model opus still gives an older "
      "generation. Re-measure after a CLI upgrade with: claude --print "
      "--output-format json --model opus 'Say OK.' (read modelUsage) and "
      "DELETE the entry once the alias is correct again.", advanced=True),
    S("api_key_env", "ai", "str", "VIRA_ANTHROPIC_KEY", "API key variable",
      "The environment variable holding the Anthropic API key when the "
      "backend is \"api\".", advanced=True),
    S("timeout", "ai", "int", 120, "Model call timeout (seconds)",
      advanced=True, range=(10, 3600)),
    S("dossier_cost_estimate_usd", "ai", "float", 0.25,
      "Estimated cost per profile build (USD)",
      "Shown beside the profile build button as a spend estimate.",
      advanced=True, range=(0, 100)),

    # ------------------------------------------------------------ workers
    S("ai_health_enabled", "workers", "bool", True, "Watch the AI login",
      "Deterministic AI-backend health watcher: probes `claude auth status` "
      "(or the API key) on a cadence so an expired login surfaces out-of-band "
      "instead of as a silently dead cockpit job. Uses none of the model. "
      "The /api/health/ai probe still works on demand when this is off."),
    S("ai_health_interval_min", "workers", "int", 5,
      "Minutes between AI health probes",
      "Clamped to a 60 second floor. The probe is cheap and spends no tokens.",
      advanced=True, range=(1, 1440)),
    S("media_archive_enabled", "workers", "bool", True,
      "Keep a copy of every iMessage attachment",
      "macOS treats ~/Library/Messages/Attachments as an evictable cache: "
      "Messages in iCloud offloads the bytes under storage pressure and "
      "leaves the chat.db row, so without this a conversation's media history "
      "decays into a list of filenames."),
    S("media_archive_root", "workers", "path", "", "Media archive folder",
      "Empty = data/media-archive. Point it at an external volume to keep it "
      "off the boot disk, whose fullness is what triggers the eviction.",
      advanced=True),
    S("media_archive_max_gb", "workers", "float", 0, "Media archive cap (GB)",
      "0 = no cap. A reached cap is reported in the sweep log and in the "
      "status payload, never silently applied.", advanced=True,
      range=(0, 100000)),
    S("media_archive_interval_min", "workers", "int", 30,
      "Minutes between media archive sweeps",
      "Each pass only has to copy what macOS has downloaded since the last "
      "one.", advanced=True, range=(1, 1440)),
    S("mail_archive_enabled", "workers", "bool", True,
      "Keep a copy of every email",
      "The raw message as the provider holds it, attachments inside, one "
      ".eml per message. Without it mail lives only on the provider: the "
      "text index keeps a stripped body and the attachment index the files "
      "it chose, and neither gives a message back."),
    S("mail_archive_root", "workers", "path", "", "Mail archive folder",
      "Empty = data/mail-archive. Point it at an external volume to keep a "
      "large mailbox off the boot disk.", advanced=True),
    S("mail_archive_max_gb", "workers", "float", 0, "Mail archive cap (GB)",
      "0 = no cap. A reached cap is reported in the sweep log and in the "
      "status payload, never silently applied.", advanced=True,
      range=(0, 100000)),
    S("mail_archive_interval_min", "workers", "int", 5,
      "Minutes between mail archive sweeps",
      "Each pass takes what arrived since the last one, then walks the "
      "history newest first, 200 messages per account at most. The text "
      "index and the attachment index read the archive's files for the "
      "accounts it covers, so new mail is held, searchable and its "
      "attachments indexed within minutes; the history is the CLI "
      "backfill's job.", advanced=True, range=(1, 1440)),
    S("mail_archive_workers", "workers", "int", 4,
      "Connections for the mail archive backfill",
      "IMAP connections walking the history at once when the CLI backfill "
      "runs; the background sweep uses one. One message per round trip per "
      "connection is the rate. Gmail allows 15 per account, shared with "
      "your mail apps; a walker refused at the limit steps aside, and the "
      "backfill retries with fewer.", advanced=True, range=(1, 8)),
    S("receipts_sweep_days", "workers", "int", 7, "Days between receipt sweeps",
      "The receipts pass sweeps mailboxes and indexed attachments for invoice "
      "and receipt evidence, flagged merchants first. Dormant until a mail "
      "account is configured. On demand per merchant from its card.",
      range=(1, 365)),
    S("mercury_poll_hours", "workers", "int", 6, "Hours between Mercury polls",
      "How often Subscriptions polls Mercury for new charges. Connect a Read "
      "Only token in Config > Banking. Dormant until connected.",
      range=(1, 168)),
    S("boards_auto_score", "workers", "bool", True,
      "Score new board roles automatically",
      "After each sweep (and every few minutes between) the poller dispatches "
      "ONE scoring session at a time over the unscored backlog, 40 roles per "
      "session, gated on an AI actually being connected. Off restores the "
      "manual Score-new button in the Applications boards strip."),
    S("boards_auto_rescore", "workers", "bool", True,
      "Re-score stale roles automatically",
      "Once nothing is unscored, drain the stale backlog the same way: roles "
      "whose analysis predates your record's last change get re-judged, one "
      "session at a time, tier-first then oldest. Real spend on a large "
      "backlog; off leaves the per-role Rescore button as the only path."),
    S("boards_score_model", "workers", "str", "", "Scoring session model",
      "Empty = the session default (cli_model).", advanced=True),
    S("boards_poll_minutes", "workers", "float", 15,
      "Minutes between board sweeps", "Dormant until boards are registered.",
      advanced=True, range=(1, 1440)),
    S("idea_tag_interval_min", "workers", "int", 10,
      "Minutes between backlog tagging passes",
      "Each tick embeds any new ideas locally and makes at most ONE model "
      "call, so a fresh backlog tags itself over an hour rather than in one "
      "spend.", advanced=True, range=(1, 1440)),
    S("doc_tag_interval_min", "workers", "int", 10,
      "Minutes between Reader tagging passes",
      "Each tick tags at most ONE batch of 12, so a fresh library groups "
      "itself over an hour or two and nothing waits on it.", advanced=True,
      range=(1, 1440)),
    S("doc_thumb_interval_min", "workers", "int", 15,
      "Minutes between Reader thumbnail sweeps",
      "Dormant without a Chrome, Chromium or Edge on the machine; the browser "
      "path below overrides the probe.", advanced=True, range=(1, 1440)),
    S("circle_refresh_min", "workers", "int", 60,
      "Minutes between circle sync passes",
      "Re-matches the Visual Network's social circles to their stable "
      "identities and re-reads (one model call each, at most four per pass) "
      "only the circles whose members or shared group chats changed.",
      advanced=True, range=(1, 1440)),

    # ----------------------------------------------------------- sessions
    S("session_default_mode", "sessions", "enum", "bypassPermissions",
      "Default permission mode",
      "manual pauses every risky tool call for your Approve/Deny; "
      "acceptEdits lets file edits land but asks before commands; "
      "bypassPermissions runs everything. The containment is structural: a "
      "writing session is placed in its own worktree and the gate refuses "
      "any write aimed at the live checkout. Per-run override in the UI.",
      choices=["manual", "acceptEdits", "bypassPermissions"]),
    S("session_max_live", "sessions", "int", 4, "Concurrent live sessions",
      "Each holds a CLI subprocess against the plan. Launches past the cap "
      "are refused with a clear error. Sessions parked in their reply window "
      "do not count.", range=(1, 32)),
    S("session_auto_allow", "sessions", "list",
      ["Read", "Grep", "Glob", "TodoWrite", "Task", "NotebookRead", "WebSearch"],
      "Tools approved without asking",
      "Read-only tools the permission gate approves on its own. Anything "
      "else that reaches the gate raises an Approve/Deny card.",
      advanced=True),
    S("session_permission_timeout", "sessions", "int", 600,
      "Seconds before an unanswered permission card is denied",
      "The session then continues rather than hanging forever.",
      advanced=True, range=(10, 86400)),
    S("session_reply_window_hours", "sessions", "int", 12,
      "Hours a finished session waits for your reply",
      "Status \"waiting on your reply\", compose bar live, before it "
      "finalizes on its own. Ending it with Finish is the normal path; this "
      "is only the safety reap.", advanced=True, range=(1, 168)),
    S("session_landing_card", "sessions", "bool", True,
      "Raise the landing card when a session parks work on its branch",
      "Vira itself raises the merge / try it first / keep playing / discard "
      "decision card (in the terminal, the Attention window, and on your "
      "phone) instead of leaving that question in the transcript. Try it "
      "first serves a fresh, local-only instance of the branch and brings the "
      "card back with its link; it is never bridged to the tailnet "
      "automatically. Off leaves the session to end however it ends."),
    S("session_branch_first", "sessions", "bool", True,
      "Place writing sessions in their own worktree",
      "The gate refuses writes to the live checkout; the result is always "
      "merged or thrown away with the branch.", advanced=True),
    S("session_ask_timeout", "sessions", "int", 21600,
      "Seconds a session waits for an owner decision",
      advanced=True, range=(60, 604800)),
    S("session_max_buffer_mb", "sessions", "int", 64,
      "Largest single session output line (MiB)",
      "The SDK bounds one NDJSON line; 1 MiB killed three sessions that ran "
      "Edit on static/app.js. Bounded, not removed, so a runaway CLI cannot "
      "exhaust the runner.", advanced=True, range=(1, 1024)),

    # -------------------------------------------------------------- atlas
    S("atlas_anchor_org", "atlas", "str", "", "Anchor organization",
      "Company name pinned as the Contact Atlas anchor cluster (substring-"
      "matched against CRM company fields). Empty = no pinned cluster; "
      "communities still form by label propagation."),
    S("atlas_max_nodes", "atlas", "int", 200, "Node cap",
      "The N most-active tiered contacts rendered in the graph. Keeps the "
      "mesh legible and the edge passes cheap.", range=(20, 5000)),
    S("atlas_min_edge_weight", "atlas", "float", 0.15, "Minimum edge weight",
      "Edges whose fused multi-signal weight falls below this are dropped.",
      range=(0, 1)),

    # ----------------------------------------------------------- channels
    S("companion_hub_url", "channels", "str", "", "Phone pairing address",
      "URL the Android companion pairing QR points the phone at, e.g. "
      "http://your-machine.your-tailnet.ts.net:8377. Empty = auto-detect: "
      "the Tailscale MagicDNS name, then the tailnet IP, then the LAN IP.",
      advanced=True),
    S("whatsapp_bridge_port", "channels", "int", 18377, "WhatsApp sidecar port",
      "Linked-device sidecar, 127.0.0.1 only. Config > WhatsApp > Connect "
      "prepares pinned packages and shows the pairing QR; needs Node.js 20 "
      "or newer with npm. Dormant until paired.", advanced=True,
      range=(1024, 65535)),
    S("whatsapp_poll_seconds", "channels", "int", 5,
      "Seconds between WhatsApp polls", advanced=True, range=(1, 300)),
    S("whatsapp_node_bin", "channels", "str", "node", "Node binary",
      "PATH name or absolute path of the node binary the sidecar runs on.",
      advanced=True),

    # ------------------------------------------------------------- career
    S("applications_universe", "career", "path", "", "Candidate universe folder",
      "Where the scored candidate universe lives (candidate-universe/role/"
      "*.json plus *-raw-scores.json). Written by the front door. Empty = "
      "<self_record>/analysis.", control="connection"),
    S("applications_packages_root", "career", "path", "",
      "Application packages folder",
      "One generated package per application (resume, cover letter, interview "
      "materials). Empty = ~/Documents/CV/15-applications when that folder "
      "exists, otherwise <self_record>/15-applications.", advanced=True),
    S("applications_locations", "career", "list", [], "Office locations",
      "Place names you will work in, e.g. [\"Berlin\", \"BER\"]. Empty means "
      "unfiltered unless another location setting is configured. Eligibility "
      "gates the phone ping and the default view, never what gets fetched."),
    S("applications_remote_regions", "career", "list", [], "Remote regions",
      "Territories from which you can accept region-limited remote work, "
      "e.g. [\"Germany\", \"Central European Time\"]. Private preference "
      "data; empty means Vira does not infer territories from your offices."),
    S("applications_remote_ok", "career", "bool", True,
      "Count remote postings as eligible"),
    S("applications_remote_exclude", "career", "json", [],
      "Remote labels outside your search",
      "A private regex or list of remote-location labels that are outside "
      "your search. Empty excludes no geography.", advanced=True),
    S("applications_region_hints", "career", "json", [],
      "Labels that establish an acceptable region",
      "A private regex or list used when a posting mixes Remote with several "
      "locations. Empty accepts no geography by product default.",
      advanced=True),
    S("applications_boards", "career", "json", None, "Board registry override",
      "Replaces the board registry file with an inline list. Normally unset; "
      "boards are registered from the Applications front door.",
      advanced=True, nullable=True),
    S("applications_sources", "career", "json", [], "Static role corpora",
      "[{slug, company, path}] explorer directories (each with a data.js) "
      "ingested as static role corpora instead of the lab root.",
      advanced=True),
    S("applications_connections_csv", "career", "path", "",
      "Connections export (CSV)",
      "A LinkedIn-style connections export used for warm-path lookups. "
      "Empty = <self_record>/connections.csv when present.", advanced=True),
    S("lab_root", "career", "path", "", "Careers teardown lab folder",
      "Local checkout holding careers-teardown explorer directories to ingest "
      "as STATIC role corpora. Optional: the Applications module's normal "
      "source is the live board poller.", advanced=True),
    S("pivot_keywords", "career", "list", [], "Pivot keywords",
      "Extra words widening the Radar \"Reconnect - pivot\" fingerprint. The "
      "target companies and lanes are always derived live from the "
      "Applications universe.", advanced=True),

    # ------------------------------------------------------------ lessons
    S("evidence_retro_dir", "lessons", "path", "", "Session retro folder",
      "Directory the Evidence Ledger mines session retros from (glob "
      "\"* vira.md\"). Empty = ~/TC-IL/Sessions. The ledger also reads this "
      "checkout's own git-ignored retros/*.md when present.", advanced=True),
    S("lessons_ledger_path", "lessons", "path", "", "Corrections ledger",
      "The ledger the lesson-recurrence counter reads (Work > RECORD > "
      "Rules). Empty = ~/.claude/LESSONS.md; a missing file leaves the module "
      "dormant. Vira only ever READS this file.", advanced=True),
    S("lessons_state_dir", "lessons", "path", "", "Lesson provenance folder",
      "results/<day>/*.json session digests plus lessons-proposed.jsonl and "
      "lessons-decided.jsonl. Empty = ~/.claude/sessions.", advanced=True),
    S("lessons_retro_dirs", "lessons", "str", "", "Retro folders (comma list)",
      "Retro directories the counter reads as the markdown fallback, ALL "
      "projects. Empty = the retro folder above (or its default) plus this "
      "checkout's retros/.", advanced=True),
    S("lessons_script_path", "lessons", "path", "", "Lessons CLI",
      "The corrections-ledger CLI the review queue shells out to; it owns "
      "every ledger write. Empty = ~/.claude/scripts/lessons.py.",
      advanced=True),
    S("lesson_promote_at", "lessons", "int", 3, "Sessions before a promotion",
      "Distinct sessions that must describe breaking a tier-2 rule before the "
      "counter stages a promotion proposal (a proposed idea in the Queue, "
      "never a ledger write).", advanced=True, range=(1, 50)),
    S("lesson_candidates_per_rule", "lessons", "int", 12,
      "Candidate reversals per rule per pass",
      "Rung-2 cost ceiling, newest first; drops are counted in the run "
      "record, never silent.", advanced=True, range=(1, 200)),
    S("review_inbox_dir", "lessons", "path", "", "Self-record capture inbox",
      "Surfaced in the review queue. Empty = <self_record>/inbox/notes.",
      advanced=True),
    S("review_history_path", "lessons", "path", "", "Canonical record",
      "Read for open adjudication flags and questions. Empty = "
      "<self_record>/canon/MASTER_HISTORY.md.", advanced=True),

    # ------------------------------------------------------------- intake
    S("correspondence", "intake", "json", {}, "Intake settings",
      "{enabled, auto_file, model_classification, auto_confidence, "
      "catchup_days, routes}. Edited in Attention > Inbox; routes name a "
      "vault and a category.", control="intake"),

    # ------------------------------------------------------------- system
    S("fixture_mode", "system", "bool", None, "Force demo or real mode",
      "true/false forces demo/real mode; null (the default) auto-detects: "
      "fixture when the contact folder has no people.json.", advanced=True,
      nullable=True),
    S("launchd_label", "system", "str", "", "launchd label (macOS)",
      "If Vira runs as a launchd service, its label (e.g. com.example.vira). "
      "The in-app updater then restarts via launchctl kickstart; empty = the "
      "updater refuses web-triggered restarts.", advanced=True),
    S("windows_task_name", "system", "str", "", "Task Scheduler task (Windows)",
      "The task that keeps Vira running (scripts\\run.ps1 -Register creates "
      "it, default name \"Vira\", and records it here). Empty = the updater "
      "refuses web-triggered restarts on Windows.", advanced=True),
    S("keychain_prefix", "system", "str", "", "Keychain service prefix",
      "Prefixes every Keychain service name so a second instance on the same "
      "Mac cannot read or overwrite the live instance's secrets. Empty keeps "
      "the historical names. VIRA_KEYCHAIN_PREFIX in the environment wins.",
      advanced=True),
    S("cpu_slots", "system", "int", 2, "CPU-heavy request slots",
      "How many CPU-heavy requests may run at once before the rest wait.",
      advanced=True, range=(1, 64)),
    S("cpu_max_wait_s", "system", "float", 15,
      "Seconds a request waits for a CPU slot", advanced=True,
      range=(0, 600)),
    S("loop_stall_threshold_s", "system", "float", None,
      "Event-loop stall threshold (seconds)",
      "Lag past which the watchdog counts the loop as stalled. Empty = 2.",
      advanced=True, nullable=True, range=(0.1, 60)),
    S("thumb_browser", "system", "path", "", "Browser for document thumbnails",
      "Explicit browser binary; empty = probe PATH and app bundles.",
      advanced=True),
    S("ytdlp_path", "system", "path", "", "yt-dlp binary",
      "Used by the reading-room full ingest to stage YouTube caption "
      "transcripts into the vault. Empty = probe PATH; an absent binary "
      "reports \"yt-dlp not installed\" and articles still stage.",
      advanced=True),
    S("site_root", "system", "path", "", "Public site checkout",
      "Checkout of the owner's public site repo. Used by the sitedocs "
      "migration and the blog publish pipeline. Empty = both stay dormant.",
      advanced=True),
    S("design_foundation_root", "system", "path",
      "~/workspace/design-foundation", "Design system checkout",
      "The design-system repo the Design studio edits; missing = dormant.",
      advanced=True),
]

BY_KEY = {s.key: s for s in SCHEMA}
assert len(BY_KEY) == len(SCHEMA), "duplicate setting key"
DEFAULTS = {s.key: s.default for s in SCHEMA}


# ---------------------------------------------------------------- lookups

def keys(group=None, *, advanced=None, control=None, editable=None):
    """Keys in table order. `advanced` and `control` filter when given;
    editable=True keeps only keys a generic form may edit (no dedicated
    control)."""
    out = []
    for s in SCHEMA:
        if group is not None and s.group != group:
            continue
        if advanced is not None and bool(s.advanced) != advanced:
            continue
        if control is not None and s.control != control:
            continue
        if editable is True and s.control:
            continue
        out.append(s.key)
    return out


def defaults_for(selected):
    return {k: DEFAULTS[k] for k in selected}


# ------------------------------------------------------------- validation

def _reject(key, why):
    raise ValueError(f"{key} {why}")


def validate(key, value):
    """The normalised value, or ValueError naming the key and the rule."""
    s = BY_KEY.get(key)
    if s is None:
        raise ValueError(f"unknown setting: {key}")
    if value is None:
        if s.nullable:
            return None
        _reject(key, "cannot be null")
    t = s.type
    if t == "bool":
        if type(value) is not bool:
            _reject(key, "must be true or false")
        return value
    if t == "int":
        if type(value) is not int:
            _reject(key, "must be a whole number")
        lo, hi = s.range or (None, None)
        if lo is not None and not lo <= value <= hi:
            _reject(key, f"must be a whole number from {lo} to {hi}")
        return value
    if t == "float":
        if type(value) not in (int, float):
            _reject(key, "must be a number")
        lo, hi = s.range or (None, None)
        if lo is not None and not lo <= value <= hi:
            _reject(key, f"must be a number from {lo} to {hi}")
        return value
    if t in ("str", "path", "tz", "enum"):
        if not isinstance(value, str):
            _reject(key, "must be text")
        if len(value) > STR_MAX:
            _reject(key, f"must be at most {STR_MAX} characters")
        if t == "path":
            value = value.strip()
        if t == "tz" and value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(
                    f"{key}: use a valid IANA time zone, or leave it empty") from exc
        if t == "enum" and value not in s.choices:
            _reject(key, "must be one of " + ", ".join(s.choices))
        return value
    if t == "list":
        if not isinstance(value, list):
            _reject(key, "must be a list")
        out = []
        for item in value:
            if not isinstance(item, str):
                _reject(key, "must be a list of text values")
            item = item.strip()
            if item:
                out.append(item)
        return out
    if t == "json":
        if not isinstance(value, (dict, list)):
            _reject(key, "must be an object or a list")
        return value
    raise AssertionError(t)


def validate_many(updates):
    if not isinstance(updates, dict):
        raise ValueError("settings must be an object of key: value")
    return {k: validate(k, v) for k, v in updates.items()}


# ------------------------------------------------------------ description

def describe(raw=None, *, group=None, advanced=None, editable=None):
    """Rows for the API and the forms: the table plus each key's current
    value (the stored one, or the default when absent or empty)."""
    raw = raw if raw is not None else {}
    rows = []
    for key in keys(group, advanced=advanced, editable=editable):
        s = BY_KEY[key]
        stored = raw.get(key)
        value = s.default if stored in (None, "") else stored
        rows.append({"key": key, "group": s.group, "type": s.type,
                     "default": s.default, "value": value, "label": s.label,
                     "help": s.help, "advanced": bool(s.advanced),
                     "range": list(s.range) if s.range else None,
                     "choices": s.choices, "control": s.control,
                     "set": key in raw})
    return rows


def payload(raw=None):
    """The whole table for GET /api/settings, grouped in display order."""
    rows = describe(raw)
    by_group = {}
    for row in rows:
        by_group.setdefault(row["group"], []).append(row)
    return {"groups": [{"id": gid, "title": title, "blurb": blurb,
                        "settings": by_group.get(gid, [])}
                       for gid, title, blurb in GROUPS]}


# --------------------------------------------------------------- template

README = ("Copy this file to data/config.json and fill in your values. Every "
          "key is optional: an absent or empty value leaves its feature "
          "dormant. Keys prefixed with an underscore are explanations. This "
          "file is generated from server/settingschema.py (python -m "
          "server.settingschema example --write); edit the table, not the "
          "file. data/ is git-ignored, so nothing in your copy reaches the "
          "repo.")


def example_document():
    doc = {"_readme": README}
    for gid, title, blurb in GROUPS:
        doc["_" + gid] = f"{title}: {blurb}"
        for s in SCHEMA:
            if s.group != gid:
                continue
            doc[s.key] = s.default
            if s.help:
                doc["_" + s.key] = s.help
    return doc


def example_text():
    return json.dumps(example_document(), indent=2, ensure_ascii=False) + "\n"


def example_in_sync():
    """True when the tracked template parses to exactly what the table says
    (compared as data, so line endings never count)."""
    try:
        current = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return list(current.items()) == list(example_document().items())


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "check"
    if cmd == "example":
        if "--write" in args:
            EXAMPLE_PATH.write_text(example_text(), encoding="utf-8")
            print(f"wrote {EXAMPLE_PATH} ({len(SCHEMA)} keys)")
        else:
            sys.stdout.write(example_text())
    elif cmd == "check":
        ok = example_in_sync()
        print("config.example.json is", "in sync" if ok else "OUT OF DATE",
              f"with the table ({len(SCHEMA)} keys)")
        sys.exit(0 if ok else 1)
    else:
        print(__doc__)
        sys.exit(2)
