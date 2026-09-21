# -*- coding: utf-8 -*-
"""本地生活发券补贴效率评估。

直接读取四个 CSV，并在 DuckDB 中按业务口径构建可复现指标。

关键口径：交易文件是“订单 × 优惠组成”明细；订单 GMV 必须先按
``Order_id`` 聚合，``Actual_pay`` 只计一次，``Reduce_amount`` 分量求和。
平台实际补贴成本是 ``Reduce_amount``，券面额不是现金成本。
``Coupon_status=3`` 是“其他”，不能直接写成“过期”。截止日以后到期的
未匹配券属于右删失。券与交易的观察性匹配不等于因果增量。
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, Mapping

import duckdb


SOURCE_FILES = {
    "profile": "①合并画像_232375490_20260123_encrypted.csv",
    "active": "②活跃_232401619_20260123.csv",
    "coupons": "③获券_232388348_20260123.csv",
    "transactions": "④交易数据_234065457_20260126.csv",
}


def _sql_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def _require_source_files(data_dir: Path) -> Dict[str, Path]:
    paths = {name: data_dir / filename for name, filename in SOURCE_FILES.items()}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("缺少数据文件: " + ", ".join(missing))
    return paths


def _validate_cutoff(cutoff: str) -> str:
    try:
        parsed = date.fromisoformat(cutoff)
    except (TypeError, ValueError) as exc:
        raise ValueError("cutoff 必须是 YYYY-MM-DD 格式的有效日期") from exc
    canonical = parsed.isoformat()
    if cutoff != canonical:
        raise ValueError("cutoff 必须是 YYYY-MM-DD 格式的有效日期")
    return canonical


def register_source_views(con: duckdb.DuckDBPyConnection, data_dir: Path | str) -> None:
    """注册四个只读 CSV 视图，并显式转换所有分析字段。"""

    paths = _require_source_files(Path(data_dir))
    con.execute(
        f"""
        CREATE OR REPLACE VIEW profile_source AS
        SELECT CAST(User_id AS VARCHAR) AS user_id,
               TRY_CAST(gender_pred AS DOUBLE) AS gender_pred,
               TRY_CAST(age_pred AS DOUBLE) AS age_pred,
               CAST(city_rank AS VARCHAR) AS city_rank,
               CAST(A_pay_level AS VARCHAR) AS a_pay_level
        FROM read_csv_auto('{_sql_path(paths['profile'])}', header=true,
                           nullstr='NULL', sample_size=100000)
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE VIEW active_source AS
        SELECT CAST(User_id AS VARCHAR) AS user_id,
               TRY_CAST(Visit_date AS DATE) AS visit_date
        FROM read_csv_auto('{_sql_path(paths['active'])}', header=true,
                           nullstr='NULL', sample_size=100000)
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE VIEW coupon_source AS
        SELECT CAST(User_id AS VARCHAR) AS user_id,
               CAST(Coupon_id AS VARCHAR) AS coupon_id,
               CAST(Coupon_batch_id AS VARCHAR) AS coupon_batch_id,
               TRY_CAST(Coupon_status AS INTEGER) AS coupon_status,
               CAST(Coupon_bu AS VARCHAR) AS coupon_bu,
               TRY_CAST(Coupon_amt AS DOUBLE) AS coupon_amt,
               TRY_CAST(Receive_date AS DATE) AS receive_date,
               TRY_CAST(Start_date AS DATE) AS start_date,
               TRY_CAST(End_date AS DATE) AS end_date,
               TRY_CAST(Price_limit AS DOUBLE) AS price_limit
        FROM read_csv_auto('{_sql_path(paths['coupons'])}', header=true,
                           nullstr='NULL', sample_size=100000)
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE VIEW transaction_source AS
        SELECT CAST(User_id AS VARCHAR) AS user_id,
               CAST(Shop_id AS VARCHAR) AS shop_id,
               CAST(Order_id AS VARCHAR) AS order_id,
               CAST(Coupon_id AS VARCHAR) AS coupon_id,
               CAST(Coupon_batch_id AS VARCHAR) AS coupon_batch_id,
               TRY_CAST(Coupon_type AS INTEGER) AS coupon_type,
               CAST(Coupon_category AS VARCHAR) AS coupon_category,
               CAST(BU_name AS VARCHAR) AS bu_name,
               TRY_CAST(Pay_date AS DATE) AS pay_date,
               TRY_CAST(Actual_pay AS DOUBLE) AS actual_pay,
               TRY_CAST(Reduce_amount AS DOUBLE) AS reduce_amount
        FROM read_csv_auto('{_sql_path(paths['transactions'])}', header=true,
                           nullstr='NULL', sample_size=100000)
        """
    )


def build_order_facts(con: duckdb.DuckDBPyConnection) -> None:
    """按订单聚合；实付只计一次，补贴分量求和，并保留一致性检查列。"""

    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE order_facts AS
        SELECT order_id,
               MIN(user_id) AS user_id,
               MIN(shop_id) AS shop_id,
               MIN(bu_name) AS bu_name,
               MIN(pay_date) AS pay_date,
               MAX(actual_pay) AS actual_pay,
               SUM(COALESCE(reduce_amount, 0.0)) AS subsidy,
               COUNT(*) AS source_rows,
               COUNT(DISTINCT actual_pay) AS actual_pay_value_count,
               COUNT(DISTINCT user_id) AS user_value_count,
               COUNT(DISTINCT shop_id) AS shop_value_count,
               COUNT(DISTINCT pay_date) AS pay_date_value_count,
               COUNT(coupon_id) AS coupon_rows,
               COUNT(DISTINCT coupon_id) AS coupon_ids
        FROM transaction_source
        GROUP BY order_id
        """
    )


