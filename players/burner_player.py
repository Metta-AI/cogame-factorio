"""Burner baseline: burner mining drills feeding stone furnaces.

Build order (each phase is one program, gated on what the observation
shows already standing so a failed step is simply retried):

1. iron patch:   up to 5 burner drills, each dropping straight into a stone
                 furnace placed at ``drill.drop_position``;
2. copper patch: up to 3 drill+furnace pairs;
3. coal + stone: drills dropping into wooden chests (coal for refuelling,
                 stone for score);
then maintenance every step: extract plates, refuel anything low on coal
(topping up from the coal chests when the inventory runs dry), hand-craft
iron gear wheels from the plates, and let the factory run (``sleep``).

Programs never rely on variables from earlier programs: every step
re-queries the world with ``get_entities`` and wraps every action in
``try/except`` so a single failing call is a printed game outcome, not an
aborted step. (FLE quirk: names starting with ``_`` are *not* persisted
between programs and get clobbered by stale values, so programs use plain
``bp_`` names.)

``python -m players.burner_player``
"""

from __future__ import annotations

from players import fle_helpers as H
from players.client import Policy, main_for

DRILL = "burner-mining-drill"
FURNACE = "stone-furnace"
CHEST = "wooden-chest"

# Drill+sink pairs marching along the patch edge in 2-tile steps (both are
# 2x2), towards the patch centre so we stay on ore. Each pair is
# independent; failures are printed and skipped. Fuel goes in immediately
# so the pair works while we move on. ``nearest`` is measured from the
# character and can be fooled by items lying around a factory, so we walk
# back to the origin first.
_PLACE_PAIRS = """
move_to(Position(x=0, y=0))
bp_ore = nearest(Resource.{resource})
bp_dx = 2
try:
    bp_patch = get_resource_patch(Resource.{resource}, bp_ore)
    if bp_patch.bounding_box.center.x < bp_ore.x:
        bp_dx = -2
except Exception as bp_e:
    print("no patch info:", bp_e)
move_to(bp_ore)
bp_ok = 0
for bp_i in range({slots}):
    if bp_ok >= {want}:
        break
    try:
        bp_p = Position(x=bp_ore.x + bp_dx * bp_i, y=bp_ore.y)
        bp_d = place_entity(Prototype.BurnerMiningDrill, direction=Direction.DOWN, position=bp_p)
        bp_s = place_entity(Prototype.{sink}, position=bp_d.drop_position)
        insert_item(Prototype.Coal, bp_d, quantity=15)
{fuel_sink}
        bp_ok += 1
        print("placed", bp_d.name, "at", bp_d.position, "->", bp_s.name, "at", bp_s.position)
    except Exception as bp_e:
        print("placement", bp_i, "failed:", bp_e)
print("pairs placed:", bp_ok)
sleep({sleep})
"""
_FUEL_SINK = "        insert_item(Prototype.Coal, bp_s, quantity=10)"

_MAINTAIN = """
bp_plates = 0
for bp_f in get_entities({{Prototype.StoneFurnace}}):
    for bp_proto in (Prototype.IronPlate, Prototype.CopperPlate):
        try:
            bp_plates += extract_item(bp_proto, bp_f.position, quantity=50)
        except Exception:
            pass
print("extracted plates:", bp_plates)
{restock}
bp_refuelled = 0
for bp_e in get_entities({{Prototype.BurnerMiningDrill, Prototype.StoneFurnace}}):
    try:
        if bp_e.fuel.get(Prototype.Coal, 0) < {low_fuel}:
            insert_item(Prototype.Coal, bp_e, quantity={refill})
            bp_refuelled += 1
    except Exception as bp_x:
        print("refuel failed at", bp_e.position, ":", bp_x)
print("refuelled:", bp_refuelled)
{craft}
sleep({sleep})
"""

_RESTOCK_COAL = """
bp_coal = 0
for bp_c in get_entities({Prototype.WoodenChest}):
    try:
        bp_coal += extract_item(Prototype.Coal, bp_c.position, quantity=50)
    except Exception:
        pass
print("restocked coal:", bp_coal)
"""

