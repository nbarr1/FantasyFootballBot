# mflbot

A monitoring and advisory bot for a [MyFantasyLeague](https://www.myfantasyleague.com)
league. It watches news, stats and league activity, and prepares three kinds of
recommendation:

1. Add/drop (waiver and free agent) moves
2. Trade proposals, and responses to offers you receive
3. Weekly starting lineups

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
| A guessed endpoint never fires | Write endpoints ship **inert**. See "Why writes start disabled" below. |
| The world may have moved | The executor re-validates preconditions immediately before submitting, and abandons rather than adapting if state changed. |
| Nothing is assumed to have worked | Success is confirmed by re-reading the relevant export, not by trusting MFL's response. |

Run `pytest tests/test_write_isolation.py tests/test_approval.py` to see these
checked.

## Why writes start disabled

MFL's own API documentation is the authoritative source for endpoint names and
parameters. **It was not reachable from the machine that generated this code**
(the whole `myfantasyleague.com` domain was blocked by network policy), so the
`import` endpoint names could not be confirmed.

Rather than ship a plausible guess and POST it at a real league, every write
capability ships as `UNVERIFIED` and refuses to fire. Running:

```bash
bot verify-endpoints
```

fetches your league's `api_info` page, extracts the endpoints MFL actually
documents, and pins the confirmed names and parameters into
`endpoints.lock.json`. Only capabilities it can confirm become usable.

Read endpoints are less dangerous — a wrong name fails loudly with no side
effect — so they ship enabled, sourced from an independently written,
working open-source MFL client and cross-checked against a second source.
Each one records its provenance in `mflbot/mfl/endpoints.py`.

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
pip install -e '.[solver,dev]'      # 'solver' adds exact ILP lineup solving
```

Requires Python 3.11+.

## First run

```bash
bot init                       # writes config.toml
$EDITOR config.toml            # set [league] id / season / host

export MFLBOT_MFL_USERNAME=...      # required for any write
export MFLBOT_MFL_PASSWORD=...
export MFLBOT_MFL_API_KEY=...       # optional; unlocks private-league reads

bot verify-endpoints           # required before writes will ever fire
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

## Deciding

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
bot run                        # scheduler in the foreground
```

The scheduler polls, analyses and notifies. It **never** submits. Approving is
always a separate, interactive act. See [`deploy/`](deploy/) for the systemd
unit and Dockerfile.

| Job | Cadence |
|---|---|
| Config + scoring refresh | daily (catches mid-season scoring edits) |
| Player database | daily (MFL's stated limit) |
| Transactions / rosters / free agents | every 45 min, configurable |
| News ingestion | every 45 min |
| Waiver analysis | weekly, plus on any roster or free-agent change |
| Trade analysis | weekly, plus on an incoming offer |
| Lineup analysis | T-48h, T-12h, T-2h from your league's **real** deadline |

## Rate limits and MFL's terms

Only MFL's documented API is used, with your own credentials. No HTML page is
scraped — MFL's `robots.txt` disallows it, and `bot verify-endpoints` reads the
public developer documentation page only.

Caching and rate limiting are client-level policy, not caller discipline: the
TTL for each endpoint is declared once, in `mflbot/mfl/endpoints.py`, so an
analysis run that asks for the player database five times produces one request
per day. A 429 triggers exponential backoff with jitter.

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
                web dashboard (scaffold)
  execute/      preconditions, submission, confirmation, audit
  notify/       webhook transport; email and telegram stubs
  schedule/     APScheduler jobs
```

Everything downstream — lineup ranking, waiver value, trade fairness — routes
through one function: `ScoringModel.score`, which converts a stat line to points
using your league's actual parsed rules. When the commissioner edits scoring, the
daily refresh updates one object and every recommendation moves with it.

## Known gaps

- **Write endpoints are unverified** until you run `bot verify-endpoints`. Some
  may need a `field_map` completed by hand in `endpoints.lock.json`; the command
  tells you which and shows the documented parameters.
- **The web dashboard is a scaffold.** It runs, but has no CSRF protection and no
  auth beyond binding to localhost. The CLI is the supported approval surface.
- **`bot validate-scoring` is partial.** A full replay needs per-player stat
  lines; MFL's `playerScores` returns points already computed. The command
  verifies that every scored position has parseable rules and says plainly what
  it cannot check.
- **Draft picks are not valued** in trade analysis, and are flagged as excluded
  when an offer contains them.
- **Email and Telegram notifiers are stubs**, as are the paid news providers.
  The webhook transport (which works with Discord) is implemented.

## Tests

```bash
pytest              # 161 tests
```

Run it as `pytest`, not `python -m pytest`. The two differ: `python -m pytest`
silently puts the working directory on `sys.path`, so an import that only works
by accident passes locally and fails in CI. CI runs the bare console script for
exactly that reason.

The ones that encode the safety properties: `test_write_isolation.py`,
`test_approval.py`, `test_executor.py`, `test_no_seed_data.py`, and
`test_end_to_end.py` — which walks the entire pipeline against a simulated MFL
and asserts that nothing is submitted without an approval.
