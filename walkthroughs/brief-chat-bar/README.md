# Chat bar under the daily brief

A chat bar at the bottom of Attention > Today lets the owner ask Vira, or
tell Vira to act, about the day the brief shows. It is Vira's own chat (the
one in Find), not a second one: the bar opens a chat with origin `brief`, so
the session is told the owner is writing from the brief, is handed today's
summary, and confirms before anything is sent as the owner. One
conversation per local day is remembered on each device; Open in chat
continues it in Find. Code: `server/virachat.py` (`ORIGINS`,
`_origin_context`) and the "brief's chat bar" block in `static/app.js`.

```mermaid
flowchart LR
  A[Chat bar under the brief] -->|POST /api/vira/chat origin=brief| B[virachat.send]
  B --> C[Agent session on Vira's tools<br/>told: from the brief, today's summary]
  C -->|progress, then the answer| D[Conversation above the bar]
  C -->|asks first before sending as the owner| E[ask_owner card]
  D -->|Open in chat| F[Find chat, same conversation]
  D -->|typing while Vira works| G[guidance to the running turn]
```

![Desktop: the end of Attention > Today with the chat section. A heading "Today's chat with Vira" with Open in chat and New buttons; the owner asked to move a 3pm design review to tomorrow morning and tell the team, and Vira replied that it moved the review to 09:30 and left a note to the team in Drafts; a second question about a site visit shows "Reading sources... 12s"; the input reads "Vira is working. Add guidance, or press Stop" with Stop and Send guidance buttons.](brief-chat-desktop.png)

![Phone at 402px: the sample brief's Today and Tomorrow cards, then the chat with one exchange, and the input row pinned just above the bottom tab bar.](brief-chat-mobile.png)

Screenshots use a synthetic conversation injected into the real renderer on
a fixture test instance; no owner data appears in them.
