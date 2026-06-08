#!/usr/bin/env python3
"""Register real application monitors with the Autonomous Integration Platform.

Usage:
  python3 scripts/register_apps.py --file scripts/apps.json --host http://127.0.0.1:8000

This script uses only the Python standard library so it runs in restricted envs.
"""

import json
import sys
from pathlib import Path
from urllib.parse import urljoin
import urllib.request


def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    # If a token is supplied via global variable, include it
    global AUTH_TOKEN
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Register application monitors")
    parser.add_argument("--file", "-f", default="scripts/apps.json", help="Path to JSON file with applications")
    parser.add_argument("--host", "-H", default="http://127.0.0.1:8000", help="Platform host URL")
    parser.add_argument("--token", "-t", default=None, help="Optional API token for protected endpoints")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(2)

    apps = json.loads(path.read_text())
    if not isinstance(apps, list):
        print("Expected an array of application objects in the JSON file")
        sys.exit(2)

    api_url = urljoin(args.host.rstrip('/') + '/', 'api/applications')
    global AUTH_TOKEN
    AUTH_TOKEN = args.token or None
    for app in apps:
        payload = {
            "name": app.get("name"),
            "url": app.get("url"),
            "sla_target_ms": app.get("sla_target_ms", 1000),
            "business_criticality": app.get("business_criticality", "high"),
            "description": app.get("description", "Added by register_apps.py")
        }
        try:
            print(f"Registering: {payload['name']} -> {payload['url']}")
            resp = post_json(api_url, payload)
            print("  OK: id=", resp.get("id"))
        except Exception as exc:
            print("  ERROR:", exc)


if __name__ == '__main__':
    main()
