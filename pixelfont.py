"""Tiny built-in pixel font (5x7 grid) used to print names on the ticket.

Why a built-in font? The ticket's title is pixel/arcade style. Drawing each
pixel as a solid square directly on the full-resolution ticket gives razor-sharp
edges (no blur, no resizing) and no extra font file is needed.
"""
from PIL import Image, ImageDraw

_G = {
 "A": "01110 10001 10001 11111 10001 10001 10001",
 "B": "11110 10001 10001 11110 10001 10001 11110",
 "C": "01110 10001 10000 10000 10000 10001 01110",
 "D": "11110 10001 10001 10001 10001 10001 11110",
 "E": "11111 10000 10000 11110 10000 10000 11111",
 "F": "11111 10000 10000 11110 10000 10000 10000",
 "G": "01110 10001 10000 10111 10001 10001 01111",
 "H": "10001 10001 10001 11111 10001 10001 10001",
 "I": "01110 00100 00100 00100 00100 00100 01110",
 "J": "00111 00010 00010 00010 00010 10010 01100",
 "K": "10001 10010 10100 11000 10100 10010 10001",
 "L": "10000 10000 10000 10000 10000 10000 11111",
 "M": "10001 11011 10101 10101 10001 10001 10001",
 "N": "10001 11001 10101 10011 10001 10001 10001",
 "O": "01110 10001 10001 10001 10001 10001 01110",
 "P": "11110 10001 10001 11110 10000 10000 10000",
 "Q": "01110 10001 10001 10001 10101 10010 01101",
 "R": "11110 10001 10001 11110 10100 10010 10001",
 "S": "01111 10000 10000 01110 00001 00001 11110",
 "T": "11111 00100 00100 00100 00100 00100 00100",
 "U": "10001 10001 10001 10001 10001 10001 01110",
 "V": "10001 10001 10001 10001 10001 01010 00100",
 "W": "10001 10001 10001 10101 10101 11011 10001",
 "X": "10001 10001 01010 00100 01010 10001 10001",
 "Y": "10001 10001 01010 00100 00100 00100 00100",
 "Z": "11111 00001 00010 00100 01000 10000 11111",
 "0": "01110 10001 10011 10101 11001 10001 01110",
 "1": "00100 01100 00100 00100 00100 00100 01110",
 "2": "01110 10001 00001 00010 00100 01000 11111",
 "3": "11110 00001 00001 01110 00001 00001 11110",
 "4": "00010 00110 01010 10010 11111 00010 00010",
 "5": "11111 10000 11110 00001 00001 10001 01110",
 "6": "00110 01000 10000 11110 10001 10001 01110",
 "7": "11111 00001 00010 00100 01000 01000 01000",
 "8": "01110 10001 10001 01110 10001 10001 01110",
 "9": "01110 10001 10001 01111 00001 00010 01100",
 ".": "00000 00000 00000 00000 00000 01100 01100",
 "-": "00000 00000 00000 11111 00000 00000 00000",
 "'": "00100 00100 01000 00000 00000 00000 00000",
 ",": "00000 00000 00000 00000 01100 00100 01000",
 ":": "00000 01100 01100 00000 01100 01100 00000",
 "/": "00001 00001 00010 00100 01000 10000 10000",
 "&": "01100 10010 10100 01000 10101 10010 01101",
 "#": "01010 01010 11111 01010 11111 01010 01010",
 " ": "00000 00000 00000 00000 00000 00000 00000",
}
SUPPORTED = set(_G)
ROWS = 7


def _rows(ch, bold):
    pattern = _G.get(ch.upper(), _G[" "]).split()
    rows = []
    for p in pattern:
        bits = [c == "1" for c in p]
        if bold:  # make strokes 1 pixel thicker, like the chunky title font
            bits = bits + [False]
            bits = [bits[i] or (i > 0 and bits[i - 1]) for i in range(len(bits))]
        rows.append(bits)
    return rows


def text_size(text, scale, bold=True, spacing=1):
    cols = 6 if bold else 5
    n = len(text)
    width = (n * (cols + spacing) - spacing) * scale if n else 0
    return width, ROWS * scale


def draw_text(img, xy, text, scale, fill, bold=True, spacing=1, shadow=None, anchor="la"):
    """Draw pixel text onto `img` (RGB or RGBA) at full resolution.

    anchor: 'la' = x,y is top-left; 'ra' = x is the right edge; 'ma' = x is centre.
    shadow: optional (colour, offset_in_pixel_units) drawn first for legibility.
    Every pixel is a hard-edged square, so the result is always perfectly sharp.
    """
    w, h = text_size(text, scale, bold, spacing)
    x, y = xy
    if anchor[0] == "r":
        x -= w
    elif anchor[0] == "m":
        x -= w // 2
    d = ImageDraw.Draw(img)
    cols = 6 if bold else 5

    def paint(ox, oy, colour):
        cx = x + ox
        for ch in text:
            for ry, row in enumerate(_rows(ch, bold)):
                for rx, on in enumerate(row[:cols]):
                    if on:
                        px, py = cx + rx * scale, y + oy + ry * scale
                        d.rectangle((px, py, px + scale - 1, py + scale - 1), fill=colour)
            cx += (cols + spacing) * scale

    if shadow:
        colour, off = shadow
        paint(off * scale, off * scale, colour)
    paint(0, 0, fill)
    return w, h
