"""Листы миниатюр кадров для разметки глазами: python contact_sheet.py 23 [42 ...] -> out/gold/sheets/p23_01.jpg"""
import sys
from PIL import Image, ImageDraw
from common import OUT, read_jsonl

W, H, COLS, ROWS = 400, 225, 4, 4
fr = read_jsonl(OUT / "frames.jsonl")
dst = OUT / "gold" / "sheets"
dst.mkdir(parents=True, exist_ok=True)
for p in map(int, sys.argv[1:]):
    items = [(i, f) for i, f in enumerate(fr) if f["project_id"] == p]
    for s in range(0, len(items), COLS * ROWS):
        sheet = Image.new("RGB", (W * COLS, (H + 18) * ROWS), "white")
        d = ImageDraw.Draw(sheet)
        for k, (i, f) in enumerate(items[s:s + COLS * ROWS]):
            x, y = (k % COLS) * W, (k // COLS) * (H + 18)
            with Image.open(f["jpg"]) as im:
                im.thumbnail((W - 4, H))
                sheet.paste(im.convert("RGB"), (x + 2, y + 18))
            d.text((x + 4, y + 2), f"#{i}", fill="red")
        out = dst / f"p{p}_{s // (COLS * ROWS) + 1:02d}.jpg"
        sheet.save(out, quality=80)
        print(out)
