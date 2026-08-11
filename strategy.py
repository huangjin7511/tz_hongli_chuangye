import json
import os
import sys
import time
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
CONFIG_PATH = BASE_DIR / "config.json"


# ==================== 交易日判断 ====================

# A股休市节假日（固定日期，月-日）
FIXED_HOLIDAYS = {
    "01-01",  # 元旦
    "05-01",  # 劳动节
    "10-01", "10-02", "10-03", "10-04", "10-05", "10-06", "10-07",  # 国庆
}

# 农历节假日（近似日期，按年维护，需定期更新）
LUNAR_HOLIDAYS = {
    2026: {"02-16", "02-17", "02-18", "02-19", "02-20", "02-21", "02-22"},  # 春节
    2027: {"02-06", "02-07", "02-08", "02-09", "02-10", "02-11", "02-12"},
    2025: {"01-28", "01-29", "01-30", "01-31", "02-03", "02-04"},
    2024: {"02-10", "02-11", "02-12", "02-13", "02-14", "02-15", "02-16"},
    2028: {"01-26", "01-27", "01-28", "01-29", "01-30", "01-31", "02-01"},
}


def is_trading_day(dt=None):
    """判断是否为A股交易日"""
    if dt is None:
        dt = datetime.now()
    if dt.weekday() >= 5:
        return False
    md = dt.strftime("%m-%d")
    if md in FIXED_HOLIDAYS:
        return False
    year_holidays = LUNAR_HOLIDAYS.get(dt.year, set())
    if md in year_holidays:
        return False
    return True


# ==================== 配置 ====================

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    webhook = os.environ.get("WECHAT_WEBHOOK_URL", "")
    if webhook and webhook != "${WECHAT_WEBHOOK_URL}":
        config["wechat"]["webhook_url"] = webhook
    return config


# ==================== 数据获取 ====================

def _validate_price(price):
    return price is not None and price > 0


def fetch_sina_price(sina_code):
    url = f"https://hq.sinajs.cn/list={sina_code}"
    headers = {
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=10)
    resp.encoding = "gbk"
    text = resp.text.strip()
    match = re.search(r'"(.+)"', text)
    if not match:
        raise ValueError(f"无法解析响应: {text[:100]}")
    fields = match.group(1).split(",")
    if len(fields) < 4:
        raise ValueError(f"字段不足: {len(fields)}")
    price = float(fields[3])
    if not _validate_price(price):
        raise ValueError(f"价格无效: {price}")
    return price


def fetch_eastmoney_price(code, market):
    secids_to_try = []
    if market == "sz":
        secids_to_try.append(f"0.{code}")
    elif market == "sh":
        secids_to_try.append(f"1.{code}")
    else:
        secids_to_try.extend([f"0.{code}", f"1.{code}"])
    secids_to_try.append(f"2.{code}")
    url = f"https://push2.eastmoney.com/api/qt/stock/get"
    headers = {
        "Referer": "https://quote.eastmoney.com",
        "User-Agent": "Mozilla/5.0"
    }
    for secid in secids_to_try:
        try:
            params = {"secid": secid, "fields": "f43,f57,f58"}
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            data = resp.json()
            price = data.get("data", {}).get("f43", 0)
            if _validate_price(price):
                result = price / 100 if price > 10000 else price
                if _validate_price(result):
                    return result
        except Exception:
            continue
    raise ValueError(f"东方财富无法获取 {code}")


def fetch_tencent_price(sina_code):
    url = f"https://qt.gtimg.cn/q={sina_code}"
    headers = {
        "Referer": "https://finance.qq.com",
        "User-Agent": "Mozilla/5.0"
    }
    resp = requests.get(url, headers=headers, timeout=10)
    resp.encoding = "gbk"
    match = re.search(r'"(.+)"', resp.text)
    if not match:
        raise ValueError("腾讯API响应解析失败")
    fields = match.group(1).split("~")
    if len(fields) < 4:
        raise ValueError(f"腾讯API字段不足: {len(fields)}")
    price = float(fields[3])
    if not _validate_price(price):
        raise ValueError(f"腾讯价格无效: {price}")
    return price


