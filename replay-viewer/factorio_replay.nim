## cogame-factorio static replay renderer (Nim -> wasm).
##
## Renders one replay step as a Bitworld sprite-protocol v1 packet
## (bitworld/spriteprotocol) that coworld-ctf's client/broadcast_core.js
## draws unchanged: a zoomable map layer holding the baked terrain bands
## (object ids 40.., z = -32768 — the client's static-band cache) plus one
## object per entity / belt tile / pipe tile / character, all drawn from
## Factorio's own sprite sheets (viewer/assets/atlas.png, see
## viewer/tools/build_atlas.py). The page owns playback (which seat, which
## step) and tells this module via the text-command channel (`0x81`):
## `s:<step>` and `v:<seat>`. Chrome JSON rides the reserved sprite 4090's
## label, as in ctf.
##
## Export surface (same shape as ctf_replay.nim): factorio_load_replay,
## factorio_frame, factorio_input, factorio_packet_ptr/len,
## factorio_error_ptr/len, factorio_stage_ptr/len.

import
  std/[json, tables, strutils, math, hashes, sets, monotimes, times],
  bitworld/spriteprotocol, pixie

const
  ChromeSpriteId = 4090        ## label carries chrome JSON (ctf convention)
  MapLayerId = 0
  MapLayerType = 0
  ZoomableFlag = 1
  BandObjectBase = 40          ## static terrain bands: ids 40..99 in the client
  MaxBands = 60
  StaticBandZ = -32768
  AtlasSpriteBase = 100        ## wire sprite id = base + atlas index
  GenSpriteBase = 3000         ## generated sprites (outlines, fallback blocks)
  DynObjectBase = 200
  MaxDynObjects = 60000
  CharGlideFrames = 12         ## 0.5 s at the 24 packet/s presentation rate
  CharTrailSteps = 6           ## faint footprints for the last N steps
  MaxBoardPixels = 8_000_000   ## above this at 32 px/tile the board renders at 16
  ## (8 Mpx = 32 MB of terrain RGBA; the client decompresses + blits every
  ## byte of it before the first frame, so this bounds time-to-first-frame:
  ## ~90x90 tiles keep full resolution, bigger bases render at 16 px/tile
  ## from a 2x2 box filter of the same sheets)
  MaxViewerPixels = 24_000_000 ## hard cap at 16 px/tile (~380x250 tiles)
  BoardMarginTiles = 4
  DirNames = ["north", "east", "south", "west"]

type
  Rgba = object
    ## Straight-alpha RGBA pixel buffer (what the wire wants).
    w, h: int
    data: seq[uint8]

  AtlasEntry = object
    name, dir: string
    x, y, w, h, cx, cy: int

  Entity = object
    name: string
    x, y, w, h: float
    dir: int
    status: string

  Belt = object
    x, y: float
    dir: int

  Pipe = object
    x, y: float

  Step = object
    entities: seq[Entity]
    belts: seq[Belt]
    pipes: seq[Pipe]
    hasChar: bool
    charX, charY: float
    stepNo: int

  Seat = object
    name: string
    steps: seq[Step]

  Resource = object
    name: string
    tx, ty, amount: int

  Replay = object
    x0, y0, x1, y1: int          ## map.bounds (tiles, half-open)
    resources: seq[Resource]
    water: seq[(int, int, int)]  ## x, y, run east
    trees: seq[(int, int)]
    seats: seq[Seat]
    names: seq[string]

var
  runtimeLoaded = false
  replay: Replay
  packet: seq[uint8]
  lastError: string
  atlas: Rgba                   ## full-res sheet (32 px/tile)
  atlasHalf: Rgba               ## 2:1 box-filtered sheet (16 px/tile), lazy
  atlasFromHost = false         ## pixels handed in by factorio_set_atlas
  atlasEntries: seq[AtlasEntry]
  atlasByName: Table[string, int]
  atlasSent: seq[bool]
  # board geometry
  tile = 32                     ## px per tile (32, or 16 for oversized boards)
  bx0, by0, bx1, by1: int       ## board bbox in tiles (half-open)
  boardW, boardH: int           ## px
  bandsEmitted = false
  # generated sprites
  genSprites: Table[string, int]
  nextGenSprite = GenSpriteBase
  # object identity (retained mode: same key keeps the same id across frames)
  objectIds: Table[string, int]
  freeObjectIds: seq[int]
  nextObjectId = DynObjectBase
  liveObjects: HashSet[int]
  # playback selection (owned by the page)
  curSeat = 0
  curStep = 0
  dirty = true
  # character presentation: FLE fast mode teleports the character between
  # steps, so a forward step glides it from the previous step's position
  # over CharGlideFrames packets (the page's overlay ring uses the same
  # duration); facing follows the movement; a faint trail marks the last
  # few step positions.
  charFrom: (float, float)
  charTo: (float, float)
  charGlideLeft = 0
  charFacing = 2               ## 0 n, 1 e, 2 s, 3 w (index into DirNames)

## --- Progress stage note (see ctf_replay.nim: survives an ABORTING_MALLOC
## abort so JS can report what the runtime was doing) ---
var
  stageNote: array[192, char]
  stageNoteLen: int
  currentStage: string

var
  profileLine: string
  profileT0: MonoTime
  profileLast: MonoTime

proc stampStage(stage: string) =
  ## Also accumulates a load-time profile (ms spent in the previous stage);
  ## factorio_load_replay echoes it once (-> console.log in the Worker).
  let now = getMonoTime()
  if currentStage.len > 0:
    profileLine.add currentStage & "=" & $((now - profileLast).inMilliseconds) & "ms "
  profileLast = now
  currentStage = stage
  stageNoteLen = min(stage.len, stageNote.len)
  if stageNoteLen > 0:
    copyMem(stageNote[0].addr, stage[0].unsafeAddr, stageNoteLen)

