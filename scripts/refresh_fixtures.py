#!/usr/bin/env python3
"""Explicitly refresh the public latest-100 snapshot; never runs during tests.

Expected outputs must be reviewed separately after refreshing this snapshot.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("monitor", ROOT / "address-monitoring.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def main():
    tree = json.loads(monitor.http(
        f"https://api.github.com/repos/{monitor.REPO}/git/trees/main?recursive=1"))
    if tree.get("truncated"):
        raise ValueError("Registry tree is truncated")
    paths = [item["path"] for item in tree["tree"]
             if item["path"].startswith("deployments/warp_routes/")
             and item["path"].endswith("/aleo-config.yaml")]
    data = monitor.fetch_messages(limit=100)
    messages = data["message_view"]
    assert len(messages) == 100
    assert len({m["msg_id"] for m in messages}) == 100
    assert all(m["destination_domain_id"] == monitor.ALEO_DOMAIN for m in messages)

    def fetch(path):
        url = f"https://raw.githubusercontent.com/{monitor.REPO}/{tree['sha']}/{path}"
        return path, monitor.http(url)

    with ThreadPoolExecutor(max_workers=8) as pool:
        configs = list(pool.map(fetch, paths))
    fixtures = ROOT / "tests" / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "api": monitor.API,
        "query": monitor.QUERY,
        "variables": {"limit": 100, "offset": 0,
                      "where": {"destination_domain_id": {"_eq": monitor.ALEO_DOMAIN}}},
        "registry_commit": tree["sha"],
        "registry_paths": paths,
        "data": data,
    }
    for path, content in configs:
        target = fixtures / "registry" / path.split("/")[-2] / "aleo-config.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    (fixtures / "aleo_latest_100.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"Captured {len(messages)} messages and {len(configs)} routes at {tree['sha']}")


if __name__ == "__main__":
    main()
