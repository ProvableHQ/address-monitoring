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
branch. Registry routes are cached for one hour under `.runtime/registry`,
separately for each ref; refreshes pin all downloads to one resolved commit.
An expired cache remains usable during registry outages, with a warning and
five-minute refresh retries. `--registry-cache-dir` overrides the cache location.
Hyperlane messages are fetched anew on every invocation.

## Run as a background process

The Bash launcher creates the virtual environment and installs PyYAML if needed.
It works from any current directory.

```sh
./run-monitor.sh start --limit 100
./run-monitor.sh status
./run-monitor.sh logs
./run-monitor.sh stop
```

This waits **10 seconds** after the previous attempt completes and continues
after fetch errors. The most recent successful result is saved atomically to
`.runtime/latest.json`; progress and errors append to `.runtime/monitor.log`.
Each JSON result replaces the previous page. Sender alerts and Merkle Science
screening records are stored separately. The API's indexing availability still
limits what can be observed; polling does not guarantee instantaneous data.
It survives closing the terminal and stops when the machine shuts down.

Use `--interval SECONDS` to change the delay, or `start --once --limit 100` to
fetch once in the background and then exit. Use
`./run-monitor.sh run --limit 100` to run once in the foreground and print JSON.
`MONITOR_STATE_DIR` can override the runtime directory; use the same value for
all launcher commands. `status` reports the saved worker PID and last attempt's
exit code. `stop` sends SIGTERM to the worker and its current child process.
Background runs keep the registry cache under the chosen runtime directory.
Restart an existing worker with `stop`, then `start`, to apply the new default.

## Local sender alerts

Sender alerts are enabled by default using the filtered
`2026-10-29 Binance - ETH SOL HyperEVM Addresses.csv` list. The file is reloaded
on each poll. The monitor checks both `origin_tx_sender` (the origin transaction
sender) and `sender` (the Hyperlane message sender, usually a bridge router).
The alert identifies which field matched. Recipient-only matches do not alert.
EVM addresses are compared without case sensitivity on Ethereum and HyperEVM;
Solana addresses are matched by their exact base58-decoded bytes. The list's EVM
addresses are watched on both EVM chains, regardless of their original CSV label.

On macOS, a match launches a **critical popup**, a notification, and three beeps.
The popup dismisses automatically after two minutes. Monitoring continues while
the popup is open. Linux uses `notify-send` when available. A graphical login
session is required for desktop alerts; sound depends on local audio settings.
Immediate desktop launch failures remain pending for retry on subsequent polls.

The monitor also prints a large alert banner to stderr (the monitor log when
running in the background) and saves:

- `.runtime/latest-alert.txt`: full details of the latest alert batch.
- `.runtime/alerts.jsonl`: an append-only record of detected matches.
- `.runtime/sender-alerts.sqlite3`: persistent notification state.

Alerts are dispatched once per source, origin chain, transaction hash, and watched
address, including across restarts and multiple messages in one transaction.
A batch of matches produces one popup. CSV matches already present among the first
100 fetched messages will alert on startup. Checks run before registry/token decoding, so a registry
outage or unknown token does not prevent a sender match from alerting.
Only messages returned by the Aleo-destination query are checked; this does not
monitor every transaction made by these wallets on their origin chains.

Test the popup and sound locally, without querying the API or recording a fake
transaction, then inspect any real saved alerts:

```sh
./run-monitor.sh test-alert
./run-monitor.sh alerts
```

Use `--watchlist /path/to/list.csv` to select another list (address in the first
column; an optional `address` header is accepted), `--no-alerts` to disable both
CSV alerts and Merkle screening, or `--alert-state-dir /path` to change state location.
The Bash launcher uses its `MONITOR_STATE_DIR` for alert state in both foreground
and background runs. Keep that directory across restarts to retain deduplication.

## Merkle Science screening for new transactions

Merkle Science screening is enabled by default alongside the CSV watchlist.
Set `MERKLE_SCIENCE_API_KEY` in the environment or in a local `.env` file:

```dotenv
MERKLE_SCIENCE_API_KEY=your-api-key
```

The `.env` file is ignored by Git and read as data without executing shell
commands. An environment variable takes precedence. The key is sent only as
the `X-API-KEY` header to the Merkle Science API, never logged or stored in
screening records. Missing credentials produce an explicit configuration error.
Use `--no-merkle` to keep only CSV alerts.

On the first run, the monitor saves a baseline of existing Hyperlane IDs without
screening historical transactions. Subsequent polls page through Aleo-bound
messages after the saved ID, in ascending ID order, in batches of 100. The normal
latest page also provides an overlap for newly observed rows indexed behind
the cursor. New transactions are queued in `.runtime/merkle-screening.sqlite3`,
identified by origin domain and transaction hash.

For **each new transaction**, the monitor makes a fresh GET to
`https://api.merklescience.com/api/v4.2/addresses/`, follows every returned page,
and keeps Ethereum, Solana, and HyperEVM records. It compares the eligible
addresses with both Hyperlane sender fields and raises the existing local
popup/sound/banner on a match. Alerts include the source and returned risk label.
Multiple messages from the same transaction share one lookup. Successful checks,
including nonmatches, persist across restarts. No new transactions means no
Merkle requests. Screening happens before registry enrichment.

The [Get List documentation](https://docs.merklescience.com/reference/list-addresses)
states that requests without a risk filter return all addresses. Accordingly,
**a list match is not a declaration that an address is malicious**. All eligible
returned records are matched as requested, including `risk_level: 0` with
`No Risk Detected`. No unrequested risk threshold is applied. HyperEVM is
recognized by its chain label; its vendor-specific numeric ID is not guessed.
EVM addresses are watched on both Ethereum and HyperEVM.

Failures, invalid responses, and incomplete pagination leave transactions pending
rather than marking them clear. Retries use exponential backoff with jitter,
starting around 5 seconds and capped around 5 minutes, honoring longer
`Retry-After` values. A failure stops the batch to avoid repeatedly querying an
unavailable service. Pending transactions survive restarts and leaving the latest
page. The poll reports a failing exit code while screening is pending and keeps
the previous successful JSON output. Failed desktop launches retry separately
without fetching the list again.

Keep the runtime directory across restarts to preserve the baseline, queue, audit
records, and deduplication. Deleting it creates a new baseline and skips existing
history again. A transaction can trigger one CSV alert and a separate Merkle
Science alert; both sources are labeled.

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
