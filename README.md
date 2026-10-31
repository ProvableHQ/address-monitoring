# Aleo destination transfer monitoring

Queries one page of Hyperlane messages whose destination domain is Aleo
(`1634493807`), newest first by send time and message row ID. Matches the origin
domain, sender router, and Aleo recipient router against the Hyperlane registry.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python address-monitoring.py --limit 100
.venv/bin/python -m unittest discover -s tests -v
```

`--offset` selects another page. `--registry-ref` accepts a registry commit or
branch; each invocation pins route downloads to the resolved commit.

Output retains the API fields and adds `origin_chain`, the origin token's
`token` symbol, `amount` in origin token units, `amount_raw` in origin atomic
units, `message_amount_raw` before reversing the registry scale, and the
`destination_address` encoded as an Aleo address. Amount fields are strings to
preserve integer precision. The amount excludes additional bridge and gas fees.
It represents the dispatched transfer, not proof of successful delivery.

Unknown routes remain in the output with null decoded fields and
`decode_status: unknown_route`. Malformed known-route messages produce
`invalid_message`. Non-Aleo messages produce `wrong_destination`.
Historical routers absent from the selected registry remain unknown. A scale
that cannot be reversed into an integral origin atomic amount is marked invalid.

## Review findings addressed

The initial file ended at the start of `token_address`, with no query execution,
decoder, or command-line entry point. The completed implementation also handles:

- EVM routers represented as either 20 bytes or zero-padded 32 bytes.
- Solana router addresses encoded in base58.
- Aleo registry address checksum validation and Bech32m output.
- Matching both routers and the origin domain before interpreting a token body.
- Big-endian uint256 amounts at bytes 32–63, with optional trailing metadata.
- Registry integer and rational scale factors, reversed before formatting amounts.
- GraphQL errors and incomplete responses, reported through a failing exit code.

## Latest-100 regression snapshot

Captured **October 30, 2026 at 5:42 p.m. America/Los_Angeles** from the public
[Hyperlane API](https://explorer-api.hyperlane.xyz/graphql). The snapshot contains
100 messages from 100 distinct origin transactions, sent October 24–30, 2026
(API timestamps). Origin counts: Ethereum 48, Solana 48, HyperEVM 4. Token
counts: ETH 42, SOL 24, ZEC 23, WBTC 8, USDT 2, UETH 1.

`tests/fixtures/aleo_latest_100.json` preserves the exact query, variables,
capture time, raw response, and registry commit. The ten route configurations
are pinned to [registry commit
98b0a3449fde140978f008261f083841c390ba3b](https://github.com/hyperlane-xyz/hyperlane-registry/tree/98b0a3449fde140978f008261f083841c390ba3b).

`aleo_latest_100_expected.json` is a separate frozen oracle. Token identities and
scales were checked against the pinned configurations; amounts were computed
separately with Python Decimal precision 100 from the wire amount. Destination
addresses were encoded with the independent
[Bech32m reference implementation](https://github.com/sipa/bech32/blob/master/ref/python/segwit_addr.py).
The wire layout and outbound scaling were checked against Hyperlane's
[TokenMessage](https://github.com/hyperlane-xyz/hyperlane-monorepo/blob/main/solidity/contracts/token/libs/TokenMessage.sol)
and [TokenRouter](https://github.com/hyperlane-xyz/hyperlane-monorepo/blob/main/solidity/contracts/token/libs/TokenRouter.sol)
contracts. Both USDT messages contain `10000000000000000` wire units, which
decode to `10000` origin atomic units and **0.01 USDT** after reversing `10^12`
scaling.

Tests run offline. There is one individually named regression test for each
transaction plus decoder, query, and CLI checks. To explicitly replace the raw
snapshot and registry configurations:

```sh
.venv/bin/python scripts/refresh_fixtures.py
```

Refreshing does **not** regenerate the expected results. Review and update the
oracle and sample assertions separately when intentionally changing the snapshot.
