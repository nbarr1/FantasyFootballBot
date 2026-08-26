"""Running the bot's own commands from the browser, one at a time.

The dashboard's action buttons do not reimplement ingestion or analysis. Each
one runs the *same* ``bot`` subcommand the CLI runs, through the same argument
parser, and streams what it prints back to the page. There is one
implementation of "sync the config", not two that can drift.

What that bridge is allowed to reach is fenced deliberately:

* :data:`RUNNABLE_COMMANDS` is an allowlist of CLI subcommands, and it contains
  no command that can submit anything to MFL. ``approve``, ``execute``,
  ``reject`` and ``edit`` are not in it and must not be added -- a decision is
  something a human makes on a specific recommendation through the approval
  routes, never a button that fires a batch. ``tests/test_web_app.py`` asserts
  the allowlist stays clean.
* Jobs run one at a time on a single worker thread. That keeps MFL request
  pacing intact (the client's rate limiter is per-process), keeps the SQLite
  connection to one writer, and makes captured stdout unambiguous.
* Output is passed through :func:`mflbot.mfl.auth.redact` before it reaches a
  browser, matching the guarantee the log formatters already make.
"""

from __future__ import annotations

import contextlib
import io
import logging
import queue
import shlex
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..logging_setup import RedactingFormatter
from ..mfl.auth import redact
from .events import EventBus

log = logging.getLogger(__name__)

#: CLI subcommands the dashboard may run. Read and analysis only: every one of
#: these is reachable from `mflbot.analysis` / `mflbot.ingest`, which
#: `tests/test_write_isolation.py` proves cannot reach the write client.
RUNNABLE_COMMANDS = frozenset(
    {
        "config-summary",
        "status",
        "whoami",
        "verify-endpoints",
        "sync-config",
        "sync-players",
        "sync-projections",
        "sync-scores",
        "poll",
        "news",
        "analyse",
        "validate-scoring",
        "audit",
    }
)

#: Commands that exist but must never be reachable from a browser button.
#: Listed explicitly so the reason is written down next to the allowlist.
FORBIDDEN_COMMANDS = frozenset(
    {
        "approve",  # a decision, made per recommendation through /approve
        "execute",  # submits to MFL; needs a token from an explicit approval
        "reject",
        "edit",
        "init",  # rewrites config.toml under the running process
        "run",  # blocking scheduler; the dashboard has its own control for it
        "serve",  # would nest a second dashboard
    }
)


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class JobParam:
    """One user-supplied argument, rendered as a form field."""

    name: str
    label: str
    kind: str = "int"
    required: bool = False
    help: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "help": self.help,
        }


@dataclass(frozen=True, slots=True)
class JobSpec:
    """A button on the dashboard, and the CLI invocation behind it."""

    id: str
    label: str
    group: str
    description: str
    argv: tuple[str, ...]
    params: tuple[JobParam, ...] = ()

    def __post_init__(self) -> None:
        command = self.argv[0]
        if command in FORBIDDEN_COMMANDS or command not in RUNNABLE_COMMANDS:
            raise ValueError(
                f"{self.id!r} maps to `bot {command}`, which the dashboard is not "
                f"allowed to run. Only read and analysis commands are runnable "
                f"from a browser."
            )

    def build_argv(self, params: dict[str, Any]) -> list[str]:
        """Turn submitted form values into command-line arguments."""
        argv = list(self.argv)
        for param in self.params:
            raw = params.get(param.name)
            value = "" if raw is None else str(raw).strip()
            if not value:
                if param.required:
                    raise ValueError(f"{param.label} is required.")
                continue
            if param.kind == "int":
                try:
                    int(value)
                except ValueError:
                    raise ValueError(
                        f"{param.label} must be a whole number, not {value!r}."
                    ) from None
            argv.extend([f"--{param.name.replace('_', '-')}", value])
        return argv

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "group": self.group,
            "description": self.description,
            "command": "bot " + " ".join(self.argv),
            "params": [p.as_dict() for p in self.params],
        }


WEEK_PARAM = JobParam(
    "week", "Week", "int", False, "Defaults to the current NFL week, read from MFL."
)

