# Security

Use GitHub's **Report a vulnerability** flow to open a private security advisory. Do not put keys, signatures, wallet exports, account payloads or exploitable details in a public issue.

What "vulnerability" means for a repository of instructions: any prose or snippet that could lead a Bot to send an order without the user's approval by ticket id, to handle a main-wallet key or seed phrase, to move funds, to resend an unknown-result order, or to print a secret. Those are bugs; report them.

Operating reminders that live in the skills:

+ Only a trade-only Hyperliquid API wallet key ever reaches the desk computer, provisioned through Grok Bot's secure secret store, never through chat.
+ All Bots for one user share a computer and sign-ins, so Bot identity is not a credential boundary; the key's permissions and the ticket protocol are.
+ If a send times out or errors after leaving the machine, do not retry. Reconcile the client order id first.
+ Suspected key misuse: the user revokes the API wallet in the Hyperliquid app first, then the desk investigates.

Supported versions:

| Version | Supported |
| --- | --- |
| 1.x | Yes |
