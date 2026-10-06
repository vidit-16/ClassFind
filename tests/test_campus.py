import unittest

import campus


class PlaceWordsTestCase(unittest.TestCase):
    def place(self, text):
        return campus.resolve_place(text)["place"]

    def test_names_people_use_reach_the_same_place(self):
        for text in ("Mech parking", "mechanical parking", "garage", "Mechanical parking and garage"):
            self.assertEqual(self.place(text), "mech-parking", text)

    def test_departments_and_rooms_point_to_their_building(self):
        for text in ("computer science lab", "CSE dept, 3rd floor", "BIT Library, 2nd floor", "VLSI lab",
                     "placement cell", "near the quadrangle", "PE room"):
            self.assertEqual(self.place(text), "main-block", text)
        self.assertEqual(self.place("MCA department"), "mba")
        self.assertEqual(self.place("physics lab"), "chem-phy")
        self.assertEqual(self.place("RAI lab"), "mech-blocks")
        self.assertEqual(self.place("Upahara Darshini"), "kims-canteen")

    def test_the_bank_is_inside_and_the_atm_is_its_own_place(self):
        for text in ("Canara bank", "near the bank counter", "bank"):
            self.assertEqual(self.place(text), "main-block", text)
        for text in ("ATM", "bank ATM", "Canara ATM", "outside the canara bank atm"):
            self.assertEqual(self.place(text), "atm", text)

    def test_paths_are_called_paths_but_road_still_works(self):
        self.assertTrue(campus.place_name("road-main").startswith("Main path"))
        self.assertEqual(self.place("main road"), "road-main")
        self.assertEqual(self.place("main path"), "road-main")

    def test_misheard_and_misspelt_places_still_match(self):
        self.assertEqual(self.place("mesh parking"), "mech-parking")
        self.assertEqual(self.place("mechnical parkin"), "mech-parking")
        self.assertEqual(self.place("kalashetra"), "kalakshetra")

    def test_words_shared_by_several_places_are_not_guessed(self):
        canteen = campus.resolve_place("near the canteen")
        self.assertIsNone(canteen["place"])
        self.assertEqual(canteen["candidates"], ["canteen", "puff-shop", "nandini"])
        self.assertEqual(campus.resolve_place("xerox")["candidates"], ["xerox-mech", "xerox-canteen"])
        self.assertEqual(campus.resolve_place("AI lab")["candidates"], ["main-block", "mech-blocks"])
        self.assertEqual(campus.resolve_place("auditorium")["candidates"], ["main-block", "kalakshetra"])
        self.assertEqual(campus.resolve_place("mech lab")["candidates"], ["mech-blocks", "workshops"])
        # Naming the shop exactly settles it.
        self.assertEqual(self.place("mech xerox"), "xerox-mech")
        self.assertEqual(self.place("xerox near canteen"), "xerox-canteen")
        self.assertEqual(self.place("KIMS canteen"), "kims-canteen")

    def test_outside_a_building_is_told_apart_from_inside(self):
        self.assertEqual(campus.resolve_place("outside the main block")["side"], "outside")
        self.assertEqual(campus.resolve_place("in front of workshops")["side"], "outside")
        self.assertEqual(campus.resolve_place("inside the library")["side"], "inside")
        self.assertEqual(campus.resolve_place("on the main road")["place"], "road-main")

    def test_kannada_and_hindi_words(self):
        self.assertEqual(campus.resolve_place("ಕ್ಯಾಂಟೀನ್ ಹತ್ತಿರ")["candidates"], ["canteen", "puff-shop", "nandini"])
        self.assertEqual(self.place("लाइब्रेरी में"), "main-block")

    def test_unknown_places_and_item_words_match_nothing(self):
        for text in ("Hostel", "Black Milton bottle", "my bag", ""):
            self.assertEqual(campus.resolve_place(text)["candidates"], [], text)

    def test_search_splits_places_from_the_item(self):
        self.assertEqual(campus.split_search("black bottle canteen"),
                         (["canteen", "puff-shop", "nandini"], "black bottle"))
        self.assertEqual(campus.split_search("mechanical parking"), (["mech-parking"], ""))
        self.assertEqual(campus.split_search("black wallet"), ([], "black wallet"))


class CampusMapTestCase(unittest.TestCase):
    def test_every_place_and_road_is_reachable(self):
        neighbours = {i: set() for i in range(len(campus.CAMPUS["nodes"]))}
        for a, b, length, road in campus.CAMPUS["edges"]:
            self.assertGreater(length, 0)
            self.assertTrue(campus.is_place(road), road)
            neighbours[a].add(b)
            neighbours[b].add(a)
        seen, todo = {0}, [0]
        while todo:
            for other in neighbours[todo.pop()] - seen:
                seen.add(other)
                todo.append(other)
        self.assertEqual(len(seen), len(neighbours))
        for place in campus.CAMPUS["places"]:
            self.assertTrue(place["doors"], place["id"])

    def test_groups_and_desk_name_real_places(self):
        for ids in campus.GROUPS.values():
            self.assertTrue(all(i in campus.PLACES for i in ids), ids)
        self.assertEqual(campus.CAMPUS["desk"]["place"], "main-block")


if __name__ == "__main__":
    unittest.main()
