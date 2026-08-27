"""cardcats.py - the card category vocabulary, in one place.

WHY THIS EXISTS
---------------
A FIFA 12 FUT card needs TWO independent labels, and collapsing them into one
field is the mistake this module exists to prevent:

  * ART CLASS - what the card LOOKS like. Drives the `rareflag` byte on the
    wire, which the client copies to CARD_TOTW whenever it is >= 3 (native
    handler RVA 0x22327). This is the only thing that changes the face art.

  * PROVENANCE - WHY the row exists. A Transfer card and an Upgrade card use
    ORDINARY bronze/silver/gold art; they are not a special class. A TOTY card
    uses the SAME art as an In Form and is still its own category.

With one field, "an UP card that is also rare" and "a TOTY that renders as an
In Form" are both inexpressible. With two, they are trivial.

THE ART TABLE IS USER-CONFIRMED (2026-08-18), decoded face by face from the
shipped textures and cross-checked against futwiz's own CSS classes, which
agree on every count: green-r 19 = iMOTM, orange-r 28 = MOTM, blue-r 137 =
TOTS, bluered-r 1 = SPECIAL.

The internal enum names (TOTY_CARD, FIFAPRO_CARD, EASPORTS_CARD, CHARITY_CARD)
are FIFA-11-era slot names EA reused and DO NOT describe what we put in them.
Our abbreviations are canonical - use them everywhere, including in any UI.

TWO PROPERTIES OF THE VALUE, NOT CHOICES
----------------------------------------
  * `rareflag & 1` sets the foil/shine. So TOTS(5) arrives RARE while iMOTM(4),
    SPECIAL(6) and MOTM(8) arrive COMMON. Not separable from the colour.
  * The coloured specials IGNORE CARD_LEVEL - one asset each, no band variants.
    A bronze TOTS and a gold TOTS render identically. Only IF has band panels.

rareflag 7 (GREEN_CARD) IS UNUSED. The user marked it so on 2026-08-18 and the
emitter must refuse it rather than silently pass it through - see `rareflag()`.
"""

# ART CLASS -> the rareflag byte that produces it.
#
# 0 and 1 are the ordinary common/rare cards every bronze, silver and gold
# player uses. 3 upwards land in the eCardTOTW enum, confirmed from the
# futmain_0.bin bytecode at 0x61d0-0x622e.
ART_COMMON = "COMMON"
ART_RARE = "RARE"
ART_IF = "IF"
ART_IMOTM = "iMOTM"
ART_TOTS = "TOTS"
ART_SPECIAL = "SPECIAL"
ART_MOTM = "MOTM"

ART_RAREFLAG = {
    ART_COMMON:  0,
    ART_RARE:    1,
    ART_IF:      3,   # eCardTOTW TOTW_CARD - cards_bg_51/52/53, band panels
    ART_IMOTM:   4,   # eCardTOTY TOTY_CARD - #60 purple
    ART_TOTS:    5,   # eCardFIFAPRO        - #61 blue
    ART_SPECIAL: 6,   # eCardEASPORTS       - #62 blue + white/red diagonal
    ART_MOTM:    8,   # eCardCHARITY        - #64 orange
}

# THE VALUE WE NEVER EMIT. Kept named rather than merely absent, so that a
# future reader sees a deliberate hole instead of an oversight.
RAREFLAG_UNUSED_GREEN = 7

# Art classes that ignore CARD_LEVEL: one texture, no bronze/silver/gold
# variants. IF is deliberately NOT in here - it has the three band panels.
ART_SINGLE_ASSET = frozenset({ART_IMOTM, ART_TOTS, ART_SPECIAL, ART_MOTM})


# PROVENANCE -> why this row exists. Orthogonal to art class.
#
# BASE is the shipped card. TRANSFER and UP are ordinary-art rows: same player,
# new club, or a raised overall. TOTY carries IF art but is its own category,
# which is precisely why this axis has to be separate.
PROV_BASE = "BASE"
PROV_IF = "IF"
PROV_TOTY = "TOTY"
PROV_TOTS = "TOTS"
PROV_MOTM = "MOTM"
PROV_IMOTM = "iMOTM"
PROV_SPECIAL = "SPECIAL"
PROV_TRANSFER = "TRANSFER"
PROV_UP = "UP"

