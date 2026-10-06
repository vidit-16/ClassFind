"""Write static/campus.json, the BIT campus map that the app and its pages share.

The layout was traced from a hand-drawn sketch of the campus, so distances are
approximate. Edit the tables below and run

    python tools/build_campus.py

The output holds every place with its outline and the words people use for it,
the roads, a graph of the roads for routing, and the door where each place
meets a road.
"""
import json
import math
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "static" / "campus.json"
# The sketch was traced with a margin; this moves it to the origin.
OX, OY = 330, 300
WIDTH, HEIGHT = 1010, 1380

# id, number on the paper map, name, short label, kind, outline, words people use.
# kind: bit (a BIT building), block (another institution), parking, small, gate.
PLACES = [
    ("kims-gate", 1, "KIMS A Block entrance", "KIMS A gate", "gate", (875, 372, 965, 406),
     ["kims a block entrance", "kims a block gate", "kims a gate", "kims gate", "kims entrance"]),
    ("kims-opd", 2, "KIMS OPD building", "KIMS OPD", "block", (605, 435, 765, 635),
     ["kims opd", "opd"]),
    ("kims-hospital", 3, "KIMS Hospital and Research Centre", "KIMS Hospital", "block", (838, 490, 1065, 630),
     ["kims hospital", "kims research centre", "hospital"]),
    ("kims-parking", 4, "KIMS parking", "KIMS Parking", "parking", (588, 662, 1065, 800),
     ["kims parking"]),
    ("kalakshetra", 6, "Kuvempu Kalakshetra", "Kalakshetra", "block", (795, 822, 1065, 868),
     ["kalakshetra", "kuvempu kalakshetra", "kala kshetra", "kalashetra"]),
    ("vs-gate", 7, "Vokkaliga Sangha entrance gate", "VS gate", "gate", (441, 712, 475, 802),
     ["vokkaliga sangha gate", "vokkaliga sangha entrance", "vs gate", "sangha gate"]),
    ("main-block", 8, "BIT Main Block", "Main Block", "bit",
     [(585, 812), (760, 812), (760, 890), (835, 890), (835, 1020), (585, 1020)],
     ["main block", "bit main block", "main building", "quadrangle", "quad", "library", "admin office",
      "administration", "office", "principal", "principal office", "placement cell", "placement office",
      "seminar hall", "seminar", "sports", "sports room", "pe", "physical education", "ignou",
      "security desk", "desk",
      "cse", "computer science", "cs lab", "cs department", "computer lab", "civil", "civil department",
      "eee", "electrical", "ete", "telecommunication", "tele communication", "telecom", "eie",
      "instrumentation", "ise", "information science", "ai ml", "aiml", "ai and ml", "ece",
      "electronics department", "electronics lab", "electronics and communication", "math", "maths", "mathematics",
      "cyber security", "cybersecurity", "cybersec", "iot", "internet of things", "data science", "vlsi",
      "ಲೈಬ್ರರಿ", "ಮೇನ್ ಬ್ಲಾಕ್", "लाइब्रेरी", "मेन ब्लॉक"]),
    ("p1", 9, "Parking 1", "P1", "parking", (495, 868, 545, 1022),
     ["parking 1", "parking one", "p1", "p 1", "first parking"]),
    ("mech-parking", 10, "Mechanical parking and garage", "Mech Parking", "parking", (905, 892, 1015, 1015),
     ["mech parking", "mechanical parking", "mechanical parking and garage", "mech garage", "garage",
      "mechanical garage"]),
    ("chem-phy", 11, "Chemistry and Physics block", "Chem/Phy", "bit", (1090, 768, 1130, 888),
     ["chemistry", "physics", "chemistry block", "physics block", "chem block", "phy block",
      "chemistry lab", "physics lab", "chem lab", "physics and chemistry", "chemistry and physics"]),
    ("mech-blocks", 12, "Mechanical blocks", "Mech Blocks", "bit", (1195, 715, 1335, 883),
     ["mech block", "mech blocks", "mechanical block", "mechanical blocks", "mechanical department",
      "mech department", "mechanical", "mech", "robotics", "rai", "robotics and ai",
      "robotics and artificial intelligence"]),
    ("society", 13, "BIT Society", "Society", "small", (1044, 945, 1076, 977),
     ["bit society", "society", "society office"]),
    ("xerox-mech", 14, "Mech parking xerox corner", "Mech xerox", "small", (1040, 1016, 1068, 1050),
     ["mech xerox", "mech parking xerox", "xerox near mech parking", "xerox near mech", "mechanical xerox"]),
    ("main-gate", 15, "BIT main gate", "BIT Main Gate", "gate", (441, 1055, 475, 1145),
     ["bit main gate", "bit gate", "main gate", "front gate", "gate 15"]),
    ("law-physio", 16, "Visveswarapura College of Law and Kempegowda Institute of Physiotherapy",
     "Law / Physio", "block", (585, 1125, 715, 1210),
     ["law college", "visveswarapura college of law", "physiotherapy", "physio", "kempegowda institute of physiotherapy"]),
    ("canteen", 17, "BIT Canteen", "Canteen", "bit", (735, 1125, 862, 1170),
     ["bit canteen", "main canteen", "college canteen"]),
    ("xerox-canteen", 18, "Xerox corner near the canteen", "Xerox", "small", (918, 1122, 945, 1158),
     ["xerox corner", "canteen xerox", "xerox near canteen", "xerox near the canteen"]),
    ("puff-shop", 19, "Puff shop", "Puff shop", "small", (920, 1192, 950, 1232),
     ["puff shop", "puff", "puffs shop", "bakery"]),
    ("dental", 20, "Vokkaligara Sangha Dental College and Hospital", "Dental College", "block",
     (965, 1125, 1245, 1280),
     ["dental college", "dental", "dental hospital", "vokkaligara sangha dental college"]),
    ("nandini", 21, "Nandini shop", "Nandini", "small", (476, 1205, 504, 1233),
     ["nandini", "nandini shop", "nandini booth", "milk booth"]),
    ("p2", 22, "Parking 2", "P2", "parking", (472, 1272, 538, 1445),
     ["parking 2", "parking two", "p2", "p 2", "second parking"]),
    ("kin-vcs", 23, "Kempegowda Institute of Nursing and Visveswarya College of Science", "KIN / VCS", "block",
     (598, 1225, 745, 1420),
     ["nursing college", "kempegowda institute of nursing", "kin", "visveswarya college of science",
      "vcs", "science college"]),
    ("vcs-gate", 24, "Visveswarya College of Science main gate", "VCS gate", "gate", (397, 1322, 431, 1412),
     ["vcs gate", "science college gate", "visveswarya college of science gate"]),
    ("kin-gate", 25, "Kempegowda Institute of Nursing main gate", "KIN gate", "gate", (464, 1500, 554, 1534),
     ["kin gate", "nursing gate", "nursing college gate"]),
    ("mba", 26, "MBA block", "MBA", "bit", (1090, 940, 1130, 1060),
     ["mba", "mba block", "mca", "mca block", "mba department", "mca department"]),
    ("workshops", 27, "Workshops", "Workshops", "bit", (1190, 935, 1280, 1060),
     ["workshop", "workshops", "mech workshop", "workshop lab"]),
    ("bit-tree", 28, "BIT Tree", "BIT Tree", "bit", (785, 1050, 813, 1078),
     ["bit tree", "the tree", "tree"]),
    ("kims-canteen", 29, "KIMS Canteen / Upahara Darshini", "KIMS Canteen", "block", (940, 422, 1045, 476),
     ["kims canteen", "upahara darshini", "upahara", "darshini"]),
]

