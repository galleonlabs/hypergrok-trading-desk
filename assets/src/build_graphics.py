#!/usr/bin/env python3
"""Build the README's animated SVG graphics from one data model.

Writes assets/hero.svg, assets/hero-mobile.svg, assets/desk-flow.svg and
assets/desk-flow-mobile.svg. Text is set in subsets of Inter and JetBrains Mono
embedded as WOFF2, so every viewer sees the same type. Needs fontTools and
brotli at build time only; see assets/src/README.md.
"""
import argparse
import base64
import io
import math
import re
from pathlib import Path

from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[2]

C = {
    "bg": "#071815", "panel": "#0d2723", "line": "#133631", "line_hi": "#1d4a43",
    "mint": "#96fbe3", "mint_dim": "#73c6b3", "text": "#d6f5eb", "muted": "#9ecec1",
    "bright": "#effdf8", "ink": "#062520",
}

FONT_FILES = {
    "Inter-500": "Inter-Medium.ttf", "Inter-600": "Inter-SemiBold.ttf",
    "Inter-700": "Inter-Bold.ttf", "Inter-800": "Inter-ExtraBold.ttf",
    "Mono-600": "JetBrainsMono-SemiBold.ttf",
}

# One loop of the ticket's journey, in seconds. The hero and both diagrams share it.
LOOP = 12


class Fonts:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.cache = {}

    def font(self, key):
        if key not in self.cache:
            self.cache[key] = TTFont(self.dir / FONT_FILES[key])
        return self.cache[key]

    def width(self, text, key, size):
        font = self.font(key)
        cmap, hmtx = font.getBestCmap(), font["hmtx"]
        units = sum(hmtx[cmap.get(ord(ch), cmap[ord("?")])][0] for ch in text)
        return units * size / font["head"].unitsPerEm

    def face(self, key, text):
        """A @font-face rule embedding only the glyphs this SVG uses."""
        options = Options()
        options.layout_features = ["kern", "liga", "calt"]
        options.name_IDs = []
        font = TTFont(self.dir / FONT_FILES[key])
        subsetter = Subsetter(options)
        subsetter.populate(text="".join(sorted(set(text + " "))))
        subsetter.subset(font)
        buf = io.BytesIO()
        font.flavor = "woff2"
        font.save(buf)
        family, weight = key.split("-")
        data = base64.b64encode(buf.getvalue()).decode()
        return (f"@font-face{{font-family:'HG {family}';font-weight:{weight};"
                f"src:url(data:font/woff2;base64,{data}) format('woff2')}}")


class Doc:
    """Collects SVG text elements so each file embeds the glyphs it uses."""

    def __init__(self, fonts):
        self.fonts = fonts
        self.used = {}

    def text(self, x, y, s, key, size, fill, anchor="start", spacing=0.0, cls=""):
        self.used[key] = self.used.get(key, "") + s
        family, weight = key.split("-")
        attrs = [f'x="{x:.1f}"', f'y="{y:.1f}"', f'font-family="HG {family}"',
                 f'font-weight="{weight}"', f'font-size="{size}"', f'fill="{fill}"']
        if anchor != "start":
            attrs.append(f'text-anchor="{anchor}"')
        if spacing:
            attrs.append(f'letter-spacing="{spacing}"')
        if cls:
            attrs.append(f'class="{cls}"')
        return f"<text {' '.join(attrs)}>{esc(s)}</text>"

    def wrap(self, s, key, size, width):
        lines, line = [], ""
        for word in s.split():
            trial = f"{line} {word}".strip()
            if line and self.fonts.width(trial, key, size) > width:
                lines.append(line)
                line = word
            else:
                line = trial
        return lines + [line]

    def style(self, css):
        faces = "".join(self.fonts.face(k, v) for k, v in sorted(self.used.items()))
        return f"<style>{faces}{css}</style>"


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg(width, height, title, desc, style, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img" aria-labelledby="t d">'
            f'<title id="t">{esc(title)}</title><desc id="d">{esc(desc)}</desc>'
            f"{style}{body}</svg>\n")


def pct(seconds):
    return f"{100 * seconds / LOOP:.2f}%"


