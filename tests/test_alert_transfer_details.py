from contextlib import closing, redirect_stderr
import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import yaml
import merkle_screening
import sender_alerts as alerts

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
spec = importlib.util.spec_from_file_location("monitor_for_transfer_details", ROOT / "address-monitoring.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
DATA = json.loads((FIXTURES / "aleo_latest_100.json").read_text())["data"]
EXPECTED = json.loads((FIXTURES / "aleo_latest_100_expected.json").read_text())
CONFIGS = [yaml.safe_load(path.read_text()) for path in (FIXTURES / "registry").rglob("*.yaml")]
ROUTES = monitor.route_index(CONFIGS, DATA["domain"])
DOMAINS = {d["id"]: d["name"] for d in DATA["domain"]}


def enrich(message):
    return monitor.enrich_message(message, ROUTES, DOMAINS)


def watchlist_for(message):
    protocol = alerts.CHAINS[message["origin_domain_id"]][0]
    raw = alerts.hex_bytes(message["origin_tx_sender"])
    if protocol == "evm":
        raw = raw[-20:]
    return {(protocol, raw): alerts.sender_address(protocol, raw)}


class AlertTransferDetails(unittest.TestCase):
    def test_all_100_messages_show_independent_asset_amount_and_recipient_expectations(self):
        for message, expected in zip(DATA["message_view"], EXPECTED):
            with self.subTest(msg_id=message["msg_id"]):
                match = alerts.sender_matches([enrich(message)], watchlist_for(message))[0]
                text = alerts.alert_text([match])
                self.assertIn("Asset: " + expected["token"], text)
                self.assertIn("Amount: " + expected["amount"] + " " + expected["token"], text)
                self.assertIn("Aleo recipient: " + expected["destination_address"], text)
                self.assertIn("Sender: " + match["origin_tx_sender"], text)
                self.assertNotIn("0x" + alerts.hex_bytes(message["sender"]).hex(), text)
                self.assertNotIn("0x" + alerts.hex_bytes(message["recipient"]).hex(), text)
                self.assertNotIn("Matched field", text)
                self.assertNotIn(".aleo/", text)

    def test_demo_signer_is_exactly_transaction_sender(self):
        match = alerts.sender_matches([enrich(DATA["message_view"][0])], watchlist_for(DATA["message_view"][0]))[0]
        self.assertEqual(match["origin_tx_sender"], "0xa9fcc50fde98ebe533f9e362c4870115c63aee42")
        self.assertIn("Amount: 0.001 ETH", alerts.alert_text([match]))

    def test_multiple_transfers_preserve_each_asset_amount_and_aleo_recipient(self):
        first, second = copy.deepcopy(DATA["message_view"][:2])
        # A batched origin transaction can dispatch more than one bridge message.
        second.update(origin_domain_id=first["origin_domain_id"], origin_tx_hash=first["origin_tx_hash"],
                      origin_tx_sender=first["origin_tx_sender"])
        first = dict(enrich(first), token="ETH", amount="0.001")
        second = dict(second, token="USDT", amount="12.34", destination_address=EXPECTED[2]["destination_address"])
        matches = alerts.sender_matches([first, second], watchlist_for(first))
        self.assertEqual(len(matches), 1)
        text = alerts.alert_text(matches)
        self.assertIn("Amount: 0.001 ETH", text)
        self.assertIn("Amount: 12.34 USDT", text)
        self.assertIn("Aleo recipient: " + EXPECTED[2]["destination_address"], text)

    def test_unknown_transfer_details_are_explicit_and_never_use_mailbox_recipient(self):
        raw = DATA["message_view"][0]
        match = alerts.sender_matches([raw], watchlist_for(raw))[0]
        text = alerts.alert_text([match])
        self.assertIn("Asset: Unavailable", text)
        self.assertIn("Amount: Unavailable", text)
        self.assertIn("Aleo recipient: Unavailable", text)
        self.assertNotIn(alerts.hex_bytes(raw["recipient"]).hex(), text)

    def test_zero_amount_is_displayed(self):
        message = dict(enrich(DATA["message_view"][0]), amount="0")
        self.assertIn("Amount: 0 ETH", alerts.alert_text(alerts.sender_matches([message], watchlist_for(message))))

    def test_popup_slack_and_saved_event_have_the_same_enriched_details(self):
        message = enrich(DATA["message_view"][0])
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), \
             patch.object(alerts, "show_local_alert", return_value=True) as popup, \
             patch.object(alerts, "send_webhook") as slack:
            alerts.check_senders([message], watchlist_for(message), directory,
                                 slack_webhook_url="https://hooks.slack.com/services/TEST/TEST/TEST")
            popup_text = popup.call_args.args[1]
            self.assertEqual(slack.call_args.args[2][0], popup_text)
            event = json.loads((Path(directory) / "alerts.jsonl").read_text())
            self.assertEqual(event["transfers"][0]["amount"], "0.001")
            self.assertEqual(event["transfers"][0]["asset"], "ETH")
            self.assertEqual(event["transfers"][0]["aleo_recipient"], EXPECTED[0]["destination_address"])

    def test_old_router_only_pending_alert_is_not_forwarded(self):
        raw = DATA["message_view"][0]
        match = alerts.sender_matches([raw], watchlist_for(raw))[0]
        match["matched_fields"] = ["sender"]
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), \
             patch.object(alerts, "show_local_alert") as popup, patch.object(alerts, "send_webhook") as slack:
            with closing(sqlite3.connect(Path(directory) / "sender-alerts.sqlite3")) as connection, connection:
                connection.execute('''CREATE TABLE alerts (source TEXT, origin_domain INTEGER, tx_hash TEXT,
                    address_key TEXT, payload TEXT, notified INTEGER, slack_sent INTEGER,
                    PRIMARY KEY(source, origin_domain, tx_hash, address_key))''')
                connection.execute("INSERT INTO alerts VALUES ('csv', 1, ?, ?, ?, 0, 0)",
                                   (match["origin_tx_hash"], match["address_key"], json.dumps(match)))
            alerts.retry_pending_alerts(directory, "https://hooks.slack.com/services/TEST/TEST/TEST")
            popup.assert_not_called()
            slack.assert_not_called()

    def test_merkle_screening_enriches_queued_transactions_before_alerting(self):
        raw = dict(DATA["message_view"][0], id="11")
        records = [{"identifier": "0xa9fcc50fde98ebe533f9e362c4870115c63aee42",
                    "blockchain_verbose": "Ethereum", "risk_level": 0}]
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), \
             patch.object(merkle_screening, "fetch_addresses", return_value=records), \
             patch.object(alerts, "show_local_alert", return_value=True) as popup:
            merkle_screening.screen_new_transactions(lambda **kwargs: {"message_view": []}, "test", directory)
            merkle_screening.screen_new_transactions(lambda **kwargs: {"message_view": [raw]}, "test", directory,
                                                      enrich_message=enrich)
            self.assertIn("Amount: 0.001 ETH", popup.call_args.args[1])
            self.assertIn("Aleo recipient: " + EXPECTED[0]["destination_address"], popup.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
