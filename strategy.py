import json
import os
import sys
import time
import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
CONFIG_PATH = BASE_DIR / "config.json"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    webhook = os.environ.get("WECHAT_WEBHOOK_URL", "")
    if webhook and webhook != "${WECHAT_WEBHOOK_URL}":
        config["wechat"]["webhook_url"] = webhook
    return config


def _validate_price(price):
    if price is None or price <= 0:
        return False
    return True


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
    elif market == "csi":
        secids_to_try.extend([f"0.{code}", f"1.{code}"])
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


def calculate_position(r_value, lower, upper):
    if r_value <= lower:
        return 9, "创业板（满仓）"
    elif r_value >= upper:
        return 1, "中证红利（满仓）"
    else:
        ratio = (r_value - lower) / (upper - lower)
        level = round(1 + ratio * 8)
        level = max(1, min(9, level))
        growth_pct = round(ratio * 100, 1)
        dividend_pct = round((1 - ratio) * 100, 1)
        desc = f"混合（创业板{growth_pct}% / 红利{dividend_pct}%）"
        return level, desc


def generate_signal(r_value, lower, upper):
    if r_value <= lower:
        return "切换至创业板", "growth"
    elif r_value >= upper:
        return "切换至中证红利", "dividend"
    else:
        return "维持当前仓位", "hold"


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
    if len(history) < 2:
        return 0
    growth_value = 1.0
    dividend_value = 1.0
    position = 5
    for i in range(1, len(history)):
        prev = history[i - 1]
        curr = history[i]
        r_prev = prev.get("r_value", 0)
        if r_prev <= lower:
            position = 9
        elif r_prev >= upper:
            position = 1
        growth_ret = (curr.get("growth_price", 0) / prev.get("growth_price", 1)) - 1 if prev.get("growth_price") else 0
        dividend_ret = (curr.get("dividend_price", 0) / prev.get("dividend_price", 1)) - 1 if prev.get("dividend_price") else 0
        growth_value *= (1 + growth_ret * (position / 9))
        dividend_value *= (1 + dividend_ret * (1 - position / 9))
    total = growth_value + dividend_value
    return total


def build_text_message(record, config, optimal=None):
    thresholds = config["thresholds"]
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
    lines.append(f"仓位: {record['position_level']}档 ({record['position_desc']})")
    if optimal:
        lines.append(f"")
        lines.append(f"回测优化阈值: {optimal['lower']} ~ {optimal['upper']} (得分: {optimal['score']:.4f})")
    return "\n".join(lines)


def main():
    print("=" * 50)
    print("  创业板 / 中证红利 轮动策略")
    print("=" * 50)

    config = load_config()
    thresholds = config["thresholds"]

    print(f"\n策略参数:")
    print(f"  阈值区间: {thresholds['lower']} ~ {thresholds['upper']}")
    print(f"  仓位档数: {config['position']['levels']}")
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

    position_level, position_desc = calculate_position(
        r_value, thresholds["lower"], thresholds["upper"]
    )
    signal, signal_type = generate_signal(
        r_value, thresholds["lower"], thresholds["upper"]
    )

    print(f"\n  信号: {signal}")
    print(f"  仓位: {position_level}档 - {position_desc}")

    now = datetime.now()
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
        "position_level": position_level,
        "position_desc": position_desc,
        "threshold_lower": thresholds["lower"],
        "threshold_upper": thresholds["upper"],
    }

    history_path = DATA_DIR / "history.json"
    signals_path = DATA_DIR / "signals.json"

    update_history(history_path, record)
    print(f"\n  历史数据已保存 ({history_path})")

    signals = update_signals(signals_path, record)
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
