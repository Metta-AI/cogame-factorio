"""Handcraft baseline: no automation, just hands and stone furnaces.

Each step the character walks to the iron patch, hand-mines ore
(``harvest_resource``), drops it into a couple of hand-placed stone
furnaces (from the starting inventory), pulls out the plates and, once
enough plates pile up, hand-crafts iron gear wheels. Modest, steady
production score.

Every program wraps each action in ``try/except`` so one failed call
(patch exhausted, furnace missing, ...) never aborts the whole step;
the next step re-queries the world with ``get_entities`` instead of
trusting variables from previous programs. (FLE quirk: names starting
with ``_`` are not persisted between programs and get clobbered by stale
values, so programs use plain ``hc_`` names.)

``python -m players.handcraft_player``
"""

from __future__ import annotations

from players import fle_helpers as H
from players.client import Policy, main_for

FURNACE = "stone-furnace"

# Placing furnaces: search for a free 2x2 spot a few tiles off the ore
# patch edge (nearest_buildable) so we never depend on fixed coordinates.
_SETUP = """
hc_ore = nearest(Resource.{resource})
move_to(hc_ore)
hc_placed = 0
for hc_dx in (0, -4, 4, -8, 8):
    if hc_placed >= {want}:
        break
    try:
        hc_bb = nearest_buildable(Prototype.StoneFurnace, BuildingBox(width=2, height=2),
                                  center_position=Position(x=hc_ore.x + hc_dx, y=hc_ore.y - 5))
        hc_f = place_entity(Prototype.StoneFurnace, position=hc_bb.center)
        insert_item(Prototype.Coal, hc_f, quantity=8)
        hc_placed += 1
        print("placed furnace at", hc_f.position)
    except Exception as hc_e:
        print("furnace placement failed:", hc_e)
print("furnaces placed:", hc_placed)
"""

_SMELT = """
hc_ore = nearest(Resource.{resource})
move_to(hc_ore)
try:
    print("harvested", harvest_resource(hc_ore, quantity={harvest}))
except Exception as hc_e:
    print("harvest failed:", hc_e)
hc_furnaces = get_entities({{Prototype.StoneFurnace}})
hc_plates = 0
for hc_f in hc_furnaces:
    try:
        hc_plates += extract_item(Prototype.{plate}, hc_f.position, quantity=50)
    except Exception:
        pass
    try:
        insert_item(Prototype.Coal, hc_f, quantity=4)
    except Exception:
        pass
    try:
        insert_item(Prototype.{ore}, hc_f, quantity={per_furnace})
    except Exception as hc_e:
        print("insert failed:", hc_e)
print("extracted plates:", hc_plates, "furnaces:", len(hc_furnaces))
{craft}
sleep({sleep})
"""

_CRAFT_GEARS = """
try:
    print("crafted gears:", craft_item(Prototype.IronGearWheel, quantity={n}))
except Exception as hc_e:
    print("craft failed:", hc_e)
"""


class HandcraftPolicy(Policy):
    """Hand-mine, hand-smelt, hand-craft; never automates."""

    def __init__(self, furnaces: int = 2, sleep_seconds: int = 20):
        self.furnaces = furnaces
        self.sleep_seconds = sleep_seconds
        self.starting_inventory: dict[str, int] = {}

    def on_welcome(self, welcome: dict) -> None:
        inv = H.get_in(welcome, "episode", "starting_inventory")
        if isinstance(inv, dict):
            self.starting_inventory = {
                str(k): int(v) for k, v in inv.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)}

    def program(self, step: int, observation: dict) -> str:
        n_furnaces = len(H.entities_named(observation, FURNACE))
        inv = H.inventory(observation) or self.starting_inventory
        have_spare = inv.get(FURNACE, 1 if step == 0 else 0)
        # (Re)build the smelting spot when nothing is standing yet and we
        # still have furnaces in inventory (step 0, or after a failure).
        if n_furnaces == 0 and (step == 0 or have_spare > 0):
            return _SETUP.format(resource="IronOre", want=self.furnaces)

        plates = H.inventory_count(observation, "iron-plate")
        craft = ""
        if plates >= 10:
            craft = _CRAFT_GEARS.format(n=min(plates // 2, 20))

        # Iron only: a furnace holding iron ore refuses copper ore, and
        # mixing patches buys nothing for a hand-fed setup.
        resource, ore, plate = "IronOre", "IronOre", "IronPlate"
        per_furnace = 25
        harvest = per_furnace * max(n_furnaces, 1)
        return _SMELT.format(
            resource=resource, ore=ore, plate=plate, harvest=harvest,
            per_furnace=per_furnace, craft=craft, sleep=self.sleep_seconds)


if __name__ == "__main__":
    main_for(HandcraftPolicy)
