"""Parallel run: new AP pipeline vs the legacy process."""

from __future__ import annotations

from src.controls.parallel_run import EXPECTED_CLASSES, run_parallel_run
from src.integrations.ap_flow import run_ap_flow
from src.sources import ap
from src.sources.seed_ap import run_seed_ap


def _run(ap_workspace, ap_engine, inject=()):
    run_seed_ap(cfg=ap_workspace.cfg, root=ap_workspace.root)
    run_ap_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    return run_parallel_run(cfg=ap_workspace.cfg, root=ap_workspace.root, inject=list(inject))


def test_clean_parallel_run_has_no_unexpected_variances(ap_workspace, ap_engine):
    result = _run(ap_workspace, ap_engine)

    assert result["status"] == "PASS"
    assert result["counts"]["unexpected"] == 0
    assert set(result["classificationCounts"]) <= set(EXPECTED_CLASSES)
    assert sum(result["classificationCounts"].values()) == result["counts"]["compared"]


def test_missing_use_tax_is_classified_expected(ap_workspace, ap_engine):
    result = _run(ap_workspace, ap_engine)

    vouchers = {v["voucherId"]: v for v in ap.load_vouchers(ap_workspace.paths)}
    self_assessed = {vid for vid, v in vouchers.items()
                     if v["status"] == "posted" and v["tax"]["selfAssessedTaxTotal"] > 0}
    flagged = {i["voucherId"] for i in result["expectedVariances"]
               if i["classification"] == "EXPECTED_MISSING_USE_TAX"}

    assert self_assessed  # the seed produces self-assessed purchases
    assert flagged == self_assessed
    for item in result["expectedVariances"]:
        if item["classification"] == "EXPECTED_MISSING_USE_TAX":
            tax = vouchers[item["voucherId"]]["tax"]["selfAssessedTaxTotal"]
            assert item["delta"] == tax
            assert item["delta"] > 0  # legacy understated cost by the use tax


def test_blocked_vouchers_are_expected_unmatched(ap_workspace, ap_engine):
    run_seed_ap(cfg=ap_workspace.cfg, root=ap_workspace.root,
                inject=["price_variance", "no_po", "duplicate_invoice"])
    run_ap_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    result = run_parallel_run(cfg=ap_workspace.cfg, root=ap_workspace.root)

    assert result["status"] == "PASS"
    assert result["counts"]["unexpected"] == 0
    assert result["classificationCounts"].get("EXPECTED_BLOCKED_UNMATCHED") == 3
    for item in result["expectedVariances"]:
        if item["classification"] == "EXPECTED_BLOCKED_UNMATCHED":
            assert item["newAmount"] == 0.0
            assert item["newStatus"] == "blocked"


def test_legacy_drift_is_unexpected(ap_workspace, ap_engine):
    result = _run(ap_workspace, ap_engine, inject=["legacy_drift"])

    assert result["status"] == "FAIL"
    assert result["counts"]["unexpected"] == 1
    drift = result["unexpectedVariances"][0]
    assert drift["delta"] == -100.0
    assert [g for g in result["knownLegacyGaps"] if "match gate" in g]
