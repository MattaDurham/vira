# Source-health sentinel

"Nothing found" used to read the same as "nothing there": a calendar that
stopped syncing gave a "Clear calendar." brief, and a dead mailbox gave
"no matches". The sentinel learns a baseline per source and, when a source
reads zero against it, labels everything drawn from that source "looks
broken since X" and files one fix-it item.
Code: `server/sourcehealth.py`; wiring in `server/brief.py`,
`server/viratools.py` (`invoke`), `server/attention.py` and
`renderSourceHealth` in `static/app.js`.

```mermaid
flowchart LR
  w[Watcher, every 30 min] --> p[Probes: local calendar and each M365 calendar today,<br/>mail archived per mailbox in 24 h,<br/>chat.db messages in 24 h, vault notes indexed]
  vs[vault_search tool<br/>tallies hits per day] --> j
  p --> j{Zero against a busy baseline?<br/>calendar per weekday, others daily;<br/>broken days never join the baseline}
  j -->|no, or still learning| ok[ok / learning / unavailable]
  j -->|yes, or unreadable after working| b[(data/source-health.json<br/>state broken, since X)]
  b --> brief[Brief banner + Today says<br/>'may not be a clear day']
  b --> tools[Every tool answer from that source<br/>leads with SOURCE HEALTH WARNING]
  b --> attn[Attention Now row with Recheck]
  b -->|first time only| idea[One proposed fix-it item<br/>in the ideas backlog]
  ok -->|reads normally again| clear[Labels clear, recovered_at set;<br/>the fix-it item stays for the owner]
```

![Daily brief rendered with synthetic data: under the summary, a red-outlined box headed "2 sources look broken" lists "Local calendar source looks broken since Oct 27, 8:30 AM: 0 events today; usually 5 on Tuesdays" and a mailbox at owner@example.com reading 0 messages in the last 24 hours against a usual 41, with Recheck and Fix-it item buttons. Below it, the Today section reads "No events read, but the calendar looks broken: this may not be a clear day."](brief-banner.png)

The screenshot runs the real `renderSourceHealth` from `static/app.js`
against the real stylesheet with synthetic rows; no owner data appears in it.

Rules worth knowing:

- No verdict without a baseline: 3 clean same-weekday readings for the
  calendar, 5 clean days for the rest. A source that never read above zero
  is `unavailable`, not broken.
- A quiet source is never broken: the usual median must reach 3 (1 for
  vault notes) before a zero counts.
- Mail is counted from the mail archive (the watcher's feed starts empty at
  every boot); with the archive off, mail reads `unavailable`.
- `GET /api/health/sources` reads the verdicts; `POST
  /api/health/sources/check` (the Recheck buttons) probes now.
  CLI: `python -m server.sourcehealth status|check [--no-file]`.
