# Contributing

HyperGrok is instructions and resources for a user's Grok Bot, not software the repository runs. Contributions are prose, prompts and snippets; the bar is accuracy against the live Hyperliquid API and the live Grok Bot product.

## Ground rules

+ Original text only. Do not copy third-party skill prose, prompts, fixtures or code. Cite sources in `docs/PROVENANCE.md`.
+ No strategies, signals, alpha or return claims anywhere in the repository. The Strategist teaches method; users bring ideas.
+ Every write path stays behind the ticket protocol: Risk PASS, user approval by id, single send, reconciliation. A change that weakens that is a rejected change.
+ Never widen the key model: API wallet only, through the secret store, never in chat or under `/workspace`.
+ Verify snippets against the current SDK versions named in `docs/PROVENANCE.md` before changing them, and update the version there if you bump it.

## Layout

+ `agents/<role>.md` - frontmatter (`name`, `title`, `description`, `seat`, `skills`, `writes_to_exchange`), a **Bot profile** section (Name, Job, Description) and a **System prompt** section.
+ `skills/<name>/SKILL.md` - frontmatter (`name` equal to the directory, `description` under 1024 characters, `license`, `metadata`), body under 300 lines.
+ `SETUP.md` - the single entry point a Grok Bot follows. Keep it linear and honest about what Grok Bot can and cannot do.

## Checks

```bash
bash scripts/check.sh
```

The check runs three passes and CI runs the same script:

+ Instruction files: frontmatter, directory/name agreement, description length, internal links and the one-writer rule.
+ `scripts/check_manifests.py` - the plugin distribution contract. Every manifest (`plugin.json`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.cursor-plugin/plugin.json`, `.grok-plugin/plugin.json`, `.grok-plugin/marketplace.json`) must be valid JSON, name the same plugin at the same version as `plugin.json`, point every declared source, logo, skills, agents and rules path at something that exists inside this repository, and claim the counts the repository actually ships. Adding or removing a skill or role means updating the manifests that state the count in prose.
+ `scripts/test_check_manifests.py` - negative fixtures that prove the manifest check fails on a version mismatch, a missing component path, invalid JSON and inventory drift.
