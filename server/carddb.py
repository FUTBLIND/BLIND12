"""
carddb.py - read the shipped FIFA 12 player database (fifa_ng_db.db).

WHY THIS EXISTS
---------------
FUT starter packs deal real player cards: name, position, nation, club, league
and stats. ALL of that ships with the game - we do not have to invent a single
card. It lives in:
    data0.big -> data/db/fifa_ng_db.db       (t3db, bit-packed)
    data0.big -> data/db/fifa_ng_db-meta.xml (schema)
Extract both with:  python assetfind.py fifa_ng_db

FORMAT - derived from the file itself, every number verified
------------------------------------------------------------
File header:
    0x00  'DB'
    0x08  u32 total file size (matches on disk)
    0x10  u32 table count (123)
    0x18  directory: 123 x { char[4] shortname, u32 offset }
          offsets are relative to the END of the directory (0x18 + 123*8)

Table header (at the directory offset):
    +0x08 u32 record size in BYTES
    +0x0c u32 recbytes*8 - 1        (NOT the packed width - do not use it)
    +0x14 u16 record count (stored twice)
    +0x1c u32 field count
    +0x2c u32 START BIT of the first field
    +0x30 field table: N x { char[4] shortname, u32 bits, u32 type,
                             u32 START BIT OF THE NEXT FIELD }
          The LAST entry is TRUNCATED to 8 bytes (name + bits only), so the
          table occupies (N-1)*16 + 8 bytes and the records begin right after.

So a field's start bit is the previous entry's trailing u32, seeded from
+0x2c. The chain is NOT monotonic - a few fields live at low bit offsets
(e.g. players has one 32-bit field at bit 0 and an 8-bit field at bit 64,
which is why the first field starts at bit 72).

Records are bit-packed LSB-FIRST across the whole record, and each field
stores (value - rangelow), so:  value = rangelow + raw

Verification that pinned this down: with data at a+0x678 and LSB-first,
500/500 sampled player rows had a sane overallrating, playerid and height,
and playerid comes out strictly ascending. Every other combination of
offset/bit-order/field-order scored near noise.

STRINGS
-------
Player names are NOT in `players` - it holds firstnameid / lastnameid /
commonnameid / playerjerseynameid, which index the `playernames` table.
"""
import io
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "asset_extract", "fifa_ng_db.db")
META = os.path.join(HERE, "asset_extract", "fifa_ng_db-meta.xml")


def load_meta(meta=None):
    """table -> (shortname, {fieldshort: (name, bits, rangelow, type)})

    `meta` defaults to fifa_ng_db-meta.xml. cardsdb.py passes the cards_ng_db
    schema instead - the two databases share this exact t3db layout, so the
    reader is the same and only the paths differ.
    """
    x = io.open(meta or META, encoding="utf-8").read()
    out = {}
    for m in re.finditer(r'<table name="([^"]+)" shortname="([^"]+)"(.*?)</table>',
                         x, re.S):
        name, short, body = m.group(1), m.group(2), m.group(3)
        f = {}
        for fm in re.finditer(r"<field ([^>]*)/>", body):
            a = dict(re.findall(r'(\w+)="([^"]*)"', fm.group(1)))
            f[a.get("shortname")] = (a.get("name"), int(a.get("depth", 0)),
                                     int(a.get("rangelow", 0)),
                                     a.get("type", ""))
        out[name] = (short, f)
    return out


# ---------------------------------------------------------------------------
# fcc_playercards - EA's finished FUT card face stats, one row per card.
#
# Opened LAZILY and BY PATH rather than through cardsdb, which imports this
# module: a top-level `import cardsdb` here would be circular. Db() already
# takes explicit db/meta paths for exactly this reason.
#
# NOT EVERY PLAYER HAS A CARD ROW - 7,934 of 14,469 do. A player with no row
# never had a FUT card, but card() can still be asked for one, so the old blend
# survives as the fallback for precisely that case and nothing else. It is kept
# rather than raising because a KeyError here would take down a whole pack
# build for one odd record.
# ---------------------------------------------------------------------------
_FCC_FACE = None