proc bytesFromPointer(data: ptr uint8, length: int): string =
  result = newString(length)
  if length > 0:
    copyMem(result[0].addr, data, length)

# ---------------------------------------------------------------------------
# Pixel buffers

proc newRgba(w, h: int): Rgba =
  Rgba(w: w, h: h, data: newSeq[uint8](w * h * 4))

proc fill(dst: var Rgba, r, g, b, a: uint8) =
  var i = 0
  while i < dst.data.len:
    dst.data[i] = r; dst.data[i + 1] = g; dst.data[i + 2] = b; dst.data[i + 3] = a
    i += 4

proc blit(dst: var Rgba, src: Rgba, sx0, sy0, sw, sh, dx, dy: int) =
  ## Source-over blit of the src rect (sx0, sy0, sw, sh) at (dx, dy).
  for y in 0 ..< sh:
    let ty = dy + y
    if ty < 0 or ty >= dst.h: continue
    let syy = sy0 + y
    for x in 0 ..< sw:
      let tx = dx + x
      if tx < 0 or tx >= dst.w: continue
      let si = (syy * src.w + sx0 + x) * 4
      let sa = int(src.data[si + 3])
      if sa == 0: continue
      let di = (ty * dst.w + tx) * 4
      if sa == 255:
        dst.data[di] = src.data[si]
        dst.data[di + 1] = src.data[si + 1]
        dst.data[di + 2] = src.data[si + 2]
        dst.data[di + 3] = 255
      else:
        let da = int(dst.data[di + 3])
        let outA = sa + da * (255 - sa) div 255
        if outA == 0: continue
        for c in 0 .. 2:
          let sc = int(src.data[si + c])
          let dc = int(dst.data[di + c])
          dst.data[di + c] = uint8((sc * sa + dc * da * (255 - sa) div 255) div outA)
        dst.data[di + 3] = uint8(outA)

proc crop(src: Rgba, x, y, w, h: int): Rgba =
  result = newRgba(w, h)
  for yy in 0 ..< h:
    copyMem(result.data[yy * w * 4].addr, src.data[((y + yy) * src.w + x) * 4].unsafeAddr, w * 4)

proc halfRes(src: Rgba): Rgba =
  ## 2:1 box filter (alpha-weighted colour) of a straight-alpha buffer: the
  ## 16 px/tile sheet, built once per atlas instead of resampling per blit.
  result = newRgba(src.w div 2, src.h div 2)
  for y in 0 ..< result.h:
    for x in 0 ..< result.w:
      var r, g, b, a = 0
      for dy in 0 .. 1:
        for dx in 0 .. 1:
          let si = ((y * 2 + dy) * src.w + x * 2 + dx) * 4
          let pa = int(src.data[si + 3])
          r += int(src.data[si]) * pa
          g += int(src.data[si + 1]) * pa
          b += int(src.data[si + 2]) * pa
          a += pa
      if a == 0: continue
      let di = (y * result.w + x) * 4
      result.data[di] = uint8(r div a)
      result.data[di + 1] = uint8(g div a)
      result.data[di + 2] = uint8(b div a)
      result.data[di + 3] = uint8(a div 4)

proc imageToStraightRgba(image: Image): Rgba =
  ## pixie images are premultiplied RGBX; the wire is straight RGBA.
  result = newRgba(image.width, image.height)
  for i in 0 ..< image.width * image.height:
    let px = image.data[i]
    let a = int(px.a)
    let o = i * 4
    if a == 0:
      continue
    result.data[o] = uint8(min(255, int(px.r) * 255 div a))
    result.data[o + 1] = uint8(min(255, int(px.g) * 255 div a))
    result.data[o + 2] = uint8(min(255, int(px.b) * 255 div a))
    result.data[o + 3] = uint8(a)

# ---------------------------------------------------------------------------
# Atlas

proc loadAtlas() =
  ## Manifest from the preloaded FS; pixels from the host if it decoded
  ## assets/atlas.png natively (factorio_set_atlas — the browser's image
  ## decoder is many times faster than inflating a 4.8 MB PNG in wasm),
  ## else decoded here with pixie (node smoke, native run).
  stampStage("load atlas manifest")
  atlasEntries.setLen(0)
  atlasByName.clear()
  let manifest = parseJson(readFile("assets/atlas.json"))
  for item in manifest["sprites"]:
    atlasEntries.add AtlasEntry(
      name: item["name"].getStr, dir: item["dir"].getStr,
      x: item["x"].getInt, y: item["y"].getInt,
      w: item["w"].getInt, h: item["h"].getInt,
      cx: item["cx"].getInt, cy: item["cy"].getInt)
  for key, idx in manifest["by_name"]:
    atlasByName[key] = idx.getInt
  if not atlasFromHost:
    stampStage("decode atlas png")
    atlas = imageToStraightRgba(decodeImage(readFile("assets/atlas.png")))
    atlasHalf = Rgba()
  atlasSent = newSeq[bool](atlasEntries.len)

proc sheet(): lent Rgba =
  ## The sheet for the current px/tile (half-res built on first use).
  if tile == 16:
    if atlasHalf.w != atlas.w div 2 or atlasHalf.h != atlas.h div 2:
      stampStage("downsample atlas (16 px/tile)")
      atlasHalf = atlas.halfRes()
    return atlasHalf
  atlas

proc atlasIndex(name, dir: string): int =
  ## -1 when the atlas has no such sprite.
  atlasByName.getOrDefault(name & "|" & dir, -1)

proc atlasSpriteId(idx: int): int =
  ## Wire sprite id for an atlas entry, defining it on first use.
  result = AtlasSpriteBase + idx
  if not atlasSent[idx]:
    let e = atlasEntries[idx]
    let s = if tile == 16: 2 else: 1
    let px = sheet().crop(e.x div s, e.y div s, e.w div s, e.h div s)
    packet.addSprite(result, px.w, px.h, px.data, e.name & "|" & e.dir)
    atlasSent[idx] = true

