"""Report limits and lifecycle rules, independent of renderers and delivery vendors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from memoryking.domain.local import LocalError

REPORT_CAPABILITIES = frozenset(
    {"report_author", "report_qa", "report_publish", "report_policy", "report_delivery"}
)
SOURCE_LIMIT = 2 * 1024 * 1024
INPUT_LIMIT = 50 * 1024 * 1024
ARTIFACT_LIMIT = 60 * 1024 * 1024
IMAGE_LIMIT = 8 * 1024 * 1024
PIXEL_LIMIT = 12_000_000
CHUNK_LIMIT = 64 * 1024
SECTION_LIMIT = 64 * 1024
MIME_LIMIT = 24_000_000
BUILD_QUEUE_LIMIT = 10
BUILD_TIMEOUT = 120

Lifecycle = Literal["draft", "building", "built", "qa_verified", "published", "withdrawn", "failed"]
DeliveryState = Literal["queued", "submitting", "smtp_accepted", "rejected", "unknown", "cancelled"]


@dataclass(frozen=True)
class ReportIdentity:
    tenant: str
    project: str
    report: str
    revision: int


def transition(current: Lifecycle, target: Lifecycle) -> None:
    allowed: dict[str, set[str]] = {
        "draft": {"building", "withdrawn"},
        "building": {"built", "failed", "withdrawn"},
        "built": {"qa_verified", "withdrawn"},
        "qa_verified": {"published", "withdrawn"},
        "published": {"withdrawn"},
        "withdrawn": set(),
        "failed": set(),
    }
    if target not in allowed.get(current, set()):
        raise LocalError("invalid_report_transition")