def fetch_price_with_sources(sina_codes, code, market, retries=2):
    errors = []
    primary_code = sina_codes[0] if sina_codes else None
    if primary_code:
        for attempt in range(retries):
            try:
                price = fetch_sina_price(primary_code)
                print(f"  新浪({primary_code}): {price:.2f}")
                return price
            except Exception as e:
                errors.append(f"新浪({primary_code}): {e}")
                if attempt < retries - 1:
                    time.sleep(1)
    try:
        price = fetch_eastmoney_price(code, market)
        print(f"  东方财富({code}): {price:.2f}")
        return price
    except Exception as e:
        errors.append(f"东方财富: {e}")
    if len(sina_codes) > 1:
        for sina_code in sina_codes[1:]:
            try:
                price = fetch_sina_price(sina_code)
                print(f"  新浪备用({sina_code}): {price:.2f}")
                return price
            except Exception as e:
                errors.append(f"新浪备用({sina_code}): {e}")
    for sina_code in sina_codes:
        try:
            price = fetch_tencent_price(sina_code)
            print(f"  腾讯({sina_code}): {price:.2f}")
            return price
        except Exception as e:
            errors.append(f"腾讯({sina_code}): {e}")
    raise RuntimeError("所有数据源均失败:\n" + "\n".join(f"  - {e}" for e in errors))


# ==================== 策略逻辑 ====================

def generate_signal(r_value, lower, upper):
    """根据R值生成轮动信号"""
    if r_value < lower:
        return "切换至创业板", "growth"
    elif r_value > upper:
        return "切换至中证红利", "dividend"
    else:
        return "维持当前仓位", "hold"


# ==================== 数据持久化 ====================

def load_json(filepath, default=None):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else {}


def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_history(history_path, record):
    history = load_json(history_path, default=[])
    if not isinstance(history, list):
        history = []
    history.append(record)
    history = history[-365:]
    save_json(history_path, history)
    return history


def update_signals(signals_path, record):
    signals = load_json(signals_path, default=[])
    if not isinstance(signals, list):
        signals = []
    signals.insert(0, record)
    signals = signals[-50:]
    save_json(signals_path, signals)
    return signals


# ==================== 阈值优化 ====================

def optimize_thresholds(history_path):
    history = load_json(history_path, default=[])
    if len(history) < 30:
        return None
    best_result = None
    best_score = -float("inf")
    test_lowers = [0.30, 0.32, 0.33, 0.34, 0.35, 0.36, 0.37, 0.38, 0.40]
    test_uppers = [0.55, 0.58, 0.60, 0.62, 0.65, 0.68, 0.70]
    for low in test_lowers:
        for high in test_uppers:
            if low >= high:
                continue
            score = backtest_strategy(history, low, high)
            if score > best_score:
                best_score = score
                best_result = {"lower": low, "upper": high, "score": score}
    return best_result


def backtest_strategy(history, lower, upper):
    """回测：80%指数+20%现金"""
    if len(history) < 2:
        return 0
    portfolio = 1.0
    for i in range(1, len(history)):
        prev = history[i - 1]
        curr = history[i]
        r_prev = prev.get("r_value", 0)
        if r_prev < lower:
            index_ret = (curr.get("growth_price", 0) / prev.get("growth_price", 1)) - 1
        elif r_prev > upper:
            index_ret = (curr.get("dividend_price", 0) / prev.get("dividend_price", 1)) - 1
        else:
            index_ret = 0
        daily_ret = index_ret * 0.8
        portfolio *= (1 + daily_ret)
    return portfolio


# ==================== 消息推送 ====================

def push_wechat(webhook_url, message):
    if not webhook_url or webhook_url == "${WECHAT_WEBHOOK_URL}":
        print("  企业微信 Webhook 未配置，跳过推送")
        return False
    try:
        payload = {
            "msgtype": "text",
            "text": {"content": message}
        }
        resp = requests.post(webhook_url, json=payload, timeout=10)
        result = resp.json()
        if result.get("errcode") == 0:
            print("  企业微信推送成功")
            return True
        else:
            print(f"  企业微信推送失败: {result}")
            return False
    except Exception as e:
        print(f"  企业微信推送异常: {e}")
        return False


def build_text_message(record, config, optimal=None):
    thresholds = config["thresholds"]
    pos_cfg = config["position"]
    etfs = config.get("etfs", {})
    growth_etf = etfs.get("growth_etf", {})
    dividend_etf = etfs.get("dividend_etf", {})
    cash_etf = etfs.get("cash_etf", {})

    # 根据信号确定投资标的
    if record["signal_type"] == "growth":
        invest_target = f"创业板ETF({growth_etf.get('code', '159915')})"
    elif record["signal_type"] == "dividend":
        div_codes = dividend_etf.get("codes", ["510880", "515080"])
        invest_target = f"红利ETF({'/'.join(div_codes)})"
    else:
        invest_target = "维持当前持仓"

    cash_info = cash_etf.get("codes", ["511880", "511990"])

    lines = []
    lines.append(f"【{config['strategy']['name']}】")
    lines.append(f"日期: {record['date']} {record['time']}")
    lines.append(f"")
    lines.append(f"创业板指数: {record['growth_price']:.2f}")
    lines.append(f"中证红利指数: {record['dividend_price']:.2f}")
    lines.append(f"R值: {record['r_value']:.4f}")
    lines.append(f"阈值区间: {thresholds['lower']} ~ {thresholds['upper']}")
    lines.append(f"")
    lines.append(f"信号: {record['signal_desc']}")
    lines.append(f"投资标的: {invest_target}")
    lines.append(f"配置: {pos_cfg['index_pct']*100:.0f}%投资 + {pos_cfg['cash_pct']*100:.0f}%现金")
    lines.append(f"现金工具: 场内货基({'/'.join(cash_info)})")
    lines.append(f"现金纪律: 永不交易、永不挪用、不再平衡")
    if optimal:
        lines.append(f"")
        lines.append(f"回测优化阈值: {optimal['lower']} ~ {optimal['upper']} (得分: {optimal['score']:.4f})")
    return "\n".join(lines)


