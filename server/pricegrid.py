# -*- coding: utf-8 -*-
"""The pricing grid - type a price for the cards the formula cannot value.

RUN IT ON DEMAND, not as part of the rig:

    python pricegrid.py            then open http://127.0.0.1:8090

It is deliberately NOT the eighth service. prices.json is written only here and
read only by futprices, so there is no shared writer and none of the
read-modify-write hazard that makes clubstore dangerous to touch from outside
the stub. Nothing needs to be in boot.ps1, and nothing needs the watchdog.

WHY IT EXISTS. The market prices every card at 2x its quick-sell value, which is
right for filler and wrong for anything with a reason to be wanted: rare golds
span only 1200-1400 across ratings 75 to 99, so Messi prices the same as an 84,
and every bronze lands on the floor. This tool is where the exceptions get their
real numbers.

WHAT IT SELECTS. A card is here if it is rated 83+, or if it hits any of the
nine property rules below at any rating. Goalkeepers are held out of the
property rules entirely and qualify only at 85+, because GK cards reuse the six
face slots for GK stats and "any face 90+" otherwise drags in 53 keepers nobody
needs to price by hand.

    base cards          586    rating 122 + rules 456 + GK 8
    In Forms            495    gold83 287 + bronze 69 + silver BRA 19
                               + rules 110 + GK 10
    transfer / upgrade  ~127   the same rules, applied to the cards the bots
                               actually list at a rating of their own

TWO ENCODING TRAPS, both of which silently produce a plausible but wrong set:

  * `skillmoves` is 0-INDEXED on the shipped player rows (0-4 = 1 to 5 stars)
    and 1-INDEXED on the scraped In Form rows (Messi stores 4 and has four
    stars). Reading either with the other convention shifts every skill rule by
    a whole star.
  * the In Form and special rows name the six stats `pac sho pas dri def phy`;
    the base rows name them `pace shooting passing dribbling defending heading`.
    Same six slots, and a missing key reads as absent rather than as an error.

PRICES ARE TYPED, NEVER BANDED (user, 2026-08-21). The number typed IS the
card's value; the plus or minus 10 per cent per-listing spread in
futmarket._price() supplies the variation.
"""
import io
import json
import os
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cardsdb
import carddb
import futpack
import futprices

PORT = 8090
PRICES_FILE = os.path.join(HERE, "prices.json")
TEMPLATE = os.path.join(HERE, "pricegrid.html")

# The six face slots, in the order every card in this game stores them.
BASE_FACE = ("pace", "shooting", "passing", "dribbling", "defending", "heading")
ROW_FACE = ("pac", "sho", "pas", "dri", "def", "phy")

GK_HAND_PRICE_FLOOR = 85       # keepers qualify on rating alone, and only here
RATING_HAND_PRICE_FLOOR = 83


def _rules(tier, position, rating, face, skills, wf):
    """Which property rules this card hits. Never called for a goalkeeper.

    Nine rules, all the user's, and each one names a reason a low-rated card is
    still worth coins in FIFA 12. The counts they produced when the set was
    defined are in the plan; the totals reproduce exactly.
    """
    hit = []
    pace = face[0]
    if tier in ("silver", "bronze") and position == "CB" and pace >= 70:
        hit.append("cb_pace70")
    if max(face) >= 90:
        hit.append("face90")
    if 75 <= rating <= 80 and pace >= 85:
        hit.append("ovr7580_pace85")
    if (tier == "bronze" and sum(1 for x in face if x >= 70) >= 2
            and sum(1 for x in face if x >= 60) >= 4):
        hit.append("bronze_bal")
    if skills >= 5:
        hit.append("skill5")
    if tier == "silver" and sum(1 for x in face if x >= 80) >= 2:
        hit.append("silver_80x2")
    if tier == "bronze" and skills == 4:
        hit.append("bronze_skill4")
    if wf >= 5 and skills >= 4:
        hit.append("wf5_skill4")
    if position == "CB" and pace >= 80:
        hit.append("cb_pace80")
    return hit


