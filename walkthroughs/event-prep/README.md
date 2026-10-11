# Event prep in the daily brief

Every open loop tied to an upcoming dated event (a trip, a birthday, a
dinner, a festival) is gathered into one countdown checklist. The checklist
climbs the brief as the date nears and clears the day after the event ends.
Code: `server/eventprep.py`; brief wiring in `server/brief.py` and
`renderEventPrep` in `static/app.js`.

```mermaid
flowchart LR
  cal[Local calendar<br/>all-day, multi-day,<br/>birthday, occasion titles] --> anchors
  loops[Open loops owed by the owner<br/>CRM profiles + assistant commitments] --> msg[Dated loops naming<br/>an occasion]
  msg --> anchors[Event anchors<br/>next 21 days]
  loops --> cluster[Cluster each loop onto<br/>its best event]
  anchors --> cluster
  cluster --> kinds[Kind: logistics, gift,<br/>packing, to-do]
  owner[(data/event-prep.json<br/>ticks, added items, hidden)] --> card
  kinds --> card[Brief card with countdown]
  card -->|0-2 days| top[Above Today]
  card -->|3-7 days| mid[Below Tomorrow]
  card -->|8-21 days| end1[End of the brief]
  card -->|day after it ends| gone[Cleared]
```

![Desktop daily brief with synthetic data. A "Coming up now" card for "Lake weekend" in 2 days sits above Today, showing 2 of 5 items ready, with logistics and packing tags and two struck-through ticked items. Below Tomorrow, a "This week" card for "Drew's birthday dinner" detected from messages, with a table reservation and a gift. At the end, a "Further out" card for "Jazz festival" in 15 days.](brief-desktop.png)

![The same synthetic brief at 402px phone width: each card wraps its date line, and item rows truncate without overflowing the screen.](brief-mobile.png)

Screenshots use synthetic data injected into the real renderer on a fixture
test instance; no owner data appears in them.
