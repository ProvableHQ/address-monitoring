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
from urllib.error import HTTPError, URLError

import sender_alerts as alerts
import slack_alerts as slack

ROOT = Path(__file__).resolve().parents[1]
MESSAGE = json.loads((ROOT / "tests/fixtures/aleo_latest_100.json").read_text())["data"]["message_view"][0]
ADDRESS = "0x5227f603C1eE34CE3C06eFe6477003BDfCCB2f8c"
WEBHOOK = "https://hooks.slack.com/services/TEST/TEST/private-test-token"


class SlackClient(unittest.TestCase):
    def opener(self, body=b"ok", status=200):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = status
        response.read.return_value = body
        return opener

    def test_json_post_contains_alert_details_and_plain_text_blocks(self):
        opener = self.opener()
        with patch.object(slack, "build_opener", return_value=opener):
            slack.send_webhook(WEBHOOK, alerts.TITLE, ["Source: CSV watchlist\nSender: " + ADDRESS])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        payload = json.loads(request.data)
        self.assertIn(ADDRESS, payload["text"])
        self.assertIn("CSV watchlist", payload["blocks"][1]["text"]["text"])
        self.assertEqual(payload["blocks"][1]["text"]["type"], "plain_text")
        self.assertNotIn(WEBHOOK, request.data.decode())
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)

    def test_fallback_does_not_create_mentions(self):
        opener = self.opener()
        with patch.object(slack, "build_opener", return_value=opener):
            slack.send_webhook(WEBHOOK, "TEST", ["Label: <!channel> & <@USER>"])
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertNotIn("<!channel>", payload["text"])
        self.assertIn("&lt;!channel&gt;", payload["text"])

    def test_http_429_keeps_retry_after_without_leaking_webhook(self):
        opener = self.opener()
        headers = Message()
        headers["Retry-After"] = "90"
        opener.open.side_effect = HTTPError(WEBHOOK, 429, "private-test-token", headers, None)
        with patch.object(slack, "build_opener", return_value=opener):
            with self.assertRaises(slack.SlackDeliveryError) as caught:
                slack.send_webhook(WEBHOOK, "TEST", ["TEST"])
        self.assertEqual(caught.exception.retry_after, 90)
        self.assertNotIn("private-test-token", str(caught.exception))
        self.assertNotIn("hooks.slack.com", str(caught.exception))

    def test_network_error_does_not_leak_secret_url(self):
        opener = self.opener()
        opener.open.side_effect = URLError("failed " + WEBHOOK)
        with patch.object(slack, "build_opener", return_value=opener):
            with self.assertRaises(slack.SlackDeliveryError) as caught:
                slack.send_webhook(WEBHOOK, "TEST", ["TEST"])
        self.assertNotIn(WEBHOOK, str(caught.exception))

    def test_non_ok_response_is_not_marked_successful(self):
        for body, status in ((b"invalid_payload", 200), (b"ok", 202)):
            with self.subTest(body=body, status=status):
                with patch.object(slack, "build_opener", return_value=self.opener(body, status)):
                    with self.assertRaises(slack.SlackDeliveryError):
                        slack.send_webhook(WEBHOOK, "TEST", ["TEST"])

    def test_redirect_is_not_followed(self):
        with self.assertRaises(slack.SlackDeliveryError):
            slack.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example")

    def test_optional_dotenv_and_environment_configuration(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env_file = Path(directory) / ".env"
            self.assertIsNone(slack.load_webhook_url(env_file))
            env_file.write_text(f'SLACK_WEBHOOK_URL="{WEBHOOK}"\n')
            self.assertEqual(slack.load_webhook_url(env_file), WEBHOOK)
            with patch.dict(os.environ, {"SLACK_WEBHOOK_URL": ""}):
                self.assertIsNone(slack.load_webhook_url(env_file))

    def test_invalid_config_error_does_not_echo_secret(self):
        for value in (WEBHOOK.replace("https:", "http:"), "https://evil.example/SECRET",
                      WEBHOOK + "?secret=yes", WEBHOOK + "#SECRET", "https://user:SECRET@hooks.slack.com/services/A/B/C"):
            with self.subTest(value=value), patch.dict(os.environ, {"SLACK_WEBHOOK_URL": value}):
                with self.assertRaises(ValueError) as caught:
                    slack.load_webhook_url()
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertNotIn(value, str(caught.exception))

    def test_retry_after_invalid_values_and_http_date(self):
        self.assertEqual(slack.retry_seconds("90"), 90)
        for value in (None, "garbage", "nan", "inf", "-10"):
            self.assertEqual(slack.retry_seconds(value), 0)
        with patch.object(slack.time, "time", return_value=0):
            self.assertEqual(slack.retry_seconds("Thu, 01 Jan 1970 00:01:00 GMT"), 60)


class SlackOutbox(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.message = dict(MESSAGE, origin_tx_sender=ADDRESS)
        self.watchlist = {alerts.watch_address(ADDRESS): ADDRESS}
        self.errors = io.StringIO()
        redirect = redirect_stderr(self.errors)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        popup = patch.object(alerts, "show_local_alert", return_value=True)
        send = patch.object(alerts, "send_webhook")
        self.popup, self.send = popup.start(), send.start()
        self.addCleanup(popup.stop)
        self.addCleanup(send.stop)

    def check(self, messages=None, webhook=WEBHOOK, **kwargs):
        return alerts.check_senders([self.message] if messages is None else messages,
                                   self.watchlist, self.state, slack_webhook_url=webhook, **kwargs)

    def rows(self):
        with closing(sqlite3.connect(self.state / "sender-alerts.sqlite3")) as connection:
            return connection.execute("SELECT notified, slack_sent FROM alerts ORDER BY rowid").fetchall()

    def test_success_sends_once_across_polls_and_restarts(self):
        self.check()
        self.check()
        alerts.retry_pending_alerts(self.state, WEBHOOK)
        self.send.assert_called_once()
        self.popup.assert_called_once()
        self.assertEqual(self.rows(), [(1, 1)])

    def test_slack_failure_does_not_repeat_successful_local_popup(self):
        self.send.side_effect = slack.SlackDeliveryError("Slack delivery failed (HTTP 429)", retry_after=90)
        with patch.object(alerts.time, "time", return_value=1000):
            self.check()
        self.assertEqual(self.rows(), [(1, 0)])
        with patch.object(alerts.time, "time", return_value=1050):
            alerts.retry_pending_alerts(self.state, WEBHOOK)
        self.assertEqual(self.send.call_count, 1)
        self.send.side_effect = None
        with patch.object(alerts.time, "time", return_value=1092):
            alerts.retry_pending_alerts(self.state, WEBHOOK)
        self.assertEqual(self.rows(), [(1, 1)])
        self.assertEqual(self.send.call_count, 2)
        self.popup.assert_called_once()

    def test_local_failure_does_not_repeat_successful_slack_post(self):
        self.popup.side_effect = [False, True]
        self.check()
        self.check()
        self.send.assert_called_once()
        self.assertEqual(self.popup.call_count, 2)

    def test_existing_alerts_are_not_replayed_when_webhook_is_added(self):
        self.check(webhook=None)
        self.check()
        self.send.assert_not_called()

    def test_csv_and_merkle_alerts_are_both_forwarded_with_risk(self):
        with patch.object(alerts.time, "time", return_value=1000):
            self.check()
        risk = {alerts.watch_address(ADDRESS): {"risk_level": 0, "risk_level_verbose": "No Risk Detected"}}
        with patch.object(alerts.time, "time", return_value=1002):
            self.check(source="merkle_science", details=risk)
        self.assertEqual(self.send.call_count, 2)
        section = self.send.call_args.args[2][0]
        self.assertIn("Merkle Science address list", section)
        self.assertIn("No Risk Detected (0)", section)

    def test_batches_and_rate_limits_without_dropping_alerts(self):
        messages = [dict(self.message, origin_tx_hash="0x" + f"{i:064x}") for i in range(6)]
        with patch.object(alerts.time, "time", return_value=1000):
            self.check(messages)
            alerts.retry_pending_alerts(self.state, WEBHOOK)
        self.send.assert_called_once()
        self.assertEqual(len(self.send.call_args.args[2]), 5)
        self.assertEqual(sum(row[1] == 0 for row in self.rows()), 1)
        with patch.object(alerts.time, "time", return_value=1002):
            alerts.retry_pending_alerts(self.state, WEBHOOK)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(sum(row[1] == 0 for row in self.rows()), 0)

    def test_webhook_secret_is_not_saved_in_state_or_logs(self):
        self.check()
        for path in self.state.iterdir():
            self.assertNotIn(WEBHOOK.encode(), path.read_bytes())
            self.assertNotIn(b"private-test-token", path.read_bytes())
        self.assertNotIn("private-test-token", self.errors.getvalue())

    def test_upgrade_preserves_old_delivery_and_does_not_backfill_slack(self):
        match = alerts.sender_matches([self.message], self.watchlist)[0]
        with closing(sqlite3.connect(self.state / "sender-alerts.sqlite3")) as connection, connection:
            connection.execute('''CREATE TABLE alerts (source TEXT, origin_domain INTEGER, tx_hash TEXT,
                address_key TEXT, payload TEXT, notified INTEGER, PRIMARY KEY(source, origin_domain, tx_hash, address_key))''')
            connection.execute("INSERT INTO alerts VALUES ('csv', ?, ?, ?, ?, 1)",
                               (1, match["origin_tx_hash"], match["address_key"], json.dumps(match)))
        self.check()
        self.send.assert_not_called()
        self.popup.assert_not_called()
        self.assertEqual(self.rows(), [(1, 1)])


if __name__ == "__main__":
    unittest.main()
