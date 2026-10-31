from contextlib import closing, redirect_stderr
from email.message import Message
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import merkle_screening as merkle
import sender_alerts

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = json.loads((ROOT / "tests/fixtures/merkle_addresses.json").read_text())
MESSAGE = json.loads((ROOT / "tests/fixtures/aleo_latest_100.json").read_text())["data"]["message_view"][0]
EVM = "0x93d8edbc42e547c571ce5af95f70c291d706925c"


def message(number, **changes):
    row = dict(MESSAGE, id=str(number), origin_tx_hash="0x" + f"{number:064x}",
               msg_id="0x" + f"{number:064x}", origin_tx_sender=EVM)
    return dict(row, **changes)


class MerkleClient(unittest.TestCase):
    def opener_for(self, pages):
        opener = MagicMock()
        responses = []
        for page in pages:
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(page).encode()
            responses.append(response)
        opener.open.side_effect = responses
        return opener

    def test_sample_filters_eth_sol_and_excludes_btc_and_other_chains(self):
        watchlist, details = merkle.filtered_watchlist(SAMPLE["results"])
        self.assertEqual(len(watchlist), 3)
        self.assertEqual(sum(key[0] == "evm" for key in watchlist), 2)
        self.assertEqual(sum(key[0] == "solana" for key in watchlist), 1)
        self.assertTrue(all(row["risk_level"] == 0 for row in details.values()))

    def test_hyperevm_label_and_numeric_ids(self):
        records = [{"identifier": EVM.upper(), "blockchain": 900, "blockchain_verbose": "HyperEVM"},
                   {"identifier": "0x" + "22" * 20, "blockchain": 1}]
        watchlist, _ = merkle.filtered_watchlist(records)
        self.assertEqual(len(watchlist), 2)

    def test_chain_label_takes_precedence_over_conflicting_numeric_id(self):
        watchlist, _ = merkle.filtered_watchlist([
            {"identifier": EVM, "blockchain": 1, "blockchain_verbose": "Bitcoin"}])
        self.assertEqual(watchlist, {})

    def test_invalid_supported_address_does_not_become_a_clean_result(self):
        for records in ([None], [{"identifier": "bad!", "blockchain_verbose": "Ethereum"}],
                        [{"identifier": EVM, "blockchain_verbose": "Solana"}]):
            with self.subTest(records=records), self.assertRaises(merkle.MerkleAPIError):
                merkle.filtered_watchlist(records)

    def test_paginated_get_uses_header_key_and_fetches_every_page(self):
        first = dict(SAMPLE, results=SAMPLE["results"][:5], next="?offset=5")
        second = dict(SAMPLE, results=SAMPLE["results"][5:])
        opener = self.opener_for([first, second])
        with patch.object(merkle, "build_opener", return_value=opener):
            self.assertEqual(merkle.fetch_addresses("test-key"), SAMPLE["results"])
        self.assertEqual(opener.open.call_count, 2)
        for call in opener.open.call_args_list:
            request = call.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("X-api-key"), "test-key")
            self.assertNotIn("test-key", request.full_url)
            self.assertEqual(call.kwargs["timeout"], 30)

    def test_empty_complete_list_is_valid(self):
        opener = self.opener_for([{"count": 0, "next": None, "results": []}])
        with patch.object(merkle, "build_opener", return_value=opener):
            self.assertEqual(merkle.fetch_addresses("test-key"), [])

    def test_malformed_incomplete_or_looping_pagination_fails(self):
        pages = [dict(SAMPLE, count=12), dict(SAMPLE, next=merkle.API),
                 {"results": []}, dict(SAMPLE, next=123), dict(SAMPLE, count=True)]
        for page in pages:
            with self.subTest(page=page):
                with patch.object(merkle, "build_opener", return_value=self.opener_for([page])):
                    with self.assertRaises(merkle.MerkleAPIError):
                        merkle.fetch_addresses("test-key")

    def test_untrusted_next_url_or_redirect_cannot_receive_api_key(self):
        for url in ("https://evil.example/api/v4.2/addresses/", "http://api.merklescience.com/api/v4.2/addresses/",
                    "https://api.merklescience.com/other", "https://api.merklescience.com:443/api/v4.2/addresses/"):
            opener = self.opener_for([dict(SAMPLE, next=url)])
            with self.subTest(url=url), patch.object(merkle, "build_opener", return_value=opener):
                with self.assertRaises(merkle.MerkleAPIError):
                    merkle.fetch_addresses("test-key")
                self.assertEqual(opener.open.call_count, 1)
                with self.assertRaises(merkle.MerkleAPIError):
                    merkle.SafeRedirect().redirect_request(None, None, 302, "", {}, url)

    def test_rate_limit_retry_after_and_no_credentials_in_error(self):
        opener = MagicMock()
        headers = Message()
        headers["Retry-After"] = "120"
        opener.open.side_effect = HTTPError(merkle.API, 429, "sensitive text test-key", headers, None)
        with patch.object(merkle, "build_opener", return_value=opener):
            with self.assertRaises(merkle.MerkleAPIError) as caught:
                merkle.fetch_addresses("test-key")
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertNotIn("test-key", str(caught.exception))
        self.assertIn("429", str(caught.exception))

    def test_env_and_dotenv_keys_are_read_without_shell_expansion(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('MERKLE_SCIENCE_API_KEY="literal-$(not-executed)"\n')
            self.assertEqual(merkle.load_api_key(path), "literal-$(not-executed)")
            with patch.dict(os.environ, {merkle.KEY_NAME: "environment-value"}):
                self.assertEqual(merkle.load_api_key(path), "environment-value")
            path.write_text("MERKLE_SCIENCE_API_KEY=\n")
            with self.assertRaises(ValueError):
                merkle.load_api_key(path)


class NewTransactionScreening(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.errors = io.StringIO()
        redirect = redirect_stderr(self.errors)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.fetch_api = patch.object(merkle, "fetch_addresses", return_value=SAMPLE["results"]).start()
        self.popup = patch.object(sender_alerts, "show_local_alert", return_value=True).start()
        self.addCleanup(patch.stopall)

    def run_pages(self, pages):
        fetch_page = MagicMock(side_effect=[{"message_view": page} for page in pages])
        result = merkle.screen_new_transactions(fetch_page, "test-key", self.state)
        return result, fetch_page

    def baseline(self):
        return self.run_pages([[message(10)]])

    def transaction_rows(self):
        with closing(sqlite3.connect(self.state / "merkle-screening.sqlite3")) as connection:
            return connection.execute("SELECT status, address_count, match_count FROM transactions ORDER BY rowid").fetchall()

    def test_first_scan_baselines_history_without_merkle_requests(self):
        result, fetch = self.baseline()
        self.assertEqual(result, 0)
        self.fetch_api.assert_not_called()
        self.popup.assert_not_called()
        fetch.assert_called_once_with(limit=100, id_order="desc")
        self.assertEqual(self.transaction_rows()[0][0], "baseline")

    def test_no_new_transactions_means_no_merkle_requests(self):
        self.baseline()
        result, fetch = self.run_pages([[]])
        self.assertEqual(result, 0)
        self.fetch_api.assert_not_called()
        fetch.assert_called_once_with(limit=100, after_id=10, id_order="asc")

    def test_new_match_alerts_and_is_not_rescreened_after_restart(self):
        self.baseline()
        self.assertEqual(self.run_pages([[message(11)]])[0], 1)
        self.assertEqual(self.run_pages([[]])[0], 0)
        self.fetch_api.assert_called_once_with("test-key")
        self.popup.assert_called_once()
        event = json.loads((self.state / "alerts.jsonl").read_text())
        self.assertEqual(event["source"], "merkle_science")
        self.assertEqual(event["risk"]["risk_level"], 0)
        self.assertIn("No Risk Detected", self.popup.call_args.args[1])
        self.assertEqual(self.transaction_rows()[-1], ("screened", 3, 1))

    def test_each_distinct_transaction_gets_fresh_merkle_response(self):
        self.baseline()
        self.assertEqual(self.run_pages([[message(11), message(12)]])[0], 2)
        self.assertEqual(self.fetch_api.call_count, 2)

    def test_multiple_messages_in_one_transaction_use_one_lookup(self):
        self.baseline()
        second = message(12, origin_tx_hash=message(11)["origin_tx_hash"])
        self.run_pages([[message(11), second]])
        self.fetch_api.assert_called_once()
        self.popup.assert_called_once()
        self.run_pages([[message(13, origin_tx_hash=message(11)["origin_tx_hash"])]])
        self.fetch_api.assert_called_once()

    def test_solana_and_hyperevm_matches(self):
        self.baseline()
        solana = SAMPLE["results"][0]["identifier"]
        sol_bytes = sender_alerts.watch_address(solana)[1]
        self.run_pages([[message(11, origin_domain_id=1399811149, origin_tx_sender="0x" + sol_bytes.hex()),
                         message(12, origin_domain_id=999)]])
        self.assertEqual(self.fetch_api.call_count, 2)
        self.assertEqual(self.popup.call_count, 2)

    def test_no_match_is_recorded_once_without_alert(self):
        self.baseline()
        self.run_pages([[message(11, origin_tx_sender="0x" + "ff" * 20)]])
        self.fetch_api.assert_called_once()
        self.popup.assert_not_called()
        self.assertEqual(self.transaction_rows()[-1], ("screened", 3, 0))

    def test_catchup_paginates_beyond_latest_hundred(self):
        self.baseline()
        self.assertEqual(self.run_pages([[message(i) for i in range(11, 111)], [message(111)]])[0], 101)
        self.assertEqual(self.fetch_api.call_count, 101)

    def test_api_failure_keeps_pending_and_retry_after_is_respected(self):
        self.baseline()
        self.fetch_api.side_effect = merkle.MerkleAPIError("HTTP 429", retry_after=120)
        with patch.object(merkle.time, "time", return_value=1000):
            with self.assertRaises(merkle.MerkleAPIError):
                self.run_pages([[message(11), message(12)]])
        self.assertEqual(self.fetch_api.call_count, 1)
        self.assertEqual([r[0] for r in self.transaction_rows()], ["baseline", "pending", "pending"])
        with patch.object(merkle.time, "time", return_value=1050):
            with self.assertRaises(merkle.MerkleAPIError):
                self.run_pages([[]])
        self.assertEqual(self.fetch_api.call_count, 1)
        self.fetch_api.side_effect = None
        with patch.object(merkle.time, "time", return_value=1122):
            self.assertEqual(self.run_pages([[]])[0], 2)
        self.assertEqual(self.fetch_api.call_count, 3)

    def test_bad_hyperlane_page_does_not_advance_cursor(self):
        self.baseline()
        with self.assertRaises(ValueError):
            self.run_pages([[message(9)]])
        self.fetch_api.assert_not_called()
        _, fetch = self.run_pages([[]])
        fetch.assert_called_once_with(limit=100, after_id=10, id_order="asc")

    def test_wrong_destination_is_rejected(self):
        self.baseline()
        with self.assertRaises(ValueError):
            self.run_pages([[message(11, destination_domain_id=1)]])
        self.fetch_api.assert_not_called()

    def test_latest_page_overlap_catches_newly_observed_older_id(self):
        self.baseline()
        fetch = MagicMock(return_value={"message_view": []})
        self.assertEqual(merkle.screen_new_transactions(fetch, "test-key", self.state,
                                                       observed_messages=[message(9)]), 1)
        self.fetch_api.assert_called_once()

    def test_baseline_includes_both_id_and_send_time_pages(self):
        fetch = MagicMock(return_value={"message_view": [message(10)]})
        merkle.screen_new_transactions(fetch, "test-key", self.state, observed_messages=[message(9)])
        fetch = MagicMock(return_value={"message_view": []})
        merkle.screen_new_transactions(fetch, "test-key", self.state, observed_messages=[message(9)])
        self.fetch_api.assert_not_called()

    def test_empty_baseline_still_detects_first_transaction(self):
        self.run_pages([[]])
        self.assertEqual(self.run_pages([[message(1)]])[0], 1)
        self.fetch_api.assert_called_once()


class AlertSourceMigration(unittest.TestCase):
    def test_old_csv_dedup_survives_migration_and_merkle_is_separate_source(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            database = Path(directory) / "sender-alerts.sqlite3"
            match = sender_alerts.sender_matches([message(11)], {sender_alerts.watch_address(EVM): EVM})[0]
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute('''CREATE TABLE alerts (origin_domain INTEGER, tx_hash TEXT, address_key TEXT,
                    payload TEXT NOT NULL, notified INTEGER, PRIMARY KEY (origin_domain, tx_hash, address_key))''')
                connection.execute("INSERT INTO alerts VALUES (?, ?, ?, ?, 1)",
                                   (1, match["origin_tx_hash"], match["address_key"], json.dumps(match)))
            watchlist = {sender_alerts.watch_address(EVM): EVM}
            with patch.object(sender_alerts, "show_local_alert", return_value=True) as popup:
                self.assertEqual(sender_alerts.check_senders([message(11)], watchlist, directory), 0)
                self.assertEqual(sender_alerts.check_senders([message(11)], watchlist, directory,
                                                             source="merkle_science"), 1)
                self.assertEqual(sender_alerts.check_senders([message(11)], watchlist, directory,
                                                             source="merkle_science"), 0)
            popup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