def _fcc_face():
    """{carddbid: (a1..a6)} from cards_ng_db, or {} if it cannot be read."""
    global _FCC_FACE
    if _FCC_FACE is not None:
        return _FCC_FACE
    out = {}
    try:
        cdb = os.path.join(HERE, "asset_extract", "cards_ng_db.db")
        cmeta = os.path.join(HERE, "asset_extract", "cards_ng_db-meta.xml")
        d = Db(cdb, cmeta)
        for r in d.rows("fcc_playercards"):
            out[int(r["carddbid"])] = tuple(
                int(r["attribute%d" % (i + 1)]) for i in range(6))
    except Exception as e:                                   # noqa: BLE001
        print("  [carddb] *** could not read fcc_playercards (%s) - face "
              "stats fall back to the computed blend" % e)
    _FCC_FACE = out
    return out


def _face_stats(p, _self):
    """The six face stats for one player row, EA's values where they exist."""
    face = _fcc_face().get(int(p["playerid"]))
    if face is not None:
        return dict(pace=face[0], shooting=face[1], passing=face[2],
                    dribbling=face[3], defending=face[4], heading=face[5])
    # No shipped card row - see the note above. This is the ONLY surviving use
    # of the old community blend.
    return dict(
        pace=round(p["acceleration"] * .45 + p["sprintspeed"] * .55),
        shooting=round(p["finishing"] * .45 + p["shotpower"] * .25 +
                       p["longshots"] * .20 + p["positioning"] * .10),
        passing=round(p["shortpassing"] * .35 + p["crossing"] * .25 +
                      p["longpassing"] * .20 + p["curve"] * .20),
        dribbling=round(p["dribbling"] * .50 + p["ballcontrol"] * .35 +
                        p["agility"] * .15),
        defending=round(p["marking"] * .35 + p["standingtackle"] * .35 +
                        p["slidingtackle"] * .30),
        heading=p["headingaccuracy"],
    )


class Db(object):
    def __init__(self, db=None, meta=None):
        """Defaults to fifa_ng_db (the player database). Pass db/meta to read
        cards_ng_db instead - see cardsdb.py."""
        self.d = open(db or DB, "rb").read()
        self.meta = load_meta(meta)
        n = struct.unpack_from("<I", self.d, 0x10)[0]
        base = 0x18 + n * 8
        self.tabs = {}
        for i in range(n):
            o = 0x18 + i * 8
            self.tabs[self.d[o:o + 4].decode("latin1")] = \
                base + struct.unpack_from("<I", self.d, o + 4)[0]

    def layout(self, table):
        """-> (list of (name, startbit, bits, rangelow, type), recbytes, count, dataoff)"""
        short, fmeta = self.meta[table]
        a = self.tabs[short]
        d = self.d
        recb = struct.unpack_from("<I", d, a + 8)[0]
        count = struct.unpack_from("<H", d, a + 0x14)[0]
        nf = struct.unpack_from("<I", d, a + 0x1C)[0]
        start = struct.unpack_from("<I", d, a + 0x2C)[0]
        lay, cur = [], start
        for i in range(nf):
            o = a + 0x30 + i * 16
            sn = d[o:o + 4].decode("latin1")
            bits = struct.unpack_from("<I", d, o + 4)[0]
            nxt = (struct.unpack_from("<I", d, o + 12)[0]
                   if i < nf - 1 else cur + bits)
            nm, mbits, lo, typ = fmeta.get(sn, (sn, bits, 0, ""))
            lay.append((nm, cur, mbits or bits, lo, typ))
            cur = nxt
        dataoff = a + 0x30 + (nf - 1) * 16 + 8
        return lay, recb, count, dataoff

    def rows(self, table, limit=None):
        lay, recb, count, off = self.layout(table)
        tot = recb * 8
        n = count if limit is None else min(count, limit)
        out = []
        for r in range(n):
            ch = self.d[off + r * recb: off + (r + 1) * recb]
            if len(ch) < recb:
                break
            v = int.from_bytes(ch, "little")
            out.append({nm: ((v >> st) & ((1 << b) - 1)) + lo
                        for nm, st, b, lo, _t in lay})
        return out


