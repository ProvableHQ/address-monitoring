"""Local sender watchlist matching and persistent alert deduplication."""

import csv
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import shutil
import sqlite3
import subprocess
import sys
import time

from slack_alerts import SlackDeliveryError, send_webhook

DEFAULT_WATCHLIST = Path(__file__).resolve().parent / "2026-10-29 Binance - ETH SOL HyperEVM Addresses.csv"
DEFAULT_STATE_DIR = Path(__file__).resolve().parent / ".runtime"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CHAINS = {1: ("evm", "Ethereum"), 999: ("evm", "HyperEVM"),
          1399811149: ("solana", "Solana")}
ALEO_DOMAIN = 1634493807
TITLE = "Hyperlane Bridge Alert:  Flagged address attempting to mint on Aleo"
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


def transfer_summary(message):
    """Only user-facing transfer data; never substitute the mailbox recipient."""
    recipient = message.get("destination_address")
    if not isinstance(recipient, str) or not recipient.startswith("aleo1") or len(recipient) != 63:
        recipient = None
    return {"msg_id": "0x" + hex_bytes(message["msg_id"]).hex(),
            "asset": message.get("token"), "amount": message.get("amount"),
            "aleo_recipient": recipient, "decode_status": message.get("decode_status", "unavailable")}


def sender_address(protocol, address):
    if protocol == "evm":
        return "0x" + address.hex()
    number, encoded = int.from_bytes(address, "big"), ""
    while number:
        number, digit = divmod(number, 58)
        encoded = BASE58[digit] + encoded
    return "1" * (len(address) - len(address.lstrip(b"\0"))) + encoded


def sender_matches(messages, watchlist):
    matches = {}
    for message in messages:
        chain = CHAINS.get(message.get("origin_domain_id"))
        if chain is None or message.get("destination_domain_id") != ALEO_DOMAIN:
            continue
        protocol, chain_name = chain
        try:
            address = hex_bytes(message.get("origin_tx_sender"))
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
        key = (message["origin_domain_id"], tx_hash, address.hex())
        if key not in matches:
            matches[key] = {
                "origin_domain_id": key[0], "origin_chain": chain_name,
                "address": display_address, "address_key": key[2],
                "origin_tx_sender": sender_address(protocol, address),
                "origin_tx_hash": "0x" + tx_hash, "msg_id": "0x" + msg_id,
                "send_occurred_at": message.get("send_occurred_at"), "matched_fields": ["origin_tx_sender"],
                "message_url": "https://explorer.hyperlane.xyz/message/0x" + msg_id,
                "transfers": [],
            }
        if "0x" + msg_id not in {t["msg_id"] for t in matches[key]["transfers"]}:
            matches[key]["transfers"].append(transfer_summary(message))
    return list(matches.values())


