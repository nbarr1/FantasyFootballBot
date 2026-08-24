"""Command-line interface.

Grouped roughly by what you do with them:

Setup and verification
    init, verify-endpoints, whoami, config-summary, status

Ingestion
    sync-config, sync-players, sync-projections, sync-scores, poll, news

Analysis (produces recommendations; never submits)
    analyse lineup | waivers | trades

Approval and execution (the only path to an MFL write)
    pending, show, approve, reject, edit, execute

Verification and audit
    validate-scoring, audit

The split matters: no command under "Analysis" can write to MFL, and every
command that can requires a recommendation you have explicitly approved.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, load_config
from .context import BotContext
from .errors import ApprovalError, MFLBotError
from .logging_setup import setup_logging

#: The config template ships inside the package. Resolving it through
#: importlib.resources rather than a path relative to __file__ is what makes
#: `bot init` work for a real (non-editable) install -- which is how both the
#: Dockerfile and the systemd deployment install it.
CONFIG_TEMPLATE = "templates/config.example.toml"


def read_config_template() -> str:
    return resources.files("mflbot").joinpath(CONFIG_TEMPLATE).read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# setup and verification
# ---------------------------------------------------------------------------

def cmd_init(args, context: BotContext | None = None) -> int:
    """Create config.toml and an empty database."""
    target = Path(args.config)
    if target.exists() and not args.force:
        print(f"{target} already exists. Use --force to overwrite.")
        return 1
    try:
        template = read_config_template()
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        print(f"Could not read the packaged config template: {exc}")
        return 1
    target.write_text(template, encoding="utf-8")
    print(f"Wrote {target}. Edit the [league] section, then:")
    print("  1. export MFLBOT_MFL_USERNAME=... MFLBOT_MFL_PASSWORD=...")
    print("  2. bot verify-endpoints")
    print("  3. bot sync-config && bot whoami")
    return 0


def cmd_verify_endpoints(args, context: BotContext) -> int:
    """Reconcile the endpoint registry against MFL's live API documentation."""
    from .mfl.verify import verify_endpoints

    report = verify_endpoints(context.client, context.registry)
    print(report.render())
    return 0 if report.all_writes_resolved else 2


def cmd_whoami(args, context: BotContext) -> int:
    """Identify which franchise the authenticated user owns."""
    if not context.client.login() and not context.client.is_authenticated():
        print(
            "No credentials configured. Set MFLBOT_MFL_USERNAME and "
            "MFLBOT_MFL_PASSWORD (or MFLBOT_MFL_API_KEY)."
        )
        return 1
    payload = context.client.league_settings()
    from .analysis.rules_parser import mfl_text

    root = payload.get("league", {}) if isinstance(payload, dict) else {}
    franchises = root.get("franchises", {}).get("franchise", [])
    if isinstance(franchises, dict):
        franchises = [franchises]

    owned = [
        f for f in franchises
        if isinstance(f, dict) and mfl_text(f.get("is_owner")) in {"1", "true", "Yes"}
    ]
    print(f"Authenticated: {context.client.is_authenticated()}")
    print(f"Write-capable: {context.client.can_write()}")
    if owned:
        for franchise in owned:
            print(
                f"Your franchise: id={mfl_text(franchise.get('id'))} "
                f"name={mfl_text(franchise.get('name'))}"
            )
        print("\nSet this id as league.franchise_id in config.toml.")
    else:
        print(
            "\nMFL did not mark any franchise as owned by this login. Find your "
            "franchise id on your league's home page and set league.franchise_id "
            "in config.toml -- the bot will not guess which team is yours."
        )
        for franchise in franchises:
            if isinstance(franchise, dict):
                print(f"  {mfl_text(franchise.get('id'))}  {mfl_text(franchise.get('name'))}")
    return 0


