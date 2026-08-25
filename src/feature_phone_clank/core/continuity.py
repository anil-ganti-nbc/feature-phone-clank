"""Observational continuity registry (ADR-0006).

Append-only JSONL registry of continuity events living in RUNTIME state
(`<db parent>/continuity/continuity-events.jsonl`), never inside the
database and never in the source tree. Every record is content-hashed;
tampering is detectable; history is never rewritten — later knowledge is
appended, earlier records are never edited.

The seed events below record operator-verified incident facts ONLY:
the 2026-08-23 destructive volume deletion destroyed all prior Feature
Phone observation history with no backup, and epoch fpc-epoch-2 begins at
that boundary. The lost history is NOT reconstructed, estimated, or
fabricated here; `previous_epoch_id` stays null because no identifier for
the destroyed epoch was ever recorded.

Evidence basis (read-only canon):
- clank-architecture ADR-0006 (event contract, vocabulary)
- clank-architecture DATA_SURVIVABILITY.md section 7 / 17.1
  ("Epoch fpc-epoch-2 begins 2026-08-23T21:36:11Z"; ACT-011 RP1)
- diagnostic-clank fleet.yaml: fpc-hetzner-prod-cron-01 (production lane)
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

CLANK_ID = "feature-phone-clank"
INSTANCE_ID = "fpc-hetzner-prod-cron-01"
LANE_ID = "production"
EPOCH_ID = "fpc-epoch-2"

# Operator-verified boundary instant (canon: ADR-0006; DATA_SURVIVABILITY §7).
EPOCH_2_START_UTC = "2026-08-23T21:36:11Z"

SEED_EVENTS: tuple[dict, ...] = (
    {
        "event_id": "fpc-20260823-data-loss-0001",
        "clank_id": CLANK_ID,
        "instance_id": INSTANCE_ID,
        "lane_id": LANE_ID,
        "event_type": "DATA_LOSS",
        "effective_start": EPOCH_2_START_UTC,
        "effective_end": None,
        "discovered_at": "2026-08-23T21:36:11Z",
        "evidence_refs": [
            "clank-architecture/adr/0006-continuity-and-epoch-semantics.md",
            "clank-architecture/DATA_SURVIVABILITY.md#7-feature-phone-new-epoch-protection-analysis",
            "clank-architecture/DATA_SURVIVABILITY.md#17-pass-2-update",
        ],
        "previous_epoch_id": None,
        "new_epoch_id": None,
        "origin": "operator",
        "notes": (
            "Destructive volume deletion removed the only copy of all prior "
            "observation history; no backup existed at loss time "
            "(DATA_SURVIVABILITY §4: 'NONE existed'). The destroyed epoch was "
            "never named; its identifier remains UNKNOWN. Nothing about the "
            "lost observations is reconstructed."
        ),
    },
    {
        "event_id": "fpc-20260823-epoch-boundary-0002",
        "clank_id": CLANK_ID,
        "instance_id": INSTANCE_ID,
        "lane_id": LANE_ID,
        "event_type": "EPOCH_BOUNDARY",
        "effective_start": EPOCH_2_START_UTC,
        "effective_end": None,
        "discovered_at": "2026-08-24T00:00:00Z",
        "evidence_refs": [
            "clank-architecture/adr/0006-continuity-and-epoch-semantics.md",
            "clank-architecture/DATA_SURVIVABILITY.md#7-feature-phone-new-epoch-protection-analysis",
        ],
        "previous_epoch_id": None,
        "new_epoch_id": EPOCH_ID,
        "origin": "operator",
        "notes": (
            "Epoch fpc-epoch-2 begins 2026-08-23T21:36:11Z and is "
            "irreplaceable from its first byte. Pre-boundary histories MUST "
            "NOT be merged with this epoch; a fresh baseline is never novelty."
        ),
    },
)

_REGISTRY_LOCK = threading.Lock()


def registry_path(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / "continuity" / "continuity-events.jsonl"


def _content_hash(record: dict) -> str:
    payload = {k: v for k, v in record.items() if k != "content_hash"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_events(db_path: str | Path) -> list[dict]:
    path = registry_path(db_path)
    if not path.exists():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return events


def verify_hashes(events: list[dict]) -> list[str]:
    """Return event_ids whose content_hash does not match their content."""
    bad: list[str] = []
    for event in events:
        expected = _content_hash(event)
        if event.get("content_hash") != expected:
            bad.append(event.get("event_id", "<unnamed>"))
    return bad


def append_event(db_path: str | Path, event: dict) -> dict:
    """Append one event with provenance defaults and a content hash.

    Append-only: existing lines are never modified. Callers must not pass
    a precomputed content_hash; it is derived here so it always covers the
    stored bytes.
    """
    record = {
        "clank_id": CLANK_ID,
        "instance_id": INSTANCE_ID,
        "lane_id": LANE_ID,
        **event,
    }
    record["content_hash"] = _content_hash(record)
    path = registry_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _REGISTRY_LOCK:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return record


def ensure_registry(db_path: str | Path) -> Path:
    """Create the runtime registry seeded with operator-verified incident
    facts if (and only if) those seed event_ids are absent. Idempotent;
    never edits or removes existing records."""
    existing_ids = {e.get("event_id") for e in read_events(db_path)}
    for seed in SEED_EVENTS:
        if seed["event_id"] not in existing_ids:
            append_event(db_path, {k: v for k, v in seed.items()})
            existing_ids.add(seed["event_id"])
    return registry_path(db_path)
