from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet
from app.services.stock_sector_labels import StockSectorLabelsService, concept_heat


@pytest.fixture(autouse=True)
def isolate_preferences(monkeypatch):
    monkeypatch.setattr("app.services.stock_sector_labels.preferences.get_monitor_ext_fields", lambda: {})


def write_dimension(tmp_path, kind, values):
    config = ExtConfig(
        id=f"ext_{kind}", label=kind, mode="snapshot",
        fields=[ExtField("symbol", "string", "代码"), ExtField(kind, "string", kind)],
    )
    ExtConfigStore(tmp_path).upsert(config)
    write_ext_parquet(pl.DataFrame({"symbol": list(values), kind: list(values.values())}), config, tmp_path)


@pytest.fixture
def repo(tmp_path):
    instruments = pl.DataFrame({"symbol": ["000001.SZ", "600001.SH", "600002.SH", "600003.SH"]})
    quotes = pl.DataFrame({
        "symbol": instruments["symbol"], "change_pct": [0.02, 0.10, -0.02, None],
        "amount": [1e8, 2e8, 1e8, 0.0], "turnover_rate": [5., 10., 2., None],
    })
    return SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_instruments=lambda: instruments,
        get_enriched_latest=lambda: (quotes, date(2026, 10, 28)),
    )


def test_hottest_is_a_member_not_first_or_global_leader(repo, tmp_path):
    write_dimension(tmp_path, "concept", {
        "000001.SZ": "冷门;热门;融资融券", "600001.SH": "热门",
        "600002.SH": "冷门", "600003.SH": "无行情",
    })
    write_dimension(tmp_path, "industry", {"000001.SZ": "金融-银行"})
    result = StockSectorLabelsService(repo).snapshot()
    item = result["stocks"]["000001.SZ"]
    assert item["concept"] == "热门"
    assert item["industry"] == "金融-银行"
    assert result["as_of"] == "2026-10-28"
    assert result["stocks"]["600003.SH"]["concept"] is None
    assert "000001.SH" not in result["stocks"]
    assert result["aliases"]["000001"] == "000001.SZ"


def test_missing_dimensions_and_quotes_keep_explicit_empty_values(repo):
    result = StockSectorLabelsService(repo).snapshot()
    assert result["stocks"]["000001.SZ"] == {"concept": None, "industry": None}
    assert result["concept_source"] is None
    repo.get_enriched_latest = lambda: (pl.DataFrame(), None)
    assert StockSectorLabelsService(repo).snapshot()["as_of"] is None


def test_map_write_invalidates_before_ttl_and_keeps_old_snapshot_immutable(repo, tmp_path):
    write_dimension(tmp_path, "concept", {"000001.SZ": "旧题材"})
    service = StockSectorLabelsService(repo)
    old = service.snapshot()
    assert service.snapshot() is old
    write_dimension(tmp_path, "concept", {"000001.SZ": "新题材"})
    new = service.snapshot()
    assert new["stocks"]["000001.SZ"]["concept"] == "新题材"
    assert old["stocks"]["000001.SZ"]["concept"] == "旧题材"


def test_quotes_refresh_after_ttl_without_mutating_previous_snapshot(repo, tmp_path, monkeypatch):
    write_dimension(tmp_path, "concept", {"000001.SZ": "主题甲;主题乙", "600001.SH": "主题甲", "600002.SH": "主题乙"})
    clock = [100.]
    monkeypatch.setattr("app.services.stock_sector_labels.time.monotonic", lambda: clock[0])
    service = StockSectorLabelsService(repo)
    before = service.snapshot()
    assert before["stocks"]["000001.SZ"]["concept"] == "主题甲"
    quotes, as_of = repo.get_enriched_latest()
    repo.get_enriched_latest = lambda: (quotes.with_columns((-pl.col("change_pct")).alias("change_pct")), as_of)
    assert service.snapshot() is before
    clock[0] += 31
    assert service.snapshot()["stocks"]["000001.SZ"]["concept"] == "主题乙"
    assert before["stocks"]["000001.SZ"]["concept"] == "主题甲"


