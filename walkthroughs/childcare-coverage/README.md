# Childcare coverage gaps

The owner enters each child's pickup time and who normally covers each
school day in the Childcare window. Vira watches three things against that
routine: a caregiver cancelling in iMessage, a half day on a school
calendar, and a closure on a day nobody covers. Each gap gets one timed
pickup block on the family calendar and one "who's taking it?" question,
asked once. Code: `server/coverage.py`; calendar path `plan_coverage` in
`server/calendarplan.py`; Decide card in `server/reviewqueue.py`; window in
`static/app.js` (`renderCoverage`).

```mermaid
flowchart LR
  routine[(data/coverage.json<br/>kids, pickup times,<br/>who covers which day)] --> detect
  msg[iMessage watcher<br/>'Pat can't come Monday'] --> detect[Gap detection<br/>deterministic regex]
  school[School calendar<br/>Calendar.app name or ICS feed] --> detect
  detect -->|half day, uncovered closure,<br/>cancellation| gap[Gap, id = date + cause + kids<br/>opened once]
  gap --> block[Pickup block on the<br/>family calendar, no invitees]
  gap --> card[Decide card + one iMessage ping<br/>I'll take it / caregiver / Not needed]
  card --> answer[Answer recorded;<br/>Not needed withdraws an unwritten block]
```

![Desktop Childcare window with synthetic data. Three red gap cards: a cancellation ("Who's taking Ada at 2:55pm on Wednesday Oct 21? Pat can't cover.") with buttons I'll take it, Robin, Not needed; a school closure on Friday Oct 23 nobody covers; and a half day moving Ada's pickup to 12:30pm. Below, the coming days list each child's pickup chip with who covers it, red where someone is needed; Monday Oct 26 shows Robin after that gap was answered.](childcare-desktop.png)

![The same synthetic window at 402px phone width: gap cards stack, answer buttons wrap, and each day's pickup chips wrap without overflowing the screen.](childcare-mobile.png)

Screenshots come from a fixture test instance with a synthetic routine
(children Ada and Bea, caregivers Pat and Robin), a synthetic ICS feed served
on localhost, and synthetic messages. Calendar writes are refused in fixture
mode, which is the "Connect real sources" line on each card.
