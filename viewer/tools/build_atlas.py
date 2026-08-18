#!/usr/bin/env python3
"""Build viewer/assets/atlas.{png,json} from FLE's Factorio sprite mirror.

Sources (all Wube-owned Factorio 1.1 art, redistributed by FLE):

  * ``--hfrepo``    a checkout of the HF dataset ``Noddybear/fle_images``
                    (``git clone`` + ``git lfs pull`` for the sheets used
                    here) — the ``__base__/graphics`` tree + ``data.json``.
  * ``--extracted`` FLE 0.3.0's ``EntitySpritesheetExtractor`` output
                    (``fle/agents/data/sprites/extractors/entities.py``, run
                    against ``--hfrepo``): ``{name}_{dir}.png`` centred on the
                    entity centre at 32 px/tile. Everything the extractor
                    handles is taken from there; the few prototypes it skips
                    (electric-mining-drill, pumpjack, storage-tank, the
                    gun-turret head, transport belts per direction, the
                    character, ores, water, trees, ground decoratives) are
                    cut straight from the sheets below with the same
                    "canvas centred on the entity centre" convention.

Output: ``atlas.png`` (RGBA, <= 4096²) and ``atlas.json``::

  {"tile_px": 32,
   "sprites": [{"name": "burner-mining-drill", "dir": "north",
                "x": .., "y": .., "w": .., "h": .., "cx": .., "cy": ..}, ...],
   "by_name": {"burner-mining-drill|north": 0, ...}}

``cx, cy`` is the pixel of the ENTITY CENTRE inside the sprite; the renderer
draws sprite (x - cx, y - cy) at entity centre px (x, y). Ore/terrain tiles
are keyed ``iron-ore|<volume 1..8>-<variant 1..8>``, ``water|<v>``,
``tree|<type>-<variant>``, ``ground|<n>`` (see GROUND_DECORATIVES).

Needs Pillow and ``basisu`` on PATH (``brew install basis_universal``) for the
.basis sheets. Deterministic given the same inputs.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

TILE = 32
MAX_ATLAS = 4096

DIRS = ["north", "east", "south", "west"]
FLE_DIR = {"up": "north", "right": "east", "down": "south", "left": "west"}

# fle_sprites_out files taken as-is (name -> list of (suffix, dir key)).
# "" suffix = single sprite keyed "name|".
EXTRACTED = {
    "burner-mining-drill": DIRS,
    "boiler": DIRS,
    "offshore-pump": DIRS,
    "oil-refinery": DIRS,
    "chemical-plant": DIRS,
    "pipe-to-ground": DIRS,
    "pump": DIRS,
    "burner-inserter": DIRS,
    "inserter": DIRS,
    "fast-inserter": DIRS,
    "long-handed-inserter": DIRS,
    "filter-inserter": DIRS,
    "stack-inserter": DIRS,
    "splitter": DIRS,
    "fast-splitter": DIRS,
    "wooden-chest": [""],
    "iron-chest": [""],
    "steel-chest": [""],
    "stone-furnace": [""],
    "steel-furnace": [""],
    "electric-furnace": [""],
    "small-electric-pole": [""],
    "medium-electric-pole": [""],
    "big-electric-pole": [""],
    "substation": [""],
    "solar-panel": [""],
    "accumulator": [""],
    "assembling-machine-1": [""],
    "assembling-machine-2": [""],
    "assembling-machine-3": [""],
    "radar": [""],
    "gun-turret": [""],  # base; the folded head is composited below
    "steam-engine": ["horizontal", "vertical"],
}
# Files whose FLE name differs from the key we want.
EXTRACTED_RENAMED = {
    ("lab", ""): "lab_0",
}
PIPE_SHAPES = [
    "straight_horizontal", "straight_vertical", "straight_vertical_single",
    "corner_up_right", "corner_up_left", "corner_down_right", "corner_down_left",
    "t_up", "t_down", "t_left", "t_right", "cross",
    "ending_up", "ending_down", "ending_left", "ending_right",
]
WALL_SHAPES = [
    "single", "straight_horizontal", "straight_vertical",
    "corner_left_down", "corner_right_down", "ending_left", "ending_right",
    "t_up",
]
UNDERGROUND = ["underground-belt", "fast-underground-belt"]

# Real Factorio ground decoratives scattered over the (flat) grass fill; the
# HF mirror carries no grass-1 tile sheet, so the ground is a flat
# grass-1-toned fill plus these Wube decorative sprites.
GROUND_DECORATIVES = [
    ("green-carpet-grass", 0), ("green-carpet-grass", 1), ("green-carpet-grass", 2),
    ("green-carpet-grass", 3), ("green-carpet-grass", 4), ("green-carpet-grass", 5),
    ("green-hairy-grass", 0), ("green-hairy-grass", 1), ("green-hairy-grass", 2),
    ("green-small-grass", 0), ("green-small-grass", 1), ("green-small-grass", 2),
    ("green-small-grass", 3), ("green-pita", 0), ("green-asterisk", 0),
    ("green-bush-mini", 0),
]

# Belt sheet rows (Factorio 1.1 belt_animation_set, 0-based). Straight rows
# then the 8 curves named <from>_to_<to> (the belt turns from travelling
# <from>-ward to <to>-ward).
BELT_ROWS = {
    "east": 0, "west": 1, "north": 2, "south": 3,
    "east_to_north": 4, "north_to_east": 5, "west_to_north": 6, "north_to_west": 7,
    "south_to_east": 8, "east_to_south": 9, "south_to_west": 10, "west_to_south": 11,
}


class Basis:
    """Transcodes .basis sheets to RGBA PIL images with basisu (cached)."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-basis-"))
        self.cache: dict[Path, Image.Image] = {}

    def load(self, path: Path, width: int, height: int) -> Image.Image:
        if path in self.cache:
            return self.cache[path]
        out = self.tmp / path.stem
        out.mkdir(exist_ok=True)
        subprocess.run(
            ["basisu", "-unpack", "-no_ktx", "-output_path", str(out), str(path)],
            check=True, capture_output=True,
        )
        pngs = sorted(out.glob("*_unpacked_rgba_RGBA32_0_0000.png")) or \
            sorted(out.glob("*_unpacked_rgb_RGBA32_0_0000.png"))
        if not pngs:
            raise FileNotFoundError(f"basisu produced no RGBA32 png for {path}")
        img = Image.open(pngs[0]).convert("RGBA")
        # basis textures are padded to a power of two; the prototype size is
        # the real sheet size.
        if width and height:
            img = img.crop((0, 0, min(width, img.width), min(height, img.height)))
        self.cache[path] = img
        return img


