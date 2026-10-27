# Graphic sources

`build_graphics.py` writes the README's animated graphics from one data model:

| File | What it shows |
| --- | --- |
| `assets/hero.svg`, `assets/hero-mobile.svg` | an isometric trading floor; a ticket moves from the Desk Lead through the analysts and Risk Manager, waits for your approval, goes once to Hyperliquid, then to the Trade Reviewer |
| `assets/desk-flow.svg`, `assets/desk-flow-mobile.svg` | the seven stages of a trade and who owns each, lit in turn on the same 12-second loop |

The README serves the mobile versions to screens up to 600px wide. Animation is plain CSS inside each SVG and stops under `prefers-reduced-motion`. Text is set in subsets of Inter and JetBrains Mono (SIL Open Font License) embedded as WOFF2, so it renders the same everywhere.

To rebuild, download the static TTFs named in `FONT_FILES` (Inter Medium, SemiBold, Bold and ExtraBold, and JetBrains Mono SemiBold) into one directory, then run from the repository root:

```bash
python3 -m venv /tmp/hg-fonts && /tmp/hg-fonts/bin/pip install fonttools brotli
/tmp/hg-fonts/bin/python assets/src/build_graphics.py --fonts <font-directory>
```

The stages, owners and timeline live in `STAGES` and `STOPS`, and the banner line in `TAGLINE`. Stages and owners must match the trade path in `README.md` and `docs/ARCHITECTURE.md`; change them together.