# Words that could mean more than one place. A search covers all of them; a
# report asks which one.
GROUPS = {
    "canteen": ["canteen", "puff-shop", "nandini"],
    "ಕ್ಯಾಂಟೀನ್": ["canteen", "puff-shop", "nandini"],
    "कैंटीन": ["canteen", "puff-shop", "nandini"],
    "xerox": ["xerox-mech", "xerox-canteen"],
    "photocopy": ["xerox-mech", "xerox-canteen"],
    "ai lab": ["main-block", "mech-blocks"],
    "ai": ["main-block", "mech-blocks"],
    "auditorium": ["main-block", "kalakshetra"],
    "mech lab": ["mech-blocks", "workshops"],
    "mechanical lab": ["mech-blocks", "workshops"],
    "parking": ["p1", "p2", "mech-parking"],
    "kims": ["kims-opd", "kims-hospital", "kims-parking", "kims-canteen", "kims-gate"],
    "shop": ["puff-shop", "nandini", "xerox-mech", "xerox-canteen"],
    "gate": ["main-gate", "vs-gate", "kims-gate", "vcs-gate", "kin-gate"],
    "lab": ["main-block", "chem-phy", "mech-blocks", "workshops"],
    "ಪಾರ್ಕಿಂಗ್": ["p1", "p2", "mech-parking"],
    "पार्किंग": ["p1", "p2", "mech-parking"],
}