# Timeline: the ticket rests at each stop for [arrive, leave] seconds.
STOPS = [
    ("DL", 0.0, 0.7), ("MA", 1.3, 2.0), ("RA", 2.6, 3.3), ("ST", 4.0, 4.7),
    ("RM", 5.4, 6.3), ("YOU", 7.0, 8.1), ("ET", 8.7, 9.1), ("HL", 9.6, 10.1),
    ("ET2", 10.6, 10.9), ("TR", 11.4, 11.8),
]
STOP_TIMES = {name: (a, b) for name, a, b in STOPS}

# The seven stages and the stop where each is happening, for the diagrams.
STAGES = [
    ("01", "idea", "You", "you", ("DL", "DL")),
    ("02", "evidence", "Market Analyst, Research Analyst, Strategist", "", ("MA", "ST")),
    ("03", "risk sign‑off", "Risk Manager sizes it and issues the ticket", "", ("RM", "RM")),
    ("04", "your approval", "You, by ticket id", "you", ("YOU", "YOU")),
    ("05", "one send", "Execution Trader, the one Bot with the keys", "send", ("ET", "HL")),
    ("06", "reconciliation", "Execution Trader confirms it from the exchange record", "", ("ET2", "ET2")),
    ("07", "review", "Trade Reviewer grades process and outcome separately", "", ("TR", "TR")),
]


def window_keyframes(name, windows, ramp=0.25, on="1", off="0", prop="opacity"):
    """Keyframes that hold `on` inside each (start, end) window in seconds and `off` otherwise."""
    frames = [(0, on if windows[0][0] - ramp <= 0 else off)]
    for start, end in windows:
        frames += [(max(start - ramp, 0), off if start - ramp > 0 else on), (start, on), (end, on),
                   (min(end + ramp, LOOP), off)]
    frames.append((LOOP, off))
    seen, body = set(), ""
    for at, value in frames:
        if pct(at) not in seen:
            seen.add(pct(at))
            body += f"{pct(at)}{{{prop}:{value}}}"
    return f"@keyframes {name}{{{body}}}"


# ---------------------------------------------------------------- isometric

COS, SIN = math.cos(math.radians(30)), math.sin(math.radians(30))


def shade(hex_colour, amount):
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    f = 1 + amount
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v * f))) for v in (r, g, b))


class Iso:
    def __init__(self, unit):
        self.u = unit
        self.boxes = []

    def p(self, x, y, z):
        return ((x - y) * COS * self.u, (x + y) * SIN * self.u - z * self.u)

    def box(self, x, y, z, w, d, h, top, cls="", screen=None, left=None, right=None, layer=0):
        self.boxes.append(dict(x=x, y=y, z=z, w=w, d=d, h=h, top=top, cls=cls,
                               screen=screen, left=left, right=right, layer=layer))

    def poly(self, pts, fill, cls=""):
        d = " ".join(f"{px:.1f},{py:.1f}" for px, py in (self.p(*q) for q in pts))
        c = f' class="{cls}"' if cls else ""
        return f'<polygon points="{d}" fill="{fill}"{c}/>'

    def render(self, layers):
        out = []
        for b in sorted((b for b in self.boxes if b["layer"] in layers), key=lambda b: (b["layer"], b["x"] + b["w"] / 2 + b["y"] + b["d"] / 2, b["z"])):
            x0, y0, z0 = b["x"], b["y"], b["z"]
            x1, y1, z1 = x0 + b["w"], y0 + b["d"], z0 + b["h"]
            left = b["left"] or shade(b["top"], -0.22)
            right = b["right"] or shade(b["top"], -0.38)
            g = [f'<g class="{b["cls"]}">' if b["cls"] else "<g>"]
            g.append(self.poly([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)], left))
            right_face = [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)]
            g.append(self.poly(right_face, right))
            if b["screen"]:
                g.append(self.poly(right_face, C["mint"], "screen"))
            g.append(self.poly([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], b["top"]))
            g.append("</g>")
            out.append("".join(g))
        return "".join(out)


# The floor runs along +x, so the desks read as one pipeline from top left to
# bottom right. The Trade Reviewer sits on its own island: it works by DM.
DESK_W, DESK_D, DESK_H = 1.5, 1.5, 0.45
DESKS = {name: (0.55 + 2.15 * i, 0.55) for i, name in enumerate(["DL", "MA", "RA", "ST", "RM"])}
DESKS["ET"] = (11.4, 0.3)
FLOOR = (13.4, 2.6)
PLINTH = (11.15, 0.05, 2.0, 2.45, 0.2)
HL_AT = (14.9, 0.45, 1.7)          # x, y, side of the exchange tower
TR_ISLAND = (2.8, -4.8, 2.3, 2.3)
TR_AT = (3.2, -4.4)


