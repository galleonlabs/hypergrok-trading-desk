#!/usr/bin/env python3
"""Record `scripts/opening_bell.py --coin ETH` into assets/opening-bell.gif.

Runs the real command against public Hyperliquid data and draws its output as a
terminal card that opens on the full output, so a still preview is never blank. Needs Pillow and a monospace TTF/TTC:

    python3 assets/src/record_opening_bell.py --font /System/Library/Fonts/Menlo.ttc
"""
import argparse
import datetime
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMAND = ["python3", "scripts/opening_bell.py", "--coin", "ETH"]
WIDTH, PAD, LINE, SIZE = 1024, 26, 24, 17
BG, RULE, TEXT, DIM, MINT = "#0f1719", "#2c3a3b", "#e6edf3", "#8b9a9c", "#7ee8cf"
REVEAL = (0, 3, 6, 10, 17)  # output lines shown while the run replays
DURATIONS = (5000, 1500, 700, 700, 700, 700, 5000)  # full, replay, full


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--font", required=True, help="path to a monospace TTF or TTC")
    args = parser.parse_args()
    out = subprocess.run(COMMAND, cwd=ROOT, capture_output=True, text=True, check=True).stdout.rstrip("\n").split("\n")
    font = ImageFont.truetype(args.font, SIZE)
    today = datetime.datetime.now(datetime.timezone.utc)
    stamp = f"{today.day} {today:%b %Y}"
    height = PAD + LINE * (len(out) + 5) + PAD * 2

    def frame(shown):
        image = Image.new("RGB", (WIDTH, height), BG)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((1, 1, WIDTH - 2, height - 2), radius=14, outline=RULE, width=2)
        draw.text((PAD, 20), "HyperGrok  /  Opening Bell", font=font, fill=MINT)
        draw.text((PAD, 51), f"Recorded {stamp} · public Hyperliquid data · no keys", font=font, fill=DIM)
        draw.line((PAD, 85, WIDTH - PAD, 85), fill=RULE, width=1)
        draw.text((PAD, 105), "$ " + " ".join(COMMAND), font=font, fill=MINT)
        for index, line in enumerate(out[:shown]):
            draw.text((PAD, 153 + LINE * index), line, font=font, fill=TEXT)
        return image.quantize(colors=32, dither=Image.Dither.NONE)

    frames = [frame(len(out))] + [frame(count) for count in REVEAL] + [frame(len(out))]
    target = os.path.join(ROOT, "assets", "opening-bell.gif")
    frames[0].save(target, save_all=True, append_images=frames[1:], duration=list(DURATIONS), loop=0, optimize=True)
    print(f"wrote {os.path.relpath(target, ROOT)}: {len(frames)} frames, {WIDTH}x{height}; first output line: {out[0]}", file=sys.stderr)
    print(out[1])


if __name__ == "__main__":
    main()