def classify_coupon_outcomes(
    con: duckdb.DuckDBPyConnection, cutoff: str = "2023-06-30"
) -> Dict[str, int]:
    """按交易匹配、到期可观察性和原始状态分类券记录。"""

    cutoff = _validate_cutoff(cutoff)
    cursor = con.execute(
        """
        WITH transaction_coupons AS (
            SELECT DISTINCT coupon_id
            FROM transaction_source
            WHERE coupon_id IS NOT NULL
        )
        SELECT COUNT(*) AS coupon_rows,
               COUNT(*) FILTER (WHERE tx.coupon_id IS NOT NULL) AS matched_coupon_rows,
               COUNT(*) FILTER (
                   WHERE tx.coupon_id IS NULL AND c.end_date <= CAST(? AS DATE)
               ) AS matured_no_transaction,
               COUNT(*) FILTER (
                   WHERE tx.coupon_id IS NULL AND c.end_date > CAST(? AS DATE)
               ) AS right_censored_no_transaction,
               COUNT(*) FILTER (WHERE c.coupon_status = 1) AS status_unused,
               COUNT(*) FILTER (WHERE c.coupon_status = 2) AS status_used,
               COUNT(*) FILTER (WHERE c.coupon_status = 3) AS status_other,
               COUNT(*) FILTER (WHERE c.coupon_status IS NULL) AS status_unknown
        FROM coupon_source c
        LEFT JOIN transaction_coupons tx USING (coupon_id)
        """,
        [cutoff, cutoff],
    )
    names = [item[0] for item in cursor.description]
    values = cursor.fetchone()
    return {name: int(value or 0) for name, value in zip(names, values)}


