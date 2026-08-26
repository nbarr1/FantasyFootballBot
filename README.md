# mflbot

A monitoring and advisory bot for a [MyFantasyLeague](https://www.myfantasyleague.com)
league. It watches news, stats and league activity, and prepares three kinds of
recommendation:

1. Add/drop (waiver and free agent) moves
2. Trade proposals, and responses to offers you receive
3. Weekly starting lineups

It runs as a **web application** (`bot serve`) or from the **command line** --
the same bot either way, over the same database, enforcing the same rules.

**It never submits anything to MFL without your explicit, per-action approval.**
That is a design invariant, not a setting. There is no auto mode, no "approve
all", and no timeout that turns silence into action.

---

## The invariant, and how it is enforced

Saying "it asks first" is easy. This is how it is made true structurally, so
that a future change cannot quietly undo it:

| Property | Mechanism |
|---|---|
| Analysis cannot submit | Analysis code is handed `MFLReadClient`, which has **no write methods**. A test walks the import graph of `mflbot/analysis` and `mflbot/ingest` and fails if either can even reach the write client. |
| A write needs approval | `MFLWriteClient.submit(payload, token)` is the only public write method, and `token` has no default. Tokens are minted only by an approval channel acting on a user decision. |
| Approval covers one exact action | The token is an HMAC over the recommendation id **and the hash of the literal payload**. A different payload fails verification. |
| Editing revokes approval | An edit changes the payload hash, so a token issued before the edit no longer matches. |
| Approval is single-use | Consumption is one atomic `UPDATE ... WHERE consumed_at IS NULL`; two racing executors produce exactly one winner. |
| Silence never executes | Recommendations expire. Expiry is the only terminal state that inaction can produce. |
| A guessed endpoint never fires | A write capability without a `DOC_VERIFIED` entry refuses outright -- no default, no fallback. See "Endpoint verification" below for which capabilities that currently is (five of six) and isn't (waiver-order claims). |
| The world may have moved | The executor re-validates preconditions immediately before submitting, and abandons rather than adapting if state changed. |
| Nothing is assumed to have worked | Success is confirmed by re-reading the relevant export, not by trusting MFL's response. |

Run `pytest tests/test_write_isolation.py tests/test_approval.py` to see these
checked.

## Endpoint verification

MFL's own API documentation is the authoritative source for endpoint names and
parameters. The whole `myfantasyleague.com` domain was blocked by network
policy on the machine that built this bot, so nothing could be confirmed by
fetching it directly — write endpoints shipped `UNVERIFIED` and refused to
fire until they were.

They now are: the user pasted MFL's own Request Reference Page directly into
the conversation, and `endpoints.lock.json` — committed to this repo, not a
generated artifact you need to produce yourself — pins the real, confirmed
`import?TYPE=...` name and parameter mapping for all six write capabilities:
lineup submission, FCFS add/drop, blind-bid waivers, trade proposals, and
trade responses all work. **Waiver-order claims are the one exception**: MFL's
`waiverRequest` import requires a `ROUND` number, and this bot has no way to
determine which round a league is processing or how many it runs per period —
rather than guess, `analyse_waivers` blocks that one waiver system outright
(`bot config-summary` will say so plainly if your league uses it). FCFS and
blind-bid leagues are unaffected.

If you ever need to re-verify against a different league or season (or MFL
changes something), `bot verify-endpoints` re-fetches the live `api_info` page
and regenerates the lock file the same way; it only overwrites entries it can
actually confirm.

Read endpoints are less dangerous — a wrong name fails loudly with no side
effect — so they shipped enabled from an independently written third-party
client from the start. Cross-checking that against the real Request Reference
Page found it wasn't perfect: a handful of endpoints (`adp`, `aav`, the
`top*` market-signal ones, and one that wasn't real at all — `playerStatus`,
now the actual `playerRosterStatus`) had parameter names that don't exist in
MFL's own docs. All of it is now hand-verified against the real reference and
corrected. Each endpoint records its provenance in `mflbot/mfl/endpoints.py`.

## No fabricated data, anywhere

The application ships with **no** seeded players, projections, scores, scoring
weights, or league settings. A freshly installed database is empty, and that is
the correct state until ingestion runs against your league.

Consequences you will notice, all deliberate:

- If the scoring rules cannot be fully parsed, scoring-dependent features are
  **blocked** and the specific unparseable rules are reported. There is no
  fallback to standard PPR.
- If MFL does not report your blind-bid budget, no bid is proposed. A bid sized
  from a guessed budget would be submitted for real.
- If the waiver system cannot be determined, no claim is prepared — the workflow
  differs materially between blind bidding and first-come-first-served.
