"""全站个股板块标签。只读归属扩展表和股票行情缓存，不触发外部行情请求。"""
from __future__ import annotations

import math
import time
from pathlib import Path
from threading import Lock

import polars as pl

from app.market_time import cn_now, cn_today
from app.services import preferences
from app.services.ext_data import ExtConfigStore
from app.services.market_overview_builder import (
    _dimension_field,
    _dimension_values,
    _ext_files,
    _read_ext_rows,
    _symbol_keys,
)
from app.services.sector_rotation import _DEFAULT_EXCLUDE_SECTORS

TTL_SECONDS = 30
# 轮动榜还过滤一些事件题材；个股标签保留这些真实题材，只排除交易/持仓属性。
_EXCLUDED = tuple(word for word in _DEFAULT_EXCLUDE_SECTORS if word not in {
    "并购重组", "股权激励", "员工持股", "次新", "壳资源", "破净", "ST板块", "ST股",
})


def concept_heat(quotes: pl.DataFrame, members: pl.DataFrame) -> dict[str, float]:
    """沿用概念页热度公式；涨跌幅为小数，换手率为百分点，无有效行情不评分。"""
    if quotes.is_empty() or members.is_empty():
        return {}
    defaults = {
        "change_pct": None, "turnover_rate": 0., "amount": 0.,
        "float_market_cap": None, "market_cap": 0.,
        "vol_ratio_5d": 1., "consecutive_limit_ups": 0.,
    }
    frame = quotes.with_columns([
        pl.lit(default, dtype=pl.Float64).alias(key)
        for key, default in defaults.items() if key not in quotes.columns
    ]).with_columns([
        pl.when(pl.col(key).cast(pl.Float64, strict=False).is_finite())
        .then(pl.col(key).cast(pl.Float64, strict=False)).otherwise(None).alias(key)
        for key in defaults
    ])

    def clamp(expr):
        return expr.clip(0., 1.)

    def log_part(expr, ceiling):
        return clamp(expr.fill_null(0.).clip(lower_bound=0.).log1p() / math.log1p(ceiling))

    leader = (
        clamp((pl.col("change_pct").fill_null(0.) + .02) / .12) * .35
        + log_part(pl.col("turnover_rate"), 30) * .22
        + log_part(pl.col("amount"), 20_000_000_000) * .18
        + log_part(pl.coalesce("float_market_cap", "market_cap"), 300_000_000_000) * .15
        + clamp((pl.col("vol_ratio_5d").fill_null(1.) - 1) / 4) * .07
        + clamp(pl.col("consecutive_limit_ups").fill_null(0.) / 5) * .03
    )
    frame = frame.with_columns(leader.alias("_leader"))
    grouped = members.unique().join(frame, on="symbol", how="left").group_by("concept").agg(
        pl.len().alias("_members"),
        pl.col("change_pct").count().alias("_priced"),
        pl.col("change_pct").mean().alias("_avg"),
        (pl.col("change_pct") > 0).sum().alias("_up"),
        (pl.col("change_pct") >= .05).sum().alias("_strong"),
        pl.col("amount").sum().alias("_amount"),
        pl.col("_leader").max().alias("_leader"),
    ).filter(pl.col("_priced") > 0)
    grouped = grouped.with_columns((
        clamp((pl.col("_avg") + .02) / .09) * .38
        + clamp((pl.col("_up") / pl.col("_priced") - .35) / .55) * .20
        + clamp(pl.col("_strong") / pl.max_horizontal(1., pl.col("_members") * .18)) * .16
        + log_part(pl.col("_amount"), 80_000_000_000) * .12
        + pl.col("_leader").fill_null(0.) * .14
    ).mul(100).alias("heat"))
    return dict(grouped.select("concept", "heat").iter_rows())