def cmd_config_summary(args, context: BotContext) -> int:
    """Print the parsed league configuration for a sanity check."""
    settings = context.league_settings()
    if settings is None:
        print("No league configuration stored. Run `bot sync-config` first.")
        return 1

    print(f"League {settings.league_id} ({settings.season}) -- {settings.name or 'unnamed'}")
    print(f"  Franchises      : {_or_unknown(settings.franchise_count)}")
    print(f"  Roster size     : {_or_unknown(settings.roster_size)}")
    print(f"  Starters        : {_or_unknown(settings.starter_count)}")
    print(f"  Taxi squad      : {_or_unknown(settings.taxi_squad_size)}")
    print(f"  Injured reserve : {_or_unknown(settings.injured_reserve)}")
    print(f"  Waiver system   : {settings.waiver_system} "
          f"(MFL reported: {settings.waiver_type_raw!r})")
    print(f"  Trade deadline  : {_or_unknown(settings.trade_deadline)}")
    print(f"  Lineup deadline : {_or_unknown(settings.lineup_deadline)}")

    print("\n  Starting lineup:")
    if not settings.lineup_slots:
        print("    (none parsed -- lineup analysis is blocked)")
    for slot in settings.lineup_slots:
        count = (
            str(slot.min_starters)
            if slot.min_starters == slot.max_starters
            else f"{slot.min_starters}-{slot.max_starters}"
        )
        print(f"    {count} x {slot.name} (eligible: {', '.join(slot.eligible_positions)})")

    print("\n  Franchises:")
    for franchise in settings.franchises:
        marker = " <- you" if franchise.is_owner else ""
        budget = f" bbid=${franchise.bbid_budget:g}" if franchise.bbid_budget is not None else ""
        print(f"    {franchise.franchise_id}  {franchise.name or '?'}{budget}{marker}")

    model = context.scoring_model()
    print(f"\n  Scoring rules parsed: {len(model.parsed.rules)}")
    if model.parsed.gaps:
        print(f"  Scoring rules NOT parsed: {len(model.parsed.gaps)}")
        for gap in model.parsed.gaps:
            print(f"    - {gap.describe()}")

    blocked = context.repos.blocked_features()
    if blocked:
        print("\n  BLOCKED FEATURES:")
        for item in blocked:
            print("    " + item.describe().replace("\n", "\n    "))
    else:
        print("\n  No features are blocked.")
    return 0


def cmd_status(args, context: BotContext) -> int:
    """Show what is stored, what is blocked, and what still needs setup."""
    counts = context.db.table_counts()
    print("Stored data:")
    for table, count in counts.items():
        print(f"  {table:<22} {count}")

    print(f"\nAuthenticated : {context.client.is_authenticated()}")
    print(f"Write-capable : {context.client.can_write()}")
    print(f"Franchise id  : {_or_unknown(context.owner_franchise_id())}")
    notifier_ok, notifier_reason = context.notifier.is_configured()
    print(f"Notifications : {context.notifier.transport_id} "
          f"({'ready' if notifier_ok else notifier_reason})")

    unverified = context.registry.unverified_writes()
    print(f"\nWrite capabilities verified: "
          f"{len(context.registry.writes) - len(unverified)}/{len(context.registry.writes)}")
    for capability in unverified:
        print(f"  BLOCKED: {capability}")
    if unverified:
        print("  Run `bot verify-endpoints` to reconcile against MFL's docs.")

    blocked = context.repos.blocked_features()
    if blocked:
        print("\nBlocked features:")
        for item in blocked:
            print(f"  {item.feature}: {item.reason}")

    pending = context.store.pending()
    print(f"\nAwaiting your decision: {len(pending)}")
    return 0


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------

def cmd_sync_config(args, context: BotContext) -> int:
    from .ingest.config_sync import sync_config

    context.client.login()
    result = sync_config(
        context.client, context.repos,
        owner_franchise_id=context.config.league.franchise_id,
        force=args.force,
    )
    print(f"League settings stored. Scoring rules parsed: {len(result.parsed_rules.rules)}")
    print(f"Scoring-event catalogue entries: {result.rule_definition_count}")

    if result.missing_fields:
        print("\nSettings this parser looked for and did NOT find:")
        for item in result.missing_fields:
            print(f"  - {item}")
        print(
            "  These are reported rather than defaulted. If a setting you need is "
            "here, the field name in MFL's export differs from what the parser "
            "expects -- report it so the probe can be extended."
        )
    if result.blocked:
        print("\nBlocked features:")
        for item in result.blocked:
            print("  " + item.describe().replace("\n", "\n  "))
    else:
        print("\nAll features have the configuration they need.")
    return 0


