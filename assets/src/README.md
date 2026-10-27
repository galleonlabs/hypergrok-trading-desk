# Graphic sources

`hero.html` and `desk-flow.html` are the editable sources for `assets/hero.png` and `assets/desk-flow.png`. They share the palette of `assets/ticket-hg-20260816-01.png` and load Inter and JetBrains Mono from Google Fonts, so render with a network connection.

Render each at 2x with a transparent background, from the repository root:

```bash
chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$chrome" --headless=new --hide-scrollbars --force-device-scale-factor=2 \
  --default-background-color=00000000 --virtual-time-budget=4000 \
  --window-size=800,300 --screenshot=assets/hero.png "file://$PWD/assets/src/hero.html"
"$chrome" --headless=new --hide-scrollbars --force-device-scale-factor=2 \
  --default-background-color=00000000 --virtual-time-budget=4000 \
  --window-size=800,600 --screenshot=assets/desk-flow.png "file://$PWD/assets/src/desk-flow.html"
```

Trim the transparent margin below `desk-flow.png`, then reduce both to a 256-colour palette before committing; the full-colour captures are several hundred kilobytes.

The stages, owners and labels in `desk-flow.html` must match the trade path in `README.md` and `docs/ARCHITECTURE.md`. Change them together.
