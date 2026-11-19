#!/usr/bin/env python3
"""Draw the app icons (icon-3-*.png): the page's equaliser bars on tomato. Needs Pillow; run on a
dev box, not the Pi:  python3 make_icons.py static/"""
import sys
from PIL import Image, ImageDraw

TOMATO = (239, 90, 71, 255)
BARS = [((255, 194, 61), 0.55), ((255, 255, 255), 0.95), ((71, 179, 234), 0.70),
        ((76, 194, 141), 0.40), ((255, 255, 255), 0.80)]   # colour, height (fraction of the art box)


def icon(size, maskable, out):
    S = size * 4
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if maskable:
        d.rectangle([0, 0, S, S], fill=TOMATO)
    else:
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=TOMATO)
    box = S * (0.50 if maskable else 0.62)          # maskable art stays inside the safe circle
    n = len(BARS)
    w = box / (n * 1.6 - 0.6)
    gap = w * 0.6
    x0 = (S - box) / 2
    bottom = (S + box * 0.80) / 2
    for i, (col, h) in enumerate(BARS):
        x = x0 + i * (w + gap)
        top = bottom - box * 0.80 * h
        d.rounded_rectangle([x, top, x + w, bottom], radius=w / 2, fill=col + (255,))
    im.resize((size, size), Image.LANCZOS).save(out)


o = sys.argv[1] if len(sys.argv) > 1 else "."
icon(192, False, f"{o}/icon-3-192.png")
icon(512, False, f"{o}/icon-3-512.png")
icon(512, True, f"{o}/icon-3-maskable-512.png")
