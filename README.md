# gqa-gonogo-version-sync

Keeps the **MAX Version Number** and **DPlus Version Number** columns of the GQA
Go/No-Go sign-off page in Confluence in step with the latest builds, on a
15-minute schedule, the same way the Fuse platform version dashboard tracks
builds.

Every 15 minutes the job reads the current builds per platform, derives the
per-brand version for each client row, and rewrites only those two columns on
the sign-off page. Everything else on the page — status macros, owners, notes,
rows this repo doesn't know about — is left exactly as it was.

## How it works

```
cron (15 min) -> GitHub Actions -> build API -> version mapping -> Confluence REST v2
```

1. **Read the page** (`gonogo/confluence.py`) and take the release train from its
   title, so a 7.13.0 build can never land on a 7.12.0 sign-off page.
2. **Fetch** the latest build per platform (`gonogo/providers.py`).
3. **Map** each device onto a client row (`gonogo/config.py`). A sign-off row
   can cover an app family rather than one device, so `Android` reads the phone
   then the Fire tablet, `LB` (leanback) reads Android TV then Fire TV, and
   `CDEV` reads Samsung then LG. The first device that reported wins, and a run warns when the others
   disagree. Versions are written as the API reports them; the `+14` store
   offset (`21.x.y.build` on Apple and Android, base `7.x.y.build` on Web, Roku
   and CDEV) is only used to check that a reported D+ suits its row.
4. **Update** the table and publish only if something actually changed.

### Things it deliberately does

- **No empty-page revisions.** If the table already matches the builds, nothing
  is published. Otherwise you'd get 96 meaningless page versions a day.
- **No silent blank.** A configured row the table does not have fails the run
  rather than warning into a log nobody reads, which is how the two Apple rows
  stayed empty for a day. Rows that did match are still written first.
- **No odd row out.** Versions are written as inline code, which is how the
  table's hand-typed rows are formatted. A cell holding the right version in
  bare text is rewritten so the column reads the same all the way down;
  `confluence.cell_format: plain` turns that off.
- **No macro corruption.** Confluence storage format is parsed as XML, not HTML.
  An HTML round-trip rewrites `<ac:structured-macro/>` and Confluence rejects the
  result, which would break every macro on the page.
- **No blanking on a bad fetch.** A platform missing from the API is logged and
  skipped, never written as an empty cell.
- **No cross-train writes.** A `7.13.0.x` build will not be written to a `7.12.0`
  sign-off page while `release.enforce_train` is on.
- **No lost race.** If someone edits the page between the read and the publish,
  Confluence answers 409; the run re-reads and reapplies instead of waiting for
  the next tick.

## The build source

Wired to the Fuse build API documented on *Build Fetch API — curl Reference*
(child of the Fuse platform version dashboard). Base URL:

```
https://sprintedge.gqa.discomax.com/sprintedge/fuse
```

The two sign-off columns are two products of the same endpoint, so the sync
makes two calls to `/api/latest-versions` per run and merges them by device:

```
GET /api/latest-versions?token=…&environment=Blue&product=Max      -> MAX Version Number
GET /api/latest-versions?token=…&environment=Blue&product=D-Plus   -> DPlus Version Number
```

Things worth knowing about this API, all of them handled in `config/clients.yml`:

- **Auth is a query parameter**, `?token=<gate token>`, not a header. A JFrog
  `api_token` is not a substitute: it only opens `get_latest_build_url`, and
  every JSON route answers `401 {"error": "Invalid or missing access token."}`
  without a gate token or a browser session. The sync says as much on a 401.
- **The token stays out of the logs.** Query auth is merged in at the moment of
  the request, so neither the log line nor the job summary nor the probe report
  echoes it. An unset `FUSE_API_TOKEN` fails before the request rather than
  sending nothing and reporting the API's 401, because an empty variable and a
  rejected credential otherwise produce the same message.
