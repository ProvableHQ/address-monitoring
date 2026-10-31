import collections
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
spec = importlib.util.spec_from_file_location("monitor", ROOT / "address-monitoring.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
SNAPSHOT = json.loads((FIXTURES / "aleo_latest_100.json").read_text())
EXPECTED = json.loads((FIXTURES / "aleo_latest_100_expected.json").read_text())
DATA = SNAPSHOT["data"]
CONFIGS = [yaml.safe_load(path.read_text())
           for path in sorted((FIXTURES / "registry").rglob("*.yaml"))]
ROUTES = monitor.route_index(CONFIGS, DATA["domain"])
DOMAINS = {domain["id"]: domain["name"] for domain in DATA["domain"]}


class Latest100Transactions(unittest.TestCase):
    def test_snapshot_selection_and_provenance(self):
        rows = DATA["message_view"]
        self.assertEqual(len(rows), 100)
        self.assertEqual(len({row["msg_id"] for row in rows}), 100)
        self.assertEqual(len({(row["origin_domain_id"], row["origin_tx_hash"])
                              for row in rows}), 100)
        self.assertTrue(all(row["destination_domain_id"] == monitor.ALEO_DOMAIN for row in rows))
        order = [(row["send_occurred_at"], int(row["id"])) for row in rows]
        self.assertEqual(order, sorted(order, reverse=True))
        self.assertEqual(SNAPSHOT["variables"], {
            "limit": 100, "offset": 0,
            "where": {"destination_domain_id": {"_eq": monitor.ALEO_DOMAIN}},
        })
        self.assertEqual(len(SNAPSHOT["registry_commit"]), 40)
        self.assertEqual(len(CONFIGS), len(SNAPSHOT["registry_paths"]))
        self.assertEqual([row["msg_id"] for row in rows], [row["msg_id"] for row in EXPECTED])

    def test_sample_covers_three_origins_and_scaled_tokens(self):
        counts = collections.Counter(row["origin_chain"] for row in EXPECTED)
        self.assertEqual(counts, {"ethereum": 48, "solanamainnet": 48, "hyperevm": 4})
        self.assertEqual(collections.Counter(row["token"] for row in EXPECTED),
                         {"ETH": 42, "SOL": 24, "ZEC": 23, "WBTC": 8, "USDT": 2, "UETH": 1})
        self.assertEqual([row["amount"] for row in EXPECTED if row["token"] == "USDT"],
                         ["0.01", "0.01"])


def transaction_test(message, expected):
    def test(self):
        # Tests must remain offline, even if the decoder gains a network dependency.
        original = copy.deepcopy(message)
        with patch.object(monitor, "http", side_effect=AssertionError("Unexpected network access")):
            actual = monitor.enrich_message(message, ROUTES, DOMAINS)
        self.assertEqual({key: actual[key] for key in expected}, expected)
        for key, value in message.items():
            self.assertEqual(actual[key], value)
        self.assertEqual(message, original)
    return test


for position, (message, expected) in enumerate(zip(DATA["message_view"], EXPECTED), 1):
    name = f"test_transaction_{position:03d}_{expected['msg_id'][2:14]}"
    setattr(Latest100Transactions, name, transaction_test(message, expected))


class DecoderFailuresAndBoundaries(unittest.TestCase):
    def decode(self, **changes):
        return monitor.enrich_message(dict(DATA["message_view"][0], **changes), ROUTES, DOMAINS)

    def assert_undecoded(self, result, status):
        self.assertEqual(result["decode_status"], status)
        for field in ("token", "amount", "amount_raw", "message_amount_raw", "destination_address"):
            self.assertIsNone(result[field])

    def test_unknown_sender_preserved(self):
        self.assert_undecoded(self.decode(sender="0x" + "11" * 20), "unknown_route")

    def test_known_sender_wrong_router_pair(self):
        other = next(row for row in DATA["message_view"] if row["origin_domain_id"] == 1399811149)
        self.assert_undecoded(self.decode(recipient=other["recipient"]), "unknown_route")

    def test_known_router_wrong_origin(self):
        self.assert_undecoded(self.decode(origin_domain_id=999), "unknown_route")

    def test_wrong_destination(self):
        self.assert_undecoded(self.decode(destination_domain_id=1), "wrong_destination")

    def test_malformed_messages(self):
        for body in (None, "", "0x00", "0x" + "00" * 63, "0xzz", "0x1"):
            with self.subTest(body=body):
                self.assert_undecoded(self.decode(message_body=body), "invalid_message")

    def test_malformed_router_addresses(self):
        for field in ("sender", "recipient"):
            for value in (None, "", "0xzz", "0x" + "00" * 33):
                with self.subTest(field=field, value=value):
                    self.assert_undecoded(self.decode(**{field: value}), "invalid_message")

    def test_padded_evm_addresses_and_hex_prefixes(self):
        row = DATA["message_view"][0]
        actual = self.decode(sender="0x" + row["sender"][2:].zfill(64),
                             recipient=row["recipient"][2:],
                             message_body="0x" + row["message_body"][2:])
        self.assertEqual(actual["amount"], "0.001")
        self.assertEqual(actual["decode_status"], "decoded")

    def test_metadata_does_not_change_amount(self):
        actual = self.decode(message_body=DATA["message_view"][0]["message_body"] + "deadbeef")
        self.assertEqual(actual["amount"], "0.001")

    def test_uint256_precision_and_zero(self):
        recipient = DATA["message_view"][0]["message_body"][2:66]
        for number, expected in (
            (0, "0"), (1, "0.000000000000000001"),
            (2**256 - 1, "115792089237316195423570985008687907853269984665640564039457.584007913129639935"),
        ):
            with self.subTest(number=number):
                actual = self.decode(message_body="0x" + recipient + f"{number:064x}")
                self.assertEqual(actual["amount_raw"], str(number))
                self.assertEqual(actual["amount"], expected)

    def test_registry_address_matches_known_wire_router(self):
        token = next(config for config in CONFIGS if config["tokens"][0]["symbol"] == "ETH")["tokens"][0]
        self.assertEqual(monitor.token_address(token).hex(),
                         "5f92a2ac0504d0afa583b071c6728594d55b8f62034804cf417f2813be614b00")
        self.assertEqual(monitor.aleo_address(monitor.token_address(token)),
                         token["addressOrDenom"].split("/")[-1])

    def test_registry_address_checksum_and_padding(self):
        address = "aleo1t7f29tq9qng2lfvrkpcuvu59jn24hrmzqdyqfn6p0u5p80npfvqqecmkj8"
        for invalid in (address[:-1] + "q", address[:-1], "not-aleo", address.upper()):
            with self.subTest(address=invalid), self.assertRaises(ValueError):
                monitor.aleo_registry_address(invalid)

    def test_base58_leading_zeros(self):
        self.assertEqual(monitor.base58_decode("1" * 32), b"\0" * 32)
        self.assertEqual(monitor.base58_encode(b"\0" * 32), "1" * 32)
        with self.assertRaises(ValueError):
            monitor.base58_decode("0OIl")

    def test_rational_scales(self):
        self.assertEqual(monitor.unscale_amount(5 * 10**18, {"numerator": 10**10, "denominator": 1}),
                         500000000)
        self.assertEqual(monitor.unscale_amount(20, {"numerator": 2, "denominator": 3}), 30)
        for scale in (0, -1, 1.5, {"numerator": 1, "denominator": 0}):
            with self.subTest(scale=scale), self.assertRaises(ValueError):
                monitor.unscale_amount(100, scale)
        with self.assertRaises(ValueError):
            monitor.unscale_amount(1, 10**12)


class QueryAndCommandLine(unittest.TestCase):
    def test_query_destination_filter_pagination_and_order(self):
        with patch.object(monitor, "http", return_value=json.dumps({"data": DATA})) as http:
            self.assertEqual(monitor.fetch_messages(100, 25), DATA)
        url, payload = http.call_args.args
        self.assertEqual(url, monitor.API)
        self.assertEqual(payload["variables"], {
            "limit": 100, "offset": 25,
            "where": {"destination_domain_id": {"_eq": monitor.ALEO_DOMAIN}},
        })
        self.assertIn("order_by: [{send_occurred_at: desc}, {id: desc}]", payload["query"])
        self.assertIn("destination_domain_id", payload["query"])

    def test_graphql_errors_do_not_look_like_empty_results(self):
        for response in ({"errors": [{"message": "unavailable"}], "data": DATA},
                         {"data": None}, {"data": {}}, {"data": {"domain": []}}):
            with self.subTest(response=response):
                with patch.object(monitor, "http", return_value=json.dumps(response)):
                    with self.assertRaises(ValueError):
                        monitor.fetch_messages()

    def test_invalid_pagination_does_not_send_requests(self):
        with patch.object(monitor, "http") as http:
            for limit, offset in ((0, 0), (-1, 0), (100, -1)):
                with self.subTest(limit=limit, offset=offset), self.assertRaises(ValueError):
                    monitor.fetch_messages(limit, offset)
            http.assert_not_called()

    def test_empty_result_is_valid(self):
        data = {"message_view": [], "domain": DATA["domain"]}
        with patch.object(monitor, "http", return_value=json.dumps({"data": data})):
            self.assertEqual(monitor.fetch_messages(), data)

    def test_cli_replays_all_100_transactions(self):
        output = io.StringIO()
        with patch("sys.argv", ["address-monitoring.py", "--limit", "100"]), \
             patch.object(monitor, "http", return_value=json.dumps({"data": DATA})), \
             patch.object(monitor, "load_routes", return_value=CONFIGS), \
             contextlib.redirect_stdout(output):
            self.assertEqual(monitor.main(), 0)
        actual = json.loads(output.getvalue())
        self.assertEqual(len(actual), 100)
        self.assertEqual([{key: row[key] for key in EXPECTED[0]} for row in actual], EXPECTED)

    def test_cli_reports_errors_without_json_on_stdout(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["address-monitoring.py"]), \
             patch.object(monitor, "http", side_effect=OSError("network unavailable")), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(monitor.main(), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("network unavailable", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
