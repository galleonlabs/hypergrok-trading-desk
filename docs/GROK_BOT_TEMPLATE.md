# Grok Bot template

`template/grok-bot.json` is the release contract for the public **HyperGrok Desk Lead** template. It records the exact public profile, source release, avatar and skill bytes that must be present when the template is published.

The template deliberately carries no plugins, memories or routines. The Opening Bell and bootstrap use public Hyperliquid data and the repository checkout, so a new user should not see a connector prompt, inherit an author's context or activate unattended work during installation.

## Author the template

Use a fresh Grok Bot named **HyperGrok Desk Lead**. Copy the `name`, `title`, `description` and avatar from `template/grok-bot.json`, then add every listed skill from its pinned `path`. Do not paraphrase the skill files. The manifest's SHA-256 values identify the reviewed bytes.

Do not add conversation history, private files, account details, plugins, memories, secrets or routines. Before sharing, confirm the template inventory is:

- one Desk Lead profile
- seventeen unique skills
- zero plugins
- zero memories
- zero routines

Run the repository gate before publishing:

```bash
bash scripts/check.sh
```

## Publish and record the link

Share the Bot publicly from Grok Bot and copy its `https://x.ai/bot/<id>` link. Change the manifest status to `published`, add that exact URL as `publicShareUrl`, and put the same link in `README.md` and `docs/FAQ.md`. The template validator rejects a published state if the URL is malformed or missing from either public install surface.

The public preview must show:

- **HyperGrok Desk Lead**
- the description from the manifest
- **Add to Grok Bot**
- no private conversation, file, account or secret

Check the preview while logged out. Importing it should create a new Bot; it must not merge into an existing one or import the author's computer, chats or tokens.

## Evaluate a clean install

Add the template as a new Bot, then send exactly:

> Start the desk.

The Desk Lead follows `hypergrok-bootstrap`. A passing result has:

1. the release tag and commit from the manifest
2. a live, timestamped Opening Bell from public Hyperliquid `/info`
3. seven named Bots or exact manual cards for any unavailable creation capability
4. exactly seventeen unique skills, each reported as `template`, `installed` or `pointer`, with no `mismatch`
5. a private Trading Floor with the six floor Bots, or an exact manual step if group creation is unavailable
6. a passing desk doctor and all five role checks
7. this sentence: **Setup stayed read-only: no key requested and no order created or sent.**

Do not describe the template as a zero-interaction installer. **Add to Grok Bot** creates the Desk Lead; **Start the desk.** runs the reviewed setup. The public path is one click and one message.
