# Start here: set up HyperGrok

You are setting up the HyperGrok trading desk from this repository.

## Do this now

1. Read the seven role files in [`agents/`](agents/).
2. Read the skill map in [`docs/GROK_BOT.md`](docs/GROK_BOT.md).
3. Create these seven roles: desk lead, market analyst, onchain analyst, risk officer, execution trader, portfolio manager and trade reviewer.
4. Give each role its matching instructions and skills.
5. Make the desk lead the user's main entry point and route work through the appropriate specialist.
6. Verify every role with one small read-only task.

If this Grok Bot can create and work with other Bots, create them. If it cannot, prepare seven clearly labelled copy-and-paste setup blocks for the user instead. Do not blur seven roles into one generic prompt.

Use two groups rather than trying to fit all seven Bots into one:

+ **Planning:** desk lead, market analyst, onchain analyst, portfolio manager and risk officer
+ **Execution review:** desk lead, execution trader and trade reviewer

The desk lead carries the approved plan and receipts between the two groups.

If the `hypergrok` command is available, run `hypergrok doctor` and `hypergrok market BTC` on testnet. Then run the same read-only checks on mainnet with the documented mainnet acknowledgement. If the command is unavailable, say **CLI unavailable** and keep the desk in research-and-review mode.

Do not request a key, fund an account or submit an order during setup.

## Finish with this receipt

+ all seven roles and how each one is running
+ the skills assigned to each role
+ testnet and mainnet read checks, or **CLI unavailable**
+ one market-analyst to risk-officer handoff
+ zero signing requests and zero order submissions