def test_heat_preserves_formula_and_never_scores_missing_prices():
    quotes = pl.DataFrame({"symbol": ["a", "b"], "change_pct": [0.07, None]})
    members = pl.DataFrame({"symbol": ["a", "b"], "concept": ["A", "B"]})
    scores = concept_heat(quotes, members)
    # Existing concept page: mean 7%, breadth 100%, strong 100%, no amount,
    # leader momentum = (7% + 2%) / 12%, other leader components zero.
    expected = 38 + 20 + 16 + 14 * (0.75 * 0.35)
    assert scores["A"] == pytest.approx(expected)
    assert "B" not in scores


def test_ties_are_stable_and_membership_is_not_truncated(repo, tmp_path):
    concepts = ";".join([f"主题{i:03}" for i in range(150)])
    write_dimension(tmp_path, "concept", {"000001.SZ": concepts})
    result = StockSectorLabelsService(repo).snapshot()
    assert len(result["concept_heat"]) == 150
    assert result["stocks"]["000001.SZ"]["concept"] == "主题000"


def test_api_returns_snapshot_and_source_validation(repo):
    from app.api.kline import router

    app = FastAPI()
    app.state.repo = repo
    app.include_router(router)
    client = TestClient(app)
    response = client.get("/api/kline/instruments/sector-labels")
    assert response.status_code == 200
    assert response.json()["stocks"]["000001.SZ"]["concept"] is None
    assert client.get("/api/kline/instruments/sector-labels?concept_source=../../bad.x").status_code == 400


def test_new_day_and_quote_ttl_recompute_without_mixing_dates(repo, tmp_path, monkeypatch):
    from app.services import stock_sector_labels as labels

    write_dimension(tmp_path, "concept", {"000001.SZ": "甲;乙", "600001.SH": "乙"})
    today = date(2026, 10, 28)
    monkeypatch.setattr(labels, "cn_today", lambda: today)
    service = StockSectorLabelsService(repo)
    assert service.snapshot()["as_of"] == "2026-10-28"
    repo.get_enriched_latest = lambda: (pl.DataFrame({
        "symbol": ["000001.SZ", "600001.SH"], "date": [date(2026, 10, 29), date(2026, 10, 28)],
        "change_pct": [0.01, .2],
    }), date(2026, 10, 29))
    today = date(2026, 10, 29)
    result = service.snapshot()
    assert result["as_of"] == "2026-10-29"
    # 乙的旧日大涨不能加入今日热度；甲和乙仅有同一只有效行情，取固定名称序。
    assert result["concept_heat"]["甲"] == pytest.approx(result["concept_heat"]["乙"])
    assert result["stocks"]["000001.SZ"]["concept"] == "乙"


def test_configured_source_and_deleted_dimension_invalidate(repo, tmp_path, monkeypatch):
    write_dimension(tmp_path, "concept", {"000001.SZ": "默认"})
    write_dimension(tmp_path, "theme", {"000001.SZ": "指定题材"})
    monkeypatch.setattr("app.services.stock_sector_labels.preferences.get_monitor_ext_fields", lambda: {
        "concept": {"field": "ext_theme.theme"},
    })
    service = StockSectorLabelsService(repo)
    assert service.snapshot()["stocks"]["000001.SZ"]["concept"] == "指定题材"
    assert service.snapshot("ext_concept.concept")["stocks"]["000001.SZ"]["concept"] == "默认"
    ExtConfigStore(tmp_path).delete("ext_theme")
    assert service.snapshot()["stocks"]["000001.SZ"]["concept"] == "默认"


def test_nonfinite_data_and_index_collision_do_not_create_false_labels(repo, tmp_path):
    write_dimension(tmp_path, "concept", {"000001.SH": "指数题材", "600003.SH": "无效"})
    repo.get_enriched_latest = lambda: (pl.DataFrame({
        "symbol": ["600003.SH"], "change_pct": [float("nan")], "amount": [float("inf")],
    }), date(2026, 10, 29))
    result = StockSectorLabelsService(repo).snapshot()
    assert result["stocks"]["000001.SZ"]["concept"] is None
    assert result["concept_heat"] == {}


def test_snapshot_readers_share_complete_results(repo, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    write_dimension(tmp_path, "concept", {"000001.SZ": "并购重组"})
    service = StockSectorLabelsService(repo)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.snapshot(), range(12)))
    assert all(r["stocks"]["000001.SZ"]["concept"] == "并购重组" for r in results)
    assert all(len(r["stocks"]) == 4 for r in results)