def centred_canvas(sprite: Image.Image, shift: tuple[float, float]) -> tuple[Image.Image, int, int]:
    """Returns (sprite, cx, cy): entity-centre pixel inside the sprite for a
    Factorio `shift` (tiles, +x east, +y south)."""
    cx = round(sprite.width / 2 - shift[0] * TILE)
    cy = round(sprite.height / 2 - shift[1] * TILE)
    return sprite, cx, cy


def bbox_trim(img: Image.Image, cx: int, cy: int) -> tuple[Image.Image, int, int]:
    box = img.getbbox()
    if not box:
        return img, cx, cy
    x0, y0, x1, y1 = box
    return img.crop(box), cx - x0, cy - y0


class Atlas:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, Image.Image, int, int]] = []
        self.keys: set[str] = set()

    def add(self, name: str, direction: str, img: Image.Image, cx: int, cy: int) -> None:
        key = f"{name}|{direction}"
        if key in self.keys:
            return
        img, cx, cy = bbox_trim(img.convert("RGBA"), cx, cy)
        self.keys.add(key)
        self.items.append((name, direction, img, cx, cy))

    def pack(self) -> tuple[Image.Image, dict]:
        # Shelf packing, tallest first, deterministic.
        order = sorted(range(len(self.items)),
                       key=lambda i: (-self.items[i][2].height, -self.items[i][2].width, i))
        pad = 1
        width = 2048
        for width in (2048, 4096):
            placed = []
            x = y = shelf_h = 0
            for i in order:
                img = self.items[i][2]
                w, h = img.width + pad, img.height + pad
                if x + w > width:
                    x = 0
                    y += shelf_h
                    shelf_h = 0
                placed.append((i, x, y))
                x += w
                shelf_h = max(shelf_h, h)
            height = y + shelf_h
            if height <= width:
                break
        else:
            raise SystemExit(f"atlas does not fit in {MAX_ATLAS}²: needs {width}x{height}")
        height = min(MAX_ATLAS, height)
        sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        sprites = []
        by_name = {}
        for i, px, py in sorted(placed, key=lambda t: t[0]):
            name, direction, img, cx, cy = self.items[i]
            sheet.paste(img, (px, py))
            by_name[f"{name}|{direction}"] = len(sprites)
            sprites.append({"name": name, "dir": direction, "x": px, "y": py,
                            "w": img.width, "h": img.height, "cx": cx, "cy": cy})
        return sheet, {"tile_px": TILE, "sprites": sprites, "by_name": by_name}