class StockSectorLabelsService:
    def __init__(self, repo):
        self.repo = repo
        self._cache: dict[tuple, tuple[float, tuple, dict]] = {}
        self._lock = Lock()

    def _source(self, configs, kind, requested=None):
        if requested:
            config_id, separator, field = requested.partition(".")
            config = next((c for c in configs if c.id == config_id), None)
            if not separator or not config or not any(f.name == field for f in config.fields):
                raise ValueError("概念数据字段不存在")
            return config, field
        configured = preferences.get_monitor_ext_fields().get(kind)
        if configured and configured.get("field"):
            config_id, _, field = configured["field"].partition(".")
            config = next((c for c in configs if c.id == config_id), None)
            if config and any(f.name == field for f in config.fields):
                return config, field
        preset = "ext_gn_ths" if kind == "concept" else "ext_hy_ths"
        for config in sorted(configs, key=lambda c: (c.id != preset, c.id)):
            field = _dimension_field(config, kind)
            if field:
                return config, field
        return None

    def snapshot(self, concept_source: str | None = None) -> dict:
        data_dir = self.repo.store.data_dir
        configs = ExtConfigStore(data_dir).load_all()
        sources = {
            "concept": self._source(configs, "concept", concept_source),
            "industry": self._source(configs, "industry"),
        }
        source_key = tuple((kind, source[0].id, source[1]) for kind, source in sources.items() if source)
        # mtime 校验保证手动导入/定时更新/配置删除立即生效，不等 30s TTL。
        files = []
        for config, _field in (s for s in sources.values() if s):
            files.extend(_ext_files(data_dir, config))
            files.append(str(data_dir / "ext_data" / config.id / "config.json"))
        stamps = []
        for file in sorted(set(files)):
            try:
                stat = Path(file).stat()
            except FileNotFoundError:
                # 删除配置/替换分区可能与标签请求并发，下一次签名会再次校验。
                continue
            stamps.append((file, stat.st_mtime_ns, stat.st_size))
        signature = (str(cn_today()), tuple(stamps))
        with self._lock:
            hit = self._cache.get(source_key)
        if hit and hit[1] == signature and time.monotonic() - hit[0] < TTL_SECONDS:
            return hit[2]

        # 构造完整快照后原子替换；读取文件和 Polars 计算均不持锁。
        payload = self._build(sources)
        with self._lock:
            self._cache[source_key] = (time.monotonic(), signature, payload)
            while len(self._cache) > 8:
                oldest = min(self._cache, key=lambda key: self._cache[key][0])
                del self._cache[oldest]
        return payload

    def _build(self, sources) -> dict:
        instruments = self.repo.get_instruments()
        quotes, as_of = self.repo.get_enriched_latest()
        symbols = set()
        for frame in (instruments, quotes):
            if "symbol" not in frame.columns:
                continue
            if "asset_type" in frame.columns:
                frame = frame.filter(pl.col("asset_type").fill_null("stock") == "stock")
            symbols.update(str(s).strip().upper() for s in frame["symbol"] if s)
        aliases = {}
        for symbol in sorted(symbols):
            bare = symbol.split(".")[0]
            aliases[bare] = symbol if bare not in aliases else None
        aliases = {k: v for k, v in aliases.items() if v}
        rows = {symbol: {"concept": None, "industry": None} for symbol in sorted(symbols)}
        memberships: dict[str, dict[str, set[str]]] = {"concept": {}, "industry": {}}
        source_fields = {}
        for kind, source in sources.items():
            source_fields[kind] = f"{source[0].id}.{source[1]}" if source else None
            if not source:
                continue
            config, field = source
            for row in _read_ext_rows(self.repo.store.data_dir, config, field):
                keys = _symbol_keys(row, config)
                # 全代码必须精确匹配，不能把 000001.SH 指数借裸代码映射成股票。
                full = [key for key in keys if "." in key]
                symbol = next((key for key in full if key in symbols), None) if full else next(
                    (aliases[key] for key in keys if key in aliases), None,
                )
                if symbol:
                    memberships[kind].setdefault(symbol, set()).update(_dimension_values(row.get(field)))

        # 和 market-snapshot 一样补齐市值，全部从仓库维表读取。
        if not quotes.is_empty():
            if "symbol" in instruments.columns:
                cols = [c for c in ("total_shares", "float_shares") if c in instruments.columns and c not in quotes.columns]
                if cols:
                    quotes = quotes.join(instruments.select("symbol", *cols), on="symbol", how="left")
            for shares, cap in (("total_shares", "market_cap"), ("float_shares", "float_market_cap")):
                if "close" in quotes.columns and shares in quotes.columns and cap not in quotes.columns:
                    quotes = quotes.with_columns((pl.col("close") * pl.col(shares)).alias(cap))
            if "date" in quotes.columns and as_of:
                quotes = quotes.filter(pl.col("date").cast(pl.Date) == as_of)
            quotes = quotes.with_columns(pl.col("symbol").str.to_uppercase()).unique("symbol")
        pairs = [(symbol, concept) for symbol, concepts in memberships["concept"].items() for concept in concepts]
        members = pl.DataFrame(pairs, schema={"symbol": pl.String, "concept": pl.String}, orient="row")
        heat = concept_heat(quotes, members) if as_of else {}
        for symbol, row in rows.items():
            candidates = [
                name for name in memberships["concept"].get(symbol, ())
                if name in heat and not any(word.casefold() in name.casefold() for word in _EXCLUDED)
            ]
            row["concept"] = min(candidates, key=lambda name: (-heat[name], name)) if candidates else None
            industries = sorted(memberships["industry"].get(symbol, ()))
            row["industry"] = " / ".join(industries) or None
        return {
            "as_of": str(as_of) if as_of else None,
            "updated_at": cn_now().isoformat(),
            "concept_source": source_fields["concept"],
            "industry_source": source_fields["industry"],
            "concept_heat": heat, "stocks": rows, "aliases": aliases,
        }