# id, name, polyline, words people use. Roads are places too: something found
# on the main road is reported there.
ROADS = [
    ("road-top", "Top road by the KIMS gate", [(330, 352), (1340, 352)], ["top road"]),
    ("road-west", "Outer road on the west side", [(380, 300), (380, 1680)], ["outer road", "west road"]),
    ("road-main", "Main road from the BIT main gate", [(380, 1100), (1340, 1100)],
     ["main road", "road from the main gate", "college road"]),
    ("road-south", "Bottom road by the nursing gate", [(330, 1625), (1340, 1625)], ["bottom road"]),
    ("road-p", "Road past P1, Nandini and P2", [(525, 757), (525, 1272)], []),
    ("road-middle", "Middle road by the canteen", [(885, 960), (885, 1262)], ["canteen road", "middle road"]),
    ("road-main-block", "Road from the Main Block to the main road", [(705, 1020), (705, 1100)], []),
    ("road-mech", "Mech road", [(1165, 800), (1165, 1100)], ["mech road"]),
    ("road-kims", "KIMS road", [(810, 655), (810, 462), (920, 462)], ["kims road"]),
    ("road-vs", "Road from the Vokkaliga Sangha gate", [(380, 757), (588, 757)], []),
    ("road-main-mech", "Road between the Main Block and Mech Parking", [(835, 950), (905, 950)], []),
    ("road-mech-parking", "Mech Parking road", [(1015, 905), (1165, 905)], ["mech parking road"]),
    ("road-xerox", "Lane by BIT Society and the Mech xerox", [(1028, 905), (1028, 1033), (1040, 1033)], []),
    ("road-below-mech-parking", "Road below Mech Parking", [(885, 1033), (1028, 1033)], []),
    ("road-main-kims", "Road from the Main Block up to KIMS parking", [(760, 850), (778, 850), (778, 800)], []),
    ("road-kims-gate", "Road from the KIMS A gate", [(920, 352), (920, 372)], []),
    ("road-kims-gate-in", "Road inside the KIMS A gate", [(920, 406), (920, 462)], []),
    ("road-kims-canteen", "Road to the KIMS canteen", [(920, 449), (940, 449)], []),
    ("road-kims-blocks", "Road between KIMS OPD and the hospital", [(765, 560), (838, 560)], []),
    ("road-kalakshetra", "Road from Kalakshetra to KIMS parking", [(900, 800), (900, 822)], []),
    ("road-society", "Road to BIT Society", [(1028, 963), (1044, 963)], []),
    ("road-chem-phy", "Road from Chem/Phy to the Mech road", [(1130, 828), (1165, 828)], []),
    ("road-mba", "Road between the MBA block and the workshops", [(1130, 985), (1190, 985)], []),
    ("road-mech-blocks", "Road into the Mech Blocks", [(1165, 800), (1195, 800)], []),
    ("road-canteen", "Road from the canteen to the main road", [(808, 1100), (808, 1125)], []),
    ("road-dental", "Road from the dental college to the main road", [(1100, 1100), (1100, 1125)], []),
    ("road-law", "Road from the law college to the main road", [(650, 1100), (650, 1125)], []),
    ("road-kin-vcs", "Road from Nursing / Science to the P2 road", [(525, 1320), (598, 1320)], []),
    ("road-vcs-gate", "Road from the VCS gate", [(380, 1367), (472, 1367)], []),
    ("road-kin-gate", "Road from P2 to the nursing gate", [(509, 1445), (509, 1500)], []),
    ("road-kin-gate-out", "Road outside the nursing gate", [(509, 1625), (509, 1534)], []),
    ("road-xerox-canteen", "Road to the xerox corner", [(885, 1140), (918, 1140)], []),
    ("road-puff", "Road to the puff shop", [(885, 1212), (920, 1212)], []),
]

# The road from the main gate to the Main Block meets it at the front, where the desk is.
DESK = {"place": "main-block", "point": (705, 1020)}
# Arrows marking the way in at each gate.
ENTRANCES = [((920, 322), (920, 368)), ((345, 735), (437, 735)), ((345, 1078), (437, 1078)),
             ((345, 1345), (393, 1345)), ((509, 1600), (509, 1540))]


def shift(point):
    return (point[0] - OX, point[1] - OY)


def outline(shape):
    if isinstance(shape, tuple):
        x0, y0, x1, y1 = shape
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return list(shape)


def project(point, a, b):
    """The nearest point to `point` on segment a-b, and its distance."""
    (px, py), (ax, ay), (bx, by) = point, a, b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0 if length == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / length))
    q = (ax + t * dx, ay + t * dy)
    return q, math.dist(point, q)