# ==================== 主流程 ====================

def main():
    print("=" * 50)
    print("  创业板 / 中证红利 轮动策略")
    print("=" * 50)

    # 交易日检查
    now = datetime.now()
    if not is_trading_day(now):
        print(f"\n  今天({now.strftime('%Y-%m-%d %A')})不是交易日，跳过执行")
        return 0

    config = load_config()
    thresholds = config["thresholds"]
    pos_cfg = config["position"]

    print(f"\n策略参数:")
    print(f"  阈值区间: {thresholds['lower']} ~ {thresholds['upper']}")
    print(f"  仓位配置: {pos_cfg['index_pct']*100:.0f}%投资 + {pos_cfg['cash_pct']*100:.0f}%现金")
    print(f"  现金纪律: 永不交易、永不挪用、不再平衡")
    print()

    print("正在获取指数数据...")
    growth_sina_codes = config["strategy"]["index_growth"].get("sina_codes", [config["strategy"]["index_growth"]["sina_code"]])
    dividend_sina_codes = config["strategy"]["index_dividend"].get("sina_codes", [config["strategy"]["index_dividend"]["sina_code"]])
    growth_code = config["strategy"]["index_growth"]["code"]
    dividend_code = config["strategy"]["index_dividend"]["code"]
    growth_market = config["strategy"]["index_growth"]["market"]
    dividend_market = config["strategy"]["index_dividend"]["market"]

    growth_price = fetch_price_with_sources(growth_sina_codes, growth_code, growth_market)
    print(f"  创业板指数: {growth_price:.2f}")

    dividend_price = fetch_price_with_sources(dividend_sina_codes, dividend_code, dividend_market)
    print(f"  中证红利指数: {dividend_price:.2f}")

    r_value = growth_price / dividend_price
    print(f"\n  R 值 = {growth_price:.2f} / {dividend_price:.2f} = {r_value:.4f}")

    signal, signal_type = generate_signal(
        r_value, thresholds["lower"], thresholds["upper"]
    )

    print(f"\n  信号: {signal}")

    history_path = DATA_DIR / "history.json"
    signals_path = DATA_DIR / "signals.json"

    record = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timestamp": now.isoformat(),
        "growth_price": growth_price,
        "dividend_price": dividend_price,
        "r_value": round(r_value, 4),
        "signal": signal,
        "signal_type": signal_type,
        "signal_desc": signal,
        "index_pct": pos_cfg["index_pct"],
        "cash_pct": pos_cfg["cash_pct"],
        "threshold_lower": thresholds["lower"],
        "threshold_upper": thresholds["upper"],
    }

    update_history(history_path, record)
    print(f"\n  历史数据已保存 ({history_path})")

    update_signals(signals_path, record)
    print(f"  信号记录已保存 ({signals_path})")

    print("\n正在进行历史回测优化...")
    optimal = optimize_thresholds(str(history_path))
    if optimal:
        print(f"  最优阈值: {optimal['lower']} ~ {optimal['upper']} (得分: {optimal['score']:.4f})")
        current_config = load_config()
        if abs(optimal["lower"] - current_config["thresholds"]["lower"]) > 0.005 or \
           abs(optimal["upper"] - current_config["thresholds"]["upper"]) > 0.005:
            print(f"  检测到更优阈值，更新配置...")
            current_config["thresholds"]["lower"] = optimal["lower"]
            current_config["thresholds"]["upper"] = optimal["upper"]
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(current_config, f, ensure_ascii=False, indent=2)
            print(f"  配置已更新: {optimal['lower']} ~ {optimal['upper']}")
    else:
        print("  历史数据不足，暂无法优化（需至少30天数据）")

    if config["wechat"]["enabled"]:
        print("\n正在推送企业微信...")
        message = build_text_message(record, config, optimal)
        push_wechat(config["wechat"]["webhook_url"], message)

    print("\n" + "=" * 50)
    print("  策略执行完毕")
    print("=" * 50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
