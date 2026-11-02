# Design Studio ships with Vira

The Design Studio window frames `/design/studio.html`. That page used to
live only in a separate design-system checkout (config
`design_foundation_root`, default `~/workspace/design-foundation`). A fresh
install had no way to get that checkout, so the window showed its skins and
background pickers above an empty 404 frame. The studio is now bundled in
`static/design/` and works on first open with no setup. A configured
checkout that holds a `studio.html` still takes precedence. The folder is
picked on every request, so a checkout cloned while Vira runs is served
without a restart, and `GET /api/design/status` names which copy is served.
Code: `server/designstudio.py` (`serve_root`, `status`, `CheckoutFiles`),
the `/design` mount in `server/main.py`.

```mermaid
flowchart LR
  open[Open Design Studio window] --> frame[iframe /design/studio.html]
  frame --> mount{design_foundation_root<br/>set and holds studio.html?}
  mount -->|yes| ext[Serve the external checkout]
  mount -->|no, the default| bundled[Serve static/design/<br/>bundled with Vira]
  bundled --> edit[Edit tokens on the live app<br/>and the Gallery]
  edit --> save[POST /api/design/save<br/>target vira: static/style.css]
  ext --> fsave[target foundation:<br/>writes the checkout, 404 when unset]
```

![The bundled Design Studio on a synthetic-data test instance: the STUDIO
toolbar with Vira and Gallery targets, the desktop canvas and mobile
parallel of the app, and the Styles rail listing Vira's color tokens (bg,
text, accent, oxidized, steel, green, red) read from the app's own
stylesheet.](studio.png)