def calculate_scenarios(
    metrics: Mapping[str, float], assumptions: Mapping[str, float]
) -> Dict[str, float]:
    """计算显式假设下的成本与资金效率敏感性，不把情景写成实证结果。"""

    unit_contact_cost = float(assumptions.get("unit_contact_cost", 0.0))
    pool_reduction_rate = float(assumptions.get("pool_reduction_rate", 0.0))
    if unit_contact_cost < 0:
        raise ValueError("unit_contact_cost 不能为负")
    if not 0.0 <= pool_reduction_rate <= 1.0:
        raise ValueError("pool_reduction_rate 必须位于 [0, 1]")

    matured_no_transaction = float(metrics["matured_no_transaction"])
    actual_subsidy = float(metrics["actual_subsidy"])
    gmv = float(metrics["gmv"])
    optimization_pool = float(metrics["optimization_subsidy_pool"])
    if min(matured_no_transaction, actual_subsidy, gmv, optimization_pool) < 0:
        raise ValueError("情景输入不能为负")

    contact_cost_saving = matured_no_transaction * unit_contact_cost
    subsidy_saving = optimization_pool * pool_reduction_rate
    subsidy_after = actual_subsidy - subsidy_saving
    if subsidy_after <= 0:
        raise ValueError("情景后的补贴必须大于 0")

    return {
        "unit_contact_cost": unit_contact_cost,
        "pool_reduction_rate": pool_reduction_rate,
        "contact_cost_saving": contact_cost_saving,
        "subsidy_saving": subsidy_saving,
        "total_cost_saving": contact_cost_saving + subsidy_saving,
        "subsidy_after": subsidy_after,
        "gmv_per_subsidy_before": gmv / actual_subsidy,
        "gmv_per_subsidy_after": gmv / subsidy_after,
    }


