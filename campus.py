"""Places on the BIT campus map, and turning what people write or say into them.

static/campus.json is shared with the map on the home page, so the server and
the page always agree on what a place is called and where it is.
"""
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

CAMPUS = json.loads((Path(__file__).resolve().parent / "static" / "campus.json").read_text(encoding="utf-8"))
PLACES = {place["id"]: place for place in CAMPUS["places"]}
ROADS = {road["id"]: road for road in CAMPUS["roads"]}
GROUPS = CAMPUS["groups"]

# Before a place, these mean the item was outdoors next to it rather than inside.
OUTSIDE_WORDS = re.compile(
    r"\b(outside|near|nearby|beside|behind|opposite|in front of|front of|next to|around|by the|close to)\s+(?:the\s+)?$"
)
# Too short or too common to match loosely: "pe" must not match "puff".
FUZZY_MIN_LENGTH = 5
FUZZY_RATIO = 0.84


def normalise(text):
    """Lower case, with punctuation and joining words reduced to single spaces."""
    text = (text or "").lower().replace("&", " and ").replace("/", " ")
    # Indian scripts are kept whole: their vowel signs are not word characters.
    text = re.sub(r"[^\w\sऀ-෿]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _aliases():
    """(alias, [place ids]) pairs, longest first so "mech parking" beats "mech".

    The shared words come first so that "canteen" means all three canteens even
    though it is also the short name of the BIT Canteen.
    """
    pairs = [(normalise(word), list(ids)) for word, ids in GROUPS.items()]
    for place in CAMPUS["places"]:
        for word in [place["name"], place["short"], *place["aliases"]]:
            pairs.append((normalise(word), [place["id"]]))
    for road in CAMPUS["roads"]:
        for word in [road["name"], *road["aliases"]]:
            pairs.append((normalise(word), [road["id"]]))
    seen, unique = set(), []
    for alias, ids in sorted(pairs, key=lambda pair: -len(pair[0])):
        if alias and alias not in seen:
            seen.add(alias)
            unique.append((alias, ids))
    return unique


ALIASES = _aliases()
# A word like this after a place belongs to it: "computer science lab" is one place.
TRAILING = r"(?:\s+(?:lab|labs|department|dept|block|room|building|hall|area|side))?"
# A loose match may not start or end on one of these, so "near canteen" is not
# read as a misspelt "main canteen".
FILLER = {"the", "a", "an", "near", "at", "in", "on", "by", "to", "and", "of", "my", "i", "inside",
          "outside", "behind", "beside", "around", "front", "next", "opposite", "lost", "found", "left"}


def is_place(place_id):
    return place_id in PLACES or place_id in ROADS


def place_name(place_id):
    entry = PLACES.get(place_id) or ROADS.get(place_id)
    return entry["name"] if entry else ""


def place_short(place_id):
    if place_id in PLACES:
        return PLACES[place_id]["short"]
    return ROADS[place_id]["name"] if place_id in ROADS else ""


def find_places(text):
    """Every place named in the text, in the order mentioned.

    Each mention is {"ids": candidates, "side": "inside" or "outside", "words":
    the alias matched, "span": where in the normalised text}. "Canteen" gives
    three candidates because people use it for the puff shop and Nandini too.
    Loose matches catch typos and misheard speech, so "mesh parking" still finds
    Mech Parking: where a loose match covers more words than an exact one, the
    loose one wins.
    """
    clean = normalise(text)
    if not clean:
        return []
    found = []
    for alias, ids in ALIASES:
        for match in re.finditer(rf"(?<!\w){re.escape(alias)}{TRAILING}(?!\w)", clean):
            found.append((match.start(), match.end(), alias, ids, 1.0))
    found += _fuzzy(clean)
    # Longest first, exact before loose; then keep what does not overlap.
    found.sort(key=lambda hit: (-(hit[1] - hit[0]), -hit[4]))
    chosen = []
    for hit in found:
        if all(hit[1] <= other[0] or hit[0] >= other[1] for other in chosen):
            chosen.append(hit)
    mentions = []
    for start, end, alias, ids, _ in sorted(chosen):
        outdoors = bool(OUTSIDE_WORDS.search(clean[:start])) or all(i in ROADS for i in ids)
        mentions.append({"ids": ids, "side": "outside" if outdoors else "inside", "words": alias,
                         "span": (start, end)})
    return mentions


def _fuzzy(clean):
    """Loose matches for runs of one to three words, for speech and typos."""
    words = clean.split()
    offsets, position = [], 0
    for word in words:
        offsets.append(position)
        position += len(word) + 1
    hits = []
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            run = words[i:i + size]
            chunk = " ".join(run)
            if len(chunk) < FUZZY_MIN_LENGTH or run[0] in FILLER or run[-1] in FILLER:
                continue
            best = None
            for alias, ids in ALIASES:
                if len(alias) < FUZZY_MIN_LENGTH or abs(len(alias) - len(chunk)) > 3 or alias == chunk:
                    continue
                ratio = SequenceMatcher(None, chunk, alias).ratio()
                if ratio >= FUZZY_RATIO and (not best or ratio > best[4]):
                    best = (offsets[i], offsets[i] + len(chunk), alias, ids, ratio)
            if best:
                hits.append(best)
    return hits


def resolve_place(text):
    """The place a report's location text points to.

    Returns {"place": id or None, "candidates": [...], "side": ...}. "place" is only
    set when the text names exactly one place, so the app never guesses between
    the two xerox shops.
    """
    mentions = find_places(text)
    if not mentions:
        return {"place": None, "candidates": [], "side": "inside"}
    first = mentions[0]
    single = first["ids"][0] if len(first["ids"]) == 1 else None
    return {"place": single, "candidates": first["ids"], "side": first["side"]}


SEARCH_FILLER = {"the", "a", "an", "near", "at", "in", "inside", "outside", "on", "by", "around",
                 "items", "item", "things", "stuff", "lost", "found", "from", "of", "front",
                 "next", "to", "behind", "beside", "opposite", "bit"}


def split_search(text):
    """Split a search into the places it names and the words left over.

    "black bottle canteen" gives the three canteen places and "black bottle", so
    the page can show bottles found at any of them.
    """
    clean = normalise(text)
    ids, keep = [], list(clean)
    for mention in find_places(text):
        ids.extend(i for i in mention["ids"] if i not in ids)
        start, end = mention["span"]
        keep[start:end] = " " * (end - start)
    rest = [word for word in "".join(keep).split() if word not in SEARCH_FILLER]
    return ids, " ".join(rest)
