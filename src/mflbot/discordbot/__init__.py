"""The Discord bot: a third place to decide on recommendations.

It sits beside the CLI and the dashboard and enforces the same rules through
the same code. It DMs its owner a card per recommendation, with Approve,
Submit, Reject and Edit buttons, and answers a few read-only slash commands.
Only the configured owner's Discord account can use any of it.

``actions``, ``render`` and ``publisher`` hold everything that decides, with no
Discord code in them. ``client`` is the thin adapter to discord.py, which is an
optional dependency (``pip install 'mflbot[discord]'``).
"""