def cmd_sync_players(args, context: BotContext) -> int:
    from .ingest.players import sync_players

    count = sync_players(context.client, context.repos, force=args.force)
    print(f"Player database: {count} rows")
    return 0


def cmd_sync_projections(args, context: BotContext) -> int:
    from .ingest.scores import sync_projections

    week = args.week or context.current_week()
    if week is None:
        print("Could not determine the current week; pass --week.")
        return 1
    count = sync_projections(context.client, context.repos, week, force=args.force)
    if count == 0:
        print(
            f"MFL published no projections for week {week} on this host.\n"
            "Projection-dependent analysis stays blocked rather than inventing "
            "numbers. Add a projection source if this league's host has none."
        )
        return 2
    print(f"Projections for week {week}: {count} rows")
    return 0


def cmd_sync_scores(args, context: BotContext) -> int:
    from .ingest.scores import sync_scores

    week = args.week or context.current_week()
    if week is None:
        print("Could not determine the current week; pass --week.")
        return 1
    count = sync_scores(context.client, context.repos, week, is_final=args.final,
                        force=args.force)
    print(f"Scores for week {week}: {count} rows")
    return 0


def cmd_poll(args, context: BotContext) -> int:
    from .ingest.league_state import poll_league_state

    context.client.login()
    diff = poll_league_state(context.client, context.repos, force=args.force)
    print(diff.summary())
    for transaction in diff.new_transactions[:20]:
        print(f"  {transaction.timestamp:%Y-%m-%d %H:%M} {transaction.trans_type} "
              f"franchise={transaction.franchise_id}")
    return 0


def cmd_news(args, context: BotContext) -> int:
    from .ingest.news.registry import build_sources, ingest_news

    sources = build_sources(
        context.config.news.sources, context.client, context.repos, context.client.cache
    )
    if not sources:
        print("No news sources are available. Check [news] sources in config.toml.")
        return 1
    results = ingest_news(sources, context.repos)
    for source in sources:
        source.close()
    for source_id, count in results.items():
        state = "FAILED" if count < 0 else f"{count} new item(s)"
        print(f"  {source_id}: {state}")
    return 0


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def cmd_analyse(args, context: BotContext) -> int:
    runners = {
        "lineup": context.run_lineup_analysis,
        "waivers": context.run_waiver_analysis,
        "trades": context.run_trade_analysis,
    }
    print(runners[args.target](week=args.week))
    return 0


# ---------------------------------------------------------------------------
# approval and execution
# ---------------------------------------------------------------------------

def cmd_pending(args, context: BotContext) -> int:
    context.store.expire_stale()
    context.channel.present(context.store.pending())
    return 0


def cmd_show(args, context: BotContext) -> int:
    recommendation = context.store.get(args.id)
    if recommendation is None:
        print(f"No recommendation with id {args.id!r}")
        return 1
    print(recommendation.render())
    print("\nEvidence:")
    print(json.dumps(recommendation.evidence.to_dict(), indent=2))
    print(f"\nStatus: {recommendation.status}")
    return 0


def cmd_approve(args, context: BotContext) -> int:
    """Approve one recommendation, then (unless --no-execute) submit it."""
    recommendation = context.store.get(args.id)
    if recommendation is None:
        print(f"No recommendation with id {args.id!r}")
        return 1

    print(recommendation.render())
    if not args.yes:
        answer = input("\nSubmit this to MFL? Type 'yes' to confirm: ").strip().lower()
        if answer != "yes":
            print("Not approved. Nothing was submitted.")
            return 1

    decision = context.channel.approve(args.id, actor=args.actor)
    print(f"Approved. Token {decision.token.token_id[:12]}... is bound to this "
          f"exact action and is valid once.")

    if args.no_execute:
        print("Not submitting (--no-execute). Run `bot execute` when ready.")
        return 0

    outcome = context.execute_approved(recommendation, decision.token)
    print(outcome.message)
    return 0 if outcome.ok else 2


def cmd_reject(args, context: BotContext) -> int:
    context.channel.reject(args.id, actor=args.actor, note=args.note)
    print(f"Rejected {args.id}. Nothing was submitted.")
    return 0


