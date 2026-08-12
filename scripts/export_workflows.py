"""Export tagged n8n workflows to n8n/workflows/*.json.

Workflows are part of the source of this project, not incidental config: they
are what the agent can do. Keeping them in git means the tool set is
reviewable, diffable, and restorable into a fresh n8n instance.

    python scripts/export_workflows.py            # export tagged workflows
    python scripts/export_workflows.py --all      # export everything
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import config
from tools.n8n_client import N8nClient
from tools.schema import slugify

OUT_DIR = ROOT / "n8n" / "workflows"

# Volatile fields: they churn on every save and would make every diff noise.
DROP_KEYS = {
    "createdAt", "updatedAt", "versionId", "activeVersionId", "activeVersion",
    "versionCounter", "triggerCount", "shared", "staticData", "pinData",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true", help="export untagged workflows too")
    args = parser.parse_args()

    client = N8nClient()
    workflows = client.list_workflows(tag=None if args.all else config.n8n_tool_tag)
    if not workflows:
        print("No workflows to export.")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Prune first. Writing new files without removing old ones leaves deleted
    # workflows in the repo, and the importer would faithfully recreate them on
    # a fresh host -- resurrecting tools that were deliberately retired.
    keep = {f"{slugify(w.name)}.json" for w in workflows}
    for stale in OUT_DIR.glob("*.json"):
        if stale.name not in keep:
            stale.unlink()
            print(f"  removed {stale.relative_to(ROOT)} (no longer in n8n)")

    for summary in workflows:
        # Refetch: the list response is incomplete (no description).
        raw = client._get(f"/workflows/{summary.id}")
        cleaned = {k: v for k, v in raw.items() if k not in DROP_KEYS}
        cleaned["tags"] = sorted(t.get("name", "") for t in (raw.get("tags") or []))

        path = OUT_DIR / f"{slugify(summary.name)}.json"
        path.write_text(json.dumps(cleaned, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"  {summary.name!r} -> {path.relative_to(ROOT)}")

    print(f"\nExported {len(workflows)} workflow(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
