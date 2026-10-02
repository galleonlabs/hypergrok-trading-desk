# Grok Bot template

`template/grok-bot.json` is the release contract for the public **HyperGrok Desk Lead** template. It records the exact public profile, source release, avatar, exported bootstrap skill and complete reviewed release inventory. `templateSkillNames` lists the skills carried by the share; `skills` contains all seventeen files and hashes that setup must install and verify.

The public template carries one exact `hypergrok-bootstrap` skill. **Add to Grok Bot** imports the Desk Lead and bootstrap; **Start the desk.** fetches the pinned release and installs the full seventeen-skill desk. Grok Bot's share helper limits its inline recipe to 100,000 characters; the full reviewed pack exceeds that limit. Keep the complete instructions in the release instead of shortening them to fit the share.

The template deliberately carries no plugins, memories or routines. The Opening Bell and bootstrap use public Hyperliquid data and the repository checkout, so a new user should not see a connector prompt, inherit an author's context or activate unattended work during installation.

## Author the template

Use a fresh Grok Bot named **HyperGrok Desk Lead**. Copy the `name`, `title`, `description` and avatar from `template/grok-bot.json`, then add the full skill body for each name in `templateSkillNames` from its pinned `path` in `skills`. Do not paraphrase the skill file or substitute a URL/hash declaration for its body. The manifest's SHA-256 values identify the reviewed bytes. The public description must include the exact `source.release`; that visible marker lets automation detect a stale public template without depending on Grok Bot's private template format.

Do not add conversation history, private files, account details, plugins, memories, secrets or routines. Before sharing, confirm the template inventory is:

- one Desk Lead profile
- one exact `hypergrok-bootstrap` skill
- zero plugins
- zero memories
- zero routines

Run the repository gate before publishing:

```bash
bash scripts/check.sh
```

## Publish and record the link

Share the Bot publicly from Grok Bot and copy its `https://x.ai/bot/<id>` link. Change the manifest status to `published`, add that exact URL as `publicShareUrl`, and put the same link in `README.md` and `docs/FAQ.md`. The template validator rejects a published state if the URL is malformed or missing from either public install surface.

Inspect the native share's Context panel before publishing: it must contain the full reviewed bootstrap body and no other skills. A file downloaded to the author's computer is not evidence that it was exported. The public preview must show:

- **HyperGrok Desk Lead**
- the description from the manifest
- **Add to Grok Bot**
- no private conversation, file, account or secret

Check the preview while logged out. Importing it should create a new Bot carrying the bootstrap; it must not merge into an existing one or import the author's computer, chats or tokens. User-wide shared skills may already exist, so setup compares their actual instructions before enabling them and never assumes a name proves the right release.

Verify the published preview directly:

```bash
python3 scripts/check_public_template.py --live
```

The scheduled **Public Grok Bot template** workflow repeats that check weekly. A failure is a release incident: re-author the public template from the manifest and rerun the workflow. Do not weaken the manifest to match stale public content.

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
