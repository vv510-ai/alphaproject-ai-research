"""第二份数据实验：读取最新报价，分别保留来源时间和本机接收时间。"""

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sys
import time

import requests


CHINA_TIME = timezone(timedelta(hours=8))
INTERVAL_SECONDS = 10
SOURCE = "腾讯行情 / qt.gtimg.cn"


class QuoteDataError(ValueError):
    """原始报价不满足本实验必要条件。"""


def market_symbol(symbol):
    if not re.fullmatch(r"[0-9]{6}", symbol):
        raise QuoteDataError("股票代码必须是六位数字")
    if symbol.startswith(("600", "601", "603", "605", "688")):
        return "sh" + symbol
    if symbol.startswith(("000", "001", "002", "003", "300", "301")):
        return "sz" + symbol
    raise QuoteDataError("本实验只处理已明确前缀的沪深 A 股，不猜其他证券的市场")


def parse_quote(body, symbol, received_at):
    # 接口返回的是带 ~ 分隔符的一串文本，不是 JSON；只解析，绝不执行文本。
    expected = market_symbol(symbol)
    match = re.fullmatch(r'\s*v_([a-z]{2}[0-9]{6})="([^"\r\n]*)";?\s*', body)
    if not match or match[1] != expected:
        raise QuoteDataError("响应格式或交易所代码不符，不能确认是所查询的股票")
    fields = match[2].split("~")
    if len(fields) < 35 or fields[2] != symbol or not fields[1].strip():
        raise QuoteDataError("报价字段缺失或股票代码不符")

    # 字段位置已和本次原始响应核对；协议变动时应失败，不猜价格。
    positions = {"price": 3, "previous_close": 4, "change": 31, "change_pct": 32}
    try:
        prices = {key: float(fields[index]) for key, index in positions.items()}
    except ValueError as error:
        raise QuoteDataError("价格或涨跌幅不是有效数字") from error
    if not all(math.isfinite(value) for value in prices.values()):
        raise QuoteDataError("报价含空值或非有限数值")
    if prices["price"] <= 0 or prices["previous_close"] <= 0:
        raise QuoteDataError("最新价或昨收不可用，不把零值当有效报价")
    calculated_change = prices["price"] - prices["previous_close"]
    calculated_pct = calculated_change / prices["previous_close"] * 100
    if abs(calculated_change - prices["change"]) > 0.02 or abs(calculated_pct - prices["change_pct"]) > 0.02:
        raise QuoteDataError("价格、昨收与涨跌字段不一致，暂不接受这份报价")

    source_time = None
    raw_time = fields[30].strip()
    if raw_time and raw_time != "-":
        if not re.fullmatch(r"[0-9]{14}", raw_time):
            raise QuoteDataError("来源时间格式变化，不能用本机时间代替")
        try:
            # 本课沪深 A 股时间按北京时间解释。此字段不等同于最后成交时刻。
            source_time = datetime.strptime(raw_time, "%Y%m%d%H%M%S").replace(tzinfo=CHINA_TIME)
        except ValueError as error:
            raise QuoteDataError("来源时间不是有效日期") from error
        if source_time > received_at + timedelta(seconds=60):
            raise QuoteDataError("来源时间明显晚于本机接收时间，需核查时钟或字段含义")

    age = round((received_at - source_time).total_seconds(), 3) if source_time else None
    if source_time is None:
        freshness = "来源时间缺失；无法判断报价新旧"
    elif source_time > received_at:
        freshness = "来源时间略晚于本机；可能存在时钟差，无法确认新鲜度"
    elif source_time.date() < received_at.date():
        freshness = "来源日期早于今天；需结合休市、停牌和缓存情况判断"
    else:
        freshness = "来源日期是今天；仍应看具体时分秒，不能仅据此称为实时"
    return {
        "symbol": symbol, "market_symbol": expected, "name": fields[1],
        **prices, "price_unit": "元", "change_pct_unit": "%",
        "source_reported_at": source_time.isoformat() if source_time else None,
        "source_time_raw": raw_time or None,
        "source_time_interpretation": "接口响应时间字段，按北京时间解析；不是已核实的最后成交时刻",
        "received_at": received_at.isoformat(timespec="milliseconds"),
        "source_age_seconds": age,
        "freshness_note": freshness,
    }


