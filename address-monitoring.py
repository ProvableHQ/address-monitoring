#!/usr/bin/env python3
"""Query one page of Aleo-bound Hyperlane messages and enrich known warp transfers.

Python 3.9+; install dependency with: python3 -m pip install PyYAML
Example: python3 address-monitoring.py --limit 100
Amount is the origin token transfer amount, excluding additional bridge/gas fees.
Unknown routes remain in the output with null token/amount and a decode_status.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import sys
from urllib.request import Request, urlopen
from urllib.parse import quote

import yaml

API = "https://explorer-api.hyperlane.xyz/graphql"
REPO = "hyperlane-xyz/hyperlane-registry"
ALEO_DOMAIN = 1634493807
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

QUERY = """
query AleoMessages($limit: Int!, $offset: Int!, $where: message_view_bool_exp!) {
  message_view(
    where: $where
    order_by: [{send_occurred_at: desc}, {id: desc}]
    limit: $limit
    offset: $offset
  ) {
    msg_id
    id
    origin_domain_id
    destination_domain_id
    sender
    recipient
    origin_tx_sender
    origin_tx_hash
    message_body
    send_occurred_at
  }
  domain { id name }
}
"""


def http(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(url, data=data, headers={
        "User-Agent": "aleo-hyperlane-transfers/1.0",
        "Content-Type": "application/json",
    })
    with urlopen(request, timeout=30) as response:
        return response.read().decode()


def raw_bytes(value):
    if not value:
        return b""
    if value.startswith(("\\x", "0x")):
        value = value[2:]
    return bytes.fromhex(value)


def base58_decode(value):
    number = 0
    for char in value:
        number = number * 58 + BASE58.index(char)
    leading = len(value) - len(value.lstrip("1"))
    return b"\0" * leading + number.to_bytes((number.bit_length() + 7) // 8, "big")


def base58_encode(value):
    number, result = int.from_bytes(value, "big"), ""
    while number:
        number, remainder = divmod(number, 58)
        result = BASE58[remainder] + result
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + result


def aleo_registry_address(value):
    # Registry addressOrDenom is program.aleo/aleo1...; message recipient is
    # the 32-byte address component. Decode the registry's bech32 payload.
    address = value.rsplit("/", 1)[-1]
    if not address.startswith("aleo1"):
        raise ValueError("Unsupported Aleo registry address")
    if len(address) != 63:
        raise ValueError("Invalid Aleo address length")
    encoded = [BECH32.index(c) for c in address[5:]]
    if bech32_polymod(hrp_expand("aleo") + encoded) != 0x2BC830A3:
        raise ValueError("Invalid Aleo address checksum")
    payload = address.split("1", 1)[1][:-6]
    bits = "".join(format(BECH32.index(c), "05b") for c in payload)
    decoded = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits) - 7, 8))
    if len(decoded) != 32 or any(c != "0" for c in bits[256:]):
        raise ValueError("Invalid Aleo address payload")
    return decoded


def token_address(token):
    address = token["addressOrDenom"]
    if token["chainName"] == "aleo":
        return aleo_registry_address(address)
    if token["standard"].startswith("Evm"):
        decoded = raw_bytes(address)
        if len(decoded) != 20:
            raise ValueError("Invalid EVM router address")
        return decoded.rjust(32, b"\0")
    if token["standard"].startswith("Sealevel"):
        decoded = base58_decode(address)
        if len(decoded) != 32:
            raise ValueError("Invalid Sealevel router address")
        return decoded
    raise ValueError("Unsupported token standard: " + token["standard"])


def hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def bech32_polymod(values):
    checksum = 1
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for i, generator in enumerate(generators):
            if (top >> i) & 1:
                checksum ^= generator
    return checksum


def aleo_address(value):
    if len(value) != 32:
        raise ValueError("Aleo addresses require 32 bytes")
    bits = "".join(format(byte, "08b") for byte in value) + "0000"
    data = [int(bits[i:i + 5], 2) for i in range(0, len(bits), 5)]
    checksum = bech32_polymod(hrp_expand("aleo") + data + [0] * 6) ^ 0x2BC830A3
    data += [(checksum >> (5 * (5 - i))) & 31 for i in range(6)]
    return "aleo1" + "".join(BECH32[v] for v in data)


def load_routes(ref="main"):
    # Pin all downloads to one commit, even if main advances during the request.
    tree = json.loads(http(f"https://api.github.com/repos/{REPO}/git/trees/{quote(ref, safe='')}?recursive=1"))
    if tree.get("truncated"):
        raise ValueError("Registry tree is truncated")
    paths = [item["path"] for item in tree["tree"]
             if item["path"].startswith("deployments/warp_routes/")
             and item["path"].endswith("/aleo-config.yaml")]
    if not paths:
        raise ValueError("No Aleo routes found in registry")

    def fetch(path):
        return yaml.safe_load(http(f"https://raw.githubusercontent.com/{REPO}/{tree['sha']}/{path}"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(fetch, paths))


def route_index(routes, domains):
    names = {domain["name"]: domain["id"] for domain in domains}
    index = {}
    for route in routes:
        destinations = [t for t in route["tokens"] if t["chainName"] == "aleo"]
        for destination in destinations:
            for origin in route["tokens"]:
                if origin["chainName"] == "aleo" or origin["chainName"] not in names:
                    continue
                key = (names[origin["chainName"]], token_address(origin), token_address(destination))
                if key in index and index[key] != origin:
                    raise ValueError("Ambiguous registry route")
                index[key] = origin
    return index


def format_amount(amount, decimals):
    # Integer arithmetic preserves all uint256 digits regardless of Decimal context.
    if not isinstance(decimals, int) or decimals < 0:
        raise ValueError("Invalid token decimals")
    if decimals == 0:
        return str(amount)
    digits = str(amount).zfill(decimals + 1)
    return (digits[:-decimals] + "." + digits[-decimals:]).rstrip("0").rstrip(".")


def unscale_amount(amount, scale):
    if isinstance(scale, dict):
        numerator, denominator = scale["numerator"], scale["denominator"]
    else:
        numerator, denominator = scale, 1
    if any(type(v) is not int or v <= 0 for v in (numerator, denominator)):
        raise ValueError("Invalid token scale")
    local_amount, remainder = divmod(amount * denominator, numerator)
    if remainder:
        raise ValueError("Message amount cannot be represented in origin token units")
    return local_amount


def enrich_message(message, routes, domains):
    result = dict(message, token=None, amount=None, amount_raw=None,
                  message_amount_raw=None, destination_address=None, decode_status="unknown_route")
    result["origin_chain"] = domains.get(message["origin_domain_id"], str(message["origin_domain_id"]))
    if message["destination_domain_id"] != ALEO_DOMAIN:
        result["decode_status"] = "wrong_destination"
        return result
    try:
        sender = raw_bytes(message["sender"])
        recipient = raw_bytes(message["recipient"])
        if len(sender) not in (20, 32) or len(recipient) != 32:
            raise ValueError("Invalid router length")
        route = routes.get((message["origin_domain_id"], sender.rjust(32, b"\0"), recipient))
        if route is None:
            return result
        body = raw_bytes(message["message_body"])
        if len(body) < 64:
            raise ValueError("Truncated token message")
        wire_amount = int.from_bytes(body[32:64], "big")
        amount = unscale_amount(wire_amount, route.get("scale", 1))
        result.update(token=route["symbol"], amount_raw=str(amount),
                      message_amount_raw=str(wire_amount),
                      amount=format_amount(amount, route["decimals"]),
                      destination_address=aleo_address(body[:32]), decode_status="decoded")
    except (ValueError, TypeError):
        result["decode_status"] = "invalid_message"
    return result


def fetch_messages(limit=100, offset=0):
    if limit < 1 or offset < 0:
        raise ValueError("limit must be positive and offset must be nonnegative")
    response = json.loads(http(API, {
        "query": QUERY,
        "variables": {"limit": limit, "offset": offset,
                      "where": {"destination_domain_id": {"_eq": ALEO_DOMAIN}}},
    }))
    if response.get("errors"):
        raise ValueError("Hyperlane GraphQL error: " + json.dumps(response["errors"]))
    data = response.get("data")
    if not data or "message_view" not in data or "domain" not in data:
        raise ValueError("Incomplete Hyperlane GraphQL response")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--registry-ref", default="main")
    args = parser.parse_args()
    try:
        data = fetch_messages(args.limit, args.offset)
        routes = route_index(load_routes(args.registry_ref), data["domain"])
        domains = {d["id"]: d["name"] for d in data["domain"]}
        print(json.dumps([enrich_message(m, routes, domains) for m in data["message_view"]], indent=2))
    except (OSError, ValueError, KeyError, yaml.YAMLError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