proc genSpriteId(key: string, build: proc (): Rgba): int =
  ## Wire id for a generated sprite, built + defined once per key.
  if key in genSprites:
    return genSprites[key]
  result = nextGenSprite
  inc nextGenSprite
  genSprites[key] = result
  let px = build()
  packet.addSprite(result, px.w, px.h, px.data, key)

# ---------------------------------------------------------------------------
# Replay parsing

proc getF(node: JsonNode): float =
  case node.kind
  of JInt: float(node.getInt)
  of JFloat: node.getFloat
  else: 0.0

proc parseStep(node: JsonNode): Step =
  result.stepNo = node{"step"}.getInt
  for row in node{"entities"}:
    if row.kind != JArray or row.len < 7: continue
    result.entities.add Entity(
      name: row[0].getStr, x: row[1].getF, y: row[2].getF,
      dir: row[3].getInt, status: row[4].getStr,
      w: max(1.0, row[5].getF), h: max(1.0, row[6].getF))
  for row in node{"belts"}:
    if row.kind != JArray or row.len < 3: continue
    result.belts.add Belt(x: row[0].getF, y: row[1].getF, dir: row[2].getInt)
  for row in node{"pipes"}:
    if row.kind != JArray or row.len < 2: continue
    result.pipes.add Pipe(x: row[0].getF, y: row[1].getF)
  let ch = node{"character"}
  if ch != nil and ch.kind == JObject:
    result.hasChar = true
    result.charX = ch{"x"}.getF
    result.charY = ch{"y"}.getF

proc parseReplay(text: string): Replay =
  let doc = parseJson(text)
  if doc{"format"}.getStr != "cogame-factorio-replay":
    raise newException(ValueError, "not a cogame-factorio replay (format=" &
      doc{"format"}.getStr & ")")
  let m = doc{"map"}
  if m == nil or m.kind != JObject:
    raise newException(ValueError, "replay has no map")
  let b = m{"bounds"}
  result.x0 = b{"x0"}.getInt(-128); result.y0 = b{"y0"}.getInt(-64)
  result.x1 = b{"x1"}.getInt(128); result.y1 = b{"y1"}.getInt(128)
  for row in m{"resources"}:
    if row.kind != JArray or row.len < 4: continue
    result.resources.add Resource(name: row[0].getStr, tx: int(floor(row[1].getF)),
      ty: int(floor(row[2].getF)), amount: int(row[3].getF))
  for row in m{"water"}:
    if row.kind != JArray or row.len < 3: continue
    result.water.add((int(floor(row[0].getF)), int(floor(row[1].getF)), max(1, row[2].getInt)))
  for row in m{"trees"}:
    if row.kind != JArray or row.len < 2: continue
    result.trees.add((int(floor(row[0].getF)), int(floor(row[1].getF))))
  for n in doc{"names"}:
    result.names.add n.getStr
  for s in doc{"seats"}:
    var seat = Seat(name: s{"name"}.getStr)
    for st in s{"steps"}:
      seat.steps.add parseStep(st)
    result.seats.add seat
  if result.seats.len == 0:
    raise newException(ValueError, "replay has no seats")

# ---------------------------------------------------------------------------
# Board geometry

proc computeBoard() =
  ## Tight bbox of terrain + every entity in every seat/step, plus margin,
  ## clamped to map.bounds; picks the px/tile scale.
  var
    minX = high(int) div 2
    minY = high(int) div 2
    maxX = low(int) div 2
    maxY = low(int) div 2
  template extend(x0i, y0i, x1i, y1i: int) =
    minX = min(minX, x0i); minY = min(minY, y0i)
    maxX = max(maxX, x1i); maxY = max(maxY, y1i)
  for seat in replay.seats:
    for st in seat.steps:
      for e in st.entities:
        extend(int(floor(e.x - e.w / 2)), int(floor(e.y - e.h / 2)),
               int(ceil(e.x + e.w / 2)), int(ceil(e.y + e.h / 2)))
      for b in st.belts: extend(int(floor(b.x)), int(floor(b.y)), int(floor(b.x)) + 1, int(floor(b.y)) + 1)
      for p in st.pipes: extend(int(floor(p.x)), int(floor(p.y)), int(floor(p.x)) + 1, int(floor(p.y)) + 1)
      if st.hasChar:
        extend(int(floor(st.charX)), int(floor(st.charY)), int(floor(st.charX)) + 1, int(floor(st.charY)) + 1)
  if minX > maxX:
    minX = replay.x0; minY = replay.y0; maxX = replay.x1; maxY = replay.y1
  # Ambient terrain (ore patches, the lake, the tree line) only widens the
  # board when it sits near the play area: the FLE lab map's ore patches and
  # trees span most of its 256x192 tiles, and a board that size (20+ Mpx at
  # 32 px/tile) both shrinks the base to a corner and costs seconds to bake,
  # ship and composite before the first frame. A patch the agent mines is
  # inside the bbox anyway (its drills are entities).
  const Reach = 16
  let cx0 = minX - Reach
  let cy0 = minY - Reach
  let cx1 = maxX + Reach
  let cy1 = maxY + Reach
  for r in replay.resources:
    if r.tx < cx0 or r.tx >= cx1 or r.ty < cy0 or r.ty >= cy1: continue
    extend(r.tx, r.ty, r.tx + 1, r.ty + 1)
  for w in replay.water:
    if w[1] < cy0 or w[1] >= cy1 or w[0] + w[2] <= cx0 or w[0] >= cx1: continue
    extend(max(cx0, w[0]), w[1], min(cx1, w[0] + w[2]), w[1] + 1)
  for t in replay.trees:
    if t[0] < cx0 or t[0] >= cx1 or t[1] < cy0 or t[1] >= cy1: continue
    extend(t[0], t[1], t[0] + 1, t[1] + 1)
  bx0 = max(replay.x0, minX - BoardMarginTiles)
  by0 = max(replay.y0, minY - BoardMarginTiles)
  bx1 = min(replay.x1, maxX + BoardMarginTiles)
  by1 = min(replay.y1, maxY + BoardMarginTiles)
  if bx1 <= bx0: bx1 = bx0 + 1
  if by1 <= by0: by1 = by0 + 1
  tile = 32
  if (bx1 - bx0) * (by1 - by0) * 32 * 32 > MaxBoardPixels:
    tile = 16
  boardW = (bx1 - bx0) * tile
  boardH = (by1 - by0) * tile

