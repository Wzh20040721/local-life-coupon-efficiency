import csv
import json
import tempfile
import unittest
from pathlib import Path

import duckdb

from analysis_full import (
    build_order_facts,
    calculate_scenarios,
    classify_coupon_outcomes,
    register_source_views,
    run_analysis,
)


class AnalysisContractsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self._write_fixture_files()

    def tearDown(self):
        self.tempdir.cleanup()

    def _write(self, filename, header, rows):
        with (self.root / filename).open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(rows)

    def _write_fixture_files(self):
        self._write(
            "①合并画像_232375490_20260123_encrypted.csv",
            ["User_id", "gender_pred", "age_pred", "city_rank", "A_pay_level"],
            [["u1", 1, 3, "A", "L3"], ["u2", 0, 4, "B", "L2"]],
        )
        self._write(
            "②活跃_232401619_20260123.csv",
            ["User_id", "Visit_date"],
            [["u1", "2023-06-01"], ["u2", "2023-06-02"]],
        )
        self._write(
            "③获券_232388348_20260123.csv",
            [
                "User_id",
                "Coupon_id",
                "Coupon_batch_id",
                "Coupon_status",
                "Coupon_bu",
                "Coupon_amt",
                "Receive_date",
                "Start_date",
                "End_date",
                "Price_limit",
            ],
            [
                ["u1", "c1", "b1", 2, "A", 5, "2023-06-01", "2023-06-01", "2023-06-30", 0],
                ["u1", "c2", "b2", 3, "A", 10, "2023-06-01", "2023-06-01", "2023-06-30", 20],
                ["u2", "c3", "b3", 1, "A", 20, "2023-06-20", "2023-06-20", "2023-07-05", 100],
                ["u2", "c4", "b4", "", "D", "", "2023-06-01", "2023-06-01", "2023-06-30", ""],
            ],
        )
        self._write(
            "④交易数据_234065457_20260126.csv",
            [
                "User_id",
                "Shop_id",
                "Order_id",
                "Coupon_id",
                "Coupon_batch_id",
                "Coupon_type",
                "Coupon_category",
                "BU_name",
                "Pay_date",
                "Actual_pay",
                "Reduce_amount",
            ],
            [
                ["u1", "s1", "o1", "c1", "b1", 1, 1, "A", "2023-06-10", 10, 2],
                ["u1", "s1", "o1", "", "", "", "", "A", "2023-06-10", 10, 1],
                ["u2", "s2", "o2", "", "", "", "", "B", "2023-06-11", 20, 0],
            ],
        )

    def test_order_grain_counts_payment_once_and_sums_discount_components(self):
        con = duckdb.connect()
        register_source_views(con, self.root)
        build_order_facts(con)
        row = con.execute(
            """
            SELECT COUNT(*) AS orders,
                   SUM(actual_pay + subsidy) AS gmv,
                   SUM(subsidy) AS subsidy
            FROM order_facts
            """
        ).fetchone()
        self.assertEqual(row[0], 2)
        self.assertAlmostEqual(row[1], 33.0)
        self.assertAlmostEqual(row[2], 3.0)

    def test_coupon_outcomes_separate_maturity_match_and_unknown_status(self):
        con = duckdb.connect()
        register_source_views(con, self.root)
        outcomes = classify_coupon_outcomes(con, "2023-06-30")
        self.assertEqual(outcomes["matched_coupon_rows"], 1)
        self.assertEqual(outcomes["matured_no_transaction"], 2)
        self.assertEqual(outcomes["right_censored_no_transaction"], 1)
        self.assertEqual(outcomes["status_other"], 1)
        self.assertEqual(outcomes["status_unknown"], 1)

    def test_savings_scenario_exposes_contact_and_subsidy_assumptions(self):
        result = calculate_scenarios(
            {
                "matured_no_transaction": 100,
                "actual_subsidy": 1000.0,
                "gmv": 10000.0,
                "optimization_subsidy_pool": 200.0,
            },
            {"unit_contact_cost": 0.05, "pool_reduction_rate": 0.25},
        )
        self.assertEqual(result["contact_cost_saving"], 5.0)
        self.assertEqual(result["subsidy_saving"], 50.0)
        self.assertEqual(result["total_cost_saving"], 55.0)
        self.assertEqual(result["subsidy_after"], 950.0)
        self.assertAlmostEqual(result["gmv_per_subsidy_before"], 10.0)
        self.assertAlmostEqual(result["gmv_per_subsidy_after"], 10.526315789473685)

    def test_invalid_cutoff_is_rejected_before_building_sql(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            run_analysis(
                self.root,
                self.root / "invalid-cutoff-output",
                "2023-06-30' OR 1=1 --",
            )

    def test_run_analysis_writes_deterministic_auditable_artifacts(self):
        output_one = self.root / "out-one"
        output_two = self.root / "out-two"

        run_analysis(self.root, output_one, "2023-06-30")
        run_analysis(self.root, output_two, "2023-06-30")

        results_one = json.loads((output_one / "results.json").read_text("utf-8"))
        results_two = json.loads((output_two / "results.json").read_text("utf-8"))
        self.assertEqual(results_one, results_two)
        self.assertEqual(
            results_one["zero_observed_return"]["face_value_exposure"], 10.0
        )

        manifest = json.loads((output_one / "run_manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["cutoff"], "2023-06-30")
        self.assertEqual(
            manifest["source_rows"],
            {"profile": 2, "active": 2, "coupons": 4, "transactions": 3},
        )
        self.assertIn("order_grain_gmv", manifest["metric_definitions"])
        self.assertIn("matured_no_transaction", manifest["metric_definitions"])

        table_one = (output_one / "tables" / "zero_return_batches.csv").read_text("utf-8")
        table_two = (output_two / "tables" / "zero_return_batches.csv").read_text("utf-8")
        self.assertEqual(table_one, table_two)
        self.assertEqual(
            [line.split(",")[0] for line in table_one.strip().splitlines()[1:]],
            ["b2", "b4"],
        )


if __name__ == "__main__":
    unittest.main()