def _fetch_one_dict(con: duckdb.DuckDBPyConnection, sql: str) -> Dict[str, Any]:
    cursor = con.execute(sql)
    names = [item[0] for item in cursor.description]
    values = cursor.fetchone()
    return {name: value for name, value in zip(names, values)}


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(_json_ready(dict(payload)), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _copy_query(con: duckdb.DuckDBPyConnection, query: str, path: Path) -> None:
    destination = str(path.resolve()).replace("'", "''")
    con.execute(f"COPY ({query}) TO '{destination}' (HEADER, DELIMITER ',')")


def run_analysis(
    data_dir: Path | str,
    output_dir: Path | str,
    cutoff: str = "2023-06-30",
) -> Dict[str, Any]:
    """运行全量分析并输出可审计、确定性的 JSON/CSV 证据。"""

    cutoff = _validate_cutoff(cutoff)
    output_path = Path(output_dir)
    tables_path = output_path / "tables"
    tables_path.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute("PRAGMA threads=4")
    register_source_views(con, data_dir)
    build_order_facts(con)

    source_rows = {
        "profile": int(con.execute("SELECT COUNT(*) FROM profile_source").fetchone()[0]),
        "active": int(con.execute("SELECT COUNT(*) FROM active_source").fetchone()[0]),
        "coupons": int(con.execute("SELECT COUNT(*) FROM coupon_source").fetchone()[0]),
        "transactions": int(
            con.execute("SELECT COUNT(*) FROM transaction_source").fetchone()[0]
        ),
    }

    order_quality = _fetch_one_dict(
        con,
        """
        SELECT COUNT(*) FILTER (WHERE source_rows > 1) AS multirow_orders,
               SUM(source_rows) - COUNT(*) AS component_rows_above_order_grain,
               COUNT(*) FILTER (WHERE actual_pay_value_count > 1) AS actual_pay_conflicts,
               COUNT(*) FILTER (
                   WHERE user_value_count > 1 OR shop_value_count > 1
                      OR pay_date_value_count > 1
               ) AS order_identity_conflicts
        FROM order_facts
        """,
    )
    coupon_quality = _fetch_one_dict(
        con,
        """
        WITH duplicated AS (
            SELECT coupon_id, COUNT(*) AS n
            FROM coupon_source
            GROUP BY coupon_id
            HAVING COUNT(*) > 1
        )
        SELECT (SELECT COUNT(*) FROM duplicated) AS duplicated_coupon_ids,
               (SELECT COALESCE(SUM(n - 1), 0) FROM duplicated) AS duplicate_coupon_rows,
               COUNT(*) FILTER (WHERE coupon_status IS NULL) AS null_status_rows,
               COUNT(*) FILTER (WHERE coupon_amt IS NULL) AS null_amount_rows,
               COUNT(*) FILTER (WHERE receive_date IS NULL) AS null_receive_date_rows,
               COUNT(*) FILTER (WHERE end_date IS NULL) AS null_end_date_rows
        FROM coupon_source
        """,
    )
    active_quality = _fetch_one_dict(
        con,
        """
        SELECT COUNT(*) AS active_rows,
               COUNT(DISTINCT user_id || '|' || CAST(visit_date AS VARCHAR)) AS user_days,
               COUNT(*) - COUNT(DISTINCT user_id || '|' || CAST(visit_date AS VARCHAR))
                   AS duplicate_user_days
        FROM active_source
        """,
    )

    coupon_outcomes = classify_coupon_outcomes(con, cutoff)
    order_metrics = _fetch_one_dict(
        con,
        """
        SELECT COUNT(*) AS orders,
               COUNT(DISTINCT user_id) AS ordering_users,
               COUNT(DISTINCT shop_id) AS shops,
               ROUND(SUM(actual_pay + subsidy), 2) AS gmv,
               ROUND(SUM(actual_pay), 2) AS actual_pay,
               ROUND(SUM(subsidy), 2) AS actual_subsidy,
               ROUND(100.0 * SUM(subsidy) / NULLIF(SUM(actual_pay + subsidy), 0), 4)
                   AS subsidy_rate_pct,
               ROUND(SUM(actual_pay + subsidy) / NULLIF(SUM(subsidy), 0), 4)
                   AS gmv_per_subsidy,
               COUNT(*) FILTER (WHERE subsidy > 0) AS subsidized_orders,
               ROUND(100.0 * COUNT(*) FILTER (WHERE subsidy > 0) / COUNT(*), 4)
                   AS subsidized_order_rate_pct
        FROM order_facts
        """,
    )
    risk_pools = _fetch_one_dict(
        con,
        """
        SELECT COUNT(*) FILTER (WHERE subsidy > 0 AND coupon_ids = 0)
                   AS no_coupon_link_orders,
               ROUND(COALESCE(SUM(subsidy) FILTER (
                   WHERE subsidy > 0 AND coupon_ids = 0
               ), 0), 2) AS no_coupon_link_subsidy,
               COUNT(*) FILTER (WHERE subsidy >= actual_pay AND subsidy > 0)
                   AS subsidy_ge_pay_orders,
               ROUND(COALESCE(SUM(subsidy) FILTER (
                   WHERE subsidy >= actual_pay AND subsidy > 0
               ), 0), 2) AS subsidy_ge_pay,
               COUNT(*) FILTER (
                   WHERE subsidy > 0 AND coupon_ids = 0 AND subsidy >= actual_pay
               ) AS overlap_orders,
               ROUND(COALESCE(SUM(subsidy) FILTER (
                   WHERE subsidy > 0 AND coupon_ids = 0 AND subsidy >= actual_pay
               ), 0), 2) AS overlap_subsidy,
               COUNT(*) FILTER (
                   WHERE subsidy > 0 AND (coupon_ids = 0 OR subsidy >= actual_pay)
               ) AS auditable_pool_orders,
               ROUND(COALESCE(SUM(subsidy) FILTER (
                   WHERE subsidy > 0 AND (coupon_ids = 0 OR subsidy >= actual_pay)
               ), 0), 2) AS auditable_pool_subsidy
        FROM order_facts
        """,
    )
    user_outcomes = _fetch_one_dict(
        con,
        f"""
        WITH transaction_coupons AS (
            SELECT DISTINCT coupon_id FROM transaction_source WHERE coupon_id IS NOT NULL
        ), users AS (
            SELECT c.user_id,
                   COUNT(*) AS issued,
                   COUNT(*) FILTER (WHERE tx.coupon_id IS NOT NULL) AS matched,
                   COUNT(*) FILTER (
                       WHERE tx.coupon_id IS NULL AND c.end_date <= DATE '{cutoff}'
                   ) AS matured_no_transaction
            FROM coupon_source c
            LEFT JOIN transaction_coupons tx USING (coupon_id)
            GROUP BY c.user_id
        )
        SELECT COUNT(*) AS coupon_users,
               COUNT(*) FILTER (WHERE matched = 0) AS users_with_zero_matched_coupon,
               ROUND(100.0 * COUNT(*) FILTER (WHERE matched = 0) / COUNT(*), 4)
                   AS zero_match_user_rate_pct,
               ROUND(AVG(issued), 4) AS avg_issued_per_user,
               ROUND(AVG(matched), 4) AS avg_matched_per_user
        FROM users
        """,
    )
    zero_return = _fetch_one_dict(
        con,
        f"""
        WITH transaction_coupons AS (
            SELECT DISTINCT coupon_id FROM transaction_source WHERE coupon_id IS NOT NULL
        ), batches AS (
            SELECT c.coupon_batch_id,
                   COUNT(*) FILTER (WHERE c.end_date <= DATE '{cutoff}') AS matured_issued,
                   COUNT(*) FILTER (
                       WHERE c.end_date <= DATE '{cutoff}' AND tx.coupon_id IS NOT NULL
                   ) AS matched,
                   COALESCE(SUM(COALESCE(c.coupon_amt, 0)) FILTER (
                       WHERE c.end_date <= DATE '{cutoff}'
                   ), 0) AS face_value_exposure
            FROM coupon_source c
            LEFT JOIN transaction_coupons tx USING (coupon_id)
            GROUP BY c.coupon_batch_id
        )
        SELECT COUNT(*) FILTER (WHERE matured_issued > 0 AND matched = 0)
                   AS zero_return_batches,
               COALESCE(SUM(matured_issued) FILTER (
                   WHERE matured_issued > 0 AND matched = 0
               ), 0) AS zero_return_coupons,
               ROUND(COALESCE(SUM(face_value_exposure) FILTER (
                   WHERE matured_issued > 0 AND matched = 0
               ), 0), 2) AS face_value_exposure,
               COUNT(*) FILTER (WHERE matured_issued >= 100 AND matched = 0)
                   AS zero_return_batches_100plus,
               COALESCE(SUM(matured_issued) FILTER (
                   WHERE matured_issued >= 100 AND matched = 0
               ), 0) AS coupons_in_zero_return_batches_100plus
        FROM batches
        """,
    )

    scenario_metrics = {
        "matured_no_transaction": coupon_outcomes["matured_no_transaction"],
        "actual_subsidy": float(order_metrics["actual_subsidy"]),
        "gmv": float(order_metrics["gmv"]),
        "optimization_subsidy_pool": float(risk_pools["auditable_pool_subsidy"]),
    }
    contact_cost_scenarios = [
        calculate_scenarios(
            scenario_metrics,
            {"unit_contact_cost": unit_cost, "pool_reduction_rate": 0.0},
        )
        for unit_cost in (0.01, 0.05, 0.10)
    ]
    subsidy_scenarios = [
        calculate_scenarios(
            scenario_metrics,
            {"unit_contact_cost": 0.0, "pool_reduction_rate": reduction},
        )
        for reduction in (0.10, 0.20, 0.30)
    ]

    results = {
        "cutoff": cutoff,
        "source_rows": source_rows,
        "data_quality": {
            "orders": order_quality,
            "coupons": coupon_quality,
            "active": active_quality,
        },
        "order_metrics": order_metrics,
        "coupon_outcomes": coupon_outcomes,
        "coupon_user_metrics": user_outcomes,
        "zero_observed_return": zero_return,
        "subsidy_audit_pools": risk_pools,
        "scenarios": {
            "contact_cost": contact_cost_scenarios,
            "subsidy_pool_reduction": subsidy_scenarios,
        },
    }

    manifest = {
        "cutoff": cutoff,
        "source_files": SOURCE_FILES,
        "source_rows": source_rows,
        "metric_definitions": {
            "order_grain_gmv": (
                "按 Order_id 聚合后，MAX(Actual_pay) + SUM(Reduce_amount)；"
                "仅当订单内 Actual_pay 一致性检查为 0 冲突时成立。"
            ),
            "actual_subsidy": "交易明细 Reduce_amount 在订单内及全局求和。",
            "matured_no_transaction": (
                "End_date 不晚于分析截止日，且 Coupon_id 未出现在交易表中的获券记录；"
                "表示无可追踪交易，不代表已证明零因果收益。"
            ),
            "auditable_pool_subsidy": (
                "无券 ID 可追踪的补贴订单，或订单补贴不低于用户实付的订单之并集；"
                "是审计优先池，不是已证实浪费。"
            ),
        },
        "scenario_policy": (
            "联系成本按外部单位成本敏感性测算；补贴节省假设审计池按给定比例削减且 GMV 不变。"
            "二者均非因果预测。"
        ),
    }

    _write_json(output_path / "results.json", results)
    _write_json(output_path / "run_manifest.json", manifest)

    _copy_query(
        con,
        """
        SELECT bu_name,
               COUNT(*) AS orders,
               ROUND(SUM(actual_pay + subsidy), 2) AS gmv,
               ROUND(SUM(subsidy), 2) AS subsidy,
               ROUND(100.0 * SUM(subsidy) / NULLIF(SUM(actual_pay + subsidy), 0), 4)
                   AS subsidy_rate_pct,
               ROUND(AVG(actual_pay + subsidy), 2) AS avg_order_gmv,
               ROUND(SUM(actual_pay + subsidy) / NULLIF(SUM(subsidy), 0), 4)
                   AS gmv_per_subsidy
        FROM order_facts
        GROUP BY bu_name
        ORDER BY subsidy DESC, bu_name
        """,
        tables_path / "bu_efficiency.csv",
    )
    _copy_query(
        con,
        f"""
        WITH transaction_coupons AS (
            SELECT DISTINCT coupon_id FROM transaction_source WHERE coupon_id IS NOT NULL
        )
        SELECT CASE
                   WHEN c.coupon_status = 1 THEN '1-unused'
                   WHEN c.coupon_status = 2 THEN '2-used'
                   WHEN c.coupon_status = 3 THEN '3-other'
                   ELSE 'unknown'
               END AS status_label,
               COUNT(*) AS issued,
               COUNT(*) FILTER (WHERE tx.coupon_id IS NOT NULL) AS transaction_matched,
               COUNT(*) FILTER (
                   WHERE tx.coupon_id IS NULL AND c.end_date <= DATE '{cutoff}'
               ) AS matured_no_transaction,
               COUNT(*) FILTER (WHERE c.end_date > DATE '{cutoff}') AS right_censored,
               ROUND(100.0 * COUNT(*) FILTER (WHERE tx.coupon_id IS NOT NULL)
                     / COUNT(*), 4) AS match_rate_pct
        FROM coupon_source c
        LEFT JOIN transaction_coupons tx USING (coupon_id)
        GROUP BY status_label
        ORDER BY status_label
        """,
        tables_path / "coupon_outcome_by_status.csv",
    )
    _copy_query(
        con,
        f"""
        WITH transaction_coupons AS (
            SELECT DISTINCT coupon_id FROM transaction_source WHERE coupon_id IS NOT NULL
        ), designed AS (
            SELECT c.*,
                   CASE
                       WHEN c.coupon_amt IS NULL THEN 'unknown'
                       WHEN c.coupon_amt < 5 THEN '00-<5'
                       WHEN c.coupon_amt < 10 THEN '01-5-<10'
                       WHEN c.coupon_amt < 20 THEN '02-10-<20'
                       WHEN c.coupon_amt < 50 THEN '03-20-<50'
                       WHEN c.coupon_amt < 100 THEN '04-50-<100'
                       ELSE '05-100+'
                   END AS amount_bucket,
                   CASE
                       WHEN c.price_limit IS NULL THEN 'unknown'
                       WHEN c.price_limit <= 0 THEN '00-no-threshold'
                       WHEN c.price_limit < 20 THEN '01-<20'
                       WHEN c.price_limit < 50 THEN '02-20-<50'
                       WHEN c.price_limit < 100 THEN '03-50-<100'
                       ELSE '04-100+'
                   END AS threshold_bucket,
                   tx.coupon_id IS NOT NULL AS matched
            FROM coupon_source c
            LEFT JOIN transaction_coupons tx USING (coupon_id)
            WHERE c.end_date <= DATE '{cutoff}'
        )
        SELECT coupon_bu, amount_bucket, threshold_bucket,
               COUNT(*) AS matured_issued,
               COUNT(*) FILTER (WHERE matched) AS transaction_matched,
               ROUND(100.0 * COUNT(*) FILTER (WHERE matched) / COUNT(*), 4)
                   AS match_rate_pct,
               ROUND(SUM(COALESCE(coupon_amt, 0)), 2) AS face_value_exposure
        FROM designed
        GROUP BY coupon_bu, amount_bucket, threshold_bucket
        ORDER BY coupon_bu, amount_bucket, threshold_bucket
        """,
        tables_path / "coupon_design_efficiency.csv",
    )
    _copy_query(
        con,
        f"""
        WITH transaction_coupons AS (
            SELECT DISTINCT coupon_id FROM transaction_source WHERE coupon_id IS NOT NULL
        ), batches AS (
            SELECT c.coupon_batch_id,
                   MIN(c.coupon_bu) AS coupon_bu,
                   COUNT(*) FILTER (WHERE c.end_date <= DATE '{cutoff}') AS matured_issued,
                   COUNT(*) FILTER (
                       WHERE c.end_date <= DATE '{cutoff}' AND tx.coupon_id IS NOT NULL
                   ) AS transaction_matched,
                   ROUND(SUM(COALESCE(c.coupon_amt, 0)) FILTER (
                       WHERE c.end_date <= DATE '{cutoff}'
                   ), 2) AS face_value_exposure
            FROM coupon_source c
            LEFT JOIN transaction_coupons tx USING (coupon_id)
            GROUP BY c.coupon_batch_id
        )
        SELECT coupon_batch_id, coupon_bu, matured_issued, transaction_matched,
               matured_issued - transaction_matched AS no_transaction_coupons,
               face_value_exposure
        FROM batches
        WHERE matured_issued > 0 AND transaction_matched = 0
        ORDER BY matured_issued DESC, coupon_batch_id
        LIMIT 5000
        """,
        tables_path / "zero_return_batches.csv",
    )
    _copy_query(
        con,
        f"""
        WITH transaction_coupons AS (
            SELECT DISTINCT coupon_id FROM transaction_source WHERE coupon_id IS NOT NULL
        ), activity AS (
            SELECT user_id, COUNT(DISTINCT visit_date) AS active_days
            FROM active_source GROUP BY user_id
        )
        SELECT CASE
                   WHEN COALESCE(a.active_days, 0) = 0 THEN '00-zero'
                   WHEN a.active_days <= 30 THEN '01-1-30'
                   WHEN a.active_days <= 90 THEN '02-31-90'
                   ELSE '03-91+'
               END AS activity_segment,
               COUNT(*) FILTER (WHERE c.end_date <= DATE '{cutoff}') AS matured_issued,
               COUNT(*) FILTER (
                   WHERE c.end_date <= DATE '{cutoff}' AND tx.coupon_id IS NOT NULL
               ) AS transaction_matched,
               ROUND(100.0 * COUNT(*) FILTER (
                   WHERE c.end_date <= DATE '{cutoff}' AND tx.coupon_id IS NOT NULL
               ) / NULLIF(COUNT(*) FILTER (WHERE c.end_date <= DATE '{cutoff}'), 0), 4)
                   AS match_rate_pct,
               COUNT(DISTINCT c.user_id) AS users
        FROM coupon_source c
        LEFT JOIN transaction_coupons tx USING (coupon_id)
        LEFT JOIN activity a USING (user_id)
        GROUP BY activity_segment
        ORDER BY activity_segment
        """,
        tables_path / "activity_segment_efficiency.csv",
    )
    _copy_query(
        con,
        """
        SELECT strftime(pay_date, '%Y-%m') AS month,
               COUNT(*) AS orders,
               ROUND(SUM(actual_pay + subsidy), 2) AS gmv,
               ROUND(SUM(subsidy), 2) AS subsidy,
               ROUND(100.0 * SUM(subsidy) / NULLIF(SUM(actual_pay + subsidy), 0), 4)
                   AS subsidy_rate_pct,
               ROUND(SUM(actual_pay + subsidy) / NULLIF(SUM(subsidy), 0), 4)
                   AS gmv_per_subsidy
        FROM order_facts
        GROUP BY month
        ORDER BY month
        """,
        tables_path / "monthly_order_efficiency.csv",
    )

    con.close()
    return _json_ready(results)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("."))
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--cutoff", default="2023-06-30")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    results = run_analysis(args.data_dir, args.output_dir, args.cutoff)
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