proc tileHash(x, y, salt: int): int =
  var h: Hash = 0
  h = h !& x !& (y * 7919) !& (salt * 104729)
  result = abs(!$h)

# ---------------------------------------------------------------------------
# Terrain bake -> static bands

proc drawAtlasAt(dst: var Rgba, idx: int, cxPx, cyPx: int) =
  ## Draws atlas sprite idx with its anchor at board px (cxPx, cyPx).
  let e = atlasEntries[idx]
  let s = if tile == 16: 2 else: 1
  dst.blit(sheet(), e.x div s, e.y div s, e.w div s, e.h div s, cxPx - e.cx div s, cyPx - e.cy div s)

proc bakeTerrain(): Rgba =
  stampStage("bake terrain (" & $boardW & "x" & $boardH & ")")
  result = newRgba(boardW, boardH)
  # Grass-1 toned ground (the FLE sprite mirror carries no grass tile
  # sheet) with per-tile shade noise, then real Factorio grass decoratives.
  # One tile-row strip is composed once and copied down the tile's rows.
  var strip = newSeq[uint8](boardW * 4)
  for ty in by0 ..< by1:
    for tx in bx0 ..< bx1:
      let n = tileHash(tx, ty, 1) mod 11
      let shade = if n < 4: 0 else: n - 7
      let r = uint8(72 + shade)
      let g = uint8(82 + shade)
      var i = (tx - bx0) * tile * 4
      for x in 0 ..< tile:
        strip[i] = r; strip[i + 1] = g; strip[i + 2] = 36; strip[i + 3] = 255
        i += 4
    let py0 = (ty - by0) * tile
    for y in py0 ..< py0 + tile:
      copyMem(result.data[y * boardW * 4].addr, strip[0].addr, strip.len)
  var groundIdx: seq[int]
  for i in 0 ..< 32:
    let idx = atlasIndex("ground", $i)
    if idx >= 0: groundIdx.add idx
  if groundIdx.len > 0:
    for ty in by0 ..< by1:
      for tx in bx0 ..< bx1:
        let h = tileHash(tx, ty, 2)
        if h mod 7 != 0: continue
        let idx = groundIdx[(h div 7) mod groundIdx.len]
        result.drawAtlasAt(idx, (tx - bx0) * tile + tile div 2 + (h div 97) mod tile - tile div 2,
                           (ty - by0) * tile + tile div 2 + (h div 13) mod tile - tile div 2)
  # Water runs.
  stampStage("bake water")
  var waterIdx: seq[int]
  for i in 0 ..< 8:
    let idx = atlasIndex("water", $i)
    if idx >= 0: waterIdx.add idx
  for w in replay.water:
    for tx in w[0] ..< w[0] + w[2]:
      if tx < bx0 or tx >= bx1 or w[1] < by0 or w[1] >= by1: continue
      if waterIdx.len > 0:
        let idx = waterIdx[tileHash(tx, w[1], 3) mod waterIdx.len]
        result.drawAtlasAt(idx, (tx - bx0) * tile + tile div 2, (w[1] - by0) * tile + tile div 2)
      else:
        let px0 = (tx - bx0) * tile
        let py0 = (w[1] - by0) * tile
        for y in py0 ..< py0 + tile:
          for x in px0 ..< px0 + tile:
            let i = (y * boardW + x) * 4
            result.data[i] = 38; result.data[i + 1] = 92; result.data[i + 2] = 130
  # Ore patches: volume stage from the amount with the game's own
  # stage_counts thresholds (iron/copper/coal/stone all use 15000..80),
  # variant by position hash. Ore sprites are 2 tiles wide and overlap
  # their neighbours, exactly as in the game.
  stampStage("bake resources")
  const StageCounts = [15000, 9500, 5500, 2900, 1300, 400, 150, 80]
  for r in replay.resources:
    if r.tx < bx0 or r.tx >= bx1 or r.ty < by0 or r.ty >= by1: continue
    let h = tileHash(r.tx, r.ty, 4)
    var idx = -1
    if r.name == "crude-oil":
      idx = atlasIndex("crude-oil", $(h mod 4 + 1))
    else:
      var vol = 1
      for i, threshold in StageCounts:
        if r.amount >= threshold:
          vol = 8 - i
          break
      idx = atlasIndex(r.name, $vol & "-" & $(h mod 8 + 1))
    let cx = (r.tx - bx0) * tile + tile div 2
    let cy = (r.ty - by0) * tile + tile div 2
    if idx >= 0:
      result.drawAtlasAt(idx, cx, cy)
    else:
      # Unknown resource kind: flat tint so the patch is still visible.
      for y in cy - tile div 2 ..< cy + tile div 2:
        for x in cx - tile div 2 ..< cx + tile div 2:
          let i = (y * boardW + x) * 4
          result.data[i] = 120; result.data[i + 1] = 110; result.data[i + 2] = 100
  # Trees (anchor = trunk base at the tile's bottom centre).
  stampStage("bake trees")
  var treeIdx: seq[int]
  for i, e in atlasEntries:
    if e.name == "tree": treeIdx.add i
  if treeIdx.len > 0:
    for t in replay.trees:
      if t[0] < bx0 or t[0] >= bx1 or t[1] < by0 or t[1] >= by1: continue
      let h = tileHash(t[0], t[1], 5)
      result.drawAtlasAt(treeIdx[h mod treeIdx.len],
        (t[0] - bx0) * tile + tile div 2, (t[1] - by0) * tile + tile - 2)

