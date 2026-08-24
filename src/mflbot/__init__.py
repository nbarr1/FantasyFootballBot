"""mflbot -- human-in-the-loop MyFantasyLeague monitoring and advisory bot.

Design invariant: no MFL write (lineup, add/drop, trade) is ever submitted
without an explicit, per-action approval from the user. There is no auto mode.
The invariant is enforced structurally: every write method requires an
:class:`mflbot.approval.token.ApprovalToken`, which only the approval channel
can mint, and which is bound to the exact payload the user saw.
"""

__version__ = "0.1.0"