def main():
    db = Db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "players"
    if cmd == "tables":
        for nm, (sn, _f) in sorted(db.meta.items()):
            if sn in db.tabs:
                a = db.tabs[sn]
                print("  %-34s rows=%-6d bytes=%d"
                      % (nm, struct.unpack_from("<H", db.d, a + 0x14)[0],
                         struct.unpack_from("<I", db.d, a + 8)[0]))
        return 0
    if cmd == "fields":
        lay, recb, count, off = db.layout(sys.argv[2])
        print("%s: %d rows, %d bytes/rec" % (sys.argv[2], count, recb))
        for nm, st, b, lo, t in lay:
            print("   %-34s bit %-4d w=%-3d low=%-6d %s" % (nm, st, b, lo, t))
        return 0
    if cmd == "cards":
        c = Cards()
        lim = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        for k in c.all(lim):
            print("  %-22s %-4s %2d  %-13s %-24s %s"
                  % (k["name"], k["position"], k["rating"], k["nation"][:13],
                     k["club"][:24], k["league"][:26]))
            print("      PAC %-3d SHO %-3d PAS %-3d DRI %-3d DEF %-3d HEA %d"
                  % (k["pace"], k["shooting"], k["passing"], k["dribbling"],
                     k["defending"], k["heading"]))
        return 0
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    for r in db.rows(cmd, lim):
        print("  ", {k: r[k] for k in list(r)[:12]})
    return 0



# ---------------------------------------------------------------------------
# Strings: teams / leagues / nations store packed ASCII INLINE (little-endian
# byte sequence in the field's bits). Player names do NOT - `players` holds
# firstnameid / lastnameid / commonnameid / playerjerseynameid which index the
# `playernames` table, and THAT table stores a 32-bit byte offset into a
# Huffman-compressed block that follows its records.
#
# The name block layout, derived from the file:
#     bytes 0..455   Huffman tree: 114 nodes x { u16 child0, u16 child1 }
#                    a child with a non-zero high byte and a zero low byte is
#                    a LEAF holding chr(value >> 8); otherwise it is a node
#                    index. Root is node 0.
#     byte  456..    the compressed stream.
# Each name record's offset points at a LENGTH BYTE; the bitstream starts at
# offset+1 and is read MSB-first. Decode exactly `length` characters - there
# is no terminator, which is why decoding "until a separator" produced names
# running together.
# Verified: 19071 names decode, alphabetically ordered, accents intact
# (Sorensen / Barragan / Fontas).
# ---------------------------------------------------------------------------

def inline_str(v):
    b = []
    while v:
        b.append(v & 0xFF)
        v >>= 8
    return bytes(b).decode("latin1", "replace")


# Standard FIFA position enum. NOT verified against the binary - the labels are
# not stored in fifa.exe as a contiguous run, they are localised strings.
POSITIONS = ["GK", "SW", "RWB", "RB", "RCB", "CB", "LCB", "LB", "LWB", "RDM",
             "CDM", "LDM", "RM", "RCM", "CM", "LCM", "LM", "RAM", "CAM", "LAM",
             "RF", "CF", "LF", "RW", "RS", "ST", "LS", "LW", "?28", "?29",
             "?30", "?31"]


# THE INTERNATIONAL "LEAGUE" - the 42 national sides.
#
# `leagues` row 78 is literally named "International", and leagueteamlinks puts
# exactly 42 teams in it (England, Italy, Brazil, ...). It is the marker that
# separates a national call-up from a club, which is what team_of below filters
# on. Named rather than inlined because a bare 78 in a comprehension is exactly
# the sort of constant that gets copied without its meaning.
INTERNATIONAL_LEAGUE_ID = 78

# GALATASARAY (teamid 325) IS EXCLUDED FROM THE UT POPULATION.
#
# The user's instruction, 2026-08-19: their card images are bugged in this build
# and they are confident the club was not in FIFA 12 Ultimate Team. It is one
# club, targeted by id.
#
# THIS REPLACED A MUCH WIDER, WRONG EXCLUSION - RECORDED SO IT IS NOT REPEATED.
# The first attempt removed the whole of league 76 "Rest of World" on the
# assumption it was a junk bucket. It is not. FIFA 12 ships 36 leagues and NONE
# of them is Greek, Argentine, Turkish or South African, so league 76 is the only
# home those countries have: AEK Athens, Olympiacos, PAOK, Panathinaikos, River
# Plate, Boca Juniors, Racing Club, Kaizer Chiefs and Orlando Pirates are all
# genuine, licensed FUT clubs and all of them belong in the population. Deleting
# the league removed four countries' football wholesale, which was never the aim.
#
# Only World XI (111072) and Classic XI (111205) are EA novelty sides rather than
# real clubs, and both already carry zero card rows, so they can never be drawn
# and need no exclusion.
#
# EXCLUDED HERE, IN team_of, ON PURPOSE. This is the one place all three of
# futpack's independent player pools meet - _carded_rows(), pick_rated() and
# inform_pool() each intersect against clubbed_ids(). Excluding here is therefore
# structural rather than a filter every pool must remember: has_club() goes False
# and card()'s team_of.get() returns 0, so nothing downstream can re-admit them.
#
# Footprint, measured: 30 linked players, 20 carded (6 rare), top rating 80,
# NONE rated 84+, ZERO In Form rows, and 3 club items (1 badge + 2 kits). So no
# rating band thins out and the In Form pool is untouched.
EXCLUDED_CLUB_TEAM_IDS = frozenset({325})       # Galatasaray SK


