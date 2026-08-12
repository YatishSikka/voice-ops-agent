"""Import n8n/workflows/*.json into an n8n instance.

The counterpart to export_workflows.py, and what makes moving to a new host
bearable: recreating four workflows by hand in the editor is an hour of
clicking and a good way to introduce a typo.

Credentials are the wrinkle. Credential *ids* are per-instance, so the ids in
the exported JSON mean nothing here. Rather than ask you to re-attach each node
by hand, this matches by credential **type** against what already exists on the
target and rewires the nodes. Create the credential in the UI first -- secrets
are never exported, and never should be.

    python scripts/import_workflows.py            # import, tag, activate
    python scripts/import_workflows.py --dry-run  # show what would happen
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from config import config  # noqa: E402

WORKFLOW_DIR = ROOT / "n8n" / "workflows"

# Fields n8n rejects or regenerates on create.
STRIP = {"id", "active", "tags", "createdAt", "updatedAt", "versionId", "shared",
         "isArchived", "meta", "nodeGroups", "staticData", "pinData",
         "triggerCount", "versionCounter", "activeVersion", "activeVersionId"}


def credentials_by_type(client: httpx.Client, base: str) -> dict[str, dict[str, str]]:
    """What credentials exist on the target, keyed by type."""
    response = client.get(f"{base}/credentials")
    if response.status_code != 200:
        print(f"  ! could not list credentials (HTTP {response.status_code})")
        return {}
    found: dict[str, dict[str, str]] = {}
    for item in response.json().get("data", []):
        # First one wins; duplicates of a type are indistinguishable to us.
        found.setdefault(item["type"], {"id": item["id"], "name": item["name"]})
    return found


def rewire(nodes: list[dict[str, Any]], available: dict[str, dict[str, str]]) -> list[str]:
    """Point each node at a credential that exists here. Returns any gaps."""
    missing = []
    for node in nodes:
        for cred_type in list(node.get("credentials") or {}):
            match = available.get(cred_type)
            if match:
                node["credentials"][cred_type] = match
            else:
                missing.append(f"{node.get('name')} needs a {cred_type} credential")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-activate", action="store_true",
                        help="import but leave workflows inactive")
    args = parser.parse_args()

    files = sorted(WORKFLOW_DIR.glob("*.json"))
    if not files:
        print(f"No workflows in {WORKFLOW_DIR.relative_to(ROOT)}")
        return 1

    base = f"{config.n8n_base_url}/api/v1"
    client = httpx.Client(timeout=60, headers={"X-N8N-API-KEY": config.n8n_api_key or "",
                                               "accept": "application/json"})

    available = credentials_by_type(client, base)
    print(f"Target: {config.n8n_base_url}")
    print(f"Credentials present: {[c['name'] for c in available.values()] or 'none'}\n")

    existing = {w["name"]: w["id"] for w in client.get(f"{base}/workflows").json().get("data", [])}

    # The tag is what makes a workflow visible to the agent at all.
    tags = client.get(f"{base}/tags").json().get("data", [])
    tag_id = next((t["id"] for t in tags if t["name"] == config.n8n_tool_tag), None)
    if tag_id is None and not args.dry_run:
        tag_id = client.post(f"{base}/tags", json={"name": config.n8n_tool_tag}).json()["id"]

    problems: list[str] = []
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        name = raw["name"]
        body = {k: v for k, v in raw.items() if k not in STRIP}
        gaps = rewire(body.get("nodes", []), available)
        problems += [f"{name}: {g}" for g in gaps]

        if args.dry_run:
            print(f"  would import {name}" + (f"  ({len(gaps)} credential gap(s))" if gaps else ""))
            continue

        if name in existing:
            client.delete(f"{base}/workflows/{existing[name]}")

        created = client.post(f"{base}/workflows", json=body)
        if created.status_code not in (200, 201):
            print(f"  FAILED {name}: HTTP {created.status_code} {created.text[:160]}")
            continue
        wid = created.json()["id"]

        # Description is not accepted on create; it needs a follow-up update.
        if raw.get("description"):
            body_with_desc = {**body, "description": raw["description"]}
            client.put(f"{base}/workflows/{wid}", json=body_with_desc)

        if tag_id:
            client.put(f"{base}/workflows/{wid}/tags", json=[{"id": tag_id}])

        status = "imported"
        if not args.no_activate and not gaps:
            act = client.post(f"{base}/workflows/{wid}/activate")
            status = "imported + activated" if act.status_code == 200 else \
                     f"imported, ACTIVATION FAILED: {act.text[:120]}"
        elif gaps:
            status = "imported, left inactive (missing credential)"
        print(f"  {name:26} {status}")

    if problems:
        print("\nCredentials still to connect in the n8n UI:")
        for problem in sorted(set(problems)):
            print(f"  - {problem}")
        print("\nCreate them, then run this again to rewire and activate.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