- **A browser session also works**, which is the way to test before a gate token
  exists: copy the whole `Cookie` header from devtools into `FUSE_API_TOKEN` and
  switch to `auth: header` with `options.auth_header: Cookie`. Session cookies
  expire, so this is for a one-off local check — the cron needs a gate token.
- **`version` is not always a version.** It can be `N/A`, `ComingSoon`,
  `NotBuilt` or `Error`, and a card may carry `fetch_error`. Those are reported
  and skipped, never written into a cell. A platform with no build yet is logged
  at info; a failed lookup is a warning.
- **Environments** are `Orange` (integration), `Blue` (staging) and `Green`
  (release candidate / prod). This sign-off tracks `Blue`. The grid opens on
  `Orange`, so what the page says will not always match what the dashboard shows
  on screen — check the environment before calling it a bug.
- **`flavour`** (`BASE`, `EMEA`, `AMER`) applies to Swift iOS/tvOS only and is
  left unset, which the API reads as `AMER`.
- **Devices** are `FireTablet`, `Android`, `FireTV`, `AndroidTV`, `tvOS`, `iOS`,
  `Roku`, `Samsung`, `LG`, `Xbox`, `Web`, `playstation-4` and `playstation-5`,
  matched case-insensitively. AAOS, Vega, Chromecast and VisionOS are not
  devices this API builds, so they cannot back a row.
- **`requested_date`** defaults to today server-side, so the config leaves it
  unset rather than pinning a date in the runner's timezone.
- **TNT-Sports and TVE** are other products of the same API and not part of this
  sign-off. TVE would additionally need a `brand` (network) parameter and has no
  tree on either PlayStation.

### D+ versions are never invented

D-Plus arrives as its own product, so its version is written exactly as
reported. When a platform reports no D+ build — iOS and tvOS both do on the
7.12.0 train — **the D+ cell is left as it is** and the run says so.

The `+14` store offset is only used to check that a reported version suits the
row's scheme, not to fill a gap. Borrowing MAX's build octet would name a build
that was never produced: iOS is MAX `7.12.0.73` against a real D+ of
`21.12.0.16`, so the guess would be `21.12.0.73`, a version nobody can install.
Set `release.derive_missing_dplus: true` to accept that guess anyway.

A reported D+ version whose major disagrees with the row's `dplus_scheme` is
still written, but logged as a warning: it usually means the row points at the
wrong platform key.

### Checking it before trusting it

```bash
export FUSE_BUILDS_API_URL='https://sprintedge.gqa.discomax.com/sprintedge/fuse/api/latest-versions'
export FUSE_API_TOKEN='<gate token>'
export ATLASSIAN_USER_EMAIL='you@wbd.com' ATLASSIAN_API_TOKEN='...'

# What the API returned, how it maps, and whether the page's rows line up.
python -m gonogo.probe --show-payload

# Rehearse the write, then publish.
python -m gonogo.sync --dry-run
python -m gonogo.sync
```

If the response shape ever changes, `python -m gonogo.probe --show-payload`
prints a `source.response` block guessed from the live payload, ready to paste
into the config.

The probe writes to Confluence never, and prints:

- the exact requests it made (URL, query, brand),
- the raw payload with `--show-payload`,
- a `source.response` block guessed from that payload,
- the platform keys parsed, with the MAX and D+ version found for each,
- configured platforms missing from the payload, and keys the config ignores,
- the page's column headers and client rows, and which config rows matched.

### Request shapes the config covers

```yaml
source:
  auth: query             # query | bearer | token | basic | jfrog | header | none
  options:
    auth_param: token     # query-string name for the token
  method: get             # post sends `body` as JSON, for query-style endpoints
  query:
    environment: Blue
    # requested_date: "{today}"   # tokens expand per run, any strftime format
  brand_requests:                 # one call per entry, merged by platform
    - { brand: max,   query: { product: Max } }
    - { brand: dplus, query: { product: D-Plus } }
  response:
    records_path: devices         # dot path to the records ("" = root)
    platform_field: name
    version_field: version
    built_at_field: date
    brand_field: brand            # only when one response mixes brands
    max_brand_value: [max, hbomax, benelux]
    dplus_brand_value: [dplus, discovery]
    brand_versions_field: versions  # when one record nests several brands
    live_field: isLive              # prefer the promoted build
```