RULE_LABEL = {
    "cb_pace70": "silver/bronze CB, pace 70+",
    "face90": "a face stat at 90+",
    "ovr7580_pace85": "75-80 rated, pace 85+",
    "bronze_bal": "bronze, balanced (2 at 70+, 2 more at 60+)",
    "skill5": "5 star skills",
    "silver_80x2": "silver, two face stats 80+",
    "bronze_skill4": "bronze, 4 star skills",
    "wf5_skill4": "5 star weak foot and 4 star skills",
    "cb_pace80": "CB, pace 80+",
    "rating": "rated 83+",
    "gk": "keeper, 85+",
    "gold83": "gold In Form, 83+",
    "bronzeif": "bronze In Form",
    "brazil": "silver Brazilian In Form",
}


_ROWS = None


def _row(rid, kind, name, rating, rare, position, club, league, nation,
         face, skills, wf, foot, wr, why):
    """One grid row. `formula` is what the card costs if nothing is typed."""
    return {
        "rid": int(rid), "k": kind, "n": name, "r": int(rating),
        "rare": bool(rare), "p": position, "cl": club, "lg": league,
        "na": nation, "t": cardsdb.tier_of(int(rating)),
        "f": [int(x) for x in face], "sk": int(skills), "wf": int(wf),
        "ft": foot, "wr": wr, "why": why,
        # NO resource_id PASSED, deliberately: this is the FORMULA price, the
        # thing a blank box falls through to. Passing the id would return the
        # typed price and the placeholder would just echo the answer back.
        "fm": futprices.base_price(int(rating), bool(rare),
                                   inform=(kind == "IF")),
    }


def _foot(p):
    return "L" if int(p.get("preferredfoot") or 1) == 2 else "R"


def _wr(p):
    a = int(p.get("attackingworkrate") or 1)
    d = int(p.get("defensiveworkrate") or 1)
    w = ("H", "M", "L")
    return "%s/%s" % (w[a] if a < 3 else "M", w[d] if d < 3 else "M")


def _base_rows():
    """586 rows: every carded player the formula cannot value properly."""
    cards = carddb.Cards()
    players = futpack._player_by_id()
    rare_ids = cardsdb.rare_player_ids()
    out = []
    for pid in cardsdb.player_card_ids():
        p = players.get(pid)
        if p is None:
            continue
        c = cards.card(p)
        rating = int(c["rating"])
        pos = c["position"]
        face = [int(c[k]) for k in BASE_FACE]
        # 0-INDEXED HERE. See the module docstring.
        skills = int(p.get("skillmoves") or 0) + 1
        wf = int(p.get("weakfootabilitytypecode") or 0)
        tier = cardsdb.tier_of(rating)
        why = []
        if pos == "GK":
            if rating >= GK_HAND_PRICE_FLOOR:
                why = ["gk"]
        else:
            why = _rules(tier, pos, rating, face, skills, wf)
            if rating >= RATING_HAND_PRICE_FLOOR:
                why = ["rating"] + why
        if not why:
            continue
        out.append(_row(pid, "base", c["name"], rating, pid in rare_ids, pos,
                        c["club"], c["league"], c["nation"], face, skills, wf,
                        _foot(p), _wr(p), why))
    return out


def _inform_rows():
    """495 rows out of the 979 In Forms.

    Gold In Forms under 83 that hit no property rule take the flat formula, as
    specified: they are user-listed only, so other users make their price.
    """
    cards = carddb.Cards()
    players = futpack._player_by_id()
    out = []
    for r in futpack.inform_pool():
        pid = int(r["playerid"])
        p = players.get(pid)
        if p is None:
            continue
        rating = int(r["ovr"])
        pos = str(r.get("position") or "")
        face = [int(r.get(k) or 0) for k in ROW_FACE]
        # 1-INDEXED HERE. See the module docstring.
        skills = int(r.get("skills") or 1)
        wf = int(r.get("weakfoot") or 1)
        tier = cardsdb.tier_of(rating)
        why = []
        if pos == "GK":
            if rating >= GK_HAND_PRICE_FLOOR:
                why = ["gk"]
        else:
            why = _rules(tier, pos, rating, face, skills, wf)
            if tier == "gold" and rating >= RATING_HAND_PRICE_FLOOR:
                why = ["gold83"] + why
            if tier == "bronze":
                why = ["bronzeif"] + why
            if tier == "silver" and int(r.get("nation") or 0) == 54:
                why = ["brazil"] + why
        if not why:
            continue
        tid = int(r.get("teamid") or 0)
        rid = futpack.inform_resource_id(pid, int(r["_variant"]))
        out.append(_row(rid, "IF", r.get("name") or cards.card(p)["name"],
                        rating, True, pos,
                        cards.teams.get(tid, ""),
                        cards.leagues.get(cards.league_of.get(tid, 0), ""),
                        cards.nations.get(int(r.get("nation") or 0), ""),
                        face, skills, wf, _foot(p), _wr(p), why))
    return out


