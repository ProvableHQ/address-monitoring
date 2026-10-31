"""Local sender watchlist matching and persistent alert deduplication."""

import csv
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

DEFAULT_WATCHLIST = Path(__file__).resolve().parent / "2026-10-29 Binance - ETH SOL HyperEVM Addresses.csv"
DEFAULT_STATE_DIR = Path(__file__).resolve().parent / ".runtime"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CHAINS = {1: ("evm", "Ethereum"), 999: ("evm", "HyperEVM"),
          1399811149: ("solana", "Solana")}
ALEO_DOMAIN = 1634493807
TITLE = "ALEO BRIDGE ALERT: WATCHLIST SENDER DETECTED"
MAC_ALERT = '''on run argv
    try
        display notification (item 2 of argv) with title (item 1 of argv) sound name "Sosumi"
    end try
    try
        beep 3
        activate
    end try
    display alert (item 1 of argv) message (item 2 of argv) as critical buttons {"Dismiss"} default button 1 giving up after 120
end run'''


def hex_bytes(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Missing hex address or transaction ID")
    if value.startswith(("0x", "0X", "\\x")):
        value = value[2:]
    return bytes.fromhex(value)


def watch_address(address):
    if address.startswith(("0x", "0X")):
        decoded = hex_bytes(address)
        if len(decoded) != 20:
            raise ValueError("EVM watchlist addresses must be 20 bytes")
        return "evm", decoded
    number = 0
    for char in address:
        number = number * 58 + BASE58.index(char)
    leading = len(address) - len(address.lstrip("1"))
    decoded = b"\0" * leading + number.to_bytes((number.bit_length() + 7) // 8, "big")
    if len(decoded) != 32:
        raise ValueError("Solana watchlist addresses must decode to 32 bytes")
    return "solana", decoded


def load_watchlist(path):
    """Read the filtered CSV's address column, preserving its display spelling."""
    watchlist = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as source:
        for line, row in enumerate(csv.reader(source), 1):
            if not row or not row[0].strip():
                continue
            address = row[0].strip()
            if line == 1 and address.lower() in {"address", "wallet", "wallet_address"}:
                continue
            try:
                key = watch_address(address)
            except ValueError as error:
                raise ValueError(f"Invalid watchlist address at {path}:{line}: {error}") from error
            watchlist[key] = address
    if not watchlist:
        raise ValueError(f"No addresses in watchlist: {path}")
    return watchlist


def sender_matches(messages, watchlist):
    matches = {}
    for message in messages:
        chain = CHAINS.get(message.get("origin_domain_id"))
        if chain is None or message.get("destination_domain_id") != ALEO_DOMAIN:
            continue
        protocol, chain_name = chain
        for field in ("origin_tx_sender", "sender"):
            try:
                address = hex_bytes(message.get(field))
                if protocol == "evm":
                    if len(address) == 32 and address[:12] == b"\0" * 12:
                        address = address[12:]
                    if len(address) != 20:
                        continue
                elif len(address) != 32:
                    continue
                display_address = watchlist.get((protocol, address))
                if display_address is None:
                    continue
                tx_hash = hex_bytes(message["origin_tx_hash"]).hex()
                msg_id = hex_bytes(message["msg_id"]).hex()
                if not tx_hash or len(msg_id) != 64:
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            # One alert per origin transaction and watched address, even if
            # one transaction dispatches multiple Hyperlane messages.
            key = (message["origin_domain_id"], tx_hash, address.hex())
            if key not in matches:
                matches[key] = {
                    "origin_domain_id": key[0], "origin_chain": chain_name,
                    "address": display_address, "address_key": key[2],
                    "origin_tx_hash": "0x" + tx_hash, "msg_id": "0x" + msg_id,
                    "send_occurred_at": message.get("send_occurred_at"), "matched_fields": [],
                    "message_url": "https://explorer.hyperlane.xyz/message/0x" + msg_id,
                }
            if field not in matches[key]["matched_fields"]:
                matches[key]["matched_fields"].append(field)
    return list(matches.values())


def alert_text(alerts):
    blocks = []
    for alert in alerts:
        risk = alert.get("risk") or {}
        source_text = "Merkle Science address list" if alert.get("source") == "merkle_science" else "CSV watchlist"
        risk_text = (f"Risk reported: {risk.get('risk_level_verbose')} ({risk.get('risk_level')})\n"
                     if risk else "")
        blocks.append(
            f"Source: {source_text}\n" + risk_text +
            f"Chain: {alert['origin_chain']} -> Aleo\n"
            f"Sender: {alert['address']}\n"
            f"Matched field(s): {', '.join(alert['matched_fields'])}\n"
            f"Transaction: {alert['origin_tx_hash']}\n"
            f"Sent: {alert['send_occurred_at']}\n"
            f"{alert['message_url']}"
        )
    return "\n\n".join(blocks)


def show_local_alert(title, message):
    """Launch a desktop alert without waiting for the user to dismiss it."""
    if sys.platform == "darwin":
        command = ["/usr/bin/osascript", "-e", MAC_ALERT, title, message]
    elif shutil.which("notify-send"):
        command = ["notify-send", "--urgency=critical", title, message]
    else:
        print("Desktop alert unavailable; see the local alert log.", file=sys.stderr)
        return False
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, start_new_session=True)
        try:
            # Detect immediate errors (such as an unavailable desktop session).
            code = process.wait(timeout=0.25)
            if code:
                print(f"Desktop alert command failed (exit {code}); notification remains pending.", file=sys.stderr)
            return code == 0
        except subprocess.TimeoutExpired:
            return True
    except OSError as error:
        print(f"Cannot launch desktop alert: {error}", file=sys.stderr)
        return False


def print_banner(title, message):
    banner = f"\n{'!' * 78}\n{title}\n{'!' * 78}\n{message}\n{'!' * 78}\n"
    if sys.stderr.isatty():
        banner = "\a\033[1;97;41m" + banner + "\033[0m"
    print(banner, file=sys.stderr, flush=True)
    return banner


def check_senders(messages, watchlist, state_dir, source="csv", details=None):
    """Record matches, notify once, and retry alerts that could not be launched."""
    matches = sender_matches(messages, watchlist)
    state_dir = Path(state_dir)
    database = state_dir / "sender-alerts.sqlite3"
    if not matches and not database.exists():
        return 0
    state_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=10)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        columns = [row[1] for row in connection.execute("PRAGMA table_info(alerts)")]
        if columns and "source" not in columns:
            connection.execute("ALTER TABLE alerts RENAME TO legacy_alerts")
        connection.execute('''CREATE TABLE IF NOT EXISTS alerts (
            source TEXT NOT NULL, origin_domain INTEGER, tx_hash TEXT, address_key TEXT,
            payload TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (source, origin_domain, tx_hash, address_key)
        )''')
        if columns and "source" not in columns:
            connection.execute("INSERT INTO alerts SELECT 'csv', origin_domain, tx_hash, address_key, payload, notified FROM legacy_alerts")
            connection.execute("DROP TABLE legacy_alerts")
        now = datetime.now(timezone.utc).isoformat()
        new_alerts = []
        for match in matches:
            key = (CHAINS[match["origin_domain_id"]][0], bytes.fromhex(match["address_key"]))
            payload = dict(match, detected_at=now, source=source, risk=(details or {}).get(key))
            inserted = connection.execute(
                "INSERT OR IGNORE INTO alerts (source, origin_domain, tx_hash, address_key, payload) VALUES (?, ?, ?, ?, ?)",
                (source, match["origin_domain_id"], match["origin_tx_hash"], match["address_key"], json.dumps(payload)))
            if inserted.rowcount:
                new_alerts.append(payload)
        connection.commit()
        if new_alerts:
            with (state_dir / "alerts.jsonl").open("a", encoding="utf-8") as log:
                for payload in new_alerts:
                    log.write(json.dumps(payload) + "\n")
        # Serialize notification dispatch across simultaneous foreground and
        # background polls using the same local state directory.
        connection.execute("BEGIN IMMEDIATE")
        pending = connection.execute(
            "SELECT source, origin_domain, tx_hash, address_key, payload FROM alerts WHERE notified = 0").fetchall()
        if not pending:
            return 0
        alerts = [json.loads(row[4]) for row in pending]
        text = alert_text(alerts)
        (state_dir / "latest-alert.txt").write_text(TITLE + "\n\n" + text + "\n", encoding="utf-8")
        print_banner(TITLE, text)
        # A batch produces one popup; full details remain in the local files.
        popup_text = alert_text(alerts[:3])
        if len(alerts) > 3:
            popup_text += f"\n\n{len(alerts) - 3} more matches. See {state_dir / 'latest-alert.txt'}"
        if show_local_alert(TITLE, popup_text):
            connection.executemany(
                "UPDATE alerts SET notified = 1 WHERE source = ? AND origin_domain = ? AND tx_hash = ? AND address_key = ?",
                [row[:4] for row in pending])
            connection.commit()
        return len(alerts)