def fetch_once(symbol):
    url = "https://qt.gtimg.cn/q=" + market_symbol(symbol)
    started = datetime.now(CHINA_TIME)
    clock = time.perf_counter()
    result = {"source": SOURCE, "url": url, "request_started_at": started.isoformat(timespec="milliseconds")}
    try:
        response = requests.get(url, timeout=(5, 10))
        received = datetime.now(CHINA_TIME)
        result.update(http_status=response.status_code, received_at=received.isoformat(timespec="milliseconds"))
        response.raise_for_status()
        body = response.content.decode("gb18030")
        result["raw_text"] = body
        quote = parse_quote(body, symbol, received)
        result.update(status="ok", quote=quote)
    except (requests.RequestException, UnicodeError, QuoteDataError) as error:
        reason = str(error) if isinstance(error, QuoteDataError) else "网络请求或响应解码失败；本次没有有效报价"
        result.update(status="failed", error_type=type(error).__name__, reason=reason)
    result["request_duration_seconds"] = round(time.perf_counter() - clock, 3)
    return result


def compare_samples(samples):
    comparison = {
        "realtime_verified": False,
        "reason": "本实验不能证明市场端到本机的延迟；盘中持续更新、交易日及停牌状态还需单独核验",
    }
    if len(samples) != 2 or any(sample["status"] != "ok" for sample in samples):
        comparison["observation"] = "两次有效报价不齐，不能比较更新情况"
        return comparison
    first, second = [sample["quote"] for sample in samples]
    comparison["price_changed"] = first["price"] != second["price"]
    times = [quote["source_reported_at"] for quote in (first, second)]
    if any(value is None for value in times):
        comparison.update(source_time_advanced=None, source_time_regressed=None, observation="来源时间缺失，不能判断是否更新")
    else:
        a, b = [datetime.fromisoformat(value) for value in times]
        comparison.update(source_time_advanced=b > a, source_time_regressed=b < a)
        if b > a:
            comparison["observation"] = "观察到来源时间前进；这不等于已确认无延迟或价格必须变化"
        elif b < a:
            comparison["observation"] = "来源时间倒退；可能取到了旧缓存，本次需复核"
        else:
            comparison["observation"] = "两次来源时间相同；重复取数没有产生一条更新的报价"
    return comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="600519", help="六位沪深 A 股代码，默认 600519")
    args = parser.parse_args()
    try:
        market_symbol(args.symbol)
    except QuoteDataError as error:
        parser.error(str(error))

    started = datetime.now(CHINA_TIME)
    print(f"股票代码：{args.symbol}；来源：{SOURCE}", flush=True)
    print("将读取两次报价，中间等待 10 秒。重点比较来源时间与本机接收时间。", flush=True)
    samples = []
    for index in range(2):
        if index:
            print("\n等待 10 秒再读一次……", flush=True)
            time.sleep(INTERVAL_SECONDS)
        result = fetch_once(args.symbol)
        samples.append(result)
        print(f"\n第 {index + 1} 次读取：", flush=True)
        if result["status"] == "failed":
            print(f"  {result['error_type']}：{result['reason']}", flush=True)
            continue
        quote = result["quote"]
        print(f"  {quote['name']}（{quote['symbol']}） 最新价 {quote['price']:.2f} 元；涨跌幅 {quote['change_pct']:+.2f}%")
        print(f"  来源时间：{quote['source_reported_at'] or '缺失，不能用当前时间代填'}")
        print(f"  接收时间：{quote['received_at']}")
        if quote["source_age_seconds"] is not None:
            print(f"  两个时间相差：{quote['source_age_seconds']:.1f} 秒（不是网络请求耗时）")
        print(f"  请求耗时：{result['request_duration_seconds']:.3f} 秒")
        print(f"  时间判断：{quote['freshness_note']}", flush=True)

    comparison = compare_samples(samples)
    success_count = sum(sample["status"] == "ok" for sample in samples)
    status = "ok" if success_count == 2 else "partial" if success_count else "failed"
    if comparison.get("source_time_regressed"):
        status = "needs_review"
    evidence = {
        "status": status, "source": SOURCE, "symbol": args.symbol,
        "data_kind": "latest_quote_snapshot", "network_mode": "system_default",
        "python_version": sys.version.split()[0], "requests_version": requests.__version__,
        "request_started_at": started.isoformat(timespec="seconds"),
        "request_finished_at": datetime.now(CHINA_TIME).isoformat(timespec="seconds"),
        "interval_seconds": INTERVAL_SECONDS, "samples": samples, "comparison": comparison,
        "calendar_and_suspension_verified": False,
    }
    output_dir = Path(__file__).resolve().parents[1] / "evidence" / "data-source"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"spot-{args.symbol}-{started:%Y%m%dT%H%M%S%f}.json"
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"\n观察结果：{comparison['observation']}")
    print("获取成功不等于已证实盘中实时性；晚间报价应按来源时间理解。")
    print(f"实验记录：{output}")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