PROVENANCE = frozenset({
    PROV_BASE, PROV_IF, PROV_TOTY, PROV_TOTS, PROV_MOTM, PROV_IMOTM,
    PROV_SPECIAL, PROV_TRANSFER, PROV_UP,
})

# The art class each provenance renders as, WHEN it has a fixed one.
#
# TRANSFER and UP are absent on purpose: their art follows the card's own
# rarity (common or rare), so it cannot be derived from provenance alone.
# TOTY maps to IF art - same texture, different category. That single line is
# the whole reason this module has two axes.
PROV_ART = {
    PROV_IF:      ART_IF,
    PROV_TOTY:    ART_IF,
    PROV_TOTS:    ART_TOTS,
    PROV_MOTM:    ART_MOTM,
    PROV_IMOTM:   ART_IMOTM,
    PROV_SPECIAL: ART_SPECIAL,
}

# futwiz's Item Type filter files TOTS cards under the label "TOTY". Rows from
# that filter are TOTS and are never called TOTY again - there is no TOTY
# filter on the site. Recorded here so the rename happens once, at ingest.
FUTWIZ_RELEASE_PROVENANCE = {
    "Transfer": PROV_TRANSFER,
    "UP":       PROV_UP,
    "MOTM":     PROV_MOTM,
    "iMOTM":    PROV_IMOTM,
    "Special":  PROV_SPECIAL,
    "TOTY":     PROV_TOTS,      # <- the rename. See above.
}

# VARIANT ALLOCATION ORDER.
#
# resourceId is (variant << 24) | playerid, and the nibble is 4 bits, so a
# player has 15 slots. In Forms occupy 1..N ranked by ASCENDING overall and
# THOSE MUST NEVER MOVE - cards already in the user's club carry those
# resourceIds, and renumbering silently changes which card someone owns.
#
# Specials are therefore allocated ABOVE a player's In Form count, in this
# fixed order, so the sequence is stable across rebuilds and across adding a
# category later. Never interleave, never sort by rating.
VARIANT_ORDER = (PROV_TOTS, PROV_MOTM, PROV_IMOTM, PROV_SPECIAL,
                 PROV_TRANSFER, PROV_UP)

VARIANT_MAX = 15        # the nibble is 4 bits; 0 means "use the database row"


def rareflag(art_class):
    """The wire `rareflag` byte for an art class.

    Raises on the unused green card rather than returning 7. The user marked
    that slot UNUSED and a card carrying it would render green with no way to
    tell it from a bug, so this refuses at the point of construction instead of
    leaving it to a reviewer to notice.
    """
    try:
        return ART_RAREFLAG[art_class]
    except KeyError:
        raise ValueError("unknown art class %r (known: %s)"
                         % (art_class, ", ".join(sorted(ART_RAREFLAG))))


def is_rare(art_class):
    """Whether the card renders with the foil.

    `rareflag & 1` IS the shine bit, so this is derived rather than stored -
    it cannot drift out of step with the value actually sent.
    """
    return bool(rareflag(art_class) & 1)


# THE SPECIAL RAREFLAGS - DERIVED FROM THE ART TABLE, NEVER TYPED.
#
# The cards that price on the special quick-sell curve (9,000+ coins) rather
# than the ordinary one: In Form and every coloured class, i.e. everything that
# is not a plain COMMON or RARE. Derived so it cannot drift out of step with
# ART_RAREFLAG the way a second hand-written list would.
SPECIAL_RAREFLAGS = frozenset(
    v for k, v in ART_RAREFLAG.items() if k not in (ART_COMMON, ART_RARE))