proc emitBands() =
  ## Slices the baked terrain into <= MaxBands horizontal band sprites and
  ## places them as static objects (ids 40.., z = -32768) — the layout
  ## broadcast_core.js bakes once into its base canvas.
  let terrain = bakeTerrain()
  stampStage("emit terrain bands")
  var bandH = max(tile, (boardH + MaxBands - 1) div MaxBands)
  bandH = ((bandH + tile - 1) div tile) * tile
  var y = 0
  var band = 0
  while y < boardH and band < MaxBands:
    let h = min(bandH, boardH - y)
    packet.addSprite(BandObjectBase + band, boardW, h,
      terrain.data.toOpenArray(y * boardW * 4, (y + h) * boardW * 4 - 1),
      "terrain band " & $band)
    packet.addObject(BandObjectBase + band, 0, y, StaticBandZ, MapLayerId, BandObjectBase + band)
    y += h
    inc band
  bandsEmitted = true

# ---------------------------------------------------------------------------
# Dynamic objects

proc objectIdFor(key: string): int =
  if key in objectIds:
    return objectIds[key]
  if freeObjectIds.len > 0:
    result = freeObjectIds.pop()
  else:
    result = nextObjectId
    inc nextObjectId
    if nextObjectId > DynObjectBase + MaxDynObjects:
      nextObjectId = DynObjectBase   # wrap; ids that far in are long dead
  objectIds[key] = result

proc resetObjects() =
  ## Seat switch: everything on the board belongs to the other seat.
  for id in liveObjects:
    packet.addDeleteObject(id)
  liveObjects.clear()
  objectIds.clear()
  freeObjectIds.setLen(0)
  nextObjectId = DynObjectBase

proc place(seen: var HashSet[int], key: string, x, y, z, spriteId: int) =
  let id = objectIdFor(key)
  seen.incl id
  liveObjects.incl id
  packet.addObject(id, clamp(x, -32000, 32000), clamp(y, -32000, 32000),
                   clamp(z, -32000, 32000), MapLayerId, spriteId)

proc dirName(d: int): string =
  DirNames[((d div 2) mod 4 + 4) mod 4]

proc spriteForEntity(e: Entity): int =
  ## Atlas index for an entity (name + direction), with the per-kind
  ## direction conventions of the sheets; -1 = not in the atlas.
  let d = dirName(e.dir)
  case e.name
  of "steam-engine":
    return atlasIndex(e.name, if d in ["north", "south"]: "vertical" else: "horizontal")
  of "storage-tank":
    return atlasIndex(e.name, if d in ["north", "south"]: "north" else: "east")
  of "underground-belt", "fast-underground-belt", "express-underground-belt":
    let n = if e.name == "express-underground-belt": "fast-underground-belt" else: e.name
    return atlasIndex(n, "in-" & d)
  of "character":
    return atlasIndex("character", d)
  else: discard
  result = atlasIndex(e.name, d)
  if result < 0: result = atlasIndex(e.name, "")
  if result < 0: result = atlasIndex(e.name, "north")

proc isProblemStatus(status: string): bool =
  status.len > 0 and status notin ["working", "normal", "", "no_recipe",
    "low_power", "charging", "discharging", "fully_charged", "waiting_for_space_in_destination",
    "waiting_for_source_items", "item_ingredient_shortage", "fluid_ingredient_shortage",
    "full_output", "no_ingredients", "no_input_fluid", "no_research_in_progress",
    "waiting_to_launch_rocket", "preparing_rocket_for_launch", "launching_rocket",
    "disabled", "not_plugged_in_electric_network", "networks_connected", "networks_disconnected"]

proc fallbackBlock(name: string, wTiles, hTiles: int): int =
  ## A flat block for a kind the atlas has no sheet for (never invisible).
  genSpriteId("block|" & name & "|" & $wTiles & "x" & $hTiles, proc (): Rgba =
    var px = newRgba(wTiles * tile, hTiles * tile)
    let h = abs(hash(name))
    px.fill(uint8(90 + h mod 100), uint8(90 + (h div 100) mod 100), uint8(90 + (h div 10000) mod 100), 255)
    for y in 0 ..< px.h:
      for x in 0 ..< px.w:
        if x == 0 or y == 0 or x == px.w - 1 or y == px.h - 1:
          let i = (y * px.w + x) * 4
          px.data[i] = 30; px.data[i + 1] = 30; px.data[i + 2] = 30
    px)

proc statusOutline(wTiles, hTiles: int): int =
  genSpriteId("outline|" & $wTiles & "x" & $hTiles, proc (): Rgba =
    var px = newRgba(wTiles * tile, hTiles * tile)
    for y in 0 ..< px.h:
      for x in 0 ..< px.w:
        if x <= 1 or y <= 1 or x >= px.w - 2 or y >= px.h - 2:
          let i = (y * px.w + x) * 4
          px.data[i] = 235; px.data[i + 1] = 40; px.data[i + 2] = 40; px.data[i + 3] = 220
    px)

