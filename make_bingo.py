"""Generate printable birthday bingo postcards (4.25" x 5.5") from a CSV of hints.

Usage:
    python make_bingo.py hints.csv -n 30
    python make_bingo.py hints.csv -n 30 --sheet letter   # 4 cards per 8.5x11 sheet

Every card gets its own random arrangement of hints. The PDF alternates
front / back, so print it double-sided (flip on long edge).
"""

import argparse
import csv
import random
import re
import sys
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

# --------------------------------------------------------------------------
# Content you might want to tweak
# --------------------------------------------------------------------------

TITLE = "Alex’s Birthday Bingo"
HOST = "Alex"

HOW_TO_PLAY = (
    "Find someone who matches a square, hear the story, and get their "
    "initials. Five in a row (any direction) wins."
)

RULES = [
    ("No Work Talk Until 9:30",
     "Don't ask what people study, where they work, or how the job hunt is "
     "going. Ask about their worst decisions, weird hobbies, or best stories "
     "instead. Work talk is back on at 9:30."),
    ("Get the Story",
     "No grabbing initials and running. Before someone signs a square, you "
     "have to hear the story behind it. The hint just gets the conversation "
     "started."),
    ("One Square Per Person",
     "Each person can only sign your card once, so you can't camp out next to "
     "one friend. Go talk to people you don't know yet."),
    ("No Signing Your Own Card",
     "If one of the squares is you, too bad, you still can't sign it. Send "
     "people your way instead."),
    ("Earn the Free Space",
     f"The middle square isn't free. Find {HOST}, have a drink or a toast "
     "with him, and get him to initial it."),
]

# --------------------------------------------------------------------------
# Look & feel
# --------------------------------------------------------------------------

CARD_W, CARD_H = 4.25 * inch, 5.5 * inch
GRID = 5

INK = HexColor("#2B2140")
MUTED = HexColor("#7A6F8F")
PAPER = HexColor("#FFFDF8")
COLUMN_COLORS = [  # one per B-I-N-G-O column
    HexColor("#FF5A5F"),  # coral
    HexColor("#FF9F1C"),  # orange
    HexColor("#2EC4B6"),  # teal
    HexColor("#4D7CFE"),  # blue
    HexColor("#9B5DE5"),  # purple
]
COLUMN_TINTS = [
    HexColor("#FFE9EA"),
    HexColor("#FFF1DE"),
    HexColor("#DFF7F5"),
    HexColor("#E6EDFF"),
    HexColor("#F1E8FC"),
]
GOLD = HexColor("#FFC93C")
GOLD_TINT = HexColor("#FFF5D6")

FONT_DIRS = [Path("fonts"), Path("C:/Windows/Fonts"), Path("/Library/Fonts"),
             Path("/usr/share/fonts/truetype")]
FONT_CHOICES = {
    # role: (candidate files..., fallback built-in font)
    "display": (["seguibl.ttf", "segoeuib.ttf", "arialbd.ttf"], "Helvetica-Bold"),
    "bold": (["segoeuib.ttf", "arialbd.ttf"], "Helvetica-Bold"),
    "body": (["segoeui.ttf", "arial.ttf"], "Helvetica"),
    "script": (["segoeprb.ttf", "segoepr.ttf"], "Helvetica-BoldOblique"),
}
FONTS = {}


def load_fonts():
    for role, (files, fallback) in FONT_CHOICES.items():
        FONTS[role] = fallback
        for name in files:
            path = next((d / name for d in FONT_DIRS if (d / name).exists()), None)
            if path:
                font_name = f"Bingo-{role}"
                pdfmetrics.registerFont(TTFont(font_name, str(path)))
                FONTS[role] = font_name
                break


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------

HINT_HEADERS = ("hint", "hints", "clue", "clues", "fact", "fun fact")