_CRAFT_GEARS = """
try:
    print("crafted gears:", craft_item(Prototype.IronGearWheel, quantity={n}))
except Exception as bp_e:
    print("craft failed:", bp_e)
"""


class BurnerPolicy(Policy):
    """Drills into furnaces on iron and copper, drills into chests on
    coal and stone, then refuel/extract/craft every step."""

    # (resource, sink prototype, pairs wanted) in build order
    PHASES = (
        ("IronOre", "StoneFurnace", 5),
        ("CopperOre", "StoneFurnace", 3),
        ("Coal", "WoodenChest", 2),
        ("Stone", "WoodenChest", 1),
    )
    MAX_PLACEMENT_STEPS = 8  # give up building after this many attempts

    def __init__(self, sleep_seconds: int = 30):
        self.sleep_seconds = sleep_seconds
        self.placement_steps = 0
        self.starting_inventory: dict[str, int] = {}
        self.given_up: set[str] = set()   # phases that failed to add drills
        self._last_attempt: tuple[str, int] | None = None  # (resource, n_drills)

    def on_welcome(self, welcome: dict) -> None:
        # welcome.episode.starting_inventory states what we begin with;
        # never hardcode counts. Kept as the fallback when an observation
        # carries no inventory.
        inv = H.get_in(welcome, "episode", "starting_inventory")
        if isinstance(inv, dict):
            self.starting_inventory = {
                str(k): int(v) for k, v in inv.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)}

    # -- phase selection ---------------------------------------------------

    def _next_phase(self, observation: dict):
        """The first phase whose drills are not all standing yet, or None."""
        n_drills = len(H.entities_named(observation, DRILL))
        inv = H.inventory(observation) or self.starting_inventory
        if not inv:
            # Neither observation nor welcome told us: assume plenty and let
            # the in-program try/except report the failures.
            inv = {DRILL: 50, FURNACE: 10, CHEST: 10}
        drills_left = inv.get(DRILL, 0)
        furnaces_left = inv.get(FURNACE, 0)
        chests_left = inv.get(CHEST, 0)

        # A phase we tried last step that added no drills is abandoned so
        # a patch we cannot build on does not eat the whole episode.
        if self._last_attempt is not None:
            resource, before = self._last_attempt
            if n_drills <= before:
                self.given_up.add(resource)
            self._last_attempt = None

        cumulative = 0
        for resource, sink, want in self.PHASES:
            if resource in self.given_up:
                continue
            cumulative += want
            if n_drills < cumulative:
                if drills_left <= 0:
                    return None
                if sink == "StoneFurnace" and furnaces_left <= 0:
                    continue
                if sink == "WoodenChest" and chests_left <= 0:
                    continue
                self._last_attempt = (resource, n_drills)
                return resource, sink, cumulative - n_drills
        return None

    def program(self, step: int, observation: dict) -> str:
        phase = None
        if self.placement_steps < self.MAX_PLACEMENT_STEPS:
            phase = self._next_phase(observation)
        if phase is not None:
            self.placement_steps += 1
            resource, sink, want = phase
            fuel_sink = _FUEL_SINK if sink == "StoneFurnace" else ""
            return _PLACE_PAIRS.format(
                resource=resource, sink=sink, want=want, slots=want + 4,
                fuel_sink=fuel_sink, sleep=self.sleep_seconds // 2 or 1)

        coal = H.inventory_count(observation, "coal")
        restock = _RESTOCK_COAL if coal < 60 else ""
        plates = H.inventory_count(observation, "iron-plate")
        craft = _CRAFT_GEARS.format(n=min(plates // 2, 25)) if plates >= 20 else ""
        return _MAINTAIN.format(
            restock=restock, low_fuel=6, refill=10, craft=craft,
            sleep=self.sleep_seconds)


if __name__ == "__main__":
    main_for(BurnerPolicy)
