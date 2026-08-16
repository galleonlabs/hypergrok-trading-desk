# Security

Use GitHub's **Report a vulnerability** flow to open a private security advisory. Do not put keys, signatures, wallet exports, account payloads or exploitable details in a public issue.

HyperGrok never needs a seed phrase or main-wallet key. Use a narrowly authorised Hyperliquid API wallet for execution. Main-wallet builder approval remains a separate user action. All Grok Bots for one user share a computer and sign-ins, so Bot identity is not a credential boundary.

If an order submission raises or times out, do not retry. Reconcile the unique cloid against open orders, historical orders and fills first.

Supported versions:

| Version | Supported |
| --- | --- |
| 1.x | Yes |
| 0.x | No |