def read_hints(csv_path, column=None):
    """Read hints from a CSV.

    Column choice: `column` (header name or 1-based number) if given, else a
    column headed 'hint'/'clue'/etc., else if there are several columns the
    one with the longest text on average (so a "Name, fun fact" export picks
    the fun facts), else the only column. Blank cells are skipped.
    """
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if any(cell.strip() for cell in r)]
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    width = max(len(r) for r in rows)

    col, has_header = None, False
    if column is not None:
        if column.isdigit():
            col = int(column) - 1
            has_header = any(h in HINT_HEADERS or h == "name" for h in header)
        elif column.strip().lower() in header:
            col, has_header = header.index(column.strip().lower()), True
        else:
            sys.exit(f"No column named {column!r} in {csv_path}. Columns: {rows[0]}")
    else:
        for key in HINT_HEADERS:
            if key in header:
                col, has_header = header.index(key), True
                break
    if col is None:
        if width == 1:
            col = 0
        else:
            def avg_len(i):
                cells = [r[i] for r in rows[1:] if len(r) > i]
                return sum(len(c) for c in cells) / max(1, len(cells))
            col = max(range(width), key=avg_len)
            has_header = True  # multi-column exports basically always have one

    data = rows[1:] if has_header else rows
    hints = [tidy(r[col]) for r in data if len(r) > col and r[col].strip()]
    return list(dict.fromkeys(hints))  # drop exact duplicates, keep order


def tidy(text):
    """Light cleanup for typed-in answers: trim, capitalize the first letter,
    and turn a standalone lowercase 'i' (i, i'm, i've...) into 'I'."""
    text = " ".join(text.split())
    text = re.sub(r"\bi(?=$|[\s'’,.!?])", "I", text)
    return text[:1].upper() + text[1:]


def make_boards(tiers, n, rng):
    """Each board holds 24 hints plus the free space in the middle.

    `tiers` is a list of hint lists in priority order. Each card takes as many
    hints as it can from the first tier (all of them, if they fit), then fills
    the remaining squares from the next tier, and so on. Positions get shuffled.
    """
    per_board = GRID * GRID - 1
    boards, seen = [], set()
    for _ in range(n):
        for _attempt in range(100):
            picks = []
            for tier in tiers:
                pool = [h for h in tier if h not in picks]
                take = min(per_board - len(picks), len(pool))
                picks += rng.sample(pool, take)
                if len(picks) == per_board:
                    break
            rng.shuffle(picks)
            if tuple(picks) not in seen:
                break
        seen.add(tuple(picks))
        mid = per_board // 2
        boards.append(picks[:mid] + [None] + picks[mid:])
    return boards


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------

def fit_text(text, font, max_w, max_h, start=9.5, minimum=5.0, leading=1.15):
    """Largest font size (and its wrapped lines) that fits inside the box."""
    size = start
    while size >= minimum:
        lines = simpleSplit(text, font, size, max_w)
        if len(lines) * size * leading <= max_h and all(
                pdfmetrics.stringWidth(l, font, size) <= max_w for l in lines):
            return size, lines
        size -= 0.25
    return minimum, simpleSplit(text, font, minimum, max_w)


def draw_confetti(c, x, y, w, h, rng, count=26, avoid=None):
    """Sprinkle little dots and dashes across a band."""
    palette = COLUMN_COLORS + [GOLD]
    for _ in range(count):
        cx, cy = x + rng.random() * w, y + rng.random() * h
        if avoid and avoid[0] < cx < avoid[2] and avoid[1] < cy < avoid[3]:
            continue
        c.setFillColor(rng.choice(palette))
        c.setStrokeColor(rng.choice(palette))
        kind = rng.random()
        if kind < 0.5:
            c.circle(cx, cy, 1.1 + rng.random() * 1.4, stroke=0, fill=1)
        else:
            c.saveState()
            c.translate(cx, cy)
            c.rotate(rng.random() * 180)
            c.setLineWidth(1.4)
            c.setLineCap(1)
            c.line(-2.6, 0, 2.6, 0)
            c.restoreState()


