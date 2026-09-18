# Attention and Work

Attention answers what needs the owner's time. Work holds the full record of
ideas, execution, and results. These are two views over existing sources of
truth; they do not create a competing task or job store.

## Navigation

- Attention / Today combines the former Now and Day views. Canonical reminders
  appear once, alongside calendar context and a short activity list.
- Attention / Review holds live answer forms, durable decisions, and uncertain
  correspondence filing. An unrelated refresh preserves pending answer inputs.
- Attention / Inbox shows retained correspondence, filing outcomes, and route
  preferences. Incoming remains the source browser, with a Keep action for
  indexed email and iMessage sources.
- Work / Ideas retains the proposal queue. Work / Automations retains the Forge
  graph editor, library, schedules, and execution traces.
- Work / Results joins branch, session, flow, historical change, and filing
  receipts by stable source identity. Gallery and Timeline render one inventory.
  Both views and Attention open the same result inspector. Explicit branch
  actions drill into the existing review workflow and retain its confirmations.
- Old brief, review, Forge, Record, and Showroom links resolve to the new homes.
  Advanced history controls, rules, and filed ideas remain available.

## Correspondence

Intake is opt-in in Attention / Inbox. Each route specifies a configured vault,
category path, purpose, optional sender/account/word constraints, and whether a
clear match may be filed automatically. The vault writer still enforces its
current writable directories, protected paths, and model exposure rules.

Local rules run without sending source text to a model. Connected-AI
classification is a separate opt-in: it sends the message body to the configured
provider, offers only eligible destination IDs, has no tools, and treats source
text as evidence. Review is the fallback when the match is unclear or the route
requires owner approval. No owner-specific vault names or filing policies are
hard-coded.

A message can become reference material, a task, both, or neither. Task
extraction delegates to the existing contact assistant and links by source
identity. Filing does not mark the task done, and saving evidence to a Self vault
does not promote it into a canonical claim.

Preservation creates a Markdown source note with provenance and a digest, plus
exactly associated local attachment copies. Stable source identities and
create-only writes make retries safe. Missing or incomplete bodies and
attachments remain visible as limitations; the UI never claims that an
unavailable attachment was saved. Later fuller source material is a revision.
Saved, indexed, and task-extraction states are independent. "Saved and indexed"
does not mean that downstream vault synthesis has run.

Private state lives in data/correspondence.json, the private configuration, and
the selected external vault. The worker stays off in passive and fixture
instances. No messages are sent by this feature.

## Reminder stickies

Drag a canonical reminder to empty desktop space, or choose Pin to desktop.
Pins persist only the source reminder ID and their geometry in
data/reminder-stickies.json. The visible text, evidence, deadline, and status
resolve from the original reminder. Done and Snooze use the existing reminder
authority. Missing sources remain visibly unavailable, rather than becoming
false completions. Keyboard movement/resizing and a phone tray cover other
input methods. Branch previews read canonical reminders and permit pin layout
changes in their complete, isolated data snapshot. Shared or linked layout
stores are refused. Done, Snooze, and deadline changes remain disabled in every
preview, including at the reminder authority.

## Review and rollout

The code is a branch candidate; installing it does not enable intake or change
vault configuration. Configure writable categories and routing purposes before
enabling it. Review automatic routes with sample messages first. Existing
records and old links remain usable, and disabling intake stops the worker
without removing already preserved evidence or reminders.
