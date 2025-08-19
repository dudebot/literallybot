import argparse
import json
import os
from pathlib import Path

from harness import create_webhook, delete_webhook, post_via_webhook, wait_for_bot_reply, CHANNEL_ID

SCEN_FILE = Path(__file__).parent.parent / "tests" / "cog_scenarios.json"


def run_commands(webhook, commands):
    notes = []
    for cmd in commands:
        ok = post_via_webhook(webhook, cmd)
        notes.append((cmd, ok))
    return notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cogs", nargs="*", help="Filter by fully qualified cog names (e.g., cogs.static.tools)")
    args = ap.parse_args()

    if not CHANNEL_ID:
        print("E2E_CHANNEL_ID not set")
        return 2

    scenarios = json.loads(Path(SCEN_FILE).read_text(encoding="utf-8")) if SCEN_FILE.exists() else {}
    selected = []
    if args.cogs:
        for c in args.cogs:
            if c in scenarios:
                selected.extend(scenarios[c]["commands"])  # use configured
            else:
                selected.extend(scenarios.get("default", {}).get("commands", []))
    else:
        # if nothing specified, gather all unique commands
        seen = set()
        for _, block in scenarios.items():
            for cmd in block.get("commands", []):
                if cmd not in seen:
                    selected.append(cmd)
                    seen.add(cmd)
    if not selected:
        print("No commands to run.")
        return 0

    webhook = create_webhook(CHANNEL_ID, "e2e-selective")
    if not webhook:
        print("Failed to create webhook")
        return 2
    try:
        results = run_commands(webhook, selected)
        for cmd, ok in results:
            print(f"{cmd}: {'SENT' if ok else 'FAIL'}")
    finally:
        delete_webhook(webhook.get("id"))


if __name__ == "__main__":
    raise SystemExit(main())
