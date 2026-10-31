import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import ANY, patch

import sender_alerts as alerts

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("monitor_for_alert_tests", ROOT / "address-monitoring.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
SAMPLE = json.loads((ROOT / "tests/fixtures/aleo_latest_100.json").read_text())["data"]
EVM = "0x5227f603C1eE34CE3C06eFe6477003BDfCCB2f8c"
SOLANA = "AffzFDKZbyMiU11zmVBoAqD25ugpr7xoWWyRyGigBEaU"


class SenderMatching(unittest.TestCase):
    def setUp(self):
        self.watchlist = alerts.load_watchlist(alerts.DEFAULT_WATCHLIST)
        self.message = dict(SAMPLE["message_view"][0], origin_tx_sender=EVM.lower())

    def test_filtered_csv_loads_all_fourteen_addresses(self):
        self.assertEqual(len(self.watchlist), 14)
        self.assertIn(alerts.watch_address(EVM), self.watchlist)
        self.assertIn(alerts.watch_address(SOLANA), self.watchlist)

    def test_evm_case_prefix_and_zero_padding(self):
        for value in (EVM, EVM.upper(), EVM[2:].lower(), "\\x" + EVM[2:].lower(),
                      "0x" + "00" * 12 + EVM[2:]):
            with self.subTest(value=value):
                matches = alerts.sender_matches([dict(self.message, origin_tx_sender=value)], self.watchlist)
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]["matched_fields"], ["origin_tx_sender"])
                self.assertEqual(matches[0]["address"], EVM)

    def test_same_evm_account_on_hyperevm(self):
        matches = alerts.sender_matches([dict(self.message, origin_domain_id=999)], self.watchlist)
        self.assertEqual(matches[0]["origin_chain"], "HyperEVM")

    def test_solana_wire_bytes_match_case_sensitive_base58(self):
        sol_bytes = alerts.watch_address(SOLANA)[1]
        message = dict(self.message, origin_domain_id=1399811149,
                       origin_tx_sender="\\x" + sol_bytes.hex())
        matches = alerts.sender_matches([message], self.watchlist)
        self.assertEqual(matches[0]["address"], SOLANA)
        different_case = SOLANA[0] + SOLANA[1].upper() + SOLANA[2:]
        message["origin_tx_sender"] = "\\x" + alerts.watch_address(different_case)[1].hex()
        self.assertEqual(alerts.sender_matches([message], self.watchlist), [])

    def test_router_only_match_does_not_trigger(self):
        message = dict(self.message, origin_tx_sender="0x" + "11" * 20, sender=EVM)
        matches = alerts.sender_matches([message], self.watchlist)
        self.assertEqual(matches, [])

    def test_both_sender_fields_and_multiple_messages_in_one_tx_coalesce(self):
        message = dict(self.message, sender=EVM)
        second = dict(message, msg_id="0x" + "ab" * 32)
        matches = alerts.sender_matches([message, second], self.watchlist)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["matched_fields"], ["origin_tx_sender"])
        self.assertEqual(len(matches[0]["transfers"]), 2)

    def test_recipient_match_does_not_trigger(self):
        message = dict(self.message, origin_tx_sender="0x" + "11" * 20,
                       recipient="0x" + "00" * 12 + EVM[2:])
        self.assertEqual(alerts.sender_matches([message], self.watchlist), [])

    def test_wrong_chain_destination_or_invalid_sender_does_not_trigger(self):
        changes = [dict(destination_domain_id=1), dict(origin_domain_id=8453),
                   dict(origin_tx_sender="0x" + "ff" * 12 + EVM[2:]),
                   dict(origin_tx_sender="0xinvalid"), dict(origin_tx_sender=None)]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(alerts.sender_matches([dict(self.message, **change)], self.watchlist), [])

    def test_matching_does_not_depend_on_body_decode(self):
        message = dict(self.message, message_body="invalid body", sender="invalid router")
        self.assertEqual(len(alerts.sender_matches([message], self.watchlist)), 1)

    def test_no_mutation_of_messages(self):
        original = copy.deepcopy(self.message)
        alerts.sender_matches([self.message], self.watchlist)
        self.assertEqual(self.message, original)

    def test_custom_csv_header_and_invalid_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.csv"
            path.write_text("address\n" + EVM + "\n")
            self.assertEqual(len(alerts.load_watchlist(path)), 1)
            for content in ("", "address\n", "address\n0x1234\n", "garbage!\n"):
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    alerts.load_watchlist(path)


