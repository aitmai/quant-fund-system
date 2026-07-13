import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import numpy as np

from src.scoring import data_fetch


class TestFetchPricePanelDecimalHandling(unittest.TestCase):
    """Regression test: psycopg2 returns Postgres NUMERIC columns as
    decimal.Decimal, not float. If fetch_price_panel doesn't cast those to
    float64 at the boundary, the resulting object-dtype 'price' column
    crashes downstream in zscore.py's pandas groupby std/mean with
    "unsupported operand type(s) for -: 'float' and 'decimal.Decimal'" —
    a real failure hit against live Supabase data, not caught by any
    existing test because every other test constructs its price panel
    directly with native floats, never through this fetch function."""

    def _conn_returning(self, rows):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn

    def test_close_and_adj_close_cast_to_float64_not_left_as_decimal(self):
        rows = [
            ("AAA", date(2026, 7, 10), Decimal("100.50"), Decimal("100.50")),
            ("AAA", date(2026, 7, 11), Decimal("101.25"), Decimal("101.25")),
        ]
        conn = self._conn_returning(rows)

        df = data_fetch.fetch_price_panel(conn, as_of=date(2026, 7, 13))

        self.assertEqual(df["close"].dtype, np.float64)
        self.assertEqual(df["adj_close"].dtype, np.float64)
        self.assertEqual(df["price"].dtype, np.float64)
        self.assertNotIsInstance(df["price"].iloc[0], Decimal)

    def test_price_prefers_adj_close_falls_back_to_close_with_decimal_input(self):
        rows = [
            ("AAA", date(2026, 7, 10), Decimal("100.00"), Decimal("105.00")),  # has adj_close
            ("AAA", date(2026, 7, 11), Decimal("102.00"), None),               # missing adj_close
        ]
        conn = self._conn_returning(rows)

        df = data_fetch.fetch_price_panel(conn, as_of=date(2026, 7, 13))

        self.assertAlmostEqual(df["price"].iloc[0], 105.00)  # adj_close preferred
        self.assertAlmostEqual(df["price"].iloc[1], 102.00)  # fell back to close

    def test_decimal_price_panel_survives_pandas_groupby_std(self):
        """The actual crash site: zscore.py's sector_zscore does a pandas
        groupby().std()/.mean() over raw factor values. This asserts that
        boundary doesn't blow up when the panel originated as Decimal."""
        rows = [(f"T{i}", date(2026, 7, 1), Decimal(f"{100 + i}.00"), Decimal(f"{100 + i}.00")) for i in range(5)]
        conn = self._conn_returning(rows)

        df = data_fetch.fetch_price_panel(conn, as_of=date(2026, 7, 13))

        import pandas as pd
        df["sector"] = "Tech"
        try:
            std = df.groupby("sector")["price"].transform(lambda g: (g - g.mean()) / g.std(ddof=1))
        except TypeError as exc:
            self.fail(f"Decimal leaked past fetch_price_panel and broke pandas std: {exc}")
        self.assertFalse(std.isna().all())


if __name__ == "__main__":
    unittest.main()