class Cards(object):
    """Join the tables into FUT-style player cards."""

    def __init__(self):
        self.db = Db()
        d, tabs = self.db.d, self.db.tabs
        a = tabs["BGwe"]
        cnt = struct.unpack_from("<H", d, a + 0x14)[0]
        rec = a + 0x30 + 16 + 8
        self.blk = rec + cnt * 8
        self.nodes = [struct.unpack_from("<HH", d, self.blk + i * 4)
                      for i in range(114)]
        self.names = {}
        for r in range(cnt):
            v = int.from_bytes(d[rec + r * 8: rec + r * 8 + 8], "little")
            off = v & 0xFFFFFFFF
            if off != 0xFFFFFFFF:
                self.names[(v >> 32) & 0x7FFF] = off
        self.nations = {r["nationid"]: inline_str(r["nationname"])
                        for r in self.db.rows("nations")}
        self.teams = {r["teamid"]: inline_str(r["teamname"])
                      for r in self.db.rows("teams")}
        self.leagues = {r["leagueid"]: inline_str(r["leaguename"])
                        for r in self.db.rows("leagues")}
        # ORDER MATTERS: team_of is built from league_of, so league_of first.
        self.league_of = {r["teamid"]: r["leagueid"]
                          for r in self.db.rows("leagueteamlinks")}
        # EVERY TEAM IN LEAGUE 78 IS A NATIONAL SIDE, NOT A CLUB.
        #
        # `teamplayerlinks` holds one row per CLUB link AND one per national
        # call-up, and the national row comes SECOND. team_of used to be a dict
        # comprehension over that table, so the last row won and every
        # international resolved to his country: Montolivo came out as Italy
        # rather than Fiorentina, Alvarez as Argentina rather than Inter.
        #
        # Measured on the shipped data: 709 of the 8,868 carded players (8.0%)
        # resolved to a national team, and 230 of the 821 In Form players
        # (28.0%) - internationals are exactly who gets a TOTW card, so the bug
        # hit the cards it was most visible on.
        #
        # league 78 is "International" (42 teams, verified by name). Filtering
        # on the LEAGUE rather than on row order is what makes this robust: it
        # does not care whether the club link happens to come first, and every
        # teamid in teamplayerlinks appears in leagueteamlinks (checked: 0
        # unknown), so no link is silently misclassified as a club.
        self.national_teams = {t for t, l in self.league_of.items()
                               if l == INTERNATIONAL_LEAGUE_ID}
        # Clubs excluded by id - see EXCLUDED_CLUB_TEAM_IDS. Kept as its own
        # set so callers that need to ask "is this club excluded?"
        # (futpack._is_real_club, club_item_pool, club_assets) do not re-derive it.
        self.excluded_clubs = {t for t in EXCLUDED_CLUB_TEAM_IDS
                               if t in self.league_of}
        self.excluded_teams = self.national_teams | self.excluded_clubs
        # setdefault, not assignment: FIRST club link wins. Measured on this
        # database no player has two (14,274 have exactly one, 194 have none
        # and all 194 are uncarded), so this is about not re-introducing a
        # last-wins rule rather than about a case that exists today.
        self.team_of = {}
        for r in self.db.rows("teamplayerlinks"):
            if r["teamid"] not in self.excluded_teams:
                self.team_of.setdefault(r["playerid"], r["teamid"])

    # ---------------------------------------------------------------- clubs
    # NO FREE AGENTS. FIFA 12 has none - every player in the shipped database
    # belongs to a club - and a card with teamid 0 would show an empty crest
    # and no league, which is not a card the game ever produced.
    #
    # Since team_of now holds CLUB links only, "has a club" is exactly
    # "is in team_of", and a player who resolves to nothing must not be drawn.
    # Measured today: 0 of the 8,868 carded players fail this, so the guard is a
    # no-op on the shipped data - it exists so that stays TRUE BY CONSTRUCTION
    # rather than by luck. 194 players in fifa_ng_db do have a national link and
    # no club (plus 1 with no link at all); none of them has a card row, but
    # nothing except this guard was stopping one from being drawn.

    def has_club(self, playerid):
        """True if this player resolves to a real club (never a national side)."""
        return playerid in self.team_of

    def clubbed_ids(self):
        """Every playerid with a club. Intersect a draw pool with this to make
        'no free agents' structural - see futpack._carded_rows()."""
        return set(self.team_of)

    def name(self, nameid):
        off = self.names.get(nameid)
        if off is None:
            return ""
        d, nodes = self.db.d, self.nodes
        ln = d[self.blk + off]
        out, bit, n = [], 0, 0
        while len(out) < ln:
            byte = d[self.blk + off + 1 + (bit >> 3)]
            b = (byte >> (7 - (bit & 7))) & 1
            bit += 1
            v = nodes[n][b]
            if v & 0xFF00 and not (v & 0xFF):
                out.append(chr(v >> 8))
                n = 0
            elif v < len(nodes):
                n = v
            else:
                break
        return "".join(out)

    def card(self, p):
        tid = self.team_of.get(p["playerid"])
        lid = self.league_of.get(tid)
        full = self.name(p["commonnameid"]) or (
            (self.name(p["firstnameid"]) + " " +
             self.name(p["lastnameid"])).strip())
        return dict(
            id=p["playerid"], name=full,
            rating=p["overallrating"],
            position=POSITIONS[p["preferredposition1"] & 31],
            nation=self.nations.get(p["nationality"], ""),
            club=self.teams.get(tid, ""), league=self.leagues.get(lid, ""),
            # THE SIX FUT STATS, READ FROM EA'S OWN TABLE - NOT COMPUTED.
            #
            # This used to blend the raw attributes with the widely-documented
            # community weightings, under a comment that admitted "NOT YET
            # VERIFIED AGAINST THE BINARY - the source attributes below are
            # exact, the blend is not". It never was verified, because it is
            # not a blend at all: EA ships the finished values, one row per
            # card, in fcc_playercards.attribute1..6.
            #
            # HOW WRONG IT WAS, over all 7,934 outfield players with a card row
            # (mean absolute error against the shipped value):
            #
            #     PAC  0.17   83% exact        DRI  1.35
            #     SHO  2.26                    DEF 11.33   worst miss 43
            #     PAS  2.14                    HEA  5.95   worst miss 35
            #
            # DEF was the bad one: `marking*.35 + standingtackle*.35 +
            # slidingtackle*.30` is essentially a marking score. Fernando
            # Torres ships 55 and the blend produced 22; Llorente ships 65 and
            # got 22.
            #
            # WHY THIS WAS INVISIBLE, and why it was still worth deleting.
            # A card with a variant row (In Form, MOTM, TRANSFER, ...)
            # overwrites all six from the scraped pool, and a card without one
            # ships resourceId nibble 0 - whose branch at CardsDLLzf 0x22241
            # has the client read all six out of fcc_playercards and discard
            # ours. So these numbers reached neither the screen nor the match
            # engine.
            #
            # They did reach US. This blend is what made a stat comparison
            # report DEF as the suspect slot in the In Form data, when the
            # scrape was right and the baseline was wrong. Anything that
            # compares cards server-side would have hit it again.
            **_face_stats(p, self),
        )

    def all(self, limit=None):
        """Cards for the first `limit` players - CLUBLESS PLAYERS EXCLUDED.

        The no-free-agents invariant, applied to this module's own
        card-producing path. `limit` still bounds the ROWS READ, not the cards
        returned, so a preview can come back a little short rather than
        silently reading further into the table than asked.
        """
        return [self.card(p) for p in self.db.rows("players", limit)
                if self.has_club(p["playerid"])]

if __name__ == "__main__":
    sys.exit(main())
