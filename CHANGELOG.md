# Changelog

All notable changes to Vira. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); Vira is continuously delivered from `main` with no release versions, so sections are merge dates, newest first.

Since 2026-08-27 every branch lands through a [pull request](https://github.com/MattaDurham/vira/pulls?q=is%3Apr), which carries its write-up. Entries before that link their `--no-ff` merge commit, each of which carries a backfilled PR-style write-up as a commit comment.

## 2026-08-27

### Added

- `pr-layer` - branch.sh gains a PR layer: pr command, merge-gate comment, close-on-discard ([#2](https://github.com/MattaDurham/vira/pull/2))

## 2026-08-26

### Changed

- `dupgate-absolute` - dupgate: the blocking refusal needs an absolute cosine, not just a relative one ([`fbedd07b2`](https://github.com/MattaDurham/vira/commit/fbedd07b2c2feeb94de61468ea61d7ca9d2feb1b))

## 2026-08-16

### Added

- `multi-vaults` - Add federated multi-vault search ([`9094c63d0`](https://github.com/MattaDurham/vira/commit/9094c63d05feb4b6f222f6efe163d6f34e1232bf))

## 2026-08-14

### Added

- `draft-check` - draftcheck: engine, route, and the record-readiness gate ([`f2b95750e`](https://github.com/MattaDurham/vira/commit/f2b95750e6ef79df06b0235b0580341980ed9540))
- `resume-onepage` - applications: ship a one-page resume too, and research the company ([`aae216891`](https://github.com/MattaDurham/vira/commit/aae21689197192e867fb52da448e65f519ec5ceb))
- `universe-expand` - jobboards: Workday fetcher, careers-URL resolver, paste-to-add companies ([`257e1707a`](https://github.com/MattaDurham/vira/commit/257e1707a938521546e937e8995b38254c237a71))

### Changed

- `apply-hiring-signals` - companywiki: read what the employer says it hires for ([`e79f0a354`](https://github.com/MattaDurham/vira/commit/e79f0a3541be734dabf47fa31e4d6c7e1521261a))
- `github-owner` - repo: point every GitHub URL at the renamed account ([`ba86d9f48`](https://github.com/MattaDurham/vira/commit/ba86d9f4870ee0575d6cdce7139a58f6e64faa42))
- `apps-archive-rank` - applications: an archived package never outranks the live one ([`cd4f37fc6`](https://github.com/MattaDurham/vira/commit/cd4f37fc6a421b2e3e48b99c33ffbfd930aadefb))
- `ingest-room` - reader: The Inflow — the nightly ingest as a browsing shelf ([`170834b48`](https://github.com/MattaDurham/vira/commit/170834b48ae62c779e26196b00bbbdd5076eb10e))

### Removed

- `module-chrome` - applications: the compare and map sheets wear real window chrome, and an empty lane takes a dropped document ([`bef0f6431`](https://github.com/MattaDurham/vira/commit/bef0f643168db1b7f36712f01b4d8dd1037aeccc))

## 2026-08-13

### Changed

- `jd-side-by-side` - applications: read two job descriptions against each other ([`cc947046d`](https://github.com/MattaDurham/vira/commit/cc947046daa38a153335f8bb06ea44113aedd1e0))
- `pkg-index-degrade` - applications: a failed package read is never cached as "not written" ([`387b64bb4`](https://github.com/MattaDurham/vira/commit/387b64bb4b39b2dd226ccf7bbe549f05d9bf7d3b))
- `session-resume` - sessions: the compose box outlives the process ([`9eca14220`](https://github.com/MattaDurham/vira/commit/9eca142206560b12623e5d409dcacbe25dffe88e))
- `bulk-rescore` - applications: bulk rescore the filtered set, and the clock on the scored line ([`ea89502cb`](https://github.com/MattaDurham/vira/commit/ea89502cbe6430efea9a381b0e93a1d16a89a449))
- `apply-written` - applications: "Write application", and a filter for what is written ([`b754820d9`](https://github.com/MattaDurham/vira/commit/b754820d95ab8dd15fa4ba18a38a6a7c5fdf52c1))
- `prompt-slugs` - queue: label the plan and export prompts, and cut their padding ([`f0a4aa04d`](https://github.com/MattaDurham/vira/commit/f0a4aa04d1d4cf29249d132ce787acd05f91242f))
- `apply-session-slug` - applications: name every Apply session by company, title and date ([`8da58661e`](https://github.com/MattaDurham/vira/commit/8da58661e79ea6e43335c86fcece43a0b611e073))

### Fixed

- `posting-link` - fix: a drag handle no longer swallows a link's click ([`3d9a35e3a`](https://github.com/MattaDurham/vira/commit/3d9a35e3a2a072d9d68fd9c9d63b362a690bc489))

## 2026-08-12

### Changed

- `perm-default-bypass` - sessions: every dispatch derives the permission default (bypass) ([`cdf7d2bdb`](https://github.com/MattaDurham/vira/commit/cdf7d2bdb8e559ac56cf0767b216f96de1de4bba))
- `scores-store` - scores: one score per role, written by Vira, with staleness reported ([`d15ef5628`](https://github.com/MattaDurham/vira/commit/d15ef5628816bfce55b982b5a1052854c684acb1))
- `serve-flags` - branch.sh serve: read every flag, refuse an unknown one ([`692b2ec89`](https://github.com/MattaDurham/vira/commit/692b2ec897acc7717e678b9371ddad0661ce7585))
- `concept-anchor` - Concept Cloud: open the note ON the passage, not at its top ([`dd3c97a47`](https://github.com/MattaDurham/vira/commit/dd3c97a47633b9ac395544bf5c231d14a456507d))
- `interesting-hamilton-2b0340` - backup: close the 2026-08-10 audit's sole-copy gaps; resolve both phantoms ([`fa05b3de2`](https://github.com/MattaDurham/vira/commit/fa05b3de223d9980f1ae5e95b4ceca389816d42f))
- `jd-workplace` - Read the posting's own workplace policy over the board's remote flag ([`84d56c966`](https://github.com/MattaDurham/vira/commit/84d56c9667a38223af9dd3d9a39f36804bc5c3c4))
- `agitated-jemison-70dd0f` - branch.sh: survive launchd still holding the label ([`61d747007`](https://github.com/MattaDurham/vira/commit/61d747007eaad42c85c1add0df3a458720c7c3f6))
- `agitated-jemison-70dd0f` - branch.sh list: link the address that actually answers ([`1e1e4b594`](https://github.com/MattaDurham/vira/commit/1e1e4b594e883991dc46ca8d348701fff7575d1c))
- `resume-viewport` - Resume viewport: read, question and annotate an application document ([`60343de3c`](https://github.com/MattaDurham/vira/commit/60343de3c96990fd39146e4dd67663a5b1ecbde9))
- Reader: the selection menu works inside document frames ([`31b270889`](https://github.com/MattaDurham/vira/commit/31b27088916717ba2412da814d02fc4b98e3ce34))
- `forge-runs-merge` - runs: one chronological stream instead of three stacked sections ([`a51b6d1c0`](https://github.com/MattaDurham/vira/commit/a51b6d1c04dd95892e0557bb439a0515d9ab0f20))
- Reader: connect external folders as sources, and make flat mean flat ([`651088ed5`](https://github.com/MattaDurham/vira/commit/651088ed5b07580b40ec3c1cb429cea928b640b5))

### Fixed

- `scores-refresh` - scores: rescore a role, and drain the stale backlog ([`2c4010baa`](https://github.com/MattaDurham/vira/commit/2c4010baafad904b4672987749320f8c1545bb23))

## 2026-08-11

### Added

- `review-queue` - Add the needs-review queue: pending decisions surfaced in the brief ([`3ddef9942`](https://github.com/MattaDurham/vira/commit/3ddef994263d029af6622970f51a2d4fc2dbd929))

### Changed

- `research-corpus-path` - Find the research corpus after the self-record restructure ([`cd14861bb`](https://github.com/MattaDurham/vira/commit/cd14861bb255bbccd4cccf80ed744b46e1893a54))
- `facts-master-history` - Repoint Vira at canon/MASTER_HISTORY.md as the one canonical record ([`11a3c9876`](https://github.com/MattaDurham/vira/commit/11a3c98767cf14d1620d33ac4f0af448dfd4b13b))
- `empty-note` - An empty note says so instead of painting a black void ([`1d0a80619`](https://github.com/MattaDurham/vira/commit/1d0a8061916fbc5fb0d8abf069eb6d5fa583b79c))
- `observer-guard` - Guard every reveal observer against a ratio threshold ([`262300c1e`](https://github.com/MattaDurham/vira/commit/262300c1e89ae447a0e6f8d2c6a9881a8fc7738d))
- `rdoc-reveal` - Reader grid: reveal on any intersection, not on a ratio ([`571a25b36`](https://github.com/MattaDurham/vira/commit/571a25b3600bdfdbc871646661c33ea54f83873e))
- `note-cap` - Serve vault notes whole; cap the agent tool honestly (qocha v0.3.2) ([`94c8c06d6`](https://github.com/MattaDurham/vira/commit/94c8c06d6ccc9b79a3dc00555ceefc04e5fd21a4))

### Fixed

- `wikilink-durability` - fix(vault): resolve wikilinks by rank, and index assets at all ([`ab07f5bef`](https://github.com/MattaDurham/vira/commit/ab07f5bef05dfa16bf48643fde41ac63e178dc4a))

## 2026-08-07

### Added

- `implement-screenshot-img-4319-jp-2309b4` - Add mobile selected-text actions ([`6d436af2d`](https://github.com/MattaDurham/vira/commit/6d436af2d1b840506a4357de1c34fc4f31a07189))
- `application-evidence-map` - Build application evidence map ([`6e79a9381`](https://github.com/MattaDurham/vira/commit/6e79a9381ba05693ca5dbcc1b224a1334e09eb53))

### Changed

- `application-map-workspace` - Integrate application evidence map workspace ([`2660688ab`](https://github.com/MattaDurham/vira/commit/2660688abbc4d237d5df7674ce7ab86601bea4c3))

### Fixed

- `application-source-ladder` - fix: ground application builds in full career record ([`d30241dbd`](https://github.com/MattaDurham/vira/commit/d30241dbd47813bc402179ba3ea0929c708fe58a))
- `research-source-integrity` - Repair research source integrity ([`7e1b5e06b`](https://github.com/MattaDurham/vira/commit/7e1b5e06ba50021dce234c2b1387b5926f4b1519))

## 2026-08-06

### Added

- `implement-need-a-job-description-816ef9` - Add job description comparison tool ([`3ccc42a14`](https://github.com/MattaDurham/vira/commit/3ccc42a1481c5f077bc70e691c294779403b83c0))
- `ai-terminology-dossier` - add: interactive dossier publishing support ([`99df0f980`](https://github.com/MattaDurham/vira/commit/99df0f9805455b8a1cec3cecac6479cc77d6bce5))
- `anthropic-research` - Add provenance-aware research workspace ([`e8160b9d0`](https://github.com/MattaDurham/vira/commit/e8160b9d0fd7be9aff6531f58797bbb571229578))

### Changed

- `implement-was-there-a-reason-why-f408f7` - Open job descriptions in native Applications view ([`b6bcce560`](https://github.com/MattaDurham/vira/commit/b6bcce560da7aac7384f245135732fe082fbfea6))
- `implement-okay-now-we-need-to-ma-92b87c` - Route automatic sessions through configured provider ([`8b7286a1d`](https://github.com/MattaDurham/vira/commit/8b7286a1d45d51cfcafc6f5197f85da4a6b6adea))
- `atlas-vault-ops` - Image Atlas vault ops — cluster select, plan-gated moves, multi-vault registry ([`f080d80cf`](https://github.com/MattaDurham/vira/commit/f080d80cfcba7dc15a7ea6f58ed7f6189bb0ca69))
- `image-atlas` - Image Atlas — the vault's images as a 3D galaxy (chaska adapter) ([`dcbff6cee`](https://github.com/MattaDurham/vira/commit/dcbff6cee91dc330efc4a26a1fb3c586bf49997c))

### Fixed

- `research-graph-integrity` - Repair research graph provenance and recurrence ([`03e5196bc`](https://github.com/MattaDurham/vira/commit/03e5196bce5d5827932c549035000b612c0a0a25))

## 2026-08-05

### Added

- `module-story` - module story: right-click a window answers What is this? — the build story ([`20ee4813f`](https://github.com/MattaDurham/vira/commit/20ee4813f9afaf0ba40dd737e44b680a849fecb7))

### Changed

- `reader-in-find` - reader: films tile, cards everywhere, thumbnails, and the Reader joins Find ([`06dbda980`](https://github.com/MattaDurham/vira/commit/06dbda980ada41aff03bbc8b2134ca0ab722b18c))
- `library-shows-all` - reader: the library opens on Everything, not the to-read slice ([`cef2cee50`](https://github.com/MattaDurham/vira/commit/cef2cee50b0599147ebd61217f46408e4a1782d2))
- `define-recede` - define: a summoned definition rides the scrim, an idle one recedes ([`f746acb27`](https://github.com/MattaDurham/vira/commit/f746acb278192412fe3d82db357e6321a5de32cb))
- `atlas-vault-people` - atlas: Beyond the CRM — vault wiki people join the Visual Network ([`a4ce1cdf7`](https://github.com/MattaDurham/vira/commit/a4ce1cdf71d9f4f3af4df0739cde7f7ef95511e7))
- `reader-read-filter` - reader docs: grid view, read filter, verb mark buttons, findable films ([`f2e240bd9`](https://github.com/MattaDurham/vira/commit/f2e240bd9ec0a645e55823176beb9bebd77b4a8c))
- `implement-add-the-ability-to-dra-ba4974` - ideas: attach screenshots and images to ideas — drag-drop, paste, mobile long-press ([`61461170d`](https://github.com/MattaDurham/vira/commit/61461170d57d95bd65e192a434e95fe94b6d43ed))
- `orphan-evidence` - orphanwork: every unlanded row carries its evidence and a Vira read ([`a81976259`](https://github.com/MattaDurham/vira/commit/a81976259ce572da5c0c5a6e0dbe754c73c40a3d))
- `silly-booth-4d67df` - forge: keep Runs cards inside a phone viewport ([`6e41d9b7c`](https://github.com/MattaDurham/vira/commit/6e41d9b7c07258c5648a5f2e209c3f2b86e11d23))
- `reader-provenance` - reader: the documents library, grouped by the feature it belongs to ([`bfc3f5537`](https://github.com/MattaDurham/vira/commit/bfc3f5537de2607e70750989989c2fb904df0197))
- `circuits-encoding` - circuits, plans: degrade on a store already written in cp1252 ([`ab8803e1f`](https://github.com/MattaDurham/vira/commit/ab8803e1fe1b4212f2472617aab706a7dbb36bf4))
- `quirky-fermi-11e3dc` - encoding: pin utf-8 on the forge-plan store IO ([`bf1eca14b`](https://github.com/MattaDurham/vira/commit/bf1eca14b49ebd3365ad3b2331963fc755532d72))
- `score-cooldown` - jobboards: floor between auto-score dispatches ([`dd14ae058`](https://github.com/MattaDurham/vira/commit/dd14ae05817dc42cd71d02b076c69205bb164e59))
- `poll-firstseen` - jobboards: a catalog tombstone that reappears no longer kills the sweep ([`77438d82c`](https://github.com/MattaDurham/vira/commit/77438d82c466631e7ad82dd7d4e1479d895f5006))
- `apps-board-quality` - applications: sort + location filter; board scoring goes automatic ([`645a44aeb`](https://github.com/MattaDurham/vira/commit/645a44aeb6ff547173da233edc10daa6fab853bf))
- `email-read-reply` - mail: read + reply on feed email cards; IMAP timeouts everywhere ([`c157f2bab`](https://github.com/MattaDurham/vira/commit/c157f2bab4bd289b2bcce255a0c153026cb47692))
- `sweep-cap` - reader: sweep cap is round-robin across sources, overflow counted ([`9fa66df52`](https://github.com/MattaDurham/vira/commit/9fa66df521a10fd8d129d8d4323c26ccb63643d3))
- `room-pills` - reader: entity-grounded room definitions — people/source/watch pills, deterministic feed sweep, merge-write path ([`cb287bb01`](https://github.com/MattaDurham/vira/commit/cb287bb010c46864cd2c9a497dfd02da5c4ec625))

### Fixed

- `rdoc-read-repair` - reader: one-time repair for browsers pinned to the unread-default slice ([`fa7f3546b`](https://github.com/MattaDurham/vira/commit/fa7f3546b4e9bad76a967b1890e2d9848c7f4ae2))
- `applications-filter-fixes` - Fix Applications compensation and multi-select filters ([`05c134a8e`](https://github.com/MattaDurham/vira/commit/05c134a8e958ddaef781a441b6e18ce3b63778a4))

### Removed

- `forge-landing` - forge: recent sessions lead Live, pills gone; unlanded work gets Land / Land all ([`17f175e7a`](https://github.com/MattaDurham/vira/commit/17f175e7ac367efb362e4cfc5e6eef2bd16e0250))
- `full-ingest` - reader: full ingest replaces pointer notes — stage material, link summaries, retire pointers ([`8bf0a71bf`](https://github.com/MattaDurham/vira/commit/8bf0a71bfd593ec6c7df20dc5529761ad4712318))
- `reader-wiki-links` - reader: explicit source/wiki links on every room card; retire the Secondhand filter ([`06a9abb0b`](https://github.com/MattaDurham/vira/commit/06a9abb0bf185aac38d2ca8aa9480a3bd40a48ee))

## 2026-08-04

### Changed

- `forge-plan` - A plan is a shape, not a permission mode ([`601965baa`](https://github.com/MattaDurham/vira/commit/601965baa0602bce645753669ffa16ee14e4e298))
- `journal-autodispatch` - journal: unapplied instructions become dispatched work, not a clipboard payload ([`2856fc2b3`](https://github.com/MattaDurham/vira/commit/2856fc2b36b11bed535b923f4a8e4343e86d623d))
- `caffeinate-window` - branch.sh: make the preview's keep-awake window genuinely bounded ([`dd8e4b39a`](https://github.com/MattaDurham/vira/commit/dd8e4b39a28c2d441ca07960b77e0b0830d97c59))
- `sweet-sinoussi-b4b99d` - loopwatch test: hold the block open until the watchdog has noticed ([`9a0a9a1ac`](https://github.com/MattaDurham/vira/commit/9a0a9a1acb012ba2d8a6e8668643574e80c3d36f))
- `aihealth-provider-probe` - aihealth: probe the configured provider's endpoint, not Anthropic's ([`ca47a5651`](https://github.com/MattaDurham/vira/commit/ca47a5651073fe95349f9ecbbff07790a61940f9))
- `define-layout` - Definition can be parked in a stage layout ([`778f922c3`](https://github.com/MattaDurham/vira/commit/778f922c3b4408df21188c54106830cbfa0a30d6))
- `define-clear` - Definition: a spawned window stays clear, outlives Find, and closes with everything else ([`9be4f6e29`](https://github.com/MattaDurham/vira/commit/9be4f6e29bfa2d28d6ab02331bb126938b084849))
- `define-anywhere` - Find cluster sized to the window; Definition pops from anywhere ([`138d2bba3`](https://github.com/MattaDurham/vira/commit/138d2bba3daa4e70c3bec8f0834c1cacb6f4456a))
- `find-companions` - Find cluster: default layout, toolbar toggles, drawer sub-rows ([`88954fa31`](https://github.com/MattaDurham/vira/commit/88954fa31484a770b06529961ce904eb133779b3))
- `export-prompt` - Give the Queue's exported prompt its own text ([`283cd9c51`](https://github.com/MattaDurham/vira/commit/283cd9c51f381a5c32e95f47d5f3e631169dfe37))
- `atlas-define` - Define any selected term, and bank the answer in the vault ([`346368a99`](https://github.com/MattaDurham/vira/commit/346368a99b89f3e003d70ad4eca4aaec6a038aa6))

## 2026-08-01

### Added

- `durham-genre-skin` - Add Durham light skin and simplify onboarding ([`38caddee2`](https://github.com/MattaDurham/vira/commit/38caddee2ad19ce1a5e97b67b43dee9d75d22d4f))
- `forge-integration` - build Forge visual orchestration module ([`48be5040c`](https://github.com/MattaDurham/vira/commit/48be5040cdbf2978248b93df06558cd7c378d8ed))
- `brain-find-chat` - Add persistent vault chat to Find ([`5d522aebb`](https://github.com/MattaDurham/vira/commit/5d522aebb295672d7857e651b055239cc96a9181))

### Changed

- `test-tailnet-access` - Expose passive test instances over tailnet ([`027a722df`](https://github.com/MattaDurham/vira/commit/027a722df8341b36efd871c9912166973a71d700))

### Fixed

- `debug-genre-module` - Fix Create a genre navigation ([`5e2ced230`](https://github.com/MattaDurham/vira/commit/5e2ced2307db8bbc21f75ef0b9b2b12357f66379))
- `brain-find-ux-fixes` - Fix Brain workspace usability ([`fd0789335`](https://github.com/MattaDurham/vira/commit/fd078933501026f4b84e0b4020284fa271ce6428))

### Removed

- `remove-flow-library-close` - Remove Forge library close button ([`4e911b8e2`](https://github.com/MattaDurham/vira/commit/4e911b8e232b8fd324da5be222f13848a6d18d33))

## 2026-07-31

### Added

- `multi-model-instructions` - Multi-model instruction layer: the contract rides the prompt and AGENTS.md ([`29f46197f`](https://github.com/MattaDurham/vira/commit/29f46197f89dd71e7e1dae50b84aba171684e08a))

### Changed

- `onboarding-try-card-lower` - Lower onboarding try card ([`b949c9f29`](https://github.com/MattaDurham/vira/commit/b949c9f29cbffdf589183acb6ad7447b9f814923))
- `sandbox-reset` - Sandbox reset: pull the latest code and hand back a real virgin install ([`ae1dcaeb3`](https://github.com/MattaDurham/vira/commit/ae1dcaeb34e7affaadeb891406749ddf7ab68c76))
- `readme-front-door` - README front door: real clone URL, the git-vs-GitHub-account distinction, an AI-agent path ([`bc88982de`](https://github.com/MattaDurham/vira/commit/bc88982de5063a0130b38ae90115e955c50d70c8))
- `network-chrome` - Visual Network: the web leads, its controls sit under it ([`65930d1df`](https://github.com/MattaDurham/vira/commit/65930d1df7bf9cfd8155f8ac39e71694523a5fa2))
- `tour-both-cards` - Tour: both closing cards at once, no Next ([`2a5ce68ab`](https://github.com/MattaDurham/vira/commit/2a5ce68abaf137f576df0656b5b19bfde7110991))
- `queue-defer` - Defer for muse proposals; filed ideas move to Record ([`ca2b66c66`](https://github.com/MattaDurham/vira/commit/ca2b66c6686ae4fb2a28440133f496deab1eedec))
- `term-chrome` - Terminal home tiles, centered spawn kept; every transient popup centers ([`6942f8c88`](https://github.com/MattaDurham/vira/commit/6942f8c8807a4712732136c1a204ba07e40c8960))
- `great-ishizaka-e879e2` - Applications availability tests compute sightings relative to now ([`6ea8e6671`](https://github.com/MattaDurham/vira/commit/6ea8e6671a806df2d49e627b18e884178b1c6aa8))
- `you-are-vira-s-coding-agent-work-3d4f60` - propose_idea refuses semantic near-duplicates before staging ([`247124d20`](https://github.com/MattaDurham/vira/commit/247124d20c3a2ebc880ab1269699994f9c1c2208))

### Fixed

- `fix-codex-jsonl-limit` - Fix oversized Codex JSONL events ([`3f79766dd`](https://github.com/MattaDurham/vira/commit/3f79766ddc1a6b50679ee493be0df843a02e31d6))

## 2026-07-30

### Added

- `hood-orphan-token` - The hood explains __orphan_sweep__, and a dispatched token can no longer ship unexplained ([`859861cc6`](https://github.com/MattaDurham/vira/commit/859861cc6ee5b5c0a32796d16d851333df9b9d74))
- `firstpaint` - A first run's first sight is the welcome, not a half-built desk ([`ed7058383`](https://github.com/MattaDurham/vira/commit/ed7058383ba6f6a395545298b0f7c3445ef723a1))
- `firstrun-sync` - The first-run welcome can fire again: sync its seen-flag both ways ([`0d855b43c`](https://github.com/MattaDurham/vira/commit/0d855b43cfb0a9ba505b01723edfb80e3b1ef6d9))

### Changed

- `circuit-step-build-744e6e` - Orphanwork tests: stub update.status — the live tree's own git state leaked an unpushed-main row into fixture sweeps ([`d21715eb2`](https://github.com/MattaDurham/vira/commit/d21715eb2fe705b1e2c86c55c135f3f633726be0))
- `circuit-step-build-744e6e` - Orphan-work sweeper: unlanded worktrees and branches, age-ranked, with one-click resume/merge/discard ([`30f947447`](https://github.com/MattaDurham/vira/commit/30f947447d733aa7555d585c5003dcfc19299b66))
- `implement-grok-live-sessions-add-39e377` - Grok CLI re-probed: the blocker is interactive OAuth, not a wedged transport ([`56d9cb43c`](https://github.com/MattaDurham/vira/commit/56d9cb43caec2b8238221e9ee9adcddcdb51004f))
- `lesson-recurrence` - Lesson recurrence: the corrections ledger read back ([`e5863bdab`](https://github.com/MattaDurham/vira/commit/e5863bdab4a739fa5ddf487b9de6e6d3be8e72f3))
- `tour-split` - Split the tour's closing card in two, each pointing at its control ([`9d009f035`](https://github.com/MattaDurham/vira/commit/9d009f03544702cd11d8161ea4d101545d4813a9))
- `work-tour-film` - The Work tour is a film now, played on the Work window itself ([`d6c247031`](https://github.com/MattaDurham/vira/commit/d6c247031c2b43c52e642a7b76686dd96734a30a))
- `routine-hood` - Open the hood on a standing loop: what it does, how it fires, and the source ([`4ad71b4a2`](https://github.com/MattaDurham/vira/commit/4ad71b4a257ca3ec365b9bba028bc05504eb1d5a))
- `aihealth-isolation` - Pin what test_aihealth is actually testing, instead of reading the machine ([`bb019334e`](https://github.com/MattaDurham/vira/commit/bb019334ec6697ba95a4549914160921b46501f3))
- `work-walkthrough` - The Work tour, and a real folder picker instead of "paste the path" ([`b1791033e`](https://github.com/MattaDurham/vira/commit/b1791033e99fb83856a436515fed0457e6ec2a77))
- `room-vault-link` - Reader cards find the vault note that already exists ([`28a765de1`](https://github.com/MattaDurham/vira/commit/28a765de1b6a4897e61b18433b1d74b07c10202d))
- `reader-card-menu` - Reader cards get their own right-click menu; notes get windows ([`67751e990`](https://github.com/MattaDurham/vira/commit/67751e990c9687119ac780500d6450093d2f3503))

### Removed

- `tile-zoom` - Parked zoom follows the layout scale; zoom floor drops to 0.3 ([`f1baa9772`](https://github.com/MattaDurham/vira/commit/f1baa977244c6088230a95b871e2115b98435b49))

## 2026-07-29

### Added

- `room-vault-fix` - Vault projection moves out of build() into the entry points ([`9fd3022f8`](https://github.com/MattaDurham/vira/commit/9fd3022f8da94b8922142a24930d98b6a6012791))
- `midturn-steer` - Steering lands mid-turn, not at the turn boundary ([`b22c5f88e`](https://github.com/MattaDurham/vira/commit/b22c5f88ef4f9d943e44d3646762f4998a8fd54c))

### Changed

- `worktree-tidy` - Worktrees live in the repo, are named for the ask, and go away when empty ([`a46dd96f6`](https://github.com/MattaDurham/vira/commit/a46dd96f65e91e2f5bc163f5bbfcd9c68591d50f))
- `room-vault` - Reading-room items become vault notes ([`49c339546`](https://github.com/MattaDurham/vira/commit/49c339546e9bb4bfb49bc59968d1bda4f7e0a51f))
- `stop-parks-2` - Stop actually parks now — the guard, not the function ([`5b423d3f8`](https://github.com/MattaDurham/vira/commit/5b423d3f8beb2f9c554ea926831729211d21a579))
- `stop-parks` - Stop is a pause, not an ending — it parks like any other turn ([`1d406fcb6`](https://github.com/MattaDurham/vira/commit/1d406fcb646f1916bde06a806f36e8d1f09372e6))
- `turn-closeout` - Turn close-out is the agent's job; record the model that actually ran ([`ef8aa3c69`](https://github.com/MattaDurham/vira/commit/ef8aa3c69b5fe09e67ecd50f9f5a01a218866b21))
- `apps-availability` - Applications: mark postings that have come down, and poll the boards that matter ([`4cd3a8e3a`](https://github.com/MattaDurham/vira/commit/4cd3a8e3a6251599b6fc0004d72aa3b5566e8224))
- `perm-rungs` - Permission rungs: arm the branch guard, name the rungs after Claude Code ([`5e182a048`](https://github.com/MattaDurham/vira/commit/5e182a04833a6c7b6b4bf17c6828bee5645f4f71))
- `ai-reveal-fda` - Config: the Full Disk Access card sells one thing ([`43bee5aef`](https://github.com/MattaDurham/vira/commit/43bee5aef01a3af49b0e80ab85f3b151002ee548))

### Fixed

- `parked-complete` - A parked session reads as complete; fix three lines that lied about state ([`d93793fd0`](https://github.com/MattaDurham/vira/commit/d93793fd04b98fcd8c15c2afeb127b9bd6eca25a))
- `apps-availability` - Pin the open-the-module sweep: stale-only, and honest on a passive instance ([`3fdaae54d`](https://github.com/MattaDurham/vira/commit/3fdaae54d9020f68e226960760ac3511e62d7df4))

## 2026-07-28

### Added

- `group-profiles` - Group profiles: a group chat as a first-class subject, with group send ([`38834b396`](https://github.com/MattaDurham/vira/commit/38834b3964a35a69c71c5738fa50dde490633a89))
- `setup-rethink` - Config becomes a status dashboard; first-run becomes one ask ([`a3a2a8e56`](https://github.com/MattaDurham/vira/commit/a3a2a8e56b128bf3e20ac8c1e4f1183ff5c5f444))

### Changed

- `mail-body-default` - settings: give mail_body_index a default ([`b35cc5b96`](https://github.com/MattaDurham/vira/commit/b35cc5b968af522a6228f5f7a6270d7fa51306c4))
- `mail-backlog-refresh` - Mail bodies backlog: route Graph accounts on their real key, watermark the Graph walk, loop the full backfill ([`0a3917593`](https://github.com/MattaDurham/vira/commit/0a391759320603170e83ba007281c8239cf6598f))
- `agent-install` - Give an installing AI a front door: AGENTS.md, one-shot installer, no-sudo install command, AI-gated routines, guided FDA ([`fa1739be5`](https://github.com/MattaDurham/vira/commit/fa1739be5fa31966d920a934e6dc332e3d6681b7))
- `model-names` - Show only model names Vira can verify right now ([`37f1865ad`](https://github.com/MattaDurham/vira/commit/37f1865addef6f5f2a13b6857af2631989b33503))
- `queue-buttons` - Queue: drop the Plan button once a plan exists, rename Copy to Export prompt ([`1246b40b9`](https://github.com/MattaDurham/vira/commit/1246b40b9712648f5eae5dc1c7ad9318ca133a16))
- `decision-layer` - Raise a session's decisions over whatever module you are in ([`88a499e22`](https://github.com/MattaDurham/vira/commit/88a499e22c6a5d5f6b343e99b6dc3ed97a854d82))
- `vigilant-sinoussi-698c9e` - Keep a finished session honest about which engine answered it ([`7b9de97ac`](https://github.com/MattaDurham/vira/commit/7b9de97ac1d683ca75ae20d5d1e130dac84c8040))
- `preflight-ci` - Preflight: make a red CI run block the merge it should have blocked ([`ba5bdd7be`](https://github.com/MattaDurham/vira/commit/ba5bdd7be7c380b1ddbe17a402c0b6ec01f52406))
- `ci-windows-green` - Make the Windows suite green again: a shebang is not a binary, a coarse clock is not zero ([`30d0ab1cf`](https://github.com/MattaDurham/vira/commit/30d0ab1cff71ae96bc1499daea247ef6e98471cf))
- `model-sessions` - Any-model live sessions: a backend seam, a model roster, honest grades ([`daa9d053e`](https://github.com/MattaDurham/vira/commit/daa9d053e63e10631b75564381e2963a415acb30))
- `reverent-mestorf-568901` - Stop CPU work in one window from darkening the whole server ([`27274e61c`](https://github.com/MattaDurham/vira/commit/27274e61cc7c3c576e005285d1be2a588e28f6e0))
- `genre-maker` - Genre Studio: strip the skin, rebuild around image-prompt fragments ([`c6bc34bef`](https://github.com/MattaDurham/vira/commit/c6bc34bef499e79380249dfe29ca7a876fbadda0))

### Fixed

- `mail-poison` - Mail walk resilience: step past poison messages, hold the watermark on a dying connection ([`fed947f45`](https://github.com/MattaDurham/vira/commit/fed947f457b431f6a7bfdc4cbc75be3fd9556943))
- `readinglist-isolation` - Root the Reader sweep at its fixture: an empty checkout has nothing to leak ([`f25acf9e4`](https://github.com/MattaDurham/vira/commit/f25acf9e4979ce98dd02b20865c31427ade8c572))

## 2026-07-27

### Added

- `evidence-ledger` - Evidence Ledger: build provenance mined into interview case studies ([`c70412436`](https://github.com/MattaDurham/vira/commit/c70412436296e4ad62f2e82456206f6b1b48e9ea))

### Changed

- `copy-sweep` - copy sweep: plain ASCII title tags in the blog and docs-shelf templates ([`2693ef914`](https://github.com/MattaDurham/vira/commit/2693ef91484f36661e1035559f8ec1072d434df5))
- `docs-merge` - sitedocs: unescape entities in extracted titles (a title is text, not markup) ([`b1b4a919f`](https://github.com/MattaDurham/vira/commit/b1b4a919f89bef8659ca1cdee85d67f8ed59ff32))
- `docs-merge` - blog gate: excuse the one canonical false positive (the site's own domain), nothing else ([`c2f30dc1d`](https://github.com/MattaDurham/vira/commit/c2f30dc1dc51cc78657e3b4ffcec314356575cf4))
- `docs-merge` - Documents merge: sitedocs migration + Vira blog pipeline ([`7ea7c3bd1`](https://github.com/MattaDurham/vira/commit/7ea7c3bd10e447f2581d36aa87d5b8c3e9c097fa))
- `reader-queue` - Reader: one queue for everything worth reading, wherever it lives ([`79548cf69`](https://github.com/MattaDurham/vira/commit/79548cf69de3967d8c4df3df03e471bd9903a92e))
- `idea-graph` - Queue: idea tags, similarity, and fold-in at dispatch ([`b06b3adab`](https://github.com/MattaDurham/vira/commit/b06b3adab8a13e6411d1131367027cca8f37d6bd))
- `routine-no-park` - Machine-dispatched sessions finalize at turn end instead of parking ([`c17d137cb`](https://github.com/MattaDurham/vira/commit/c17d137cbe5ec1f57a18d736a6982e3aba010eea))
- `mobile-width-402` - Mobile: kill both page-widening behaviors on iOS (WebKit engines) ([`a8c678acd`](https://github.com/MattaDurham/vira/commit/a8c678acd5ad93a4b439db0f25460f915d409f34))
- `pivot-reconnect` - Pivot reconnect: dormant contacts re-ranked against the live job search ([`a652a7333`](https://github.com/MattaDurham/vira/commit/a652a7333de7c83dc52975b10386036f2cbeefc7))
- `loop-consolidate` - Brief: consolidate multiple owed-by-me loops per person into one bundle row ([`8a18cb32c`](https://github.com/MattaDurham/vira/commit/8a18cb32c529a1e3e698242d86f0212cd32805c7))

## 2026-07-25

### Changed

- `preflight` - Preflight: encode process lessons as checks, not paragraphs ([`92a9d6517`](https://github.com/MattaDurham/vira/commit/92a9d65172b9561603e9ca2ee918710f843aafc0))
- `daily-provenance` - Find: exhaustive literal search, and stop truncating at eight ([`fef55f240`](https://github.com/MattaDurham/vira/commit/fef55f24083443eb9c844e26b31c3a6bef7bbb46))
- `pii-scrub` - Scrub a real name and two real companies from a public docstring ([`ca8e57303`](https://github.com/MattaDurham/vira/commit/ca8e573038ec002ce2362f4e8110eb51a414e5de))
- `ci-green` - CI: the missing dependency, and the tests that only ever ran on a Mac ([`ad0e652ad`](https://github.com/MattaDurham/vira/commit/ad0e652ad5225429aea402da7da69d543d51f4b2))
- `decision-picker` - Decision card: a numbered picker; and unpin the parked terminal ([`802a77c55`](https://github.com/MattaDurham/vira/commit/802a77c55e42dd7a7ae75ba50a4463fc77dcb542))
- `reader-scrollbar` - Reading rooms: the scrollbar was the browser's, not Vira's ([`93c900301`](https://github.com/MattaDurham/vira/commit/93c900301e6c6c8a7b13ba09136d4143432f40ac))
- `radar-networking-fold` - Radar folds into People as the Networking tab ([`7ddce8365`](https://github.com/MattaDurham/vira/commit/7ddce83650a96c5252c274b1d3bbae5c7f7806a8))
- `subs-account-email` - Subscriptions: record the account a sub bills to, and surface duplicates ([`2a0a53233`](https://github.com/MattaDurham/vira/commit/2a0a53233871a9ebd78c705e7dd92b6971d21658))
- `session-harness` - Session harness: place sessions in a worktree, and let them ask ([`eb717a822`](https://github.com/MattaDurham/vira/commit/eb717a82266da45a9386a28e3cf677d3436b1073))

## 2026-07-24

### Added

- `genre-studio` - Genre Studio: build a skin from reference images, like patching a synth ([`0a58dae98`](https://github.com/MattaDurham/vira/commit/0a58dae9878a7a6a8fd15a00f9f1b0bb4a390319))

### Changed

- `apple-contacts-push` - Apple Contacts write-back: the Apple spoke of the CRM sync engine ([`7d9097376`](https://github.com/MattaDurham/vira/commit/7d90973761a522e5a8b2a5fb23831c93bac27a8f))
- `queue-sort-notes` - Queue: journal notes sort with the ideas — no more pinned lane ([`23f1dc7f1`](https://github.com/MattaDurham/vira/commit/23f1dc7f16a9dbf66733ddebba0a58d8d0d3999e))
- `parked-inert` - Parked modules: claim the whole press, not just the click ([`6cd5d5700`](https://github.com/MattaDurham/vira/commit/6cd5d57007c46507e134e5f128f479faa9773abe))
- `layout-states` - Layout edit: pass-correct chrome, saved parked scroll, session grow memory ([`8bedd2ca4`](https://github.com/MattaDurham/vira/commit/8bedd2ca4ad55406819ce82f8f0ea116b10d14a1))
- `layout-zoom` - Layout: content zoom is per state, and stops bypassing every guard ([`3b4ac9f8b`](https://github.com/MattaDurham/vira/commit/3b4ac9f8bf05b01f5c73afea82da4d7e0669945a))
- `layout-stage` - Layout: stage is a property of a layout, with staged grow positions ([`fe6a862b0`](https://github.com/MattaDurham/vira/commit/fe6a862b04071cc6d353c9eb419f00fdd0271d0d))
- `layout-edit-mode` - Layout templates: edit mode — right-click to tune & save a layout ([`ba3e741c1`](https://github.com/MattaDurham/vira/commit/ba3e741c1659ebacf9ff31d9746c3d523587752d))
- `layout-templates` - Layout templates: Perimeter — dock modules to the edges, grow to a stage ([`e45734074`](https://github.com/MattaDurham/vira/commit/e45734074fc983e39ec643a510e4ec7d673df743))
- `queue-journal-clear` - Queue: clear "needs a session" cards; fold completed in every sort ([`cfd5e9b12`](https://github.com/MattaDurham/vira/commit/cfd5e9b121f4f12ecbdf07a489a19285aeb26a08))
- `skins` - Skins: genre-compiled jumping-off points at the top of the Design Studio ([`56ee8864e`](https://github.com/MattaDurham/vira/commit/56ee8864e80865a4cec5c1bc707798c076b8f642))

## 2026-07-23

### Changed

- `loops-edit-refine` - Loops/hooks edit: strip the loop toggles, label the fields, unify hover ([`dbdb94bc4`](https://github.com/MattaDurham/vira/commit/dbdb94bc46d04cf3a9354cac30bbde8445c8d4bd))
- `crm-contact-card` - Contact card: an editable top pane for every CRM person ([`1c2989155`](https://github.com/MattaDurham/vira/commit/1c2989155b19d6d9c223e8265877e4cb669aee2c))
- `launchpad-longpress` - Launchpad: press and hold works at the desk, not just on the phone ([`103da6063`](https://github.com/MattaDurham/vira/commit/103da6063fa0bcd5803699bd371820dd16600f80))
- `imessage-sms-fallback` - Send: SMS fallback for iMessage-less (Android) recipients ([`b41b51524`](https://github.com/MattaDurham/vira/commit/b41b51524f149aaf66cbf3929edf95b7110c9e79))
- `queue-copy-button` - Queue idea rows get a Copy button beside Plan/Implement ([`511860581`](https://github.com/MattaDurham/vira/commit/511860581c67199fc6a1d87a85bad6316bde78b1))
- `perm-mode` - Permission ladder replaces the autopilot checkbox; sessions hold open for a reply ([`bfcfb3b5f`](https://github.com/MattaDurham/vira/commit/bfcfb3b5f9356936cb6c4ea7adc46843c1e172a1))
- `focused-mclaren-9b507e` - Mobile feed and brief stop overflowing the phone screen ([`22fb16483`](https://github.com/MattaDurham/vira/commit/22fb16483451aba1fbb5ceb591b5eb82d1efa5c8))

### Removed

- `ideas-sort-refine` - Queue sorts: merge the redundant status view, relabel by axis, fold done/dropped ([`d6de02ff3`](https://github.com/MattaDurham/vira/commit/d6de02ff3f13a3a1118d8db961c52a385d13b60c))

## 2026-07-22

### Added

- `new-session-9e106b` - Radar: introductions become groupings, and what people send you becomes a reason to talk ([`e6acdaa3f`](https://github.com/MattaDurham/vira/commit/e6acdaa3f0964f3934091451c44ee36371669ffb))
- `new-session-28db08` - Mobile nav: the Launchpad becomes a left column you swipe in ([`277ac983d`](https://github.com/MattaDurham/vira/commit/277ac983d11aa4ecc030fc7a11312194af62c881))
- `channels-paint` - Setup Phone & channels + Updates cards: defer first loads past card attach ([`b1784f360`](https://github.com/MattaDurham/vira/commit/b1784f360ac6d5dfa91e9c6577d2692c82b18a9b))

### Changed

- `drawer-slide` - Drawer slides the page instead of zooming it out ([`2b7ee079f`](https://github.com/MattaDurham/vira/commit/2b7ee079fc73c8c97491c93d94a932e0c15ec79a))
- `new-session-f3ca83` - branch.sh finds a branch's worktree wherever it lives ([`63b670036`](https://github.com/MattaDurham/vira/commit/63b670036c191dd0a1ed1557dac17cd4f36c4223))
- `new-session-6fadbb` - crmindex: pending means no vector, not changed text ([`8906887a7`](https://github.com/MattaDurham/vira/commit/8906887a72c37e77c3ceb7e02cb20c67ac25d541))
- `new-session-6fadbb` - Find: one search over notes, media, people and messages ([`bfd3750a3`](https://github.com/MattaDurham/vira/commit/bfd3750a3e41d4aaed13db82be3ec4cba7058c10))
- `new-session-f3ca83` - Contact photos: the most recently modified card wins across stores ([`e7fe22391`](https://github.com/MattaDurham/vira/commit/e7fe2239182bf70c8481669bae2439e1369c5ef4))
- `skill-popup-escape-drag-e90d3a` - Sheets answer Escape, and their header is a title bar you can drag ([`2d7a40ea8`](https://github.com/MattaDurham/vira/commit/2d7a40ea8316a4f6477696edb4b78361c49cf405))
- `new-session-f3ca83` - Shared media survives iCloud eviction; contact photos refresh ([`5079b9863`](https://github.com/MattaDurham/vira/commit/5079b98633da019062f310fe6a87d5a5a0e68c65))
- `personal-modules-onboarding-100837` - Module front doors: the path from a dormant module to a live one ([`f143e9346`](https://github.com/MattaDurham/vira/commit/f143e93468d52c6f4c756a3c108f8a0c7953e4f2))
- `new-session-a9458b` - Model dropdowns that know what's installed, and circuit steps you can tune ([`1314e31eb`](https://github.com/MattaDurham/vira/commit/1314e31eb5f3dc27955eb14a76348a964609f5a3))
- `mobile-long-press-reorg-64cfca` - Phone: a five-app access bar, a column that fades into it, and long-press to rearrange ([`4b59a699f`](https://github.com/MattaDurham/vira/commit/4b59a699fa784758f11b6abc7eeb81e2cfbfaaf5))
- `beautiful-easley-2c288f` - branch.sh: a vanishing sqlite sidecar no longer kills the data clone ([`4e0a05616`](https://github.com/MattaDurham/vira/commit/4e0a056166a777bf31fbbbd20ecb40661f76496b))
- `home-screen` - Home screen: a real app icon, and no same-origin tab escapes on mobile ([`19151da57`](https://github.com/MattaDurham/vira/commit/19151da57175e38d62de34c5708f73b20a178af7))
- `charming-euler-ee1a84` - Make the subscriptions suite deterministic on every calendar day ([`e98127d95`](https://github.com/MattaDurham/vira/commit/e98127d950c01cc5ac19967764eef6870ccdf67e))
- `ci-utf8` - utf-8 everywhere: pin text encoding at the wave-2 read sites ([`1687d46bb`](https://github.com/MattaDurham/vira/commit/1687d46bb0eeeddac9706dfaa7a2558a7420836e))
- `consolidation` - backup: cover applications.json, mail-accounts.json, circuits.json ([`25a44a7f4`](https://github.com/MattaDurham/vira/commit/25a44a7f4739b998d5509074d894cef4e5eec645))

## 2026-07-21

### Added

- `guided-setup` - Guided setup: AI first, one step at a time ([`e019d19d5`](https://github.com/MattaDurham/vira/commit/e019d19d57991f4dfc543206d30cdec0170774c0))
- `first-run-defaults` - First run opens three windows, not seven ([`99d0bc681`](https://github.com/MattaDurham/vira/commit/99d0bc681e92af5f397f3aeebca99c2ac877fa9a))

### Changed

- `plan-viewer` - Plans window: save Plan-mode output to the vault, reopenable in-app ([`e0780c130`](https://github.com/MattaDurham/vira/commit/e0780c130c7a587575a3945f92f15bb7dc5fe1f8))
- `p4-windows` - Windows install: run.ps1, Task Scheduler supervisor, honest wizard ([`850d33b15`](https://github.com/MattaDurham/vira/commit/850d33b15fd9a105b27877c0a257cc0687b1c14e))
- `p3-whatsapp` - WhatsApp connector: linked-device sidecar, receive-only v1 ([`d903f67bd`](https://github.com/MattaDurham/vira/commit/d903f67bd30c85c8d95feeb13e0b681810aca30a))
- `p2-android` - Companion hub: pairing, SMS/notification ingest, pings ([`bd7c85939`](https://github.com/MattaDurham/vira/commit/bd7c859399d5f0c70baec250de1957d76f02cb79))
- `p1-sources` - P1 cross-platform: source registry + platform-forked Setup ([`4997ab679`](https://github.com/MattaDurham/vira/commit/4997ab67995961465ecc14fb502c90efdd1b9fb3))
- `portable-core` - Login cards hand over commands that actually work ([`96b3ae5a3`](https://github.com/MattaDurham/vira/commit/96b3ae5a3231838b1c96356acf6216ffc9d8cc04))
- `sandbox-harness` - Sandbox harness: test the download as a stranger would ([`586ff1a5c`](https://github.com/MattaDurham/vira/commit/586ff1a5c6a07cc9f60639179838105e7ec7f622))
- Make the subscriptions suite deterministic on every calendar day (#1) ([`cea017cd3`](https://github.com/MattaDurham/vira/commit/cea017cd3537afe45a38a73111c2da1d3b849e9c))
- `v9-onboarding` - v9 onboarding: Setup window, contact importers, dossier builder, Brain wiring ([`5fdedff20`](https://github.com/MattaDurham/vira/commit/5fdedff209323c9180a3e3252abebd325ee27bbe))
- `qocha-pin` - Updater installs dependencies on apply; pin qocha to v0.2.0 ([`6c384417f`](https://github.com/MattaDurham/vira/commit/6c384417fd0a562be5a8b23abb812f0c5387bc54))

### Fixed

- `triage-resolver` - Triage contact resolver: Tell-Vira box + referral traversal, pill wrap fix ([`c1684dba7`](https://github.com/MattaDurham/vira/commit/c1684dba7233a6569a6af3af88df9cc2467368cc))

### Removed

- `setup-config-merge` - Merge Settings into the Setup window; retire the Phone Link module ([`5727d23b3`](https://github.com/MattaDurham/vira/commit/5727d23b3eb4690012576cc88ab92758b80f0f9e))