proc beltSpriteKey(step: Step, b: Belt, byTile: Table[(int, int), int]): string =
  ## Straight belt, or the curve <feeder>_to_<this> when exactly one
  ## perpendicular neighbour feeds this tile and nothing feeds it from
  ## straight behind (the game's rule).
  const dx = [0, 1, 0, -1]
  const dy = [-1, 0, 1, 0]
  let d = ((b.dir div 2) mod 4 + 4) mod 4
  let tx = int(floor(b.x))
  let ty = int(floor(b.y))
  proc feeds(nx, ny, intoDir: int): bool =
    # Does the belt at (nx, ny) point into (tx, ty)?
    let i = byTile.getOrDefault((nx, ny), -1)
    if i < 0: return false
    let nd = ((step.belts[i].dir div 2) mod 4 + 4) mod 4
    nd == intoDir
  let behindFeeds = feeds(tx - dx[d], ty - dy[d], d)
  if behindFeeds:
    return DirNames[d]
  let leftDir = (d + 3) mod 4      # left of travel
  let rightDir = (d + 1) mod 4
  # The tile on our left feeds us if it points rightward (toward us).
  let leftFeeds = feeds(tx + dx[leftDir], ty + dy[leftDir], rightDir)
  let rightFeeds = feeds(tx + dx[rightDir], ty + dy[rightDir], leftDir)
  if leftFeeds and not rightFeeds:
    return DirNames[rightDir] & "_to_" & DirNames[d]
  if rightFeeds and not leftFeeds:
    return DirNames[leftDir] & "_to_" & DirNames[d]
  DirNames[d]

proc pipeShape(n, e, s, w: bool): string =
  ## FLE renderers/pipe.py mapping (up = north).
  let count = int(n) + int(e) + int(s) + int(w)
  case count
  of 0: "straight_vertical_single"
  of 1:
    if n: "ending_up" elif e: "ending_right" elif s: "ending_down" else: "ending_left"
  of 2:
    if n and e: "corner_up_right"
    elif n and s: "straight_vertical"
    elif n and w: "corner_up_left"
    elif e and s: "corner_down_right"
    elif e and w: "straight_horizontal"
    else: "corner_down_left"
  of 3:
    if not n: "t_down" elif not e: "t_left" elif not s: "t_up" else: "t_right"
  else: "cross"

proc emitStep() =
  ## Places every object of the current (seat, step); deletes what left.
  let seat = replay.seats[curSeat]
  var seen: HashSet[int]
  if seat.steps.len == 0:
    for id in liveObjects: packet.addDeleteObject(id)
    liveObjects.clear()
    return
  let st = seat.steps[clamp(curStep, 0, seat.steps.len - 1)]
  let d2 = tile == 16
  let s = if d2: 2 else: 1
  template px(v: float): int = int(round(v * float(tile)))
  # Belts (ground level).
  var byTile: Table[(int, int), int]
  for i, b in st.belts:
    byTile[(int(floor(b.x)), int(floor(b.y)))] = i
  for i, b in st.belts:
    let key = beltSpriteKey(st, b, byTile)
    var idx = atlasIndex("transport-belt", key)
    if idx < 0: idx = atlasIndex("transport-belt", dirName(b.dir))
    let tx = int(floor(b.x)); let ty = int(floor(b.y))
    let cx = (tx - bx0) * tile + tile div 2
    let cy = (ty - by0) * tile + tile div 2
    if idx >= 0:
      let e = atlasEntries[idx]
      seen.place("belt|" & $tx & "|" & $ty, cx - e.cx div s, cy - e.cy div s, 1, atlasSpriteId(idx))
    else:
      seen.place("belt|" & $tx & "|" & $ty, cx - tile div 2, cy - tile div 2, 1, fallbackBlock("transport-belt", 1, 1))
  # Pipes (ground level, shape from neighbours).
  var pipeSet: HashSet[(int, int)]
  for p in st.pipes: pipeSet.incl((int(floor(p.x)), int(floor(p.y))))
  for e in st.entities:
    if e.name == "pipe-to-ground" or e.name == "offshore-pump" or e.name == "pump":
      pipeSet.incl((int(floor(e.x)), int(floor(e.y))))
  for p in st.pipes:
    let tx = int(floor(p.x)); let ty = int(floor(p.y))
    let shape = pipeShape((tx, ty - 1) in pipeSet, (tx + 1, ty) in pipeSet,
                          (tx, ty + 1) in pipeSet, (tx - 1, ty) in pipeSet)
    let idx = atlasIndex("pipe", shape)
    let cx = (tx - bx0) * tile + tile div 2
    let cy = (ty - by0) * tile + tile div 2
    if idx >= 0:
      let a = atlasEntries[idx]
      seen.place("pipe|" & $tx & "|" & $ty, cx - a.cx div s, cy - a.cy div s, 2, atlasSpriteId(idx))
    else:
      seen.place("pipe|" & $tx & "|" & $ty, cx - tile div 2, cy - tile div 2, 2, fallbackBlock("pipe", 1, 1))
  # Entities, y-sorted by their bottom edge (z), atlas sprites anchored on
  # the entity centre.
  for e in st.entities:
    let cx = px(e.x - float(bx0))
    let cy = px(e.y - float(by0))
    let bottom = px(e.y + e.h / 2 - float(by0))
    let z = clamp(bottom, 3, 32000)
    let key = "e|" & e.name & "|" & $e.x & "|" & $e.y
    let idx = spriteForEntity(e)
    if idx >= 0:
      let a = atlasEntries[idx]
      seen.place(key, cx - a.cx div s, cy - a.cy div s, z, atlasSpriteId(idx))
    else:
      let wT = max(1, int(round(e.w)))
      let hT = max(1, int(round(e.h)))
      seen.place(key, px(e.x - e.w / 2 - float(bx0)), px(e.y - e.h / 2 - float(by0)), z,
                 fallbackBlock(e.name, wT, hT))
    if isProblemStatus(e.status):
      let wT = max(1, int(round(e.w)))
      let hT = max(1, int(round(e.h)))
      seen.place("st|" & key, px(e.x - e.w / 2 - float(bx0)), px(e.y - e.h / 2 - float(by0)),
                 z + 1, statusOutline(wT, hT))
  # Character trail: the last few steps' positions as faint footprints
  # (ground level, under everything), oldest faintest.
  let stepI = clamp(curStep, 0, seat.steps.len - 1)
  for back in 1 .. CharTrailSteps:
    let k = stepI - back
    if k < 0: break
    let prev = seat.steps[k]
    if not prev.hasChar: continue
    if prev.charX == st.charX and prev.charY == st.charY: continue
    let alpha = uint8(150 - back * 20)
    let sprite = genSpriteId("trail|" & $back, proc (): Rgba =
      let r = max(4, tile div 3)
      var px = newRgba(r * 2 + 1, r * 2 + 1)
      for y in 0 .. r * 2:
        for x in 0 .. r * 2:
          if (x - r) * (x - r) + (y - r) * (y - r) <= r * r:
            let i = (y * px.w + x) * 4
            px.data[i] = 245; px.data[i + 1] = 225; px.data[i + 2] = 160; px.data[i + 3] = alpha
      px)
    let tcx = px(prev.charX - float(bx0))
    let tcy = px(prev.charY - float(by0))
    let r = max(4, tile div 3)
    seen.place("trail|" & $back, tcx - r, tcy - r, 2, sprite)
  # Character, at its glide position (see applyCommand), facing its motion.
  if st.hasChar:
    var gx = st.charX
    var gy = st.charY
    if charGlideLeft > 0:
      let t = 1.0 - float(charGlideLeft) / float(CharGlideFrames)
      gx = charFrom[0] + (charTo[0] - charFrom[0]) * t
      gy = charFrom[1] + (charTo[1] - charFrom[1]) * t
    let idx = atlasIndex("character", DirNames[charFacing])
    let cx = px(gx - float(bx0))
    let cy = px(gy - float(by0))
    # Always on top: the character is what the viewer follows, and it is
    # usually standing right next to (i.e. behind) the tall thing it built.
    const CharZ = 31000
    if idx >= 0:
      let a = atlasEntries[idx]
      seen.place("char", cx - a.cx div s, cy - a.cy div s, CharZ, atlasSpriteId(idx))
    else:
      seen.place("char", cx - tile div 2, cy - tile div 2, CharZ, fallbackBlock("character", 1, 1))
  # Delete what is no longer on the board.
  var gone: seq[int]
  for id in liveObjects:
    if id notin seen: gone.add id
  for id in gone:
    packet.addDeleteObject(id)
    liveObjects.excl id
  # Free the ids of departed keys so they can be recycled.
  var deadKeys: seq[string]
  for key, id in objectIds:
    if id notin liveObjects: deadKeys.add key
  for key in deadKeys:
    freeObjectIds.add objectIds[key]
    objectIds.del key

