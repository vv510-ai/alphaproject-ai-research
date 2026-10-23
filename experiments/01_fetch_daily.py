"""第一份数据实验：取一只 A 股最近的日线，并保留来源和实际结果。"""

import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from importlib import import_module
from importlib.metadata import version
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import patch

import akshare as ak
import pandas as pd
import requests


SOURCES = {
    "eastmoney": ("AKShare / 东方财富", "stock_zh_a_hist"),
    "tencent": ("AKShare / 腾讯证券", "stock_zh_a_hist_tx"),
}
COLUMNS = ["日期", "股票代码", "开盘", "收盘", "最高", "最低", "成交量"]
TIMEOUT = (5, 10)  # 连接最多等 5 秒，等待响应数据最多等 10 秒；不是整轮总时长。


class DataValidationError(ValueError):
    """拿到数据后仍需检查，不能只凭请求成功就认为结果可用。"""


def fetch_raw(source, symbol, start, end):
    dates = {"start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}
    if source == "eastmoney":
        return ak.stock_zh_a_hist(
            symbol=symbol, period="daily", adjust="", timeout=TIMEOUT, **dates
        )

    # 只给已明确的沪深 A 股加交易所前缀，不猜其他证券所属市场。
    if symbol.startswith(("600", "601", "603", "605", "688")):
        tx_symbol = "sh" + symbol
    elif symbol.startswith(("000", "001", "002", "003", "300", "301")):
        tx_symbol = "sz" + symbol
    else:
        raise DataValidationError("腾讯备用接口尚未验证该类代码")

    module = import_module(ak.stock_zh_a_hist_tx.__module__)
    with requests.Session() as session:
        def get_with_timeout(url, **kwargs):
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = TIMEOUT
            return session.get(url, **kwargs)

        # 已安装版本的上市日期查询没有超时参数。只在本次单线程调用中补上，
        # 保留系统代理和证书校验，不修改依赖文件或电脑设置。
        start_module = import_module(module.get_tx_start_year.__module__)
        with ExitStack() as stack:
            for request_module in {module, start_module}:
                stack.enter_context(patch.object(request_module, "requests", SimpleNamespace(get=get_with_timeout)))
            data = ak.stock_zh_a_hist_tx(
                symbol=tx_symbol, adjust="", timeout=TIMEOUT, **dates
            )

    # 已核验 1.18.96 源码：sz000 股票被漏乘 100；其他代码已由库换算为股。
    if version("akshare") == "1.18.96" and tx_symbol.startswith("sz000") and "volume" in data:
        data = data.copy()
        data["volume"] = pd.to_numeric(data["volume"], errors="coerce") * 100
    return data


def normalize_and_check(data, source, symbol, start, end):
    if data.empty:
        raise DataValidationError("接口返回空表，没有可用日线")
    if source == "tencent":
        data = data.rename(columns={
            "date": "日期", "open": "开盘", "close": "收盘",
            "high": "最高", "low": "最低", "volume": "成交量",
        }).copy()
        # 腾讯返回的数据表没有代码列；此列来自请求，不是独立身份核验。
        data["股票代码"] = symbol
    missing = [column for column in COLUMNS if column not in data]
    if missing:
        raise DataValidationError("缺少必要字段：" + "、".join(missing))
    sample = data[COLUMNS].copy()
    sample["日期"] = pd.to_datetime(sample["日期"], errors="coerce").dt.date
    sample["股票代码"] = sample["股票代码"].astype("string")
    numbers = ["开盘", "收盘", "最高", "最低", "成交量"]
    for column in numbers:
        sample[column] = pd.to_numeric(sample[column], errors="coerce")
    if source == "eastmoney":
        sample["成交量"] *= 100  # 东方财富接口为手；本实验统一展示股。

    checks = {
        "no_missing_values": not sample.isna().any().any(),
        "finite_numbers": not sample[numbers].isin([float("inf"), -float("inf")]).any().any(),
        "dates_within_request": sample["日期"].between(start, end).all(),
        "unique_dates": not sample["日期"].duplicated().any(),
        "positive_prices": (sample[["开盘", "收盘", "最高", "最低"]] > 0).all().all(),
        "price_range_consistent": (
            (sample["最低"] <= sample[["开盘", "收盘"]].min(axis=1))
            & (sample["最高"] >= sample[["开盘", "收盘"]].max(axis=1))
        ).all(),
        "nonnegative_volume": (sample["成交量"] >= 0).all(),
    }
    if source == "eastmoney":
        checks["response_symbol_matches"] = (sample["股票代码"] == symbol).all()
    checks = {key: bool(value) for key, value in checks.items()}
    failed = [key for key, passed in checks.items() if not passed]
    if failed:
        raise DataValidationError("数据校验未通过：" + ", ".join(failed))
    sample = sample.sort_values("日期")
    sample["日期"] = sample["日期"].astype(str)
    return sample, checks


def error_details(error):
    # 只保存错误类别和已知原因，不把请求 URL、代理账号或令牌写进记录。
    pending, seen, chain, system_errors = [error], set(), [], set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(type(current).__name__)
        system_errors.update(re.findall(r"WinError \d+", str(current)))
        linked = [current.__cause__, current.__context__, getattr(current, "reason", None)]
        pending.extend(item for item in linked + list(current.args) if isinstance(item, BaseException))
    if isinstance(error, DataValidationError):
        reason = str(error)
    elif "RemoteDisconnected" in chain:
        reason = "连接在收到响应前被关闭，具体由哪一环节关闭仍未确定"
    elif any("Timeout" in name for name in chain):
        reason = "等待数据超时"
    elif "ProxyError" in chain:
        reason = "通过代理请求失败，需结合其他连接对照判断原因"
    else:
        reason = "接口未成功返回可验证的数据"
    return {
        "error_type": type(error).__name__, "error_chain": chain,
        "reason": reason, "system_errors": sorted(system_errors),
    }


def fetch_daily(symbol, start, end):
    attempts = []
    for source, (name, interface) in SOURCES.items():
        print(f"正在尝试：{name}", flush=True)
        attempt = {"source": name, "interface": interface}
        try:
            raw = fetch_raw(source, symbol, start, end)
            sample, checks = normalize_and_check(raw, source, symbol, start, end)
        except Exception as error:
            attempt.update(status="failed", **error_details(error))
            attempts.append(attempt)
            print(f"  {attempt['error_type']}：{attempt['reason']}", flush=True)
            if source == "eastmoney":
                print("  改用腾讯备用来源，仍查询同一只股票和同一段日期。", flush=True)
            continue

        attempt.update(status="ok", row_count=len(sample))
        attempts.append(attempt)
        return {
            "status": "ok", "source": name, "interface": interface,
            "attempts": attempts, "checks": checks, "row_count": len(sample),
            "latest_trade_date": sample.iloc[-1]["日期"],
            "rows": sample.to_dict(orient="records"),
            "price_unit": "元", "volume_unit": "股",
            "symbol_basis": "响应代码已核验" if source == "eastmoney" else "请求参数；响应数据表不含代码列",
        }
    return {"status": "failed", "source": None, "interface": None, "attempts": attempts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="600519", help="六位股票代码")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{6}", args.symbol):
        parser.error("股票代码必须是六位数字，例如 600519 或 000001")

    china_time = timezone(timedelta(hours=8))
    started = datetime.now(china_time)
    # 先查到昨天，避免把盘中仍在变化的数值当成最终收盘价。
    end = started.date() - timedelta(days=1)
    start = end - timedelta(days=14)
    evidence = {
        "status": "pending",
        "symbol": args.symbol,
        "data_kind": "daily_unadjusted",
        "source_priority": [name for name, _ in SOURCES.values()],
        "network_mode": "system_default",
        "akshare_version": version("akshare"),
        "python_version": sys.version.split()[0],
        "source_document": "https://akshare.akfamily.xyz/data/stock/stock.html",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "request_started_at": started.isoformat(timespec="seconds"),
    }
    print(f"股票代码：{args.symbol}；先试东方财富，失败后尝试腾讯")
    print(f"查询日期：{start} 至 {end}；类型：日线")
    print(f"本次请求时间：{started:%Y-%m-%d %H:%M:%S} 北京时间\n")

    evidence.update(fetch_daily(args.symbol, start, end))
    if evidence["status"] == "ok":
        sample = pd.DataFrame(evidence["rows"])
        print(f"\n实际来源：{evidence['source']}；共 {evidence['row_count']} 条日线")
        print("价格单位：元；成交量单位：股；以下展示最后五条：")
        print(sample.tail(5).to_string(index=False))
        print(f"\n最新一行的交易日期：{evidence['latest_trade_date']}")
        print("\n这些是按交易日记录的日线，不是此刻的实时报价。")
    else:
        print("\n两个来源均未取得可用行情。本次失败，不生成虚构价格。")

    evidence["request_finished_at"] = datetime.now(china_time).isoformat(timespec="seconds")
    output_dir = Path(__file__).resolve().parents[1] / "evidence" / "data-source"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"daily-{args.symbol}-{started:%Y%m%dT%H%M%S%f}.json"
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"\n实验记录：{output}")
    return 0 if evidence["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