def _variant_rows():
    """The TRANSFER and UPGRADE cards that qualify - and these are LIVE.

    Added after the set was first defined, and they matter more than the In
    Forms for the economy today: the bots list them, so a wrong price is a wrong
    price on the market right now, where an In Form is only ever user-listed.

    Skills and weak foot come from the BASE player row. A transfer changes club
    and sometimes rating; it does not teach anyone a new skill move, and the
    scraped rows do not carry either field.
    """
    cards = carddb.Cards()
    players = futpack._player_by_id()
    out = []
    for r in futpack.specials_pool():
        if r.get("category") not in ("TRANSFER", "UP"):
            continue
        pid = int(r.get("playerid") or 0)
        variant = int(r.get("_variant") or 0)
        p = players.get(pid)
        if p is None or not variant:
            continue
        rating = int(r.get("ovr") or 0)
        pos = str(r.get("position") or "")
        face = [int(r.get(k) or 0) for k in ROW_FACE]
        skills = int(p.get("skillmoves") or 0) + 1
        wf = int(p.get("weakfootabilitytypecode") or 0)
        tier = cardsdb.tier_of(rating)
        why = []
        if pos == "GK":
            if rating >= GK_HAND_PRICE_FLOOR:
                why = ["gk"]
        else:
            why = _rules(tier, pos, rating, face, skills, wf)
            if rating >= RATING_HAND_PRICE_FLOOR:
                why = ["rating"] + why
        if not why:
            continue
        tid = int(r.get("teamid") or 0)
        rid = futpack.inform_resource_id(pid, variant)
        out.append(_row(rid, r.get("category"), r.get("name") or "", rating,
                        bool(r.get("rare")), pos,
                        cards.teams.get(tid, ""),
                        cards.leagues.get(cards.league_of.get(tid, 0), ""),
                        cards.nations.get(int(r.get("nation") or 0), ""),
                        face, skills, wf, _foot(p), _wr(p), why))
    return out


def rows(reload=False):
    """Every row in the grid, built once."""
    global _ROWS
    if _ROWS is not None and not reload:
        return _ROWS
    out = _base_rows() + _inform_rows() + _variant_rows()
    seen = set()
    for r in out:
        if r["rid"] in seen:
            # A resourceId names exactly one card. Two rows sharing one would
            # make a price land on the wrong card with nothing on screen to
            # show it, so this is a hard failure rather than a warning.
            raise ValueError("resourceId collision in the grid: %d" % r["rid"])
        seen.add(r["rid"])
    _ROWS = out
    return out


# ---------------------------------------------------------------------------
# prices.json
#
# THE SHAPE IS FIXED BY futprices.load_overrides(): a flat {resourceId: price}
# map of integers. It reads int(k) and int(v) and skips anything else, so a
# richer record here would be silently ignored and every hand price would
# vanish. The grid owns readability; the file stays machine-simple.
# ---------------------------------------------------------------------------
_LOCK = threading.Lock()


def load_prices():
    try:
        if not os.path.exists(PRICES_FILE):
            return {}
        raw = json.loads(io.open(PRICES_FILE, encoding="utf-8").read())
    except Exception as e:
        print("  prices.json unreadable (%s) - starting from empty" % e)
        return {}
    out = {}
    for k, v in (raw or {}).items():
        try:
            k, v = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            out[k] = v
    return out