#: Every action the dashboard offers, in the order it presents them.
JOB_SPECS: tuple[JobSpec, ...] = (
    JobSpec(
        "sync-config",
        "Sync league config",
        "Ingest",
        "Pull league settings and scoring rules. Run this first, and again "
        "whenever the commissioner changes scoring.",
        ("sync-config", "--force"),
    ),
    JobSpec(
        "sync-players",
        "Sync player database",
        "Ingest",
        "Refresh MFL's player database. Once a day is MFL's stated limit.",
        ("sync-players",),
    ),
    JobSpec(
        "poll",
        "Poll league state",
        "Ingest",
        "Rosters, free agents and new transactions since the last poll.",
        ("poll",),
    ),
    JobSpec(
        "news",
        "Ingest news",
        "Ingest",
        "Pull player news from every configured source.",
        ("news",),
    ),
    JobSpec(
        "sync-projections",
        "Sync projections",
        "Ingest",
        "Pull MFL's projected scores for a week. Lineup and waiver analysis "
        "stay blocked without these.",
        ("sync-projections",),
        (WEEK_PARAM,),
    ),
    JobSpec(
        "sync-scores",
        "Sync actual scores",
        "Ingest",
        "Pull the real scores for a completed week.",
        ("sync-scores",),
        (WEEK_PARAM,),
    ),
    JobSpec(
        "analyse-lineup",
        "Analyse lineup",
        "Analyse",
        "Compare your submitted lineup against the highest projected legal one. "
        "Produces a recommendation; submits nothing.",
        ("analyse", "lineup"),
        (WEEK_PARAM,),
    ),
    JobSpec(
        "analyse-waivers",
        "Analyse waivers",
        "Analyse",
        "Look for add/drop moves that clear your configured thresholds.",
        ("analyse", "waivers"),
        (WEEK_PARAM,),
    ),
    JobSpec(
        "analyse-trades",
        "Analyse trades",
        "Analyse",
        "Draft trade proposals that project as a gain for both sides.",
        ("analyse", "trades"),
        (WEEK_PARAM,),
    ),
    JobSpec(
        "verify-endpoints",
        "Verify endpoints",
        "Verify",
        "Reconcile the endpoint registry against MFL's live API documentation. "
        "A write capability with no verified entry refuses to fire.",
        ("verify-endpoints",),
    ),
    JobSpec(
        "whoami",
        "Identify my franchise",
        "Verify",
        "Ask MFL which franchise this login owns.",
        ("whoami",),
    ),
    JobSpec(
        "status",
        "Print status",
        "Verify",
        "Run `bot status` and show its output verbatim -- the same summary the "
        "CLI prints, useful for confirming the two surfaces agree.",
        ("status",),
    ),
    JobSpec(
        "config-summary",
        "Print config summary",
        "Verify",
        "Run `bot config-summary`: what the parser found, what it looked for and "
        "did not find, and what is consequently blocked.",
        ("config-summary",),
    ),
    JobSpec(
        "validate-scoring",
        "Validate scoring",
        "Verify",
        "Check the scoring parser against a week of real results, and report "
        "plainly what it cannot check.",
        ("validate-scoring",),
        (WEEK_PARAM,),
    ),
)

JOB_SPECS_BY_ID: dict[str, JobSpec] = {spec.id: spec for spec in JOB_SPECS}


@dataclass(slots=True)
class JobRun:
    """One execution, live or finished."""

    id: str
    spec_id: str
    label: str
    #: The exact argument vector handed to the CLI parser. Kept as a list
    #: rather than re-split from the display string, so a value containing a
    #: space cannot turn into two arguments between submission and execution.
    argv: tuple[str, ...]
    actor: str
    status: str = JobStatus.QUEUED
    lines: list[str] = field(default_factory=list)
    exit_code: int | None = None
    error: str = ""
    queued_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def command(self) -> str:
        return "bot " + shlex.join(self.argv)

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now(UTC)
        return round((end - self.started_at).total_seconds(), 1)

    @property
    def is_finished(self) -> bool:
        return self.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}

    def as_dict(self, *, include_output: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id,
            "spec_id": self.spec_id,
            "label": self.label,
            "command": self.command,
            "actor": self.actor,
            "status": self.status,
            "exit_code": self.exit_code,
            "error": self.error,
            "queued_at": self.queued_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
        }
        if include_output:
            data["lines"] = list(self.lines)
        return data


