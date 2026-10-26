"""Private Slack <-> Firstmate bridge for the captain's DMs.

The pure pieces (routing, receipts, keeper, text) import only the standard
library so the test suite runs offline; only ``slack_app`` and ``slack_api``
need the Slack SDK.
"""

__version__ = "0.1.0"
