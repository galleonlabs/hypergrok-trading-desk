# Security

Use GitHub's **Report a vulnerability** flow to open a private security advisory. Do not put keys, signatures, wallet exports, account payloads or exploitable details in a public issue.

What "vulnerability" means for a repository of instructions: any prose or snippet that could lead a Bot to send an order without the user's approval by ticket id, to handle a main-wallet key or seed phrase, to move funds, to resend an unknown-result order, or to print a secret. Those are bugs; report them.

Operating reminders that live in the skills:

+ Only a trade-only Hyperliquid API wallet key ever reaches the desk computer, provisioned through Grok Bot's secure secret store, never through chat.
+ All Bots for one user share a computer and sign-ins, so Bot identity is not a credential boundary; the key's permissions and the ticket protocol are.
+ The native approval control must cover the actual SDK, script and HTTP send paths. If coverage is unverified, remain research-only and do not provision a key. A chat phrase, mutable proposal file or regex hook does not enforce approval.
+ Resting entries and unknown sends retain risk and margin reservations until terminal reconciliation. A restart, old ticket expiry or cancel acknowledgement cannot free dispatched capacity; a feed reconnect requires account recovery before new risk resumes.
+ If a send times out or errors after leaving the machine, do not retry. Reconcile the client order id first, and treat a replacement as unsafe until the original send's expiry has passed and a further check is clean - not finding an order is not proof it will never arrive.
+ Suspected key misuse: the user revokes the API wallet in the Hyperliquid app first, then the desk investigates.

Supported versions:

| Version | Supported |
| --- | --- |
| 1.x | Yes |