def desk_at(name):
    if name == "TR":
        return TR_AT[0], TR_AT[1], 0
    x, y = DESKS["ET" if name == "ET2" else name]
    return x, y, PLINTH[4] if name.startswith("ET") else 0


def scene(doc, unit, label_size):
    """The trading floor: six desks (the writer's on a plinth), the exchange and the reviewer's island."""
    iso = Iso(unit)
    floor = "#0f2e29"
    iso.box(0, 0, -0.5, FLOOR[0], FLOOR[1], 0.5, floor, layer=0)
    iso.box(TR_ISLAND[0], TR_ISLAND[1], -0.5, TR_ISLAND[2], TR_ISLAND[3], 0.5, floor, layer=0)
    iso.box(FLOOR[0], 0.85, -0.3, HL_AT[0] - FLOOR[0], 0.9, 0.3, "#174640", layer=1)   # bridge
    x, y, w, d, h = PLINTH
    iso.box(x, y, 0, w, d, h, "#5fd4b6", left="#3c9c84", right="#2b7a67", layer=2)

    def desk(name):
        x, y, z = desk_at(name)
        cls = f"desk desk-{name}"
        iso.box(x, y, z, DESK_W, DESK_D, DESK_H, "#2a5f56", cls=cls, layer=3)
        iso.box(x + 0.1, y + 0.2, z + DESK_H, 0.14, DESK_D - 0.4, 0.8, "#1d4a43",
                right="#0a231f", screen=True, cls=f"{cls} mon mon-{name}", layer=3)

    for name in list(DESKS) + ["TR"]:
        desk(name)

    hx, hy, side = HL_AT
    levels = ["#1d4a43", "#174640", "#1d4a43", "#174640", "#73c6b3"]
    for i, colour in enumerate(levels):
        cls = "hl-top" if i == len(levels) - 1 else ""
        iso.box(hx, hy, -0.5 + i * 0.68, side, side, 0.6, colour, cls=cls, layer=4 + i)

    grid = []
    for i in range(1, int(FLOOR[0])):
        a, b = iso.p(i, 0, 0), iso.p(i, FLOOR[1], 0)
        grid.append(f"M{a[0]:.1f} {a[1]:.1f}L{b[0]:.1f} {b[1]:.1f}")
    for j in range(1, int(FLOOR[1]) + 1):
        a, b = iso.p(0, j, 0), iso.p(FLOOR[0], j, 0)
        grid.append(f"M{a[0]:.1f} {a[1]:.1f}L{b[0]:.1f} {b[1]:.1f}")
    grid_svg = f'<path d="{"".join(grid)}" stroke="#174640" stroke-width="1" fill="none"/>'
    body = iso.render({0}) + grid_svg + iso.render(set(range(1, 10)))

    def centre(name):
        if name == "HL":
            return iso.p(hx + side / 2, hy + side / 2, 3.35)
        if name == "YOU":
            return iso.p(DESKS["ET"][0], 0.9, 2.6)
        x, y, z = desk_at(name)
        return iso.p(x + DESK_W / 2, y + DESK_D / 2, z + 1.9)

    labels = []
    for name in list(DESKS) + ["TR"]:
        x, y, z = desk_at(name)
        lx, ly = iso.p(x + DESK_W / 2, y + DESK_D + (0.38 if name == "ET" else 0.42), z)
        fill = C["ink"] if name == "ET" else C["muted"]
        labels.append(doc.text(lx, ly + label_size * 0.35, name, "Mono-600", label_size, fill,
                               anchor="middle"))
    lx, ly = iso.p(hx + side / 2, hy + side / 2, 2.9)
    labels.append(doc.text(lx, ly - unit * 1.75, "HYPERLIQUID", "Inter-700", label_size * 0.85, C["mint_dim"],
                           anchor="middle", spacing=1.6))
    lx, ly = iso.p(TR_ISLAND[0] + TR_ISLAND[2], TR_ISLAND[1] + 0.2, 0)
    labels.append(doc.text(lx + 8, ly + 4, "BY DM", "Inter-700", label_size * 0.8, C["mint_dim"],
                           spacing=1.4))

    yx, yy = centre("YOU")
    size = label_size * 1.05
    pill_w = doc.fonts.width("you approve", "Mono-600", size) + size * 1.8
    pill_h = size * 1.9
    top = yy - pill_h - 12
    you = (f'<g class="you"><rect x="{yx - pill_w / 2:.1f}" y="{top:.1f}" width="{pill_w:.1f}" height="{pill_h:.1f}" '
           f'rx="{pill_h / 2:.1f}" fill="{C["bg"]}" stroke="{C["mint_dim"]}" stroke-dasharray="3 2.5"/>'
           f'<rect class="you-on" x="{yx - pill_w / 2:.1f}" y="{top:.1f}" width="{pill_w:.1f}" height="{pill_h:.1f}" '
           f'rx="{pill_h / 2:.1f}" fill="{C["mint"]}"/>'
           + doc.text(yx, top + pill_h / 2 + size * 0.36, "you approve", "Mono-600", size, C["mint"], anchor="middle")
           + doc.text(yx, top + pill_h / 2 + size * 0.36, "you approve", "Mono-600", size, C["ink"], anchor="middle", cls="you-on")
           + "</g>")

    r = unit * 0.3
    ticket = ('<g class="ticket"><g class="ticket-body">'
              f'<circle r="{r * 1.7:.1f}" fill="{C["mint"]}" opacity=".16"/>'
              f'<path d="M0 {-r:.1f}L{r:.1f} 0L0 {r:.1f}L{-r:.1f} 0Z" fill="{C["mint"]}" stroke="{C["ink"]}" stroke-width="1.5"/>'
              f'<path d="M0 {-r * .42:.1f}L{r * .42:.1f} 0L0 {r * .42:.1f}L{-r * .42:.1f} 0Z" fill="{C["ink"]}"/></g></g>')

    frames, first = [], None
    for name, arrive, leave in STOPS:
        px, py = centre(name)
        first = first or (px, py)
        frames += [(arrive, px, py), (leave, px, py)]
    kf = "".join(f"{pct(t)}{{transform:translate({x:.1f}px,{y:.1f}px)}}" for t, x, y in frames)
    kf += f"100%{{transform:translate({first[0]:.1f}px,{first[1]:.1f}px)}}"
    et = centre("ET")
    css = [f"@keyframes route{{{kf}}}",
           f"@keyframes fade{{0%{{opacity:0}}{pct(0.25)}{{opacity:1}}{pct(STOPS[-1][2])}{{opacity:1}}"
           f"{pct(STOPS[-1][2] + 0.15)}{{opacity:0}}100%{{opacity:0}}}}",
           f".ticket{{transform:translate({et[0]:.1f}px,{et[1]:.1f}px);animation:route {LOOP}s ease-in-out infinite}}",
           f".ticket-body{{animation:fade {LOOP}s linear infinite}}",
           ".you-on{opacity:0}"]
    for name in list(DESKS) + ["TR"]:
        windows = [(a, b) for stop, a, b in STOPS if stop in (name, name + "2")]
        css.append(window_keyframes(f"glow-{name}", windows))
        css.append(f".mon-{name} .screen{{opacity:0;animation:glow-{name} {LOOP}s linear infinite}}")
    a, b = STOP_TIMES["YOU"]
    css.append(window_keyframes("approve", [(a + 0.35, b)]))
    css.append(f".you-on{{animation:approve {LOOP}s linear infinite}}")
    a, b = STOP_TIMES["HL"]
    css.append(window_keyframes("fill", [(a, b)], prop="fill", on=C["mint"], off="#73c6b3"))
    css.append(f".hl-top polygon:last-child{{animation:fill {LOOP}s linear infinite}}")
    css.append("@media (prefers-reduced-motion:reduce){*{animation:none!important}.mon-ET .screen{opacity:1}}")
    return body + "".join(labels) + you + ticket, css


