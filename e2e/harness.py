import os
import sys
import time
import json
import uuid
from typing import Optional

import requests

API_BASE = "https://discord.com/api/v10"

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("E2E_GUILD_ID")
CHANNEL_ID = os.getenv("E2E_CHANNEL_ID")
OWNER_ID = os.getenv("E2E_OWNER_ID")

if not DISCORD_TOKEN:
    print("Missing DISCORD_TOKEN in environment. Exiting.")
    sys.exit(2)

if not CHANNEL_ID:
    print("Missing E2E_CHANNEL_ID in environment. Exiting.")
    sys.exit(2)

HEADERS = {
    "Authorization": f"Bot {DISCORD_TOKEN}",
    "Content-Type": "application/json",
}


def create_webhook(channel_id: str, name: str) -> Optional[dict]:
    r = requests.post(
        f"{API_BASE}/channels/{channel_id}/webhooks",
        headers=HEADERS,
        data=json.dumps({"name": name}),
        timeout=15,
    )
    if r.status_code // 100 != 2:
        print("Failed to create webhook:", r.status_code, r.text)
        return None
    return r.json()


def delete_webhook(webhook_id: str):
    requests.delete(f"{API_BASE}/webhooks/{webhook_id}", headers=HEADERS, timeout=15)


def post_via_webhook(webhook: dict, content: str) -> bool:
    url = webhook.get("url")
    if not url:
        wid = webhook.get("id")
        token = webhook.get("token")
        if not (wid and token):
            return False
        url = f"https://discord.com/api/webhooks/{wid}/{token}"
    r = requests.post(url, json={"content": content}, timeout=15)
    return r.status_code // 100 == 2


def fetch_messages(channel_id: str, limit: int = 25):
    r = requests.get(
        f"{API_BASE}/channels/{channel_id}/messages",
        headers=HEADERS,
        params={"limit": limit},
        timeout=15,
    )
    if r.status_code // 100 != 2:
        print("Failed to fetch messages:", r.status_code, r.text)
        return []
    return r.json()


def wait_for_bot_reply(channel_id: str, correlation: str, timeout_s: int = 30) -> Optional[dict]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        msgs = fetch_messages(channel_id, limit=15)
        for m in msgs:
            # naive match: look for correlation token in content and authored by bot
            if isinstance(m.get("author", {}), dict) and m["author"].get("bot") and correlation in (m.get("content") or ""):
                return m
        time.sleep(1.0)
    return None


def scenario_echo(webhook: dict):
    # Use Tools.echo which deletes the command message and echos text
    token = str(uuid.uuid4())[:8]
    content = f"!echo e2e-{token}"
    if not post_via_webhook(webhook, content):
        return False, "Failed to post initial message via webhook"
    reply = wait_for_bot_reply(CHANNEL_ID, f"e2e-{token}", timeout_s=30)
    if not reply:
        return False, "Timed out waiting for echo reply"
    return True, "Echo flow OK"


def scenario_ping(webhook: dict):
    # Use Tools.ping which returns latency text with ms
    if not post_via_webhook(webhook, "!ping"):
        return False, "Failed to post initial message via webhook"
    # Just ensure bot replied in channel within timeout; not asserting exact ms text
    reply = wait_for_bot_reply(CHANNEL_ID, "ms", timeout_s=30)
    if not reply:
        return False, "Timed out waiting for ping reply"
    return True, "Ping flow OK"


def main():
    # Create a temporary webhook
    name = f"e2e-{int(time.time())}"
    webhook = create_webhook(CHANNEL_ID, name)
    if not webhook:
        print("Could not create webhook; ensure the bot has Manage Webhooks permission.")
        sys.exit(2)
    try:
        scenarios = [scenario_echo, scenario_ping]
        failures = []
        for s in scenarios:
            ok, note = s(webhook)
            print(f"{s.__name__}: {'PASS' if ok else 'FAIL'} - {note}")
            if not ok:
                failures.append(note)
        if failures:
            print("E2E: FAIL")
            for f in failures:
                print(" -", f)
            sys.exit(1)
        print("E2E: PASS")
    finally:
        try:
            delete_webhook(webhook.get("id"))
        except Exception:
            pass


if __name__ == "__main__":
    main()