def draw_star(c, cx, cy, r_out, r_in, points=5):
    import math
    p = c.beginPath()
    for i in range(points * 2):
        r = r_out if i % 2 == 0 else r_in
        a = math.pi / 2 + i * math.pi / points
        px, py = cx + r * math.cos(a), cy + r * math.sin(a)
        p.moveTo(px, py) if i == 0 else p.lineTo(px, py)
    p.close()
    c.drawPath(p, stroke=0, fill=1)


def draw_background(c, ox, oy):
    c.setFillColor(PAPER)
    c.rect(ox, oy, CARD_W, CARD_H, stroke=0, fill=1)
    # rainbow stripe along the top and bottom edges
    stripe_w = CARD_W / len(COLUMN_COLORS)
    for i, col in enumerate(COLUMN_COLORS):
        c.setFillColor(col)
        c.rect(ox + i * stripe_w, oy + CARD_H - 6, stripe_w + 0.5, 6, stroke=0, fill=1)
        c.rect(ox + i * stripe_w, oy, stripe_w + 0.5, 4, stroke=0, fill=1)


# --------------------------------------------------------------------------
# Card faces
# --------------------------------------------------------------------------

def draw_front(c, ox, oy, board, card_no, rng):
    draw_background(c, ox, oy)
    margin = 0.18 * inch

    # --- header ---------------------------------------------------------
    title_y = oy + CARD_H - 0.52 * inch
    title_size = 21
    while pdfmetrics.stringWidth(TITLE, FONTS["display"], title_size) > CARD_W - 2 * margin - 10:
        title_size -= 0.5
    tw = pdfmetrics.stringWidth(TITLE, FONTS["display"], title_size)
    draw_confetti(c, ox + margin, title_y - 6, CARD_W - 2 * margin, 30, rng,
                  avoid=(ox + (CARD_W - tw) / 2 - 4, title_y - 6,
                         ox + (CARD_W + tw) / 2 + 4, title_y + title_size))
    c.setFillColor(INK)
    c.setFont(FONTS["display"], title_size)
    c.drawCentredString(ox + CARD_W / 2, title_y, TITLE)

    # name line + card number
    line_y = title_y - 0.24 * inch
    c.setFont(FONTS["bold"], 7.5)
    c.setFillColor(MUTED)
    c.drawString(ox + margin + 2, line_y, "NAME")
    c.setStrokeColor(MUTED)
    c.setLineWidth(0.6)
    c.line(ox + margin + 28, line_y - 1.5, ox + CARD_W - margin - 46, line_y - 1.5)
    c.drawRightString(ox + CARD_W - margin - 2, line_y, f"CARD #{card_no:02d}")

    # --- grid -----------------------------------------------------------
    grid_w = CARD_W - 2 * margin
    cell_w = grid_w / GRID
    letters_h = 0.3 * inch
    grid_top = line_y - 0.12 * inch
    grid_bottom = oy + 0.2 * inch
    cell_h = (grid_top - letters_h - grid_bottom) / GRID
    gx = ox + margin
    gap = 2.2  # spacing between cells

    # B I N G O tabs
    for col, ch in enumerate("BINGO"):
        x = gx + col * cell_w + gap / 2
        y = grid_top - letters_h
        c.setFillColor(COLUMN_COLORS[col])
        c.roundRect(x, y + gap / 2, cell_w - gap, letters_h - gap, 4, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont(FONTS["display"], 15)
        c.drawCentredString(x + (cell_w - gap) / 2, y + letters_h / 2 - 5, ch)

    # squares
    pad = 3.5
    initials_h = 8  # room at the bottom of each square for a signature
    for idx, hint in enumerate(board):
        row, col = divmod(idx, GRID)
        x = gx + col * cell_w + gap / 2
        y = grid_top - letters_h - (row + 1) * cell_h + gap / 2
        w, h = cell_w - gap, cell_h - gap

        if hint is None:  # free space
            c.setFillColor(GOLD_TINT)
            c.setStrokeColor(GOLD)
            c.setLineWidth(1.4)
            c.roundRect(x, y, w, h, 5, stroke=1, fill=1)
            c.setFillColor(GOLD)
            draw_star(c, x + w / 2, y + h / 2 + 7, 11, 4.6)
            c.setFillColor(INK)
            c.setFont(FONTS["script"], 8.5)
            c.drawCentredString(x + w / 2, y + h / 2 - 9, "Toast w/")
            c.drawCentredString(x + w / 2, y + h / 2 - 18, HOST + "!")
            continue

        c.setFillColor(COLUMN_TINTS[col] if (row + col) % 2 == 0 else white)
        c.setStrokeColor(COLUMN_COLORS[col])
        c.setLineWidth(0.9)
        c.roundRect(x, y, w, h, 5, stroke=1, fill=1)

        # signature line
        c.setStrokeColor(COLUMN_COLORS[col])
        c.setLineWidth(0.4)
        c.setDash(1, 1.5)
        c.line(x + 7, y + 5, x + w - 7, y + 5)
        c.setDash()

        # hint text, vertically centered in the space above the line
        avail_h = h - 2 * pad - initials_h
        size, lines = fit_text(hint, FONTS["body"], w - 2 * pad, avail_h)
        lead = size * 1.15
        block_h = len(lines) * lead
        top = y + initials_h + pad + avail_h / 2 + block_h / 2 - size * 0.85
        c.setFillColor(INK)
        c.setFont(FONTS["body"], size)
        for i, line in enumerate(lines):
            c.drawCentredString(x + w / 2, top - i * lead, line)


def draw_back(c, ox, oy, rng):
    draw_background(c, ox, oy)
    margin = 0.3 * inch
    inner_w = CARD_W - 2 * margin

    # heading
    y = oy + CARD_H - 0.55 * inch
    c.setFillColor(INK)
    c.setFont(FONTS["display"], 19)
    c.drawCentredString(ox + CARD_W / 2, y, "Rules of Engagement")
    draw_confetti(c, ox + margin, y - 4, inner_w, 26, rng, count=14,
                  avoid=(ox + margin + 10, y - 6, ox + CARD_W - margin - 10, y + 20))

    # how to play
    y -= 0.26 * inch
    c.setFillColor(MUTED)
    size, lines = fit_text(HOW_TO_PLAY, FONTS["body"], inner_w, 30, start=8)
    c.setFont(FONTS["body"], size)
    for line in lines:
        c.drawCentredString(ox + CARD_W / 2, y, line)
        y -= size * 1.25

    # numbered rules: pick one font size so they all fit in the remaining space
    y -= 6
    bottom = oy + 0.42 * inch
    badge = 15
    text_x = ox + margin + badge + 7
    text_w = ox + CARD_W - margin - text_x
    rule_gap = 7

    def layout(size):
        total, blocks = 0, []
        for title, body in RULES:
            body_lines = simpleSplit(body, FONTS["body"], size, text_w)
            block = size + 3 + len(body_lines) * size * 1.22
            blocks.append((title, body_lines, block))
            total += block + rule_gap
        return total - rule_gap, blocks

    size = 10
    while size > 5.5:
        total, blocks = layout(size)
        if total <= y - bottom:
            break
        size -= 0.25
    # spread any leftover space between the rules
    rule_gap += min(14, (y - bottom - total) / max(1, len(RULES) - 1))

    for i, (title, body_lines, block) in enumerate(blocks):
        color = COLUMN_COLORS[i % len(COLUMN_COLORS)]
        c.setFillColor(color)
        c.circle(ox + margin + badge / 2, y - size / 2 + 1, badge / 2, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont(FONTS["display"], 9)
        c.drawCentredString(ox + margin + badge / 2, y - size / 2 - 2, str(i + 1))

        c.setFillColor(color)
        c.setFont(FONTS["bold"], size + 1)
        c.drawString(text_x, y - size + 1, title)
        ty = y - size - 3 - size * 1.05
        c.setFillColor(INK)
        c.setFont(FONTS["body"], size)
        for line in body_lines:
            c.drawString(text_x, ty, line)
            ty -= size * 1.22
        y -= block + rule_gap

    # sign-off
    c.setFillColor(MUTED)
    c.setFont(FONTS["script"], 8)
    c.drawCentredString(ox + CARD_W / 2, oy + 0.14 * inch, "thanks for coming!")


# --------------------------------------------------------------------------
# Page layout
# --------------------------------------------------------------------------

def crop_marks(c, sheet_w, sheet_h, xs, ys):
    c.setStrokeColor(HexColor("#BBBBBB"))
    c.setLineWidth(0.3)
    for x in xs:
        c.line(x, 0, x, 10)
        c.line(x, sheet_h - 10, x, sheet_h)
    for y in ys:
        c.line(0, y, 10, y)
        c.line(sheet_w - 10, y, sheet_w, y)


def build_pdf(boards, out_path, sheet, backs, seed):
    deco_rng = random.Random(seed)
    if sheet == "postcard":
        c = canvas.Canvas(str(out_path), pagesize=(CARD_W, CARD_H))
        for i, board in enumerate(boards, 1):
            draw_front(c, 0, 0, board, i, deco_rng)
            c.showPage()
            if backs:
                draw_back(c, 0, 0, deco_rng)
                c.showPage()
    else:  # 4-up on US letter, 4.25x5.5 is exactly a quarter sheet
        sw, sh = letter
        slots = [(0, CARD_H), (CARD_W, CARD_H), (0, 0), (CARD_W, 0)]
        c = canvas.Canvas(str(out_path), pagesize=letter)
        for start in range(0, len(boards), 4):
            chunk = boards[start:start + 4]
            for j, board in enumerate(chunk):
                draw_front(c, *slots[j], board, start + j + 1, deco_rng)
            crop_marks(c, sw, sh, [CARD_W], [CARD_H])
            c.showPage()
            if backs:
                # long-edge flip mirrors left/right, so back slots swap columns
                back_slots = [(CARD_W, CARD_H), (0, CARD_H), (CARD_W, 0), (0, 0)]
                for j in range(len(chunk)):
                    draw_back(c, *back_slots[j], deco_rng)
                crop_marks(c, sw, sh, [CARD_W], [CARD_H])
                c.showPage()
    c.setTitle(TITLE)
    c.setAuthor(HOST)
    c.save()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+",
                    help="one or more hint CSVs in priority order. Hints from the "
                         "first file are used first; later files fill leftover squares")
    ap.add_argument("--column",
                    help="which column holds the hints (header name or 1-based "
                         "number). Default: auto-detect")
    ap.add_argument("-n", "--num", type=int, default=20, help="number of cards (default 20)")
    ap.add_argument("-o", "--out", default="bingo_cards.pdf", help="output PDF path")
    ap.add_argument("--sheet", choices=["postcard", "letter"], default="postcard",
                    help="postcard: one 4.25x5.5 card per page (default); "
                         "letter: four cards per 8.5x11 sheet with cut marks")
    ap.add_argument("--no-backs", action="store_true", help="skip the rules side")
    ap.add_argument("--seed", type=int, help="random seed, to regenerate the same cards")
    args = ap.parse_args()

    tiers, seen = [], set()
    for path in args.csv:
        tier = [h for h in read_hints(path, args.column) if h not in seen]
        seen.update(tier)
        tiers.append(tier)
        print(f"  {path}: {len(tier)} hints")
    total = sum(len(t) for t in tiers)
    need = GRID * GRID - 1
    if total < need:
        sys.exit(f"Need at least {need} unique hints in total, found {total}. "
                 f"Add more, or pass a second CSV to fill the gaps, e.g.\n"
                 f"  python make_bingo.py {args.csv[0]} hints.csv")
    if args.num < 1:
        sys.exit("--num must be at least 1")

    load_fonts()
    rng = random.Random(args.seed)
    boards = make_boards(tiers, args.num, rng)
    build_pdf(boards, args.out, args.sheet, not args.no_backs, args.seed)

    pages = "" if args.no_backs else " (front + back)"
    print(f"Wrote {args.num} cards{pages} from {total} hints -> {args.out}")


if __name__ == "__main__":
    main()
