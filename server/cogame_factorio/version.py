"""Game version (GV): bumps whenever what a policy sees or how scores are
computed changes. Stamped into the replay header (``game_version``), the
``welcome`` message and the ``/global`` status snapshot.

Changelog (prepend-only; shape ``GVnn (short rule name): HEADLINE``):

GV1 (genesis): FLE 0.3.0 / Factorio 1.1.110, per-seat servers,
    cogame.factorio.v1 wire.
"""

GAME_VERSION = "1"  # GV1 (genesis): FLE 0.3.0 / Factorio 1.1.110, per-seat servers, cogame.factorio.v1 wire.
