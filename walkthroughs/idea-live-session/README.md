# Live session banner on an idea card

A session started from an idea used to show up on the idea only as a
"dispatched implement ..." stamp in the faint meta line, written after the
launch request returned, so it arrived late and read as metadata. Now the
card carries a bold bar from the moment Implement, Plan or Approve & build
is pressed, and the bar tracks the session until it ends.
Code: `ideaSessionBanner` / `syncIdeaLive` and `dispatchIdeaRun` in
`static/app.js`; `.idea-live*` in `static/style.css`; `idea_id` on each row
of `GET /api/jobs` (`Sessions.recent` in `server/session.py`).

```mermaid
flowchart LR
  click[Implement / Plan / Approve & build] --> start[Card: STARTING SESSION<br/>before any request goes out]
  start --> post[POST /api/actions/run]
  post -->|job id| named[Card names the job]
  post -->|fails or cancelled| gone[Bar removed]
  named --> reg[GET /api/jobs every 15 s<br/>rows carry idea_id]
  reg --> phase{Session phase}
  phase -->|working| w[SESSION WORKING]
  phase -->|permission or question| n[SESSION NEEDS YOU + Answer it]
  phase -->|turn ended| f[SESSION FINISHED ITS TURN]
  phase -->|session ended| off[Bar removed; the note stamp stays]
```

![Five synthetic idea cards in the Work > Ideas queue. The first has a bright green left edge and a green bar reading "STARTING SESSION, started just now". The second reads "SESSION WORKING, started 4 min ago, job 1a2b3c4d" with an Open session button. The third has a clay-red edge and bar reading "SESSION NEEDS YOU" with an Answer it button. The fourth has a bone edge and a grey bar reading "SESSION FINISHED ITS TURN" with Open session. The fifth has no bar: the card as it looked before.](idea-cards.png)

The screenshot runs the real `jobPhase` and `ideaSessionBanner` from
`static/app.js` against the real stylesheet with synthetic ideas and jobs;
no owner data appears in it.