proc chromeJson(): string =
  let seat = replay.seats[curSeat]
  let n = seat.steps.len
  var j = %*{
    "kind": "factorio",
    "seat": curSeat,
    "step": clamp(curStep, 0, max(0, n - 1)),
    "steps": n,
    "board": {"tile": tile, "x0": bx0, "y0": by0, "x1": bx1, "y1": by1, "w": boardW, "h": boardH},
    "names": replay.names,
  }
  $j

proc renderCurrent() =
  packet.setLen(0)
  if not bandsEmitted:
    packet.addLayer(MapLayerId, MapLayerType, ZoomableFlag)
    packet.addViewport(MapLayerId, boardW, boardH)
    emitBands()
  if dirty:
    stampStage("render step")
    emitStep()
    dirty = false
  # Chrome JSON on the reserved sprite (1x1, transparent), every frame.
  let chrome = chromeJson()
  packet.addSprite(ChromeSpriteId, 1, 1, [0'u8, 0, 0, 0], chrome)

# ---------------------------------------------------------------------------
# Exports

proc factorioSetAtlas(data: ptr uint8, width, height, half: cint): cint
    {.exportc: "factorio_set_atlas", cdecl.} =
  ## Host-decoded straight-alpha RGBA pixels of assets/atlas.png (the Worker
  ## decodes it with createImageBitmap, which beats an in-wasm PNG inflate by
  ## an order of magnitude); half != 0 hands over the 2:1 downsample (the
  ## 16 px/tile sheet) the same way, saving the in-wasm box filter. Must
  ## precede factorio_load_replay; without it the runtime decodes the PNG
  ## itself (and builds the half sheet on first use).
  try:
    if width <= 0 or height <= 0 or data == nil: return 0
    var px = newRgba(int(width), int(height))
    copyMem(px.data[0].addr, data, px.data.len)
    if half != 0:
      atlasHalf = px
    else:
      atlas = px
      atlasHalf = Rgba()
      atlasFromHost = true
    return 1
  except Exception:
    return 0

proc factorioLoadReplay(data: ptr uint8, length: cint): cint
    {.exportc: "factorio_load_replay", cdecl.} =
  try:
    lastError = ""
    runtimeLoaded = false
    profileLine = ""
    profileT0 = getMonoTime()
    profileLast = profileT0
    currentStage = ""
    if atlasEntries.len == 0:
      loadAtlas()
    stampStage("parse replay")
    replay = parseReplay(data.bytesFromPointer(int(length)))
    stampStage("compute board")
    computeBoard()
    let mapNote = " (board " & $boardW & "x" & $boardH & " px, " & $tile & " px/tile)"
    stampStage("check viewer capacity" & mapNote)
    if boardW * boardH > MaxViewerPixels:
      raise newException(ValueError, "replay board is too large for the browser viewer" & mapNote)
    bandsEmitted = false
    curSeat = 0
    curStep = 0
    dirty = true
    charGlideLeft = 0
    charFacing = 2
    for i in 0 ..< atlasSent.len: atlasSent[i] = false
    genSprites.clear()
    nextGenSprite = GenSpriteBase
    resetObjects()
    stampStage("render first frame" & mapNote)
    renderCurrent()
    stampStage("loaded")
    profileLine.add "total=" & $((getMonoTime() - profileT0).inMilliseconds) &
      "ms packet=" & $packet.len & "B"
    runtimeLoaded = true
    return 1
  except Exception as error:
    runtimeLoaded = false
    lastError = currentStage & ": " & error.msg & "\n" & error.getStackTrace()
    return 0

proc charPos(seatI, stepI: int): (bool, float, float) =
  let steps = replay.seats[seatI].steps
  if stepI < 0 or stepI >= steps.len or not steps[stepI].hasChar: return (false, 0.0, 0.0)
  (true, steps[stepI].charX, steps[stepI].charY)

proc applyCommand(text: string) =
  if text.startsWith("s:"):
    let v = try: parseInt(text[2 .. ^1]) except ValueError: -1
    if v >= 0 and v != curStep:
      # Autoplay (one step forward): glide the character from where it was
      # drawn to the new position and face the way it moves. Scrubs snap.
      let (hadFrom, fx, fy) = charPos(curSeat, clamp(curStep, 0, high(int) div 2))
      let (hasTo, tx, ty) = charPos(curSeat, v)
      var fromX = fx
      var fromY = fy
      if charGlideLeft > 0:
        let t = 1.0 - float(charGlideLeft) / float(CharGlideFrames)
        fromX = charFrom[0] + (charTo[0] - charFrom[0]) * t
        fromY = charFrom[1] + (charTo[1] - charFrom[1]) * t
      charGlideLeft = 0
      if hadFrom and hasTo and (tx != fromX or ty != fromY):
        if abs(tx - fromX) >= abs(ty - fromY): charFacing = (if tx > fromX: 1 else: 3)
        else: charFacing = (if ty > fromY: 2 else: 0)
        if v == curStep + 1:
          charFrom = (fromX, fromY)
          charTo = (tx, ty)
          charGlideLeft = CharGlideFrames
      curStep = v
      dirty = true
  elif text.startsWith("v:"):
    let v = try: parseInt(text[2 .. ^1]) except ValueError: -1
    if v >= 0 and v < replay.seats.len and v != curSeat:
      curSeat = v
      charGlideLeft = 0
      charFacing = 2
      resetObjects()
      dirty = true

proc factorioInput(data: ptr uint8, length: cint)
    {.exportc: "factorio_input", cdecl.} =
  if not runtimeLoaded: return
  try:
    for item in data.bytesFromPointer(int(length)).parseSpriteClientMessages():
      if item.kind == SpriteClientChatMessage:
        applyCommand(item.text)
  except Exception:
    discard

proc factorioFrame(): cint {.exportc: "factorio_frame", cdecl.} =
  if not runtimeLoaded:
    return 0
  try:
    if charGlideLeft > 0:
      dec charGlideLeft
      dirty = true
    renderCurrent()
    return 1
  except Exception as error:
    lastError = "render step: " & error.msg & "\n" & error.getStackTrace()
    return -1

proc factorioPacketPointer(): ptr uint8 {.exportc: "factorio_packet_ptr", cdecl.} =
  if packet.len == 0: nil else: packet[0].addr

proc factorioPacketLength(): cint {.exportc: "factorio_packet_len", cdecl.} =
  cint(packet.len)

proc factorioErrorPointer(): ptr uint8 {.exportc: "factorio_error_ptr", cdecl.} =
  if lastError.len == 0: nil else: cast[ptr uint8](lastError[0].addr)

proc factorioErrorLength(): cint {.exportc: "factorio_error_len", cdecl.} =
  cint(lastError.len)

proc factorioProfilePointer(): ptr uint8 {.exportc: "factorio_profile_ptr", cdecl.} =
  ## Load-time profile of the last factorio_load_replay ("stage=Nms ...").
  if profileLine.len == 0: nil else: cast[ptr uint8](profileLine[0].addr)

proc factorioProfileLength(): cint {.exportc: "factorio_profile_len", cdecl.} =
  cint(profileLine.len)

proc factorioStagePointer(): ptr uint8 {.exportc: "factorio_stage_ptr", cdecl.} =
  if stageNoteLen == 0: nil else: cast[ptr uint8](stageNote[0].addr)

proc factorioStageLength(): cint {.exportc: "factorio_stage_len", cdecl.} =
  cint(stageNoteLen)

when defined(emscripten):
  proc emscriptenExitWithLiveRuntime() {.
    importc: "emscripten_exit_with_live_runtime", cdecl.}

when isMainModule and defined(emscripten):
  # See ctf_replay.nim: Nim's main would run every module-global destructor
  # on return while JS keeps calling into the module. Exit with the runtime
  # alive so the globals live for the page's lifetime.
  emscriptenExitWithLiveRuntime()

when isMainModule and not defined(emscripten):
  # Native smoke: `nim c -r replay-viewer/factorio_replay.nim <replay.json>`
  # loads a replay and renders a few frames (64-bit sanity; the wasm32 truth
  # is tools/wasm_replay_smoke.cjs).
  import std/os
  if paramCount() >= 1:
    setCurrentDir(currentSourcePath().parentDir().parentDir() / "viewer")
    let bytes = readFile(paramStr(1))
    let ok = factorioLoadReplay(cast[ptr uint8](bytes[0].unsafeAddr), cint(bytes.len))
    if ok != 1:
      echo "load failed: ", lastError
      quit(1)
    echo "first packet ", packet.len, " bytes; board ", boardW, "x", boardH
    for i in 0 ..< 3:
      let cmd = "s:" & $(i + 1)
      var msg: seq[uint8]
      msg.add 0x81'u8
      msg.addU16(cmd.len)
      for ch in cmd: msg.add uint8(ord(ch))
      factorioInput(msg[0].addr, cint(msg.len))
      doAssert factorioFrame() == 1, lastError
      echo "frame ", i, " packet ", packet.len, " bytes"