def is_special_rareflag(rf):
    """Whether a wire rareflag belongs to a special card.

    The MINT path already gets this right, because it asks the art class. The
    club and market paths hold only the rareflag byte, and testing that against
    In Form's 3 alone silently reprices every coloured card - MOTM, iMOTM,
    SPECIAL, TOTS - onto the common curve.
    """
    return rf in SPECIAL_RAREFLAGS


def art_for(provenance, rare=False):
    """Art class for a provenance.

    `rare` is consulted ONLY for the ordinary-art provenances (BASE, TRANSFER,
    UP), where the card's own rarity is what picks the texture. For the special
    categories the art is fixed and `rare` is ignored - passing rare=True to a
    MOTM does not make it a different card.
    """
    if provenance not in PROVENANCE:
        raise ValueError("unknown provenance %r" % (provenance,))
    art = PROV_ART.get(provenance)
    if art is not None:
        return art
    return ART_RARE if rare else ART_COMMON


def variant_of(provenance, index, inform_count):
    """The variant nibble for a special row on a player who has In Forms.

    `inform_count` is how many In Form versions that player already has - their
    nibbles are 1..inform_count and are immovable. `index` is the row's
    position among the same player's rows of this provenance, 0-based.

    Raises rather than wrapping if the result exceeds the nibble: an overflow
    would alias onto another player's resourceId, which is silent corruption of
    somebody's club.
    """
    if provenance not in VARIANT_ORDER:
        raise ValueError("%r is not allocated above the In Forms"
                         % (provenance,))
    v = inform_count + VARIANT_ORDER.index(provenance) + int(index) + 1
    if not 1 <= v <= VARIANT_MAX:
        raise ValueError(
            "variant %d out of range 1..%d for %s (player has %d In Forms)"
            % (v, VARIANT_MAX, provenance, inform_count))
    return v


# IMPORT-TIME INVARIANTS, in the style of POSITION_CARD_CONVERSIONS: a silent
# drift in any of these writes the wrong art onto a card, which is expensive to
# spot on screen and trivial to catch here.
assert RAREFLAG_UNUSED_GREEN not in ART_RAREFLAG.values(), \
    "rareflag 7 is UNUSED and must never be reachable from an art class"
assert len(set(ART_RAREFLAG.values())) == len(ART_RAREFLAG), \
    "two art classes share a rareflag - they would be indistinguishable"
assert set(PROV_ART) <= PROVENANCE, \
    "PROV_ART names a provenance that does not exist"
assert set(PROV_ART.values()) <= set(ART_RAREFLAG), \
    "PROV_ART names an art class that has no rareflag"
assert set(FUTWIZ_RELEASE_PROVENANCE.values()) <= PROVENANCE, \
    "the futwiz release map names a provenance that does not exist"
assert PROV_TOTY not in FUTWIZ_RELEASE_PROVENANCE.values(), \
    "futwiz's TOTY filter holds TOTS cards - it must not stay labelled TOTY"
assert (set(VARIANT_ORDER) <= PROVENANCE
        and len(set(VARIANT_ORDER)) == len(VARIANT_ORDER)), \
    "VARIANT_ORDER must be distinct real provenances"
assert PROV_IF not in VARIANT_ORDER and PROV_TOTY not in VARIANT_ORDER, \
    "IF and TOTY already own nibbles 1..N and must not be reallocated"
# The two ordinary-art provenances must NOT have a fixed art class, or a rare
# Transfer would silently render common.
assert (PROV_TRANSFER not in PROV_ART and PROV_UP not in PROV_ART
        and PROV_BASE not in PROV_ART), \
    "TRANSFER, UP and BASE take their art from the card's own rarity"
# The shine bit is a property of the value; spot-check the ones that matter.
assert is_rare(ART_TOTS) and is_rare(ART_RARE), "TOTS and RARE carry the foil"
assert (not is_rare(ART_IMOTM) and not is_rare(ART_MOTM)
        and not is_rare(ART_SPECIAL) and not is_rare(ART_COMMON)), \
    "iMOTM, MOTM, SPECIAL and COMMON arrive without the foil"
