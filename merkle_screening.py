"""Screen newly indexed Aleo-bound transactions against Merkle Science lists."""

from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import random
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sender_alerts import ALEO_DOMAIN, check_senders, hex_bytes, sender_matches, watch_address
from monitor_config import ENV_FILE, secret_value

API = "https://api.merklescience.com/api/v4.2/addresses/"
KEY_NAME = "MERKLE_SCIENCE_API_KEY"


class MerkleAPIError(ValueError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def load_api_key(env_file=ENV_FILE):
    value = secret_value(KEY_NAME, env_file)
    if not value:
        raise ValueError(f"Set {KEY_NAME} in the environment or local .env, or use --no-merkle")
    return value.strip()


def validated_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "api.merklescience.com"
            or parsed.path != "/api/v4.2/addresses/" or parsed.fragment):
        raise MerkleAPIError("Refusing an unexpected Merkle Science pagination/redirect URL")
    return url


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        validated_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def retry_seconds(value):
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


def fetch_addresses(api_key):
    """Retrieve every page afresh for one new transaction; never log the key."""
    opener = build_opener(SafeRedirect())
    url, visited, records, expected_count = API, set(), [], None
    while url:
        validated_url(url)
        if url in visited:
            raise MerkleAPIError("Merkle Science pagination contains a loop")
        visited.add(url)
        request = Request(url, headers={"X-API-KEY": api_key, "Accept": "application/json",
                                       "Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=30) as response:
                page = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            delay = retry_seconds(error.headers.get("Retry-After")) if error.headers else 0
            status = error.code
            error.close()
            raise MerkleAPIError(f"Merkle Science HTTP {status}", delay) from None
        except (URLError, OSError, UnicodeError, ValueError):
            raise MerkleAPIError("Merkle Science network failure or invalid JSON response") from None
        if (not isinstance(page, dict) or not isinstance(page.get("results"), list)
                or type(page.get("count")) is not int or page["count"] < 0
                or "next" not in page):
            raise MerkleAPIError("Incomplete Merkle Science address-list response")
        if expected_count is None:
            expected_count = page["count"]
        if page["count"] != expected_count:
            raise MerkleAPIError("Merkle Science address count changed during pagination; retry required")
        records.extend(page["results"])
        next_page = page["next"]
        if next_page is not None and not isinstance(next_page, str):
            raise MerkleAPIError("Invalid Merkle Science next-page value")
        url = urljoin(API, next_page) if next_page else None
    if len(records) != expected_count:
        raise MerkleAPIError("Merkle Science pagination returned an incomplete address list")
    return records


def filtered_watchlist(records):
    watchlist, details = {}, {}
    for record in records:
        if not isinstance(record, dict):
            raise MerkleAPIError("Invalid Merkle Science address record")
        label = record.get("blockchain_verbose")
        label = "".join(str(label or "").lower().split()).replace("-", "")
        if label:
            protocol = {"eth": "evm", "ethereum": "evm", "hyperevm": "evm",
                        "sol": "solana", "solana": "solana"}.get(label)
        else:
            # IDs confirmed by the supplied response. HyperEVM uses its label;
            # its vendor-specific numeric ID must not be guessed.
            protocol = {1: "evm", 24: "solana"}.get(record.get("blockchain"))
        if protocol is None:
            continue
        identifier = record.get("identifier")
        try:
            if not isinstance(identifier, str):
                raise ValueError("Missing identifier")
            key = watch_address(identifier.strip())
        except ValueError:
            raise MerkleAPIError("Invalid supported-chain address in Merkle Science response") from None
        if key[0] != protocol:
            raise MerkleAPIError("Merkle Science chain and address format disagree")
        watchlist[key] = identifier.strip()
        details[key] = {"risk_level": record.get("risk_level"),
                        "risk_level_verbose": record.get("risk_level_verbose"),
                        "blockchain_verbose": record.get("blockchain_verbose")}
    return watchlist, details


def meta_get(connection, name, default=None):
    row = connection.execute("SELECT value FROM metadata WHERE name = ?", (name,)).fetchone()
    return json.loads(row[0]) if row else default


def meta_set(connection, name, value):
    connection.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (name, json.dumps(value)))


def register_message(connection, message, status="pending"):
    if message.get("destination_domain_id") != ALEO_DOMAIN:
        raise ValueError("Non-Aleo message in new-transaction feed")
    tx_hash = hex_bytes(message["origin_tx_hash"]).hex()
    if not tx_hash:
        raise ValueError("Missing origin transaction hash")
    domain = message["origin_domain_id"]
    row = connection.execute("SELECT messages, status FROM transactions WHERE domain = ? AND tx_hash = ?",
                             (domain, tx_hash)).fetchone()
    if row:
        # The transaction sender is common to its messages. Retain all newly
        # observed messages before the transaction's first screening request.
        if row[1] == "pending":
            messages = json.loads(row[0])
            if message["msg_id"] not in {m["msg_id"] for m in messages}:
                messages.append(message)
                connection.execute("UPDATE transactions SET messages = ? WHERE domain = ? AND tx_hash = ?",
                                   (json.dumps(messages), domain, tx_hash))
    else:
        connection.execute("INSERT INTO transactions (domain, tx_hash, messages, status) VALUES (?, ?, ?, ?)",
                           (domain, tx_hash, json.dumps([message]), status))


def screen_new_transactions(fetch_page, api_key, state_dir, observed_messages=(), slack_webhook_url=None,
                            enrich_message=None):
    """Baseline once, durably queue new IDs, then perform one list fetch per tx."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(state_dir / "merkle-screening.sqlite3", timeout=10)) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute('''CREATE TABLE IF NOT EXISTS transactions (
            domain INTEGER, tx_hash TEXT, messages TEXT NOT NULL, status TEXT NOT NULL,
            checked_at TEXT, address_count INTEGER, match_count INTEGER,
            PRIMARY KEY (domain, tx_hash)
        )''')
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        cursor = meta_get(connection, "cursor")
        if cursor is None:
            baseline = fetch_page(limit=100, id_order="desc")["message_view"]
            for message in baseline:
                register_message(connection, message, "baseline")
            for message in observed_messages:
                register_message(connection, message, "baseline")
            cursor = max((int(m["id"]) for m in baseline), default=0)
            meta_set(connection, "cursor", cursor)
            meta_set(connection, "baseline_at", datetime.now(timezone.utc).isoformat())
            print(f"Merkle Science baseline initialized at Hyperlane ID {cursor}; existing transactions skipped.",
                  file=sys.stderr)
            return 0
        # Reconcile the normal latest page as an overlap for delayed indexing
        # of rows whose IDs precede the cursor. Known transactions stay skipped.
        for message in observed_messages:
            register_message(connection, message)
        while True:
            messages = fetch_page(limit=100, after_id=cursor, id_order="asc")["message_view"]
            for message in messages:
                message_id = int(message["id"])
                if message_id <= cursor:
                    raise ValueError("Hyperlane new-message page did not advance its ID cursor")
                register_message(connection, message)
                cursor = message_id
            meta_set(connection, "cursor", cursor)
            if len(messages) < 100:
                break
        connection.commit()
        if time.time() < meta_get(connection, "retry_at", 0):
            raise MerkleAPIError("Merkle Science screening pending; waiting for retry backoff")
        screened = 0
        while True:
            # Holding this transaction serializes screening across local runs.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT domain, tx_hash, messages FROM transactions WHERE status = 'pending' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                connection.commit()
                break
            try:
                records = fetch_addresses(api_key)
                watchlist, details = filtered_watchlist(records)
                messages = json.loads(row[2])
                if enrich_message is not None:
                    messages = [enrich_message(message) for message in messages]
                count = len(sender_matches(messages, watchlist))
                check_senders(messages, watchlist, state_dir, source="merkle_science", details=details,
                              slack_webhook_url=slack_webhook_url)
            except MerkleAPIError as error:
                failures = meta_get(connection, "failures", 0) + 1
                delay = max(error.retry_after, min(300, 5 * 2 ** min(failures - 1, 6)) + random.uniform(0, 1))
                meta_set(connection, "failures", failures)
                meta_set(connection, "retry_at", time.time() + delay)
                meta_set(connection, "last_error", str(error))
                connection.commit()
                raise
            checked_at = datetime.now(timezone.utc).isoformat()
            connection.execute('''UPDATE transactions SET status = 'screened', checked_at = ?,
                                  address_count = ?, match_count = ? WHERE domain = ? AND tx_hash = ?''',
                               (checked_at, len(watchlist), count, row[0], row[1]))
            meta_set(connection, "failures", 0)
            meta_set(connection, "retry_at", 0)
            meta_set(connection, "last_error", None)
            meta_set(connection, "last_success_at", checked_at)
            connection.commit()
            screened += 1
            print(f"Merkle Science screened transaction {row[1]}: {len(watchlist)} eligible addresses, {count} alert(s).",
                  file=sys.stderr)
        return screened
