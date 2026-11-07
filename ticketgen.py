"""Builds the personalised ticket image and the unique QR code.

The original artwork (top 517px) is NEVER altered except for the attendee's name,
which is drawn in the upper-right area at the original resolution.
A QR "stub" is added underneath so the artwork itself stays untouched.
"""
from pathlib import Path
from PIL import Image, ImageDraw
from reportlab.graphics.barcode import qrencoder

import pixelfont as pf

ROOT = Path(__file__).resolve().parent
ART = ROOT / "static" / "ticket-original.jpg"

# ---- Name position (upper-right of the photo area, on the same line as "PARTY") ----
NAME_RIGHT_X = 1236     # right edge of the name
NAME_CENTER_Y = 56      # vertical centre of the name
NAME_MAX_WIDTH = 480    # longest allowed width in pixels
NAME_COLOUR = (240, 228, 226)   # soft cream, same family as the artwork's text
SHADOW = (0, 0, 0)

STUB_H = 262
PINK = (214, 194, 187)
DARK = (21, 21, 21)
INK = (58, 40, 40)


def make_qr_matrix(data, ec="Q"):
    level = {"L": qrencoder.QRErrorCorrectLevel.L, "M": qrencoder.QRErrorCorrectLevel.M,
             "Q": qrencoder.QRErrorCorrectLevel.Q, "H": qrencoder.QRErrorCorrectLevel.H}[ec]
    last = None
    for version in range(1, 41):
        try:
            q = qrencoder.QRCode(version, level)
            q.addData(data)
            q.make()
            n = q.getModuleCount()
            return [[bool(q.isDark(r, c)) for c in range(n)] for r in range(n)]
        except Exception as e:  # data too long for this version -> try bigger
            last = e
    raise ValueError(f"Cannot build QR: {last}")


def qr_image(data, box=6, border=4):
    m = make_qr_matrix(data)
    n = len(m)
    size = (n + 2 * border) * box
    img = Image.new("RGB", (size, size), "white")
    d = ImageDraw.Draw(img)
    for r in range(n):
        for c in range(n):
            if m[r][c]:
                x, y = (c + border) * box, (r + border) * box
                d.rectangle((x, y, x + box - 1, y + box - 1), fill="black")
    return img


def fit_name(name):
    """Pick the biggest pixel scale (3 -> 2) so the name fits; returns (text, scale)."""
    text = " ".join(name.upper().split())
    for scale in (3, 2):
        w, _ = pf.text_size(text, scale)
        if w <= NAME_MAX_WIDTH:
            return text, scale
    # Extremely long: shorten middle names to initials, then hard-trim.
    parts = text.split()
    if len(parts) > 2:
        text = " ".join([parts[0]] + [p[0] + "." for p in parts[1:-1]] + [parts[-1]])
    while pf.text_size(text, 2)[0] > NAME_MAX_WIDTH and len(text) > 3:
        text = text[:-1]
    return text.strip(), 2


def make_ticket_png(full_name, ticket_ref, token, out_path, qr_path=None):
    art = Image.open(ART).convert("RGB")
    W, H = art.size
    # --- name on the original artwork (full resolution, hard pixel edges) ---
    text, scale = fit_name(full_name)
    _, th = pf.text_size(text, scale)
    pf.draw_text(art, (NAME_RIGHT_X, NAME_CENTER_Y - th // 2), text, scale, NAME_COLOUR,
                 shadow=(SHADOW, 1), anchor="ra")

    # --- QR stub below the artwork ---
    canvas = Image.new("RGB", (W, H + STUB_H), DARK)
    canvas.paste(art, (0, 0))
    d = ImageDraw.Draw(canvas)
    split_x = 1280
    d.rectangle((split_x, H, W, H + STUB_H), fill=PINK)
    for x in range(10, W - 10, 18):   # dashed tear line
        d.rectangle((x, H, x + 8, H + 1), fill=(235, 225, 222) if x < split_x else (120, 90, 90))

    qr = qr_image(token, box=6, border=4)
    if qr.width > STUB_H - 24:
        qr = qr_image(token, box=5, border=3)
    qy = H + (STUB_H - qr.height) // 2 + 2
    canvas.paste(qr, (36, qy))
    tx = 36 + qr.width + 36
    pf.draw_text(canvas, (tx, H + 34), "ADMIT ONE", 5, (255, 255, 255))
    pf.draw_text(canvas, (tx, H + 84), text, scale if scale == 3 else 2, NAME_COLOUR)
    pf.draw_text(canvas, (tx, H + 126), "4 DEC 2026  2PM-10PM", 3, (214, 194, 187))
    pf.draw_text(canvas, (tx, H + 156), "SUNDARAM, TIKONIYA", 3, (214, 194, 187))
    pf.draw_text(canvas, (tx, H + 196), "SCAN AT ENTRY. VALID FOR ONE ENTRY ONLY.", 2, (170, 160, 160))
    pf.draw_text(canvas, (tx, H + 216), "DO NOT SHARE THIS IMAGE PUBLICLY.", 2, (170, 160, 160))
    # pink panel: ticket reference
    pf.draw_text(canvas, (split_x + 30, H + 70), "TICKET NO.", 3, INK)
    pf.draw_text(canvas, (split_x + 30, H + 104), ticket_ref, 3, INK, bold=True)
    canvas.save(out_path, format="PNG", optimize=True)
    if qr_path:
        qr_image(token, box=8, border=4).save(qr_path, format="PNG")
    return text, scale