def alert_text(alerts):
    blocks = []
    for alert in alerts:
        risk = alert.get("risk") or {}
        source_text = {"merkle_science": "Merkle Science address list", "demo": "DEMO (real transaction; watchlist not evaluated)"}.get(
            alert.get("source"), "CSV watchlist")
        risk_text = (f"Risk reported: {risk.get('risk_level_verbose')} ({risk.get('risk_level')})\n"
                     if risk else "")
        signer = alert.get("origin_tx_sender")
        if signer is None and "origin_tx_sender" in alert.get("matched_fields", []):
            signer = alert.get("address")
        transfers = alert.get("transfers") or [{}]
        transfer_lines = []
        for position, transfer in enumerate(transfers, 1):
            asset = transfer.get("asset") or "Unavailable"
            amount = transfer.get("amount")
            recipient = transfer.get("aleo_recipient")
            if not isinstance(recipient, str) or not recipient.startswith("aleo1") or len(recipient) != 63:
                recipient = "Unavailable"
            if len(transfers) > 1:
                transfer_lines.append(f"Transfer {position}:")
            transfer_lines.extend([f"Asset: {asset}",
                                   f"Amount: {str(amount) + ' ' + asset if amount is not None else 'Unavailable'}",
                                   f"Aleo recipient: {recipient}"])
        blocks.append(
            f"Source: {source_text}\n" + risk_text +
            f"Chain: {alert['origin_chain']} -> Aleo\n"
            f"Sender: {signer or 'Unavailable'}\n" + "\n".join(transfer_lines) + "\n" +
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


def dispatch_slack(connection, webhook_url):
    if webhook_url is None:
        return
    state = connection.execute("SELECT retry_at, failures FROM delivery_state WHERE sink = 'slack'").fetchone()
    if state and time.time() < state[0]:
        return
    rows = connection.execute('''SELECT source, origin_domain, tx_hash, address_key, payload
                                 FROM alerts WHERE slack_sent = 0 ORDER BY rowid LIMIT 5''').fetchall()
    if not rows:
        return
    sections = [alert_text([json.loads(row[4])]) for row in rows]
    try:
        send_webhook(webhook_url, TITLE, sections)
    except SlackDeliveryError as error:
        failures = (state[1] if state else 0) + 1
        delay = max(error.retry_after, min(300, 5 * 2 ** min(failures - 1, 6)) + random.uniform(0, 1))
        connection.execute("INSERT OR REPLACE INTO delivery_state VALUES ('slack', ?, ?, ?)",
                           (time.time() + delay, failures, str(error)))
        print(f"{error}; Slack alerts remain queued (retry in {delay:.0f}s).", file=sys.stderr)
        return
    connection.executemany('''UPDATE alerts SET slack_sent = 1
                             WHERE source = ? AND origin_domain = ? AND tx_hash = ? AND address_key = ?''',
                          [row[:4] for row in rows])
    # Slack permits about one incoming-webhook message per second. Batch up to
    # five alerts, then let a later invocation drain more without blocking polls.
    connection.execute("INSERT OR REPLACE INTO delivery_state VALUES ('slack', ?, 0, NULL)", (time.time() + 1,))
    print(f"Slack delivered {len(rows)} sender alert(s).", file=sys.stderr)


def retry_pending_alerts(state_dir, slack_webhook_url=None):
    return check_senders([], {}, state_dir, slack_webhook_url=slack_webhook_url)


def check_senders(messages, watchlist, state_dir, source="csv", details=None, slack_webhook_url=None):
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
            slack_sent INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (source, origin_domain, tx_hash, address_key)
        )''')
        if columns and "source" not in columns:
            connection.execute('''INSERT INTO alerts (source, origin_domain, tx_hash, address_key, payload, notified)
                                  SELECT 'csv', origin_domain, tx_hash, address_key, payload, notified FROM legacy_alerts''')
            connection.execute("DROP TABLE legacy_alerts")
        if "slack_sent" not in [row[1] for row in connection.execute("PRAGMA table_info(alerts)")]:
            # Do not replay historical alerts when Slack is first enabled.
            connection.execute("ALTER TABLE alerts ADD COLUMN slack_sent INTEGER NOT NULL DEFAULT 1")
        # Retire queued hits created under the former router-matching rule.
        for row in connection.execute('''SELECT source, origin_domain, tx_hash, address_key, payload
                                         FROM alerts WHERE notified = 0 OR slack_sent = 0''').fetchall():
            payload = json.loads(row[4])
            if "origin_tx_sender" not in payload.get("matched_fields", []):
                connection.execute('''UPDATE alerts SET notified = 1, slack_sent = 1
                                      WHERE source = ? AND origin_domain = ? AND tx_hash = ? AND address_key = ?''', row[:4])
        connection.execute('''CREATE TABLE IF NOT EXISTS delivery_state (
            sink TEXT PRIMARY KEY, retry_at REAL NOT NULL, failures INTEGER NOT NULL, last_error TEXT
        )''')
        now = datetime.now(timezone.utc).isoformat()
        new_alerts = []
        for match in matches:
            key = (CHAINS[match["origin_domain_id"]][0], bytes.fromhex(match["address_key"]))
            payload = dict(match, detected_at=now, source=source, risk=(details or {}).get(key))
            inserted = connection.execute(
                "INSERT OR IGNORE INTO alerts (source, origin_domain, tx_hash, address_key, payload, slack_sent) VALUES (?, ?, ?, ?, ?, ?)",
                (source, match["origin_domain_id"], match["origin_tx_hash"], match["address_key"], json.dumps(payload),
                 0 if slack_webhook_url else 1))
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
        if pending:
            alerts = [json.loads(row[4]) for row in pending]
            text = alert_text(alerts)
            (state_dir / "latest-alert.txt").write_text(TITLE + "\n\n" + text + "\n", encoding="utf-8")
            print_banner(TITLE, text)
            popup_text = alert_text(alerts[:3])
            if len(alerts) > 3:
                popup_text += f"\n\n{len(alerts) - 3} more matches. See {state_dir / 'latest-alert.txt'}"
            if show_local_alert(TITLE, popup_text):
                connection.executemany(
                    "UPDATE alerts SET notified = 1 WHERE source = ? AND origin_domain = ? AND tx_hash = ? AND address_key = ?",
                    [row[:4] for row in pending])
        dispatch_slack(connection, slack_webhook_url)
        return len(pending)