def add_extracted(atlas: Atlas, extracted: Path) -> None:
    def take(name: str, direction: str, filename: str) -> None:
        path = extracted / f"{filename}.png"
        if not path.exists():
            print(f"warning: missing extracted sprite {path.name}", file=sys.stderr)
            return
        img = Image.open(path).convert("RGBA")
        atlas.add(name, direction, img, img.width // 2, img.height // 2)

    for name, dirs in EXTRACTED.items():
        for d in dirs:
            take(name, d, f"{name}_{d}" if d else name)
    for (name, d), filename in EXTRACTED_RENAMED.items():
        take(name, d, filename)
    for shape in PIPE_SHAPES:
        take("pipe", shape, f"pipe_{shape}")
    for shape in WALL_SHAPES:
        take("stone-wall", shape, f"stone-wall_{shape}")
    for name in UNDERGROUND:
        for io in ("in", "out"):
            for fle_dir, d in FLE_DIR.items():
                take(name, f"{io}-{d}", f"{name}_{io}_{fle_dir}")


def add_from_sheets(atlas: Atlas, hf: Path, basis: Basis) -> None:
    gfx = hf / "__base__" / "graphics"
    data = json.load(open(hf / "data.json"))["entities"]

    def sheet(spec: dict) -> Image.Image:
        fn = spec["filename"].replace("__base__/graphics/", "")
        path = gfx / fn
        if not path.exists():
            path = path.with_suffix(".basis")
        if path.suffix == ".basis":
            return basis.load(path, 0, 0)
        return Image.open(path).convert("RGBA")

    def frame(spec: dict, col: int = 0, row: int = 0) -> tuple[Image.Image, int, int]:
        img = sheet(spec)
        w, h = spec["width"], spec["height"]
        crop = img.crop((col * w, row * h, col * w + w, row * h + h))
        return centred_canvas(crop, tuple(spec.get("shift", [0, 0])))

    # electric-mining-drill: main animation frame 0 per direction.
    for d in DIRS:
        spec = data["electric-mining-drill"]["graphics_set"]["animation"][d]["layers"][0]
        atlas.add("electric-mining-drill", d, *frame(spec))

    # pumpjack: base sheet (4 directions side by side) + horsehead frame 0.
    base = data["pumpjack"]["base_picture"]["sheets"][0]
    head = data["pumpjack"]["animations"]["north"]["layers"][0]
    for i, d in enumerate(DIRS):
        b, bcx, bcy = frame(base, i, 0)
        h, hcx, hcy = frame(head, 0, 0)
        atlas.add("pumpjack", d, *composite([(b, bcx, bcy), (h, hcx, hcy)]))

    # storage-tank: two frames (north, east).
    spec = data["storage-tank"]["pictures"]["picture"]["sheets"][0]
    atlas.add("storage-tank", "north", *frame(spec, 0, 0))
    atlas.add("storage-tank", "east", *frame(spec, 1, 0))

    # gun-turret: base + folded head (4 directions stacked vertically).
    bspec = data["gun-turret"]["base_picture"]["layers"][0]
    hspec = data["gun-turret"]["folded_animation"]["layers"][0]
    for i, d in enumerate(DIRS):
        b = frame(bspec, 0, 0)
        h = frame(hspec, 0, i)
        atlas.add("gun-turret", d, *composite([b, h]))

    # transport belts: 64x64 frames, 16 columns of animation, 20 rows.
    for belt in ("transport-belt", "fast-transport-belt", "express-transport-belt"):
        spec = data[belt]["belt_animation_set"]["animation_set"]
        for key, row in BELT_ROWS.items():
            atlas.add(belt, key, *frame(spec, 0, row))

    # character: level1_idle.png = 22 frames x 8 directions (N, NE, E, SE,
    # S, SW, W, NW rows), frame 46x58; feet ~ centre + 0.5 tile.
    img = Image.open(gfx / "entity" / "character" / "level1_idle.png").convert("RGBA")
    fw, fh = img.width // 22, img.height // 8
    for row, d in ((0, "north"), (2, "east"), (4, "south"), (6, "west")):
        crop = img.crop((0, row * fh, fw, row * fh + fh))
        atlas.add("character", d, crop, fw // 2, fh // 2 + 8)

    # ores: 8x8 sheet, row 0 = full volume(8) .. row 7 = volume 1, col = variant.
    for ore in ("iron-ore", "copper-ore", "coal", "stone", "uranium-ore"):
        path = gfx / "resources" / ore / f"{ore}.png"
        if not path.exists():
            print(f"warning: missing {path}", file=sys.stderr)
            continue
        img = Image.open(path).convert("RGBA")
        cell = img.width // 8
        for row in range(8):
            for col in range(8):
                crop = img.crop((col * cell, row * cell, col * cell + cell, row * cell + cell))
                atlas.add(ore, f"{8 - row}-{col + 1}", crop, cell // 2, cell // 2)
    path = gfx / "resources" / "crude-oil" / "crude-oil.png"
    if path.exists():
        img = Image.open(path).convert("RGBA")
        cw = img.width // 4
        for col in range(4):
            crop = img.crop((col * cw, 0, col * cw + cw, img.height))
            atlas.add("crude-oil", str(col + 1), crop, cw // 2, img.height // 2)

    # water: 8 variants of 32x32.
    for name in ("water", "deepwater"):
        path = gfx / "terrain" / name / f"{name}1.png"
        if not path.exists():
            continue
        img = Image.open(path).convert("RGBA")
        for col in range(img.width // 32):
            crop = img.crop((col * 32, 0, col * 32 + 32, 32))
            atlas.add(name, str(col), crop, 16, 16)

    # trees: trunk + leaves (first triptych state = full foliage), aligned by
    # centre-x and bottom; the entity position is the trunk base.
    for t in ("01", "02", "03", "04", "05"):
        for v in ("a", "b", "c", "d"):
            tdir = gfx / "resources" / "tree" / t
            trunk_p = tdir / f"tree-{t}-{v}-trunk.png"
            leaves_p = tdir / f"tree-{t}-{v}-leaves.png"
            if not trunk_p.exists() or trunk_p.stat().st_size < 200:
                continue
            trunk = Image.open(trunk_p).convert("RGBA")
            layers = [(trunk, trunk.width // 2, trunk.height - 8)]
            if leaves_p.exists() and leaves_p.stat().st_size > 200:
                leaves = Image.open(leaves_p).convert("RGBA")
                lw = leaves.width // 3 if leaves.width > 2 * leaves.height else leaves.width
                leaves = leaves.crop((0, 0, lw, leaves.height))
                layers.append((leaves, leaves.width // 2, leaves.height - 8))
            atlas.add("tree", f"{t}-{v}", *composite(layers))

    # ground decoratives.
    for n, (name, idx) in enumerate(GROUND_DECORATIVES):
        path = gfx / "decorative" / name / f"{name}-{idx:02d}.png"
        if not path.exists() or path.stat().st_size < 200:
            continue
        img = Image.open(path).convert("RGBA")
        atlas.add("ground", str(n), img, img.width // 2, img.height // 2)


def composite(layers: list[tuple[Image.Image, int, int]]) -> tuple[Image.Image, int, int]:
    """Stacks layers so their (cx, cy) anchors coincide."""
    left = max(cx for _, cx, _ in layers)
    top = max(cy for _, _, cy in layers)
    right = max(img.width - cx for img, cx, _ in layers)
    bottom = max(img.height - cy for img, _, cy in layers)
    out = Image.new("RGBA", (left + right, top + bottom), (0, 0, 0, 0))
    for img, cx, cy in layers:
        out.alpha_composite(img, (left - cx, top - cy))
    return out, left, top


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hfrepo", required=True, type=Path)
    ap.add_argument("--extracted", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "assets")
    args = ap.parse_args()

    atlas = Atlas()
    add_extracted(atlas, args.extracted)
    add_from_sheets(atlas, args.hfrepo, Basis())
    sheet, manifest = atlas.pack()
    args.out.mkdir(parents=True, exist_ok=True)
    sheet.save(args.out / "atlas.png", optimize=True)
    with open(args.out / "atlas.json", "w") as f:
        json.dump(manifest, f, separators=(",", ":"), sort_keys=True)
    print(f"atlas: {sheet.width}x{sheet.height}, {len(manifest['sprites'])} sprites, "
          f"{(args.out / 'atlas.png').stat().st_size // 1024} KB png")


if __name__ == "__main__":
    main()