- If the trade deadline is unknown, no proposals are drafted.
- A player with no projection is never silently treated as scoring zero, and is
  never the one the bot suggests you drop.

`bot status` and `bot config-summary` list exactly what is blocked and why.

The only synthetic data in this repository is in `tests/`, clearly labelled, and
`tests/test_no_seed_data.py` asserts it never leaks into the application.

## Install

```bash
git clone https://github.com/nbarr1/FantasyFootballBot
cd FantasyFootballBot
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[solver,web,dev]'  # 'solver': exact ILP lineups; 'web': the dashboard
```

Requires Python 3.11+.

## First run

```bash
bot init                       # writes config.toml
$EDITOR config.toml            # set [league] id / season / host

export MFLBOT_MFL_USERNAME=...      # required for any write
export MFLBOT_MFL_PASSWORD=...
export MFLBOT_MFL_API_KEY=...       # optional; unlocks private-league reads

# Treat all three like passwords: set them in your shell or .env (gitignored),
# never in config.toml or any other file this repo tracks, and never paste a
# real value into an issue, chat log, or commit message. If one ever ends up
# somewhere it shouldn't, MFL will invalidate and reissue an API key on
# request; change your password the normal way for the other two.

bot sync-config                # pull league settings + scoring rules
bot whoami                     # find your franchise id
$EDITOR config.toml            # set league.franchise_id
bot config-summary             # READ THIS -- confirm it matches your league
```

`bot config-summary` is the important one. It prints what the bot parsed, what
it looked for and could not find, and which features are consequently blocked.
If the waiver system or scoring looks wrong, stop and sort that out — every
downstream number depends on it.

Then ingest and analyse:

```bash
bot sync-players
bot poll                       # rosters, free agents, transactions
bot sync-projections
bot news
bot analyse lineup             # or: waivers, trades
```

## The web application

```bash
export MFLBOT_WEB_PASSWORD=...        # or leave unset for a printed login link
bot serve                             # http://127.0.0.1:8765
```

Everything the CLI does, in a browser, plus live output:

| Page | What it is for |
|---|---|
| Dashboard | What is awaiting your decision, what is blocked and why, the league's real deadlines, and quick actions |
| Recommendations | Every recommendation, filterable by state; each one opens onto its full rationale, evidence, caveats and literal payload |
| Team | Your roster and the free-agent pool with this week's projections (a player with no projection shows `--`, never `0.0`) |
| League | The parsed configuration -- slots, franchises, scoring rules, unparsed rules, blocked features, endpoint verification |
| Actions | Run ingestion, analysis and verification; watch the command's output stream in; start or stop the scheduler |
| Audit | Every write ever attempted, with the request sent and MFL's reply |

Approving is two clicks by default, and they are different clicks: **Approve**
records the decision and mints the token; **Submit to MFL** spends it. (There is
an *Approve and submit now* button for when you have already decided, and
`--no-submit` for when you want the dashboard never to be able to write at all.)

### It authenticates, always

The dashboard can mint approval tokens, so "bound to localhost" is not an access
control -- any other process on the machine, any container sharing the network
namespace, and anything on the far end of an SSH port-forward can reach a
localhost port. So:

- **A login is required.** Set `MFLBOT_WEB_PASSWORD` (compared as a scrypt hash
  held in memory, never written to disk), or leave it unset and the server
  generates an access token at startup and prints the login link (valid while
  that process runs). There is no anonymous mode -- `WebSecurity` raises rather
  than construct one.
- **Sessions live on the server.** The cookie carries an opaque random id and
  nothing else. Signing out, or restarting the server, really does end them.
- **Every mutating request needs a CSRF token**, plus an Origin check when the
  browser sends one.
- **Failed logins are throttled**, and a non-loopback bind without a password is
  refused outright.

It speaks plain HTTP by design. Reach it over an SSH tunnel
(`ssh -N -L 8765:127.0.0.1:8765 you@host`) or put a TLS-terminating proxy in
front of it.

### What the buttons can and cannot do

The action buttons run the *same* `bot` subcommands the CLI runs, through the
same argument parser, one at a time on a worker thread -- there is one
implementation of "sync the config", not two that can drift. Which commands they
may run is an allowlist (`mflbot/web/jobs.py`) containing only read and analysis
commands; `approve`, `execute`, `reject` and `edit` are excluded by name, and a
test fails if that stops being true. A decision is something you make on one
recommendation, never a button that fires a batch.

Output is passed through the same redactor the log formatters use before it
reaches a browser.

## Deciding

Either surface. In the dashboard, every pending recommendation has its
rationale, its evidence, its caveats and the literal payload on one page, with
Approve / Reject / Edit next to them. From the CLI:

```bash
bot pending                    # everything awaiting a decision
bot show <id>                  # rationale, evidence, caveats, literal payload
bot approve <id>               # approve THIS one, then submit it
bot reject <id> "reason"
bot edit <id> bid_amount=12    # edit, then approve separately
bot audit                      # every write attempted and what happened
```

Every recommendation shows the exact request that would be sent to MFL, the
reasoning, the data behind it, and an explicit note when the evidence is thin.
Projections are presented as estimates, because that is what they are.

## Running continuously

```bash
bot run                              # scheduler alone, in the foreground
bot serve --with-scheduler           # scheduler + dashboard, one process
```

The scheduler polls, analyses and notifies. It **never** submits. Approving is
always a separate, interactive act. Run one of the two, not both: two processes
against one SQLite file contend for its write lock. See "Where to run it" below,
and [`deploy/`](deploy/) for systemd units (one per shape) and the Dockerfile.

| Job | Cadence |
|---|---|
| Config + scoring refresh | daily (catches mid-season scoring edits) |
| Player database | daily (MFL's stated limit) |
| Transactions / rosters / free agents | every 45 min, configurable |
| News ingestion | every 45 min |
| Waiver analysis | weekly, plus on any roster or free-agent change |
| Trade analysis | weekly, plus on an incoming offer |
| Lineup analysis | T-48h, T-12h, T-2h from your league's **real** deadline |

## Where to run it

This is a stateful daemon that happens to serve a web page, not a web page that
happens to do work. Anywhere it runs needs three things:

1. **A persistent disk.** The SQLite file holds every recommendation, approval
   token and audit row. Lose it and you lose the audit trail.
2. **A process that stays up.** Lineup analysis is scheduled from your league's
   *real* deadline (T-48h, T-12h, T-2h), computed at runtime — not on a fixed
   clock someone else can trigger.
3. **One process at a time.** Sessions, the live event stream, the response
   cache and the MFL rate limiter are all per-process. Two copies against one
   database means a doubled request rate against MFL and a write lock they will
   fight over.

Anything always-on satisfies that: a small VPS, a Raspberry Pi at home, or a
container host with a persistent volume (Fly.io, Railway, Render). See
[`deploy/`](deploy/) for the systemd units and the Dockerfile.

For reaching it from elsewhere, the dashboard serves plain HTTP on loopback by
default and expects one of:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@host    # then http://127.0.0.1:8765
```

Tailscale or a Cloudflare Tunnel work the same way. To expose it directly,
`MFLBOT_WEB_PASSWORD` becomes mandatory (`bot serve` refuses a non-loopback bind
without one) and a TLS-terminating reverse proxy is on you.

### Not serverless

Vercel, Netlify Functions, Lambda and friends are the wrong shape, despite
running FastAPI perfectly well:

| What the bot needs | What a function platform gives |
|---|---|
| A SQLite file that persists | An ephemeral filesystem; `/tmp`, per invocation |
| A scheduler holding deadline-relative timers | Cron on a fixed expression, plus `waitUntil` tied to one response |
| In-process sessions, SSE bus, rate limiter | As many instances as there is traffic, sharing none of it |
| Jobs that outlive a request (`bot sync-players`) | A `maxDuration` ceiling |

The rate limiter is the one that would bite quietly rather than loudly: MFL
throttles per client, and this bot's pacing is enforced once per process. Spread
across instances it stops being a limit at all.

A serverless port is possible, but it is a re-architecture rather than a deploy:
implement the Postgres backend (`StorageSettings.dsn` is a declared, unimplemented
field), move sessions and the event bus to a shared store, replace the scheduler
with cron endpoints, and give up live-streamed job output. For one manager
watching one league, that buys nothing a $5 VPS does not already do.

## Rate limits and MFL's terms

Only MFL's documented API is used, with your own credentials. No HTML page is
scraped — MFL's `robots.txt` disallows it, and `bot verify-endpoints` reads the
public developer documentation page only.

Caching and rate limiting are client-level policy, not caller discipline: the
TTL for each endpoint is declared once, in `mflbot/mfl/endpoints.py`, so an
analysis run that asks for the player database five times produces one request
per day. A 429 triggers exponential backoff with jitter, and requests are
spaced at least a second apart by default, matching MFL's own guidance.

Two routing details, confirmed against MFL's own developer documentation and
enforced by the client rather than left to each call site: a request with no
league parameter (the player database, injuries, the NFL schedule, ADP, login
itself...) goes to `api.myfantasyleague.com` rather than your configured
league host, and the `APIKEY` alternate-auth parameter is never sent on a
write — MFL's docs state plainly it "does not work for import requests, only
export."

MFL also throttles unregistered API clients harder than registered ones
(~2.5x lower ceiling). Registering (MFL's API Client Registration page, plus
an SMS validation code) is free and optional; set `MFLBOT_USER_AGENT` to the
exact string you register once you have, and every request will carry it.
Unset, the bot runs at the unregistered tier, which is fully functional.

## Configuration

`config.toml` holds **behaviour** settings only — your risk tolerance and job
cadences. League data is never configured there; it comes from the API.

The thresholds worth tuning first are in `[waivers]` and `[trades]`. They gate
*how many* recommendations surface, and the shipped defaults are deliberately
conservative — fewer suggestions, not better ones. `bot init` writes a fully
commented template ([`src/mflbot/templates/config.example.toml`](src/mflbot/templates/config.example.toml))
explaining what each one does.

Secrets come from the environment only (see `.env.example`), are never stored in
the database, never written to a log — every formatter routes through a
redactor — and never rendered by the approval interface.

### Storage

SQLite, because this watches one league on one host and the whole dataset is
small. `[storage] engine` accepts only `"sqlite"`; anything else raises rather
than silently falling back.

Swapping in Postgres is a matter of writing one more backend, not editing
analysis code: everything above the database talks to `Repositories`, never to
SQL. It means implementing a `Database`-shaped class against the `dsn` field
that `StorageSettings` already declares, and porting `storage/schema.sql`. The
analysis engines, the approval flow and the executor are untouched by it. Until
someone does, `engine = "postgres"` refuses at startup and says so.

## Architecture

```
mflbot/
  mfl/          read client, approval-gated write client, endpoint registry,
                auth, caching, rate limiting, doc verification
  ingest/       league config + scoring rules, players, league-state diffing,
                scores/projections, news sources
  analysis/     rules parser, valuation core, lineup optimiser, waivers, trades
  recommend/    recommendation records and the literal action payloads
  approval/     ApprovalChannel interface, token service, CLI channel,
                web channel (delegates to the CLI one, so they cannot drift)
  web/          the dashboard: routes, templates, sessions/CSRF, the
                allowlisted job bridge, the server-sent-events stream
  execute/      preconditions, submission, confirmation, audit
  notify/       webhook transport; email and telegram stubs
  schedule/     APScheduler jobs
```

Everything downstream — lineup ranking, waiver value, trade fairness — routes
through one function: `ScoringModel.score`, which converts a stat line to points
using your league's actual parsed rules. When the commissioner edits scoring, the
daily refresh updates one object and every recommendation moves with it.

## Known gaps

- **Waiver-order leagues are blocked.** MFL's `waiverRequest` import requires a
  `ROUND` number this bot has no way to determine automatically; rather than
  guess, the whole waiver system stays blocked for leagues that use it. FCFS
  and blind-bid leagues are fully supported. See "Endpoint verification" above.
- **`bot validate-scoring` is partial.** A full replay needs per-player stat
  lines; MFL's `playerScores` returns points already computed. The command
  verifies that every scored position has parseable rules and says plainly what
  it cannot check. This one MFL's API cannot ever close directly: its terms
  forbid distributing raw player stats under its stats licensing agreement. A
  full replay needs a stat-line source from one of the paid providers stubbed
  in `mflbot/ingest/news/paid_stubs.py` — MFL's own docs name FantasyData.com,
  Sportradar and XML Team as the sanctioned options.
- **Draft picks are not valued** in trade analysis, and are flagged as excluded
  when an offer contains them.
- **Email and Telegram notifiers are stubs**, as are the paid news providers.
  The webhook transport (which works with Discord) is implemented.
- **The dashboard has one account and no TLS.** It is a single-user tool: one
  shared secret, no roles, no per-user audit beyond `web:` on the token. It
  serves plain HTTP and expects a tunnel or a reverse proxy in front of it for
  anything other than localhost. Sessions and the run history live in the
  server process, so a restart signs you out and clears the console (what a run
  *produced* is in the database, and survives).

## Tests

```bash
pytest              # 213 tests
```

Run it as `pytest`, not `python -m pytest`. The two differ: `python -m pytest`
silently puts the working directory on `sys.path`, so an import that only works
by accident passes locally and fails in CI. CI runs the bare console script for
exactly that reason.

The ones that encode the safety properties: `test_write_isolation.py`,
`test_approval.py`, `test_executor.py`, `test_no_seed_data.py`,
`test_end_to_end.py` — which walks the entire pipeline against a simulated MFL
and asserts that nothing is submitted without an approval — and
`test_web_app.py`, which asserts the same things through the dashboard: no
session, no CSRF token, or a cross-site Origin and the approval does not
happen; an edit after approving invalidates the token; and no action button can
reach a command that writes.
