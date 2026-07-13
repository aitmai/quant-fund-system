import unittest

from src.scoring.zscore import average_available_zscores, sector_zscore


class TestSectorZscore(unittest.TestCase):
    def test_zscore_computed_within_sector_not_across_universe(self):
        # Two sectors with wildly different scales — if z-scoring were
        # universe-wide, Tech's values would dominate and Utilities would
        # all look artificially extreme. Sector-relative keeps them separate.
        raw = {"T1": 100.0, "T2": 200.0, "T3": 300.0, "U1": 1.0, "U2": 2.0, "U3": 3.0}
        sectors = {"T1": "Tech", "T2": "Tech", "T3": "Tech", "U1": "Utilities", "U2": "Utilities", "U3": "Utilities"}

        z = sector_zscore(raw, sectors)

        # Middle ticker in each sector should be ~0 (at the sector mean)
        self.assertAlmostEqual(z["T2"], 0.0)
        self.assertAlmostEqual(z["U2"], 0.0)
        # Same relative position within its own sector -> same z-score,
        # regardless of the sectors' totally different raw scales.
        self.assertAlmostEqual(z["T3"], z["U3"])
        self.assertAlmostEqual(z["T1"], z["U1"])

    def test_sector_with_fewer_than_two_tickers_is_excluded(self):
        raw = {"SOLO": 50.0, "A": 1.0, "B": 2.0}
        sectors = {"SOLO": "Aerospace", "A": "Tech", "B": "Tech"}

        z = sector_zscore(raw, sectors)

        self.assertNotIn("SOLO", z)
        self.assertIn("A", z)
        self.assertIn("B", z)

    def test_zero_dispersion_sector_gets_zscore_zero_not_excluded(self):
        # Every ticker in the sector has the identical raw value —
        # legitimately z=0 for all (no dispersion), not "can't compute."
        raw = {"A": 5.0, "B": 5.0, "C": 5.0}
        sectors = {"A": "Tech", "B": "Tech", "C": "Tech"}

        z = sector_zscore(raw, sectors)

        self.assertEqual(z, {"A": 0.0, "B": 0.0, "C": 0.0})

    def test_empty_raw_returns_empty(self):
        self.assertEqual(sector_zscore({}, {}), {})

    def test_ticker_missing_from_sector_map_falls_back_to_unknown_bucket(self):
        raw = {"A": 1.0, "B": 2.0, "C": 3.0}
        sectors = {"A": "Tech", "B": "Tech"}  # C has no sector entry

        z = sector_zscore(raw, sectors)

        # C alone in "Unknown" -> excluded (n<2), doesn't crash.
        self.assertNotIn("C", z)
        self.assertIn("A", z)


class TestAverageAvailableZscores(unittest.TestCase):
    def test_averages_across_available_submetrics_only(self):
        sub1 = {"A": 1.0, "B": 2.0}
        sub2 = {"A": 3.0}  # B missing from this one

        avg = average_available_zscores([sub1, sub2])

        self.assertAlmostEqual(avg["A"], 2.0)  # (1+3)/2
        self.assertAlmostEqual(avg["B"], 2.0)  # just sub1's value, not diluted by a missing sub2

    def test_ticker_missing_from_every_submetric_is_absent(self):
        avg = average_available_zscores([{"A": 1.0}, {"A": 2.0}])
        self.assertNotIn("Z", avg)

    def test_empty_list_returns_empty(self):
        self.assertEqual(average_available_zscores([]), {})

    def test_single_submetric_dict_passthrough(self):
        avg = average_available_zscores([{"A": 5.0}])
        self.assertEqual(avg, {"A": 5.0})


if __name__ == "__main__":
    unittest.main()
