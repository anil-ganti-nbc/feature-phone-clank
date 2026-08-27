import sys
sys.path.insert(0, 'src')
import json
import feature_phone_clank.collectors as _  # registration
from feature_phone_clank.core.registry import collectors
from feature_phone_clank.core.runner import run_experimental
from feature_phone_clank.providers.sqlite import SqliteStore

store = SqliteStore(":memory:")
summary = {}
for sid in ("doro-gb", "mudita-com", "sunbeam-f1-us", "tcl-alcatel-global"):
    cls = collectors.get(sid)
    c1 = cls()
    r1, stats1 = run_experimental(c1, store, manufacturer=c1.manufacturer,
                                  source_type=c1.source_type, region=c1.region,
                                  base_url=c1.base_url)
    c2 = cls()
    r2, stats2 = run_experimental(c2, store, manufacturer=c2.manufacturer,
                                  source_type=c2.source_type, region=c2.region,
                                  base_url=c2.base_url)
    summary[sid] = {
        "baseline": {"status": r1.status, "discovered": r1.discovered,
                     "accepted": (stats1.get("stats") or {}).get("accepted_count")
                     if isinstance(stats1, dict) else None},
        "resight": {"status": r2.status,
                    "resighted": (stats2.get("stats") or {}).get("resighted_count")
                    if isinstance(stats2, dict) else None,
                    "new": (stats2.get("stats") or {}).get("new_products")
                    if isinstance(stats2, dict) else None},
        "notifications_pending": len(store.pending_notifications("discord")),
        "classified_other_first_run": len(c1.classification_log),
    }
print(json.dumps(summary, indent=1))
