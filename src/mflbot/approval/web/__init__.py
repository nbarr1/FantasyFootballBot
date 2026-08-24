"""Local web dashboard -- the alternative approval channel.

Status: **scaffolded, not finished.** Routes, templates and the
:class:`~mflbot.approval.channel.ApprovalChannel` implementation are in place and
the app runs, but it has had no browser testing, no CSRF protection, and no
authentication beyond binding to localhost. The CLI channel is the supported
default; see the end-of-run report.

What is missing before this should be trusted with approvals:

* CSRF tokens on the approve/reject/edit forms.
* Some form of local authentication, so that anything able to reach the port is
  not automatically able to approve MFL writes.
* A real edit form (the current one accepts raw field=value pairs).
* Browser testing.
"""

from .app import WebApprovalChannel, build_app

__all__ = ["build_app", "WebApprovalChannel"]