def _write(prices):
    """Atomic, and it has to be: the market reads this file on an mtime change,
    so a half-written file would be read as a corrupt one by a live server."""
    tmp = PRICES_FILE + ".tmp"
    body = json.dumps({str(k): int(v) for k, v in sorted(prices.items())},
                      indent=1)
    io.open(tmp, "w", encoding="utf-8", newline="\n").write(body)
    if os.path.exists(PRICES_FILE):
        os.remove(PRICES_FILE)
    os.rename(tmp, PRICES_FILE)


def set_price(rid, price):
    """Store one price, or clear it when price is falsy. Returns the stored value.

    SNAPPED TO THE LADDER HERE, once, so the number in the file is the number
    the market uses. futprices.round_to_increment owns the ladder - 50s below
    1000, 100s above - and doing it at write time means the grid can show what
    a typed figure will become instead of changing it silently later.
    """
    with _LOCK:
        prices = load_prices()
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            # A bad id is a bad request, not a server fault. Returning -1 keeps
            # the caller honest without a traceback and without touching a file
            # the live market is reading.
            return -1
        try:
            price = int(price or 0)
        except (TypeError, ValueError):
            price = 0
        if price <= 0:
            prices.pop(rid, None)
            _write(prices)
            return 0
        price = futprices.round_to_increment(price)
        prices[rid] = price
        _write(prices)
        return price


def set_many(rids, price):
    """One typed price applied to a batch. Still a typed price, just repeated."""
    with _LOCK:
        prices = load_prices()
        try:
            price = int(price or 0)
        except (TypeError, ValueError):
            price = 0
        if price > 0:
            price = futprices.round_to_increment(price)
        n = 0
        for rid in rids or []:
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                if prices.pop(rid, None) is not None:
                    n += 1
            else:
                prices[rid] = price
                n += 1
        _write(prices)
        return n, price


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------
try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:
    from BaseHTTPServer import BaseHTTPRequestHandler
    ThreadingHTTPServer = None


def page():
    t = io.open(TEMPLATE, encoding="utf-8").read()
    data = {
        "rows": rows(),
        "prices": {str(k): v for k, v in load_prices().items()},
        "labels": RULE_LABEL,
        "minPrice": futprices.MARKET_MIN_PRICE,
        "break": futprices.INCREMENT_BREAK,
        "stepLow": futprices.INCREMENT_LOW,
        "stepHigh": futprices.INCREMENT_HIGH,
    }
    return t.replace("__DATA__", json.dumps(data, ensure_ascii=False))


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *a):
        pass                                   # the console is for the report

    def _send(self, code, body, ctype="application/json"):
        body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/prices"):
            return self._send(200, json.dumps(
                {str(k): v for k, v in load_prices().items()}))
        try:
            return self._send(200, page(), "text/html")
        except Exception as e:
            return self._send(500, "grid failed to build: %s" % e, "text/plain")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception as e:
            return self._send(400, json.dumps({"error": str(e)}))
        try:
            if self.path.startswith("/api/price"):
                stored = set_price(req.get("rid"), req.get("price"))
                if stored < 0:
                    return self._send(400, json.dumps(
                        {"error": "bad resourceId %r" % (req.get("rid"),)}))
                return self._send(200, json.dumps({"rid": int(req.get("rid")),
                                                   "price": stored}))
            if self.path.startswith("/api/bulk"):
                n, price = set_many(req.get("rids"), req.get("price"))
                return self._send(200, json.dumps({"n": n, "price": price}))
        except Exception as e:
            return self._send(500, json.dumps({"error": str(e)}))
        return self._send(404, json.dumps({"error": "no such endpoint"}))


def main():
    all_rows = rows()
    priced = load_prices()
    by_kind = {}
    for r in all_rows:
        by_kind[r["k"]] = by_kind.get(r["k"], 0) + 1
    print("")
    print("  pricing grid - %d cards" % len(all_rows))
    for k in sorted(by_kind):
        print("      %-10s %4d" % (k, by_kind[k]))
    print("      priced already: %d" % sum(1 for r in all_rows
                                           if r["rid"] in priced))
    url = "http://127.0.0.1:%d/" % PORT
    print("")
    print("  %s" % url)
    print("  writes %s - close this window when you are done." % PRICES_FILE)
    print("")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("  stopped.")


if __name__ == "__main__":
    main()