def cmd_edit(args, context: BotContext) -> int:
    changes = {}
    for pair in args.changes:
        if "=" not in pair:
            print(f"Expected field=value, got {pair!r}")
            return 1
        key, _, value = pair.partition("=")
        changes[key.strip()] = value.strip()

    decision = context.channel.edit(args.id, changes, actor=args.actor)
    print(f"Edited {args.id}: {', '.join(sorted(changes))}")
    print(decision.note)
    updated = context.store.get(args.id)
    if updated:
        print("\n" + updated.render())
    return 0


def cmd_execute(args, context: BotContext) -> int:
    """Submit a previously approved recommendation using its issued token."""
    recommendation = context.store.get(args.id)
    if recommendation is None:
        print(f"No recommendation with id {args.id!r}")
        return 1
    token = context.tokens.load(args.token)
    if token is None:
        print(f"No approval token with id {args.token!r}")
        return 1
    outcome = context.execute_approved(recommendation, token)
    print(outcome.message)
    return 0 if outcome.ok else 2


# ---------------------------------------------------------------------------
# verification and audit
# ---------------------------------------------------------------------------

def cmd_validate_scoring(args, context: BotContext) -> int:
    """Replay real weekly scores through the valuation core and report drift.

    This is how the scoring parser earns trust: MFL already computed each
    player's points under this league's rules, so re-deriving them locally and
    comparing is a real check rather than a self-assessment.

    It needs per-player *stat lines* to score. MFL's ``playerScores`` export
    gives totals, not stat lines, so this command reports what it can verify
    and states plainly what it cannot.
    """
    week = args.week or context.current_week()
    if week is None:
        print("Could not determine the current week; pass --week.")
        return 1

    model = context.scoring_model()
    stored = context.repos.load_scores(context.config.league.id, context.config.league.season, week)

    print(f"Week {week} scoring validation")
    print(f"  Parsed scoring rules : {len(model.parsed.rules)}")
    print(f"  Unparsed rules       : {len(model.parsed.gaps)}")
    print(f"  Stored actual scores : {len(stored)}")

    if model.parsed.gaps:
        print("\n  Rules that could not be parsed (these block scoring entirely):")
        for gap in model.parsed.gaps:
            print(f"    - {gap.describe()}")

    if not stored:
        print(
            f"\n  No stored scores for week {week}. Run "
            f"`bot sync-scores --week {week}` after the week completes."
        )
        return 2

    positions = {}
    lookup = context.repos.get_players(list(stored))
    for player_id in stored:
        player = lookup.get(player_id)
        if player and player.position:
            positions.setdefault(player.position, 0)
            positions[player.position] += 1

    print("\n  Positions with stored scores, and whether rules exist for them:")
    for position, count in sorted(positions.items()):
        rules = model.rules_for_position(position)
        gaps = model.gaps_for_position(position)
        state = "OK" if rules and not gaps else ("BLOCKED" if gaps else "NO RULES")
        print(f"    {position:<6} {count:>4} players  rules={len(rules):<3} {state}")

    print(
        "\n  Note: a full replay needs per-player stat lines, which MFL's "
        "playerScores export does not provide -- it returns points already "
        "computed. The check above confirms every scored position has parseable "
        "rules. To verify the arithmetic itself, add a stat-line source and "
        "re-run; the valuation core is ready for it."
    )
    return 0


def cmd_audit(args, context: BotContext) -> int:
    entries = context.repos.audit_entries(limit=args.limit)
    if not entries:
        print("No API writes have been attempted.")
        return 0
    for entry in entries:
        print(
            f"{entry['at']}  {entry['outcome']:<11} {entry['capability'] or '-':<20} "
            f"rec={entry['recommendation_id'] or '-'}"
        )
        print(f"    {entry['request_summary']}")
        if entry["response_summary"]:
            print(f"    -> {entry['response_summary'][:160]}")
    return 0


