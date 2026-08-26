"""The mflbot dashboard: the interactive surface over the whole bot.

``bot serve`` builds this. It presents everything the CLI does -- status,
league configuration, roster and free agents, recommendations, the audit trail
-- and lets you run ingestion and analysis, decide on recommendations, and
submit approved actions, with output streaming live into the page.

The application is assembled from four pieces, each independently testable:

``security``  authentication, server-side sessions, CSRF
``jobs``      the allowlisted bridge to the CLI's own read/analysis commands
``events``    an in-process bus feeding the server-sent-events stream
``views``     domain objects flattened into exactly what a template renders
"""

from .app import build_app
from .jobs import JOB_SPECS, JobManager
from .security import WebSecurity

__all__ = ["build_app", "WebSecurity", "JobManager", "JOB_SPECS"]
