# Aleo bridge sender monitor

Monitors Hyperlane messages with **Aleo as the destination** and raises local
alerts when an Ethereum, Solana, or HyperEVM sender matches the CSV watchlist
or the Merkle Science address list.

## Setup

Requires Python 3.9+ and internet access. The launcher creates a virtual
environment and installs dependencies automatically.

Add your Merkle Science key to `.env` in this directory:

```dotenv
MERKLE_SCIENCE_API_KEY=your-api-key
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/your/webhook/url
```

You can also export this environment variable; it takes precedence over `.env`.
Secrets are not logged, and `.env` is ignored by Git. `SLACK_WEBHOOK_URL` is
optional: set it to forward alerts to the webhook's Slack channel, or leave it
unset for local alerts only.

## Run and check the monitor

```bash
./run-monitor.sh start --limit 100
```

This starts a background process and returns to your prompt. It waits
**10 seconds after each poll completes** before polling again.

| Command | Purpose |
| --- | --- |
| `./run-monitor.sh status` | Check for `Running (PID ...)` and the last exit code. |
| `./run-monitor.sh logs` | Follow activity and errors. Ctrl+C closes the log viewer only. |
| `./run-monitor.sh alerts` | Read the latest saved alert. |
| `./run-monitor.sh stop` | Stop the monitor. |
| `./run-monitor.sh test-alert` | Test the popup and sound without API calls or fake transaction records. |
| `./run-monitor.sh test-slack` | Send a clearly labeled Slack test message, without querying Hyperlane or Merkle. |

Restart with `stop`, then `start`, after changing launch options.

`--limit 100` controls the recent messages saved to JSON and checked against
the CSV. **It does not cap Merkle Science screening**, which catches up on new
transactions separately in batches of 100 messages.

Other examples:

```bash
./run-monitor.sh run --limit 10                 # Fetch once in the foreground.
./run-monitor.sh start --once --limit 10        # Fetch once in the background.
./run-monitor.sh start --interval 30 --limit 100 # Change the polling delay.
./run-monitor.sh start --no-merkle              # CSV alerts only; no API key needed.
```

## What triggers an alert?

- **CSV:** Each poll checks recent messages against
  `2026-10-29 Binance - ETH SOL HyperEVM Addresses.csv`. Existing messages can
  trigger alerts on startup. Use `--watchlist /path/to/list.csv` for another list.
- **Merkle Science:** The first run baselines existing history. Each newly
  observed origin transaction then triggers a fresh address-list lookup,
  including all response pages. Only Ethereum, Solana, and HyperEVM address
  records are compared. Successful checks persist across restarts. Idle polls
  skip Merkle lookups unless a failed check is due for retry.

Both checks match **only `origin_tx_sender`**, the origin transaction signer.
Router and recipient matches do not trigger alerts. EVM addresses are matched without case sensitivity on Ethereum
and HyperEVM; Solana address matching preserves case.

Merkle matches include the returned risk label. **A list match can say
`No Risk Detected (0)`**; no risk threshold is applied, and a match alone does
not establish that an address is malicious.

## Alert examples

On macOS, a hit produces a **critical popup, a notification, and three beeps**,
plus a large log banner. Dismiss the popup or let it close after two minutes;
polling continues while it is open. Desktop alerts require a graphical session,
and sound depends on local audio settings. Linux uses `notify-send` when available.

These examples use placeholders, not real detections.

**CSV match:**

```text
Hyperlane Bridge Alert:  Flagged address attempting to mint on Aleo

Source: CSV watchlist
Chain: Ethereum -> Aleo
Sender: 0x<matched-address>
Asset: ETH
Amount: 0.001 ETH
Aleo recipient: aleo1<recipient-address>
Transaction: 0x<transaction-hash>
Sent: <transaction-time>
https://explorer.hyperlane.xyz/message/0x<message-id>
```

**Merkle Science match:**

```text
Hyperlane Bridge Alert:  Flagged address attempting to mint on Aleo

Source: Merkle Science address list
Risk reported: High (4)
Chain: Solana -> Aleo
Sender: <matched-Solana-address>
Asset: SOL
Amount: 0.05 SOL
Aleo recipient: aleo1<recipient-address>
Transaction: 0x<transaction-hash>
Sent: <transaction-time>
https://explorer.hyperlane.xyz/message/0x<message-id>
```

Repeated polls do not repeat a dispatched alert for the same source, origin
chain, transaction, and address. A transaction matching both sources can produce
one CSV alert and one Merkle alert. Failed API checks remain pending and retry
with backoff; immediate desktop launch failures also retry.

Alerts show only the transaction signer and the decoded Aleo recipient as
addresses. Routers and program addresses are kept out of alerts. Asset and
amount use the origin token's units, excluding additional bridge/gas fees.
Transactions with multiple messages list each transfer's asset, amount, and
Aleo recipient. Unknown or undecodable details are labeled `Unavailable`;
they do not suppress a signer match.

When Slack is configured, both CSV and Merkle alerts are also sent as JSON
webhook POSTs with the same details shown above. Confirmed deliveries are not
resent on later polls or restarts. Failed posts remain queued with backoff and
respect Slack's `Retry-After`; local popups continue independently. Enabling
Slack does not replay historical alerts. A timeout after Slack accepts a message
can cause a duplicate on retry, since incoming webhooks do not provide an
idempotency guarantee.

## Saved files and tests

| File | Contents |
| --- | --- |
| `.runtime/latest.json` | Latest successful message output, including decoded tokens and amounts. |
| `.runtime/monitor.log` | Poll activity, errors, and alert banners. |
| `.runtime/latest-alert.txt` | Latest alert details. |
| `.runtime/alerts.jsonl` | Detected matches and their sources. |
| `.runtime/*.sqlite3` | Alert deduplication, Slack delivery queue, and Merkle checks. |

Keep `.runtime` across restarts. Deleting it resets alert history and the Merkle
baseline. Set `MONITOR_STATE_DIR` to use another directory, consistently for all
launcher commands. Failed polls retain the previous JSON output, so check the
logs for freshness. Monitoring covers messages visible in Hyperlane's index;
it does not guarantee instantaneous data or monitor every origin-chain transfer.

Run the offline tests, including the 100 captured Aleo-bound transactions:

```bash
.venv/bin/python -m unittest discover -s tests -q
```

For all Python options, run `.venv/bin/python address-monitoring.py --help`.