def cmd_run(args, context: BotContext) -> int:
    """Run the scheduler in the foreground."""
    import time

    from .schedule.jobs import JobRunner, build_scheduler

    context.client.login()
    scheduler = build_scheduler(JobRunner(context), context.config)
    scheduler.start()
    print("Scheduler running. Recommendations will appear in `bot pending`.")
    print("Nothing is ever submitted to MFL without your explicit approval.")
    print("Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print("\nStopped.")
    return 0


def cmd_serve(args, context: BotContext) -> int:
    """Run the (scaffolded) local approval dashboard."""
    try:
        import uvicorn
    except ImportError:
        print("The dashboard needs the web extra: pip install 'mflbot[web]'")
        return 1
    from .approval.web.app import build_app

    print(
        "NOTE: the dashboard is a scaffold -- no CSRF protection and no auth "
        "beyond binding to localhost. `bot pending` is the supported surface."
    )
    app = build_app(context.store, context.tokens, executor=None,
                    notifier=context.notifier)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _or_unknown(value) -> str:
    return "UNKNOWN (not reported by MFL)" if value is None else str(value)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bot",
        description="Monitoring and advisory bot for a MyFantasyLeague league. "
                    "Never submits anything to MFL without your explicit approval.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--log-file")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create config.toml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init, needs_context=False)

    sub.add_parser(
        "verify-endpoints",
        help="reconcile endpoint names against MFL's API docs (required before writes)",
    ).set_defaults(func=cmd_verify_endpoints)

    sub.add_parser("whoami", help="identify your franchise").set_defaults(func=cmd_whoami)
    sub.add_parser("config-summary", help="print the parsed league config").set_defaults(
        func=cmd_config_summary
    )
    sub.add_parser("status", help="what is stored, blocked, and pending").set_defaults(
        func=cmd_status
    )

    p = sub.add_parser("sync-config", help="pull league settings and scoring rules")
    p.add_argument("--force", action="store_true", help="bypass the response cache")
    p.set_defaults(func=cmd_sync_config)

    p = sub.add_parser("sync-players", help="refresh the player database")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_sync_players)

    p = sub.add_parser("sync-projections", help="pull MFL projections for a week")
    p.add_argument("--week", type=int)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_sync_projections)

    p = sub.add_parser("sync-scores", help="pull actual scores for a week")
    p.add_argument("--week", type=int)
    p.add_argument("--final", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_sync_scores)

    p = sub.add_parser("poll", help="poll transactions, rosters and free agents")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_poll)

    sub.add_parser("news", help="ingest player news").set_defaults(func=cmd_news)

    p = sub.add_parser("analyse", help="run an analysis engine (produces recommendations)")
    p.add_argument("target", choices=["lineup", "waivers", "trades"])
    p.add_argument("--week", type=int)
    p.set_defaults(func=cmd_analyse)
    # American spelling alias.
    p = sub.add_parser("analyze", help=argparse.SUPPRESS)
    p.add_argument("target", choices=["lineup", "waivers", "trades"])
    p.add_argument("--week", type=int)
    p.set_defaults(func=cmd_analyse)

    sub.add_parser("pending", help="list recommendations awaiting your decision").set_defaults(
        func=cmd_pending
    )

    p = sub.add_parser("show", help="full detail for one recommendation")
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve", help="approve one recommendation and submit it")
    p.add_argument("id")
    p.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--no-execute", action="store_true",
                   help="approve without submitting yet")
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="reject one recommendation")
    p.add_argument("id")
    p.add_argument("note", nargs="?", default="")
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("edit", help="edit an action before approving it")
    p.add_argument("id")
    p.add_argument("changes", nargs="+", metavar="field=value")
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("execute", help="submit an already-approved recommendation")
    p.add_argument("id")
    p.add_argument("token")
    p.set_defaults(func=cmd_execute)

    p = sub.add_parser("validate-scoring", help="check the scoring parser against real data")
    p.add_argument("--week", type=int)
    p.set_defaults(func=cmd_validate_scoring)

    p = sub.add_parser("audit", help="show the API write audit trail")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_audit)

    sub.add_parser("run", help="run the scheduler in the foreground").set_defaults(
        func=cmd_run
    )

    p = sub.add_parser("serve", help="run the local approval dashboard (scaffold)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=args.verbose, log_file=args.log_file)

    if not getattr(args, "needs_context", True):
        return args.func(args, None)

    try:
        config = load_config(args.config)
    except MFLBotError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    context = BotContext.build(config)
    try:
        return args.func(args, context)
    except (ApprovalError, MFLBotError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        context.close()


if __name__ == "__main__":
    raise SystemExit(main())