Brand lists are preference order. The dashboard cards show a platform's builds
labelled `HBOMAX` and `BENELUX`, so `max_brand_value: [hbomax, benelux]` reads
as "HBOMAX in the MAX column, BENELUX only if that's all this platform reports";
a label in neither list is ignored. With `live_field` set, the build carrying
the dashboard's `LIVE` badge wins over a newer one of the same brand, because a
sign-off tracks what was promoted rather than whatever built last.

Understood payloads: a flat list of records, a map keyed by platform, records
nesting a `{brand: version}` block, one request per brand, and a version
embedded in an artifact filename (`max-android-7.12.0.66-release.apk`). Date
tokens are `{today}`, `{yesterday}`, `{tomorrow}`, `{now}` and `{epoch_ms}`,
each accepting a strftime format, and they work in query values and anywhere in
a POST body. If a real response needs a shape beyond that, `collect_builds` in
`gonogo/providers.py` is the single place to change.

Switching to Artifactory instead is `BUILD_PROVIDER=jfrog` plus a
`source.options.aql_searches` block; the Confluence half stays untouched.

## Setup

### 1. Repository secrets

`Settings -> Secrets and variables -> Actions -> Secrets`:

| Secret | What it is |
| --- | --- |
| `ATLASSIAN_USER_EMAIL` | Atlassian account that will edit the page |
| `ATLASSIAN_API_TOKEN` | [API token](https://id.atlassian.com/manage-profile/security/api-tokens) for that account |
| `FUSE_BUILDS_API_URL` | `https://sprintedge.gqa.discomax.com/sprintedge/fuse/api/latest-versions` |
| `FUSE_API_TOKEN` | SprintEdge gate token, sent as `?token=` (a JFrog token will not do) |
| `JFROG_URL` / `JFROG_TOKEN` | Only for `BUILD_PROVIDER=jfrog` |

The account behind `ATLASSIAN_API_TOKEN` must have edit permission on the page.
A service account is better than a personal one: page history will show every
sync under that name.

### 2. Repository variables

`Settings -> Secrets and variables -> Actions -> Variables`:

| Variable | Default | What it is |
| --- | --- | --- |
| `SYNC_RUNNER` | `ubuntu-latest` | Runner label. Set to a self-hosted label if the build API is internal. |
| `CONFLUENCE_PAGE_ID` | unset | Pin one page. Unset = follow the feed's train. Point it at a copy to rehearse |
| `CONFLUENCE_DOMAIN` | `wbdstreaming.atlassian.net` | Atlassian site |
| `RELEASE_TRAIN` | from page title | Pin the train instead of reading it from the title |
| `ENFORCE_TRAIN` | `true` | Set `false` to accept builds from any train |
| `BUILD_PROVIDER` | `fuse_api` | `fuse_api`, `grafana` or `jfrog` |
| `MIN_PLATFORMS` / `MIN_ROWS` | `1` | Health check fails below these counts |

### 3. Point it at your table

`config/clients.yml` holds the release train, the page id, the column headers to
look for, and the client rows. It is wired to the seven rows of the Go/No-Go
table:

| Row | Devices read | D+ scheme |
| --- | --- | --- |
| `Web` | `web` | base |
| `Roku` | `roku` | base |
| `Apple IOS` | `ios` | offset |
| `Apple TV` | `tvos` | offset |
| `Android` | `android`, `firetablet` | offset |
| `LB` | `androidtv`, `firetv` | offset |
| `CDEV` | `samsung`, `lg` | base |

Row matching ignores case and punctuation, reads the `aliases` list, and
matches a table row that only extends the configured one, so `CDEV (Samsung,
Bounty Flow, Linux)` matches `CDEV` and `Leanback` matches `LB`. An exact
spelling always wins, and a label that two rows could extend is skipped with a
warning naming both -- which is how the table's separate `Apple IOS` and `Apple
TV` rows were found, after a single `Apple` row had silently matched neither. Rows in the table that aren't in the config are never touched,
which covers the Notes and Go/No-Go columns and any row added later.
A repeated header row, which this table has, is not read as
a client. The API also builds `xbox`, `playstation4` and `playstation5`; add a
row when the table grows one.

### Moving to the next train

Each train gets its own sign-off page: 7.12.0 has one, 7.13.0 will get another.
The config ships with no `page_id`, so a scheduled run handles that hand-off
without anyone editing anything, as long as the `CONFLUENCE_PAGE_ID` variable is
also unset:

1. Fetch the builds and read the newest train in the feed.
2. Look for that train's page, from `confluence.title_template`
   (`{train} Build GQA App Sign off`) in the `GQA` space.
3. If it exists, write it. If it doesn't, keep the current train's page updated
   and warn that the new page needs creating.

So the day 7.13.0 starts building, the next run moves to the 7.13.0 page by
itself. Titles match loosely, so the live "Copy of 7.12.0 Build GQA App Sign
off" is found by the 7.12.0 title, and an exact title wins over a copy.

The feed decides *which page is current*; the page title still decides *which
builds it accepts*. That split is what keeps a 7.13.0 build off a 7.12.0 page.

**To pin a train instead**, set the `RELEASE_TRAIN` variable (or pass it to a
manual run). That is worth doing while a sign-off is in progress and the feed
has already moved on, since it stops the run following the builds.

**To rehearse against a copy**, set the `CONFLUENCE_PAGE_ID` variable to the
copy's id. Every run then writes only that page, and deleting the variable is
the whole go-live step. Worth knowing before you delete it: when both
`7.12.0 Build GQA App Sign off` and `Copy of 7.12.0 ...` exist, the exact title
wins, so the sync moves to the real page.

The 6-hourly health job fails when the feed is on a train whose page does not
exist, because that is the one situation the sync cannot resolve alone:

```
the build feed is on train 7.13.0 but this page signs off 7.12.0: create the
7.13.0 sign-off page, or set RELEASE_TRAIN to the train to write
```

**Two loose matches is an error, not a coin toss.** If a train has both a page
and a draft copy, the run stops and lists the candidates rather than picking
one, because publishing to the wrong sign-off page is worse than not running.
Set `CONFLUENCE_PAGE_ID` to settle it. A train with no page yet gets a message
saying so instead of a 404.

Precedence: `--page-id` > `--page-title` > `CONFLUENCE_PAGE_ID` > the title
template. Pinning `CONFLUENCE_PAGE_ID` keeps the old behaviour exactly.

### Which train a run will write

In order of precedence: the `RELEASE_TRAIN` variable (or `--release-train`), then
the version in the page title, then `release.train` in the config. Moving to the
next train needs no change at all once the page exists — and if the feed has
moved on while a pinned page has not, the run says so explicitly rather than
silently skipping every row.

## Scheduled jobs

| Workflow | Schedule | What it does |
| --- | --- | --- |
| `sync-signoff-versions.yml` | `7,22,37,52 * * * *` | The sync. Manual runs default to a dry run and accept a page id and train. |
| `build-api-health.yml` | `23 */6 * * *` | Runs the probe. Fails if the API or page is unreachable, or if coverage falls below `MIN_PLATFORMS` / `MIN_ROWS`. |
| `tests.yml` | `40 5 * * 1` | Tests plus a credential-free rehearsal of the whole pipeline against fixtures. |

The health check exists because the sync can succeed while doing nothing useful:
a renamed row, a platform that stopped reporting or an expiring token all look
like warnings in a green run. The probe turns those into a red one.

### How close to live this can get

Table cells in a Confluence page are static content: something has to write
them, so "live" means "written often", and there are only three levers.

1. **Push instead of poll.** The sync also answers to `repository_dispatch`, so
   a build pipeline that POSTs to the repo gets the page updated within about a
   minute of a build landing. This is as live as a written page gets, and it
   costs one run per build rather than 96 a day.
2. **A shorter schedule.** `*/5` is GitHub's documented floor — a shorter cron
   is accepted by the YAML parser and silently never fires — and even `*/5` is
   best effort: runs are delayed at busy times and queued ticks can be dropped.
   Going from 15 to 5 minutes triples the run count to buy maybe ten minutes of
   freshness, and every real change is a page revision that notifies watchers.
3. **Embed the dashboard.** Genuinely live, but then the numbers live in an
   iframe rather than in the sign-off table, so they can't be signed off
   alongside the Go/No-Go statuses, and Confluence Cloud admins often disable
   the HTML and iframe macros anyway.

For a page people read a few times a day around a sign-off, 15 minutes plus the
dispatch hook is the useful combination.

## Running it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Offline rehearsal: no tokens, no network, no writes. One file per product,
# the way /api/latest-versions is called for real.
python -m gonogo.sync --builds-file max=tests/fixtures/latest_versions_max.json \
                      --builds-file dplus=tests/fixtures/latest_versions_dplus.json \
                      --page-file tests/fixtures/sample_page.xhtml

# Against the real build API and the real page, still without publishing.
cp .env.example .env    # fill in the tokens
set -a && source .env && set +a
python -m gonogo.sync --dry-run --verbose

# Publish for real.
python -m gonogo.sync
```

Useful flags: `--page-id` to target a different page, `--page-file` to work from
a saved body, `--output body.xhtml` to dump the storage format it would publish,
`--builds-file` (repeatable as `brand=path`) to replay payloads,
`--release-train` to override the train.

```bash
pytest    # 129 tests, no network needed
```

### When Confluence answers 404

A 404 from `/wiki/api/v2/pages/{id}` for a page you can open in a browser has
several unrelated causes: a token that authenticates against Jira but not
Confluence, an account that differs from the one in your browser, view
restrictions on the page (which return 404, not 403), or an id that belongs to a
whiteboard or database rather than a page.

```bash
set -a && source .env && set +a
python tools/diagnose_confluence.py
```

That asks each question separately — who the token authenticates as, whether the
space is visible, whether v1 sees the id and as what type, and which ids a title
search returns — then prints a verdict. It never mixes up "could not connect"
with "credential refused", and it masks the token.

## Known limits

- **GitHub may never fire the schedule at all.** Scheduled runs are a request,
  not a guarantee: they run on shared capacity and GitHub drops queued ones
  under load, which peaks on the hour and quarter-hour, so the crons here sit on
  odd minutes. On this repo the `schedule` event has never fired while manual
  runs succeed, which is a reported pattern for new private repositories on the
  Free plan, and when it does fire it delivers about one tick in ten, minutes
  late. What actually keeps the page current is a pair of local timers:
  `gonogo-sync.timer` runs the sync on the quarter-hours and
  `gonogo-probe.timer` the health probe seven minutes later, both installed
  from `tools/`. The build pipeline can also POST to the `repository_dispatch`
  hook to have Actions do the work on demand.
- **Scheduled workflows go dormant.** GitHub disables schedules in a repository
  with no activity for 60 days. The weekly test run keeps this one awake.
- **Network reachability is not the same as authentication.** GitHub-hosted
  runners live on the public internet. If `sprintedge.gqa.discomax.com` only
  resolves inside the WBD network, the job needs a self-hosted runner via the
  `SYNC_RUNNER` variable regardless of which tokens it holds. Check this first:
  a gate token cannot fix a DNS failure, and the symptom looks the same in a log.
- **Page history.** Every real change is a page version, attributed to the token
  owner, and will notify page watchers.