class _StreamCapture(io.TextIOBase):
    """Collects written text line by line and hands each finished line onward."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._buffer = ""

    def write(self, text: str) -> int:  # type: ignore[override]
        self._buffer += text
        while "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            self._emit(line)
        return len(text)

    def flush(self) -> None:  # pragma: no cover - nothing buffered downstream
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""

    def writable(self) -> bool:
        return True


class _LogCapture(logging.Handler):
    def __init__(self, emit: Callable[[str], None]) -> None:
        super().__init__(level=logging.INFO)
        self._emit = emit
        self.setFormatter(RedactingFormatter("%(levelname)-7s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emit(self.format(record))
        except Exception:  # pragma: no cover - a broken log line must not kill a job
            self.handleError(record)


class JobManager:
    """Queues jobs, runs them one at a time, and publishes their output."""

    #: How many finished runs to keep. They are in memory only; the durable
    #: record of anything a job produced is the database it wrote to.
    history_limit = 25

    def __init__(self, context, bus: EventBus, *, enabled: bool = True) -> None:
        self._context = context
        self._bus = bus
        self._enabled = enabled
        self._queue: queue.Queue[JobRun] = queue.Queue()
        self._runs: dict[str, JobRun] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not self._enabled or self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._work, name="mflbot-jobs", daemon=True
        )
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._queue.put(None)  # type: ignore[arg-type]
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join(timeout=timeout)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # -- submission --------------------------------------------------------

    def submit(self, spec_id: str, params: dict[str, Any], actor: str) -> JobRun:
        if not self._enabled:
            raise PermissionError(
                "This dashboard was started with --no-jobs, so it can display "
                "data but not run ingestion or analysis."
            )
        spec = JOB_SPECS_BY_ID.get(spec_id)
        if spec is None:
            raise KeyError(f"No such action: {spec_id!r}")
        argv = spec.build_argv(params)
        run = JobRun(
            id=uuid.uuid4().hex[:12],
            spec_id=spec.id,
            label=spec.label,
            argv=tuple(argv),
            actor=actor,
        )
        run.lines.append(f"$ {run.command}")
        with self._lock:
            self._runs[run.id] = run
            self._order.insert(0, run.id)
            self._trim()
        self._queue.put(run)
        self.start()
        self._publish(run)
        return run

    def _trim(self) -> None:
        while len(self._order) > self.history_limit:
            dropped = self._order.pop()
            candidate = self._runs.get(dropped)
            if candidate is not None and not candidate.is_finished:
                # Never evict something still running: put it back and stop.
                self._order.append(dropped)
                return
            self._runs.pop(dropped, None)

    # -- inspection --------------------------------------------------------

    def runs(self, limit: int = 25) -> list[JobRun]:
        with self._lock:
            return [self._runs[i] for i in self._order[:limit] if i in self._runs]

    def get(self, run_id: str) -> JobRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def active(self) -> JobRun | None:
        with self._lock:
            for run_id in self._order:
                run = self._runs.get(run_id)
                if run is not None and not run.is_finished:
                    return run
        return None

    # -- execution ---------------------------------------------------------

    def _publish(self, run: JobRun) -> None:
        self._bus.publish("job", **run.as_dict(include_output=False))

    def _work(self) -> None:
        while not self._stopping.is_set():
            run = self._queue.get()
            if run is None:
                break
            try:
                self._execute(run)
            except Exception:  # noqa: BLE001 - a failed job must not kill the worker
                log.exception("job %s crashed", run.id)

    def _execute(self, run: JobRun) -> None:
        from ..cli import build_parser

        def emit(line: str) -> None:
            clean = redact(line)
            with self._lock:
                run.lines.append(clean)
            self._bus.publish("job.log", run_id=run.id, line=clean)

        run.status = JobStatus.RUNNING
        run.started_at = datetime.now(UTC)
        self._publish(run)

        handler = _LogCapture(emit)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            args = build_parser().parse_args(list(run.argv))
            capture = _StreamCapture(emit)
            with contextlib.redirect_stdout(capture):
                exit_code = args.func(args, self._context)
            capture.flush()
            run.exit_code = int(exit_code or 0)
            run.status = JobStatus.SUCCEEDED if run.exit_code == 0 else JobStatus.FAILED
            if run.exit_code:
                run.error = f"exited with code {run.exit_code}"
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            run.status = JobStatus.FAILED
            run.error = f"{type(exc).__name__}: {redact(str(exc))}"
            emit(run.error)
            log.exception("job %s (%s) failed", run.id, run.spec_id)
        finally:
            root.removeHandler(handler)
            run.finished_at = datetime.now(UTC)
            self._publish(run)
            # The page shows counts and blocked features that a job may have
            # changed, so nudge every connected tab to re-read them.
            self._bus.publish("state.changed", reason=run.spec_id)