class AlertPersistence(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.watchlist = alerts.load_watchlist(alerts.DEFAULT_WATCHLIST)
        self.message = dict(SAMPLE["message_view"][0], origin_tx_sender=EVM)
        self.stderr = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.stderr)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def check(self, messages=None):
        return alerts.check_senders([self.message] if messages is None else messages,
                                   self.watchlist, self.state)

    def test_alert_logged_and_not_repeated_across_polls_or_restart(self):
        with patch.object(alerts, "show_local_alert", return_value=True) as popup:
            self.assertEqual(self.check(), 1)
            self.assertEqual(self.check(), 0)
            # Each check opens a fresh SQLite connection, as each process does.
            self.assertEqual(self.check(), 0)
        popup.assert_called_once()
        saved = json.loads((self.state / "alerts.jsonl").read_text())
        self.assertEqual(saved["address"], EVM)
        self.assertEqual(saved["matched_fields"], ["origin_tx_sender"])
        self.assertIn(EVM.lower(), (self.state / "latest-alert.txt").read_text())
        self.assertIn(alerts.TITLE, self.stderr.getvalue())

    def test_new_transaction_for_same_address_alerts_again(self):
        with patch.object(alerts, "show_local_alert", return_value=True) as popup:
            self.check()
            self.assertEqual(self.check([dict(self.message, origin_tx_hash="0x" + "cc" * 32)]), 1)
        self.assertEqual(popup.call_count, 2)

    def test_transaction_on_different_origin_has_separate_identity(self):
        with patch.object(alerts, "show_local_alert", return_value=True) as popup:
            self.check()
            self.check([dict(self.message, origin_domain_id=999)])
        self.assertEqual(popup.call_count, 2)

    def test_failed_desktop_launch_retries_without_duplicate_log_records(self):
        with patch.object(alerts, "show_local_alert", side_effect=[False, True, True]) as popup:
            self.assertEqual(self.check(), 1)
            # Retry persists even if the matching message falls off the page.
            self.assertEqual(self.check([]), 1)
            self.assertEqual(self.check(), 0)
        self.assertEqual(popup.call_count, 2)
        self.assertEqual(len((self.state / "alerts.jsonl").read_text().splitlines()), 1)

    def test_no_matches_create_no_alert_state(self):
        with patch.object(alerts, "show_local_alert") as popup:
            self.assertEqual(self.check([]), 0)
        popup.assert_not_called()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_batch_uses_one_popup_and_keeps_full_details(self):
        messages = [dict(self.message, origin_tx_hash="0x" + f"{i:064x}") for i in range(5)]
        with patch.object(alerts, "show_local_alert", return_value=True) as popup:
            self.assertEqual(self.check(messages), 5)
        popup.assert_called_once()
        self.assertIn("2 more matches", popup.call_args.args[1])
        self.assertEqual(len((self.state / "alerts.jsonl").read_text().splitlines()), 5)


