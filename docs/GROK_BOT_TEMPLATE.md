# Grok Bot template

`template/grok-bot.json` is the release contract for the public **HyperGrok Desk Lead** template. It records the exact public profile, source release, avatar, exported bootstrap skill and complete reviewed release inventory. `templateSkillNames` lists the skills carried by the share; `skills` contains all seventeen files and hashes that setup must install and verify.

The public template carries one `hypergrok-bootstrap` skill. **Add to Grok Bot** imports the Desk Lead and bootstrap; **Start the desk.** fetches the pinned release and installs the full seventeen-skill desk. Grok Bot's share helper limits its inline recipe to 100,000 characters; the full reviewed pack exceeds that limit. Keep the complete instructions in the release instead of shortening them to fit the share.

The template deliberately carries no plugins, memories or routines. The Opening Bell and bootstrap use public Hyperliquid data and the repository checkout, so a new user should not see a connector prompt, inherit an author's context or activate unattended work during installation.

## Author the template

Use a fresh Grok Bot named **HyperGrok Desk Lead**. Copy the `name`, `title`, `description` and avatar from `template/grok-bot.json`, then add the full skill body for each name in `templateSkillNames` from its pinned `path` in `skills`. Do not paraphrase the skill file or substitute a URL/hash declaration for its body. The manifest's SHA-256 values identify the reviewed bytes. The public description must include the exact `source.release`; that visible marker lets automation detect a stale public template without depending on Grok Bot's private template format.

Grok Bot stores the skill's `name` and `description` separately and rebuilds their YAML wrapper. Its native content field rejects a complete file beginning with `---` and does not retain the source's `license` or `metadata` fields. Copy the source name and description unchanged, and supply all Markdown after the closing frontmatter delimiter, omitting only empty lines at the beginning and the final newline. Preserve all internal content. The shared native skill is therefore not byte-identical to the complete `SKILL.md` file.

Keep all seventeen full-file hashes in the release manifest: they verify the pinned checkout, including its frontmatter. For native publication, separately compare the name, description and normalized Markdown body with the source. Record the normalization, body length and hash, and distinguish a hash of the locally staged content argument from an independent readback of the exported or imported skill. A staged argument proves what was supplied, not what the product stored.

Do not add conversation history, private files, account details, plugins, memories, secrets or routines. Before sharing, confirm the template inventory is:

- one Desk Lead profile
- one `hypergrok-bootstrap` skill with the reviewed Markdown body
- zero plugins
- zero memories
- zero routines

Run the repository gate before publishing:

```bash
bash scripts/check.sh
```

## Publish and record the link

Share the Bot publicly from Grok Bot and copy its `https://x.ai/bot/<id>` link. Change the manifest status to `published`, add that exact URL as `publicShareUrl`, and put the same link in `README.md` and `docs/FAQ.md`. The template validator rejects a published state if the URL is malformed or missing from either public install surface.

Inspect the native share's Context panel before publishing: it must contain the reviewed bootstrap instructions and no other skills. Independently read back the stored content to verify its body; a Context entry or a file downloaded to the author's computer does not prove all exported bytes. Verify the exported avatar separately from the publishing Bot's avatar. The public preview must show:

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

## Publication receipt - 2026-10-02

The native Publish action created the public [HyperGrok Desk Lead template](https://x.ai/bot/1HXwKKdavqHZRzSsWbDCN). A fresh anonymous preview passed the checks for the exact `v1.4.7` name and description, **Add to Grok Bot** action and matching template-id deep link. The native Context panel showed one `hypergrok-bootstrap` skill. These checks establish publication and the visible profile; they do not establish the complete stored skill bytes or a passing clean install.

The source is `skills/hypergrok-bootstrap/SKILL.md` at the reviewed `v1.4.7` release, commit `1063e6c40fcfd287edf2d118669689a5297d066f`. Its complete file is 6,085 bytes with SHA-256 `bdc8c996ff73d69fbd7863bdb571e2e8cd414108b3b48665d216cb01134bf9ca`. Removing its YAML frontmatter, leading blank line and final newline yields the locally staged native content argument: 5,594 bytes with SHA-256 `264dc49723a2a9cac4b4c0b61026ecc075a363a21ac01a6b89b5c474c85ae622`. This is a source-derived argument hash; independent export/readback of the complete stored body remains pending.

The publishing Bot's mascot visually matched the released asset, but the share modal showed a default white icon. Exported avatar matching is unverified. Fresh import and the clean-install acceptance checks above remain pending; publication does not establish that the seventeen-skill desk was installed.