def bounds(svg_body):
    xs, ys = [], []
    for pts in re.findall(r'points="([^"]+)"', svg_body):
        for pair in pts.split():
            x, y = pair.split(",")
            xs.append(float(x))
            ys.append(float(y))
    return min(xs), min(ys), max(xs), max(ys)


TAGLINE = ("Your trading desk, ", "fully staffed.")


def tagline(doc, x, y, size):
    lead, accent = TAGLINE
    doc.used["Inter-600"] = doc.used.get("Inter-600", "") + accent
    return (doc.text(x, y, lead, "Inter-500", size, C["muted"])
            .replace("</text>", f'<tspan fill="{C["mint"]}" font-weight="600">{accent}</tspan></text>'))


def hero(fonts, mobile):
    doc = Doc(fonts)
    mascot = base64.b64encode((ROOT / "assets/mascot-320.jpg").read_bytes()).decode()

    def brand(x, y, size, eyebrow):
        return (f'<clipPath id="m"><rect x="{x}" y="{y}" width="{size}" height="{size}" rx="{size * .27:.1f}"/></clipPath>'
                f'<image href="data:image/jpeg;base64,{mascot}" x="{x}" y="{y}" width="{size}" height="{size}" clip-path="url(#m)"/>'
                + doc.text(x + size + 12, y + size / 2 + eyebrow * 0.36, "HYPERLIQUID TRADING DESK FOR GROK BOT",
                           "Inter-700", eyebrow, C["mint_dim"], spacing=eyebrow * 0.16))

    if mobile:
        W = 400
        body, css = scene(doc, 15.5, 10.5)
        x0, y0, x1, y1 = bounds(body)
        sx, sy = (W - (x1 - x0)) / 2 - x0, 176 - y0
        H = round(sy + y1 + 26)
        text = [brand(24, 26, 30, 8.6),
                doc.text(22, 112, "HyperGrok", "Inter-800", 50, C["bright"], spacing=-1.4),
                tagline(doc, 24, 144, 16)]
    else:
        W, H = 800, 440
        body, css = scene(doc, 29, 11.5)
        x0, y0, x1, y1 = bounds(body)
        sx, sy = W - 28 - x1, 34 - y0
        text = [brand(44, 286, 34, 10),
                doc.text(41, 380, "HyperGrok", "Inter-800", 60, C["bright"], spacing=-1.7),
                tagline(doc, 44, 412, 19)]

    bg = (f'<defs><radialGradient id="bg" cx="{0.62 if not mobile else 0.5}" cy="{0.4 if not mobile else 0.62}" r="0.75">'
          f'<stop offset="0" stop-color="#133a34"/><stop offset="0.55" stop-color="{C["panel"]}"/>'
          f'<stop offset="1" stop-color="{C["bg"]}"/></radialGradient></defs>'
          f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="18" fill="url(#bg)" stroke="{C["line"]}"/>')
    scene_g = f'<g transform="translate({sx:.1f} {sy:.1f})">{body}</g>'
    title = "HyperGrok: a Hyperliquid trading desk for Grok Bot"
    desc = ("Your trading desk, fully staffed. An isometric trading floor: a ticket moves from the "
            "Desk Lead through the analysts and the Risk Manager, waits for your approval, then the "
            "Execution Trader sends it once to Hyperliquid and the Trade Reviewer records it.")
    return svg(W, H, title, desc, doc.style("".join(css)), bg + "".join(text) + scene_g)


# ---------------------------------------------------------------- diagrams

def stage_css(i, stop_from, stop_to):
    a = STOP_TIMES[stop_from][0]
    b = STOP_TIMES[stop_to][1]
    return (window_keyframes(f"on{i}", [(a, b)])
            + f".on{i}{{opacity:0;animation:on{i} {LOOP}s linear infinite}}")


def card(doc, i, x, y, w, h, stage, inline):
    n, name, who, kind, (s_from, s_to) = stage
    pad = 14
    send = kind == "send"
    fill = C["mint"] if send else C["bg"]
    stroke = C["mint"] if send else (C["mint_dim"] if kind == "you" else C["line_hi"])
    dash = ' stroke-dasharray="5 4"' if kind == "you" else ""
    name_fill = C["ink"] if send else (C["mint"] if kind == "you" else C["bright"])
    who_fill = "#0b3a32" if send else C["muted"]
    n_fill = "#0b3a32" if send else C["mint_dim"]
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{stroke}"{dash}/>']
    glow_fill = "none" if send else C["mint"]
    out.append(f'<g class="on{i}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{glow_fill}" '
               f'fill-opacity=".07" stroke="{C["mint"] if not send else C["bright"]}" stroke-width="2"/></g>')
    if inline:
        out.append(doc.text(x + pad, y + 27, n, "Mono-600", 12, n_fill))
        tx = x + pad + 34
        out.append(doc.text(tx, y + 27, name, "Inter-700", 18, name_fill))
        lines = doc.wrap(who, "Inter-500", 14, w - (tx - x) - pad)
        for k, line in enumerate(lines):
            out.append(doc.text(tx, y + 50 + 19 * k, line, "Inter-500", 14, who_fill))
    else:
        out.append(doc.text(x + pad, y + 24, n, "Mono-600", 12, n_fill))
        out.append(doc.text(x + pad, y + 49, name, "Inter-700", 19, name_fill))
        lines = doc.wrap(who, "Inter-500", 14.5, w - 2 * pad)
        for k, line in enumerate(lines):
            out.append(doc.text(x + pad, y + 73 + 19.5 * k, line, "Inter-500", 14.5, who_fill))
    return "".join(out), stage_css(i, s_from, s_to)


def card_height(doc, who, w, inline):
    if inline:
        return 38 + 19 * len(doc.wrap(who, "Inter-500", 14, w - 14 - 34 - 14)) + 12
    return 62 + 19.5 * len(doc.wrap(who, "Inter-500", 14.5, w - 28)) + 6


def legend(doc, x, y, stacked):
    items = [
        f'<rect x="{x}" y="{y - 10}" width="11" height="11" rx="3" fill="none" stroke="{C["mint_dim"]}" stroke-dasharray="2.5 2"/>'
        + doc.text(x + 18, y, "You", "Inter-600", 13.5, C["text"]) + doc.text(x + 18 + doc.fonts.width("You ", "Inter-600", 13.5), y, "decide", "Inter-500", 13.5, C["muted"]),
    ]
    ox = x if stacked else (x + 18 + doc.fonts.width("You ", "Inter-600", 13.5)
                            + doc.fonts.width("decide", "Inter-500", 13.5) + 28)
    oy = y + 24 if stacked else y
    items.append(f'<rect x="{ox}" y="{oy - 10}" width="11" height="11" rx="3" fill="{C["mint"]}"/>'
                 + doc.text(ox + 18, oy, "One writer", "Inter-600", 13.5, C["text"])
                 + doc.text(ox + 18 + doc.fonts.width("One writer ", "Inter-600", 13.5), oy, "touches the exchange", "Inter-500", 13.5, C["muted"]))
    return "".join(items)


def flow(fonts, mobile):
    doc = Doc(fonts)
    css = ["@media (prefers-reduced-motion:reduce){*{animation:none!important}[class^=on]{opacity:0}}"]
    parts = []
    if not mobile:
        W, pad = 800, 28
        head_y = 46
        parts.append(doc.text(pad, head_y, "ONE TRADE, SEVEN STAGES", "Inter-700", 14, C["mint"], spacing=2.6))
        parts.append(doc.text(W - pad, head_y, "The Desk Lead keeps every trade on this path", "Inter-500", 14, C["mint_dim"], anchor="end"))
        gap, arrow = 10, 18
        inner = W - 2 * pad
        frs = [0.8, 1.2, 1.2, 1.2]
        unit_w = (inner - 3 * (arrow + 2 * gap)) / sum(frs)
        widths_a = [f * unit_w for f in frs]
        w_b = (inner - 2 * (arrow + 2 * gap)) / 3
        top = 70
        h_a = max(card_height(doc, s[2], w, False) for s, w in zip(STAGES[:4], widths_a))
        h_b = max(card_height(doc, s[2], w_b, False) for s in STAGES[4:])
        turn = 40
        row_b = top + h_a + turn
        x = pad
        centres = []
        for i, (stage, w) in enumerate(zip(STAGES[:4], widths_a)):
            body, c = card(doc, i, x, top, w, h_a, stage, False)
            parts.append(body)
            css.append(c)
            centres.append(x + w / 2)
            if i < 3:
                ax = x + w + gap
                parts.append(f'<path d="M{ax:.1f} {top + h_a / 2:.1f}h{arrow}m-5 -4.5l5 4.5l-5 4.5" fill="none" stroke="{C["mint_dim"]}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>')
            x += w + arrow + 2 * gap
        x = pad
        b_centres = []
        for i, stage in enumerate(STAGES[4:], start=4):
            body, c = card(doc, i, x, row_b, w_b, h_b, stage, False)
            parts.append(body)
            css.append(c)
            b_centres.append(x + w_b / 2)
            if i < 6:
                ax = x + w_b + gap
                parts.append(f'<path d="M{ax:.1f} {row_b + h_b / 2:.1f}h{arrow}m-5 -4.5l5 4.5l-5 4.5" fill="none" stroke="{C["mint_dim"]}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>')
            x += w_b + arrow + 2 * gap
        y_mid = top + h_a + turn / 2
        d = f"M{centres[3]:.1f} {top + h_a + 6:.1f}V{y_mid:.1f}H{b_centres[0]:.1f}V{row_b - 6:.1f}"
        parts.append(f'<path d="{d}" fill="none" stroke="{C["mint_dim"]}" stroke-width="1.5"/>'
                     f'<path d="M{b_centres[0] - 4.5:.1f} {row_b - 11:.1f}l4.5 5l4.5 -5" fill="none" stroke="{C["mint_dim"]}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>')
        foot = row_b + h_b + 22
        parts.append(f'<path d="M{pad} {foot}H{W - pad}" stroke="{C["line"]}"/>')
        parts.append(legend(doc, pad, foot + 30, False))
        parts.append(doc.text(pad, foot + 54, "Six Bots share the Trading Floor; the Trade Reviewer works by DM", "Inter-500", 13.5, C["muted"]))
        H = foot + 74
    else:
        W, pad = 400, 18
        parts.append(doc.text(pad, 38, "ONE TRADE, SEVEN STAGES", "Inter-700", 14, C["mint"], spacing=2.4))
        parts.append(doc.text(pad, 60, "The Desk Lead keeps every trade on this path", "Inter-500", 14, C["mint_dim"]))
        y = 80
        w = W - 2 * pad
        link = 16
        for i, stage in enumerate(STAGES):
            h = card_height(doc, stage[2], w, True)
            body, c = card(doc, i, pad, y, w, h, stage, True)
            parts.append(body)
            css.append(c)
            y += h
            if i < 6:
                parts.append(f'<path d="M{pad + 30} {y}v{link}" stroke="{C["mint_dim"]}" stroke-width="1.5"/>')
                y += link
        y += 22
        parts.append(f'<path d="M{pad} {y}H{W - pad}" stroke="{C["line"]}"/>')
        parts.append(legend(doc, pad, y + 28, True))
        lines = doc.wrap("Six Bots share the Trading Floor; the Trade Reviewer works by DM", "Inter-500", 13.5, w)
        for k, line in enumerate(lines):
            parts.append(doc.text(pad, y + 80 + 19 * k, line, "Inter-500", 13.5, C["muted"]))
        H = y + 80 + 19 * (len(lines) - 1) + 22
    H = round(H)
    bg = (f'<defs><radialGradient id="bg" cx="0.85" cy="0" r="0.9"><stop offset="0" stop-color="#123832"/>'
          f'<stop offset="0.45" stop-color="{C["panel"]}"/><stop offset="1" stop-color="{C["bg"]}"/></radialGradient></defs>'
          f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="18" fill="url(#bg)" stroke="{C["line"]}"/>')
    desc = ("1 idea, from you. 2 evidence, from the Market Analyst, Research Analyst and Strategist. "
            "3 risk sign-off: the Risk Manager sizes the trade and issues the ticket. 4 your approval, by ticket id. "
            "5 one send, by the Execution Trader, the one Bot with the keys. 6 reconciliation: the Execution Trader "
            "confirms it from the exchange record. 7 review: the Trade Reviewer grades process and outcome separately.")
    return svg(W, H, "One trade, seven stages", desc, doc.style("".join(css)), bg + "".join(parts))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fonts", required=True, help="directory holding the TTF files named in FONT_FILES")
    args = parser.parse_args()
    fonts = Fonts(args.fonts)
    outputs = {
        "hero.svg": hero(fonts, False), "hero-mobile.svg": hero(fonts, True),
        "desk-flow.svg": flow(fonts, False), "desk-flow-mobile.svg": flow(fonts, True),
    }
    for name, content in outputs.items():
        path = ROOT / "assets" / name
        path.write_text(content, encoding="utf-8")
        print(f"{path.relative_to(ROOT)}  {len(content.encode()) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
