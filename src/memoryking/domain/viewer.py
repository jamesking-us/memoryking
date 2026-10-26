"""Viewer identity constants and immutable context, independent of transports/stores."""

from __future__ import annotations

from dataclasses import dataclass, field

GITHUB_ISSUER = "https://github.com"
VIEWER_CAPABILITIES = frozenset({"read"})


@dataclass(frozen=True)
class VerifiedViewer:
    """Internal authenticated-subject context; never serialize its credential to a browser.

    Construction belongs to the trusted provider/session adapter. Store operations still
    reauthorize its signed identity and generations; this object grants no privilege.
    """

    store: str
    tenant: str
    principal: str
    user_id: str
    incarnation: str
    generation: int
    policy_epoch: int
    credential: str = field(repr=False)