class DesktopAndCLI(unittest.TestCase):
    def setUp(self):
        webhook = patch.object(monitor, "load_webhook_url", return_value=None)
        pending = patch.object(monitor, "retry_pending_alerts")
        webhook.start()
        pending.start()
        self.addCleanup(webhook.stop)
        self.addCleanup(pending.stop)

    def test_macos_arguments_are_data_and_popup_does_not_block_polling(self):
        with patch.object(alerts.sys, "platform", "darwin"), \
             patch.object(alerts.subprocess, "Popen") as popen:
            popen.return_value.wait.side_effect = subprocess.TimeoutExpired("osascript", 0.25)
            self.assertTrue(alerts.show_local_alert("TEST", 'Text with "quotes" and $shell'))
        args = popen.call_args.args[0]
        self.assertEqual(args[-2:], ["TEST", 'Text with "quotes" and $shell'])
        self.assertEqual(args[0], "/usr/bin/osascript")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_native_launch_failure_is_detected(self):
        with patch.object(alerts.sys, "platform", "darwin"), \
             patch.object(alerts.subprocess, "Popen") as popen, \
             contextlib.redirect_stderr(io.StringIO()):
            popen.return_value.wait.return_value = 1
            self.assertFalse(alerts.show_local_alert("TEST", "test"))

    def test_missing_desktop_support_is_reported(self):
        errors = io.StringIO()
        with patch.object(alerts.sys, "platform", "linux"), \
             patch.object(alerts.shutil, "which", return_value=None), \
             contextlib.redirect_stderr(errors):
            self.assertFalse(alerts.show_local_alert("TEST", "test"))
        self.assertIn("unavailable", errors.getvalue())

    def test_cli_still_checks_signers_when_registry_is_unavailable(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["address-monitoring.py", "--no-merkle"]), \
             patch.object(monitor, "fetch_messages", return_value=SAMPLE), \
             patch.object(monitor, "check_senders") as check, \
             patch.object(monitor, "cached_routes", side_effect=OSError("registry unavailable")), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(monitor.main(), 1)
        check.assert_called_once()
        checked = check.call_args.args[0]
        self.assertEqual([m["msg_id"] for m in checked], [m["msg_id"] for m in SAMPLE["message_view"]])
        self.assertTrue(all(m["amount"] is None for m in checked))

    def test_cli_alert_preserves_json_and_deduplicates_across_runs(self):
        data = copy.deepcopy(SAMPLE)
        data["message_view"][0]["origin_tx_sender"] = EVM
        output, errors = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
             patch("sys.argv", ["address-monitoring.py", "--no-merkle", "--alert-state-dir", directory]), \
             patch.object(monitor, "fetch_messages", return_value=data), \
             patch.object(monitor, "cached_routes", return_value=[]), \
             patch.object(alerts, "show_local_alert", return_value=True) as popup, \
             contextlib.redirect_stderr(errors):
            for _ in range(2):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(monitor.main(), 0)
                self.assertEqual(len(json.loads(output.getvalue())), 100)
            popup.assert_called_once()
            self.assertEqual(len((Path(directory) / "alerts.jsonl").read_text().splitlines()), 1)
        self.assertIn(EVM.lower(), errors.getvalue())

    def test_test_alert_does_not_query_network_or_record_fake_matches(self):
        errors = io.StringIO()
        with patch("sys.argv", ["address-monitoring.py", "--test-alert"]), \
             patch.object(monitor, "http") as http, \
             patch.object(monitor, "check_senders") as check, \
             patch.object(monitor, "show_local_alert", return_value=True) as popup, \
             contextlib.redirect_stderr(errors):
            self.assertEqual(monitor.main(), 0)
        http.assert_not_called()
        check.assert_not_called()
        self.assertEqual(popup.call_args.args[0], alerts.TITLE + " (TEST)")

    def test_test_slack_only_sends_labeled_message_without_api_queries(self):
        errors = io.StringIO()
        with patch("sys.argv", ["address-monitoring.py", "--test-slack"]), \
             patch.object(monitor, "load_webhook_url", return_value="https://hooks.slack.com/services/TEST/TEST/TEST"), \
             patch.object(monitor, "send_webhook") as send, \
             patch.object(monitor, "http") as http, \
             patch.object(monitor, "check_senders") as check, \
             contextlib.redirect_stderr(errors):
            self.assertEqual(monitor.main(), 0)
        http.assert_not_called()
        check.assert_not_called()
        self.assertEqual(send.call_args.args[1], alerts.TITLE + " (TEST)")

    def test_invalid_watchlist_fails_before_network_request(self):
        errors = io.StringIO()
        with patch("sys.argv", ["address-monitoring.py", "--watchlist", "/nonexistent/watch.csv"]), \
             patch.object(monitor, "http") as http, contextlib.redirect_stderr(errors):
            self.assertEqual(monitor.main(), 1)
        http.assert_not_called()

    def test_no_alerts_explicitly_disables_watchlist(self):
        output = io.StringIO()
        with patch("sys.argv", ["address-monitoring.py", "--no-alerts"]), \
             patch.object(monitor, "fetch_messages", return_value={"message_view": [], "domain": []}), \
             patch.object(monitor, "cached_routes", return_value=[]), \
             patch.object(monitor, "load_watchlist") as load, \
             patch.object(monitor, "check_senders") as check, contextlib.redirect_stdout(output):
            self.assertEqual(monitor.main(), 0)
        load.assert_not_called()
        check.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), [])

    def test_default_cli_screens_new_transactions_with_same_alert_state(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
             patch("sys.argv", ["address-monitoring.py", "--alert-state-dir", directory]), \
             patch.object(monitor, "load_api_key", return_value="test-key"), \
             patch.object(monitor, "fetch_messages", return_value=SAMPLE) as fetch, \
             patch.object(monitor, "cached_routes", return_value=[]), \
             patch.object(monitor, "check_senders"), \
             patch.object(monitor, "screen_new_transactions") as screen, \
             contextlib.redirect_stdout(output):
            self.assertEqual(monitor.main(), 0)
        screen.assert_called_once_with(fetch, "test-key", Path(directory),
                                       observed_messages=SAMPLE["message_view"], slack_webhook_url=None,
                                       enrich_message=ANY)

    def test_merkle_failure_is_not_reported_as_successful_clean_scan(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["address-monitoring.py"]), \
             patch.object(monitor, "load_api_key", return_value="test-key"), \
             patch.object(monitor, "fetch_messages", return_value=SAMPLE), \
             patch.object(monitor, "check_senders"), \
             patch.object(monitor, "screen_new_transactions", side_effect=ValueError("Merkle Science HTTP 429")), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(monitor.main(), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("429", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