def on_segment(point, a, b, tolerance=0.5):
    return project(point, a, b)[1] <= tolerance


def segments():
    for road_id, _, points, _ in ROADS:
        for a, b in zip(points, points[1:]):
            yield road_id, a, b


def crossing(a, b, c, d):
    """Where two axis-aligned segments cross, if they do."""
    if a[0] == b[0] and c[1] == d[1]:
        x, y = a[0], c[1]
    elif a[1] == b[1] and c[0] == d[0]:
        x, y = c[0], a[1]
    else:
        return None
    if on_segment((x, y), a, b) and on_segment((x, y), c, d):
        return (x, y)
    return None


def inside(point, poly):
    x, y = point
    hit = False
    for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            hit = not hit
    return hit


def boundary_distance(point, poly):
    return min(project(point, a, b)[1] for a, b in zip(poly, poly[1:] + poly[:1]))


def doors():
    """Where each place meets the road network.

    A road that stops at a building's wall is its door. A gate sits on its road,
    so its door is where the road runs through it. Anything else uses the
    nearest point of the nearest road.
    """
    result = {}
    ends = {pt for _, points_, _ in ((r[0], r[2], r[3]) for r in ROADS) for pt in (points_[0], points_[-1])}
    for place_id, _, _, _, _, shape, _ in PLACES:
        poly = outline(shape)
        touching = [pt for pt in ends if boundary_distance(pt, poly) <= 9 or inside(pt, poly)]
        if not touching:
            centre = (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))
            best = min((project(centre, a, b) for _, a, b in segments()), key=lambda hit: hit[1])
            touching = [tuple(round(v) for v in best[0])]
        result[place_id] = sorted(touching)
    return result


def graph(door_points):
    """Split the roads wherever they meet, and return nodes and edges for routing."""
    cuts = {}
    segs = list(segments())
    for road_id, a, b in segs:
        cuts.setdefault((road_id, a, b), {a, b})
    extra = {pt for pts in door_points.values() for pt in pts}
    for i, (_, a, b) in enumerate(segs):
        key = segs[i]
        for j, (_, c, d) in enumerate(segs):
            if i == j:
                continue
            for pt in (c, d):
                if on_segment(pt, a, b):
                    cuts[key].add(pt)
            hit = crossing(a, b, c, d)
            if hit:
                cuts[key].add(hit)
        for pt in extra:
            if on_segment(pt, a, b):
                cuts[key].add(pt)
    nodes, index, edges = [], {}, []

    def node(pt):
        pt = (round(pt[0]), round(pt[1]))
        if pt not in index:
            index[pt] = len(nodes)
            nodes.append(pt)
        return index[pt]

    for (road_id, a, b), points in cuts.items():
        ordered = sorted(points, key=lambda pt: math.dist(a, pt))
        for p, q in zip(ordered, ordered[1:]):
            if p != q:
                edges.append((node(p), node(q), round(math.dist(p, q), 1), road_id))
    # Some roads only meet through a building or a car park, e.g. the road from
    # the Main Block to Mech Parking. A walk can cut through those.
    for place_id, pts in door_points.items():
        for i, p in enumerate(pts):
            for q in pts[i + 1:]:
                edges.append((node(p), node(q), round(math.dist(p, q), 1), place_id))
    return nodes, index, edges


def main():
    door_points = doors()
    nodes, index, edges = graph(door_points)
    data = {
        "note": "Generated by tools/build_campus.py from a hand-drawn sketch; distances are approximate.",
        "viewBox": [0, 0, WIDTH, HEIGHT],
        "places": [],
        "roads": [],
        "groups": GROUPS,
        "desk": {"place": DESK["place"], "point": shift(DESK["point"])},
        "entrances": [[shift(a), shift(b)] for a, b in ENTRANCES],
        "nodes": [shift(pt) for pt in nodes],
        "edges": [[a, b, length, road] for a, b, length, road in edges],
    }
    for place_id, number, name, short, kind, shape, words in PLACES:
        data["places"].append({
            "id": place_id, "number": number, "name": name, "short": short, "kind": kind,
            "outline": [shift(pt) for pt in outline(shape)],
            "doors": [index[(round(x), round(y))] for x, y in door_points[place_id]],
            "aliases": words,
        })
    for road_id, name, points, words in ROADS:
        data["roads"].append({"id": road_id, "name": name, "points": [shift(pt) for pt in points],
                              "aliases": words})
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{OUT.name}: {len(data['places'])} places, {len(data['roads'])} roads, "
          f"{len(nodes)} nodes, {len(edges)} edges")


if __name__ == "__main__":
    main()
