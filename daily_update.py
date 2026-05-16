"""
daily_update.py - 本地每日数据更新脚本
收盘后运行，用AKShare获取指数数据，计算指标，输出JSON，git push到GitHub

数据源：
  - 931446 东证红利低波：AKShare → 腾讯财经API（ETF价格512890）
  - 980081 国证价值100：AKShare → 腾讯财经API（ETF价格159263）
  - NDX 纳斯达克100：AKShare stock_market_pe_lg + index_us_stock_sina

用法：
  python daily_update.py           # 正常运行
  python daily_update.py --no-push  # 只生成JSON，不push
"""
import json
import os
import sys
import math
import subprocess
import urllib.request
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dateutil import relativedelta

# ============ 三标的配置 ============
ETFS = [
    {
        "type": "rsi_ma",
        "etf_code": "512890",
        "etf_name": "红利低波ETF",
        "index_code": "931446",
        "index_name": "东证红利低波",
        "fund_code": "012708",
        "fund_name": "东方红红利低波A",
        "weight": "40%",
        "tencent_prefix": "sh",   # 腾讯API前缀
        "is_index_price": False,  # ETF价格而非指数价格
    },
    {
        "type": "rsi_ma",
        "etf_code": "159263",
        "etf_name": "价值100ETF",
        "index_code": "980081",
        "index_name": "国证价值100",
        "fund_code": "025497",
        "fund_name": "易方达价值100A",
        "weight": "40%",
        "tencent_prefix": "sz",
        "is_index_price": False,
    },
    {
        "type": "pe_drawdown",
        "etf_code": "159941",
        "etf_name": "纳指100ETF联接A",
        "index_code": "NDX",
        "index_name": "纳斯达克100",
        "fund_code": "159941",
        "fund_name": "广发纳指100ETF联接A",
        "weight": "20%",
        "tencent_prefix": None,  # 纳指不用A股API
        "is_index_price": True,
    },
]

RSI_PERIOD = 21
MA_PERIOD = 250

# 脚本所在目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, 'docs')


# ============ 数据获取 ============
def _build_no_proxy_opener():
    """创建无代理的urllib opener，绕过沙箱/系统代理"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    urllib.request.install_opener(opener)
    return opener


def fetch_etf_tencent_klines(etf_code, prefix):
    """使用腾讯财经API获取ETF日K线
    格式: [date, open, close, high, low, volume]
    支持分批获取，合并超过800条的早期数据
    """
    _build_no_proxy_opener()
    full_code = f"{prefix}{etf_code}"
    print(f"  获取ETF K线: {full_code}（腾讯财经）")

    try:
        # 第一批：最近数据
        url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={full_code},day,,,800,qfq'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode('utf-8'))

        stock_data = data.get('data', {}).get(full_code, {})
        field = 'qfqday' if 'qfqday' in stock_data else 'day'
        klines = stock_data.get(field, [])
        if not klines:
            raise Exception("腾讯API返回空数据")

        all_klines = list(klines)

        # 第二批：如果第一批满了800条，尝试拿更早的数据
        if len(klines) >= 800:
            earliest_date = klines[0][0]
            prev_date = (datetime.strptime(earliest_date, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')
            url2 = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={full_code},day,,{prev_date},800,qfq'
            req2 = urllib.request.Request(url2, headers={'User-Agent': 'Mozilla/5.0'})
            resp2 = urllib.request.urlopen(req2, timeout=15)
            data2 = json.loads(resp2.read().decode('utf-8'))
            stock_data2 = data2.get('data', {}).get(full_code, {})
            field2 = 'qfqday' if 'qfqday' in stock_data2 else 'day'
            klines2 = stock_data2.get(field2, [])
            if klines2 and klines2[0][0] < earliest_date:
                all_klines = list(klines2) + all_klines

        print(f"  ✓ 腾讯 {full_code}: {len(all_klines)}条, {all_klines[0][0]} → {all_klines[-1][0]}")

        # 转为DataFrame
        rows = []
        for k in all_klines:
            rows.append({
                'date': k[0], 'open': float(k[1]), 'close': float(k[2]),
                'high': float(k[3]), 'low': float(k[4]), 'volume': float(k[5]),
            })

        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)

        # 去重（分批可能有重叠）
        df = df.drop_duplicates(subset=['date']).reset_index(drop=True)

        return df, False  # df, is_index_price=False（ETF价格）

    except Exception as e:
        print(f"  ✗ 腾讯 {full_code} 失败: {str(e)[:80]}")
        return None, False


def fetch_index_kline(config):
    """获取A股指数/ETF日K线 - 多重数据源自动回退
    1. AKShare index_zh_a_hist（东方财富，指数原始数据）
    2. 腾讯财经API（ETF价格，作为回退）
    """
    index_code = config['index_code']
    index_name = config['index_name']
    etf_code = config['etf_code']
    prefix = config.get('tencent_prefix')

    # ---- 方案A: AKShare 东方财富指数 ----
    print(f"  尝试获取指数K线: {index_name}({index_code})")
    try:
        _build_no_proxy_opener()
        df = ak.index_zh_a_hist(
            symbol=index_code,
            period="daily",
            start_date="20180101",
            end_date="20991231"
        )
        if df is not None and len(df) > 30:
            col_map = {
                '日期': 'date', '开盘': 'open', '收盘': 'close',
                '最高': 'high', '最低': 'low', '成交量': 'volume',
                '涨跌幅': 'change_pct',
            }
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
            df = df.sort_values('date').reset_index(drop=True)
            for col in ['open', 'high', 'low']:
                if col not in df.columns:
                    df[col] = df['close']
            if 'volume' not in df.columns:
                df['volume'] = 0
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            print(f"  ✓ 东方财富 {index_name}: {len(df)}条, {df['date'].min().strftime('%Y-%m-%d')} → {df['date'].max().strftime('%Y-%m-%d')}")
            return df, True  # is_index_price=True
        else:
            print(f"  ✗ 东方财富 {index_name}: 数据为空或不足")
    except Exception as e:
        print(f"  ✗ 东方财富 {index_name} 失败: {str(e)[:80]}")

    # ---- 方案B: 腾讯财经API（ETF价格回退）----
    if prefix:
        print(f"  → 回退到腾讯ETF数据: {prefix}{etf_code}")
        df, _ = fetch_etf_tencent_klines(etf_code, prefix)
        if df is not None and len(df) > 30:
            return df, False  # is_index_price=False
        else:
            print(f"  ✗ 腾讯回退也失败")
    
    return None, False


def fetch_nasdaq_pe():
    """获取纳指100 PE(TTM) - 乐咕乐股"""
    print("  获取纳指100 PE数据（乐咕乐股）...")
    try:
        df = ak.stock_market_pe_lg(symbol='纳指100')
        df = df.rename(columns={'日期': 'date', '市盈率': 'pe_ttm', '总市值': 'market_cap'})
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        print(f"  ✓ 纳指100 PE: {len(df)}条")
        return df
    except Exception as e:
        print(f"  ✗ 乐咕PE失败: {str(e)[:80]}")
        return None


def fetch_nasdaq_price():
    """获取纳指100指数价格 - 新浪财经"""
    print("  获取纳指100指数价格（新浪）...")
    try:
        df = ak.index_us_stock_sina(symbol='.NDX')
        col_map = {'日期': 'date', '日期时间': 'date', '收盘': 'close',
                   '开盘': 'open', '最高': 'high', '最低': 'low'}
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
        df = df.sort_values('date').reset_index(drop=True)
        print(f"  ✓ 纳指100价格: {len(df)}条")
        return df
    except Exception as e:
        print(f"  ✗ 新浪纳指价格失败: {str(e)[:80]}")
        return None


# ============ 技术指标 ============
def calculate_ma(prices, period, min_periods=None):
    if min_periods is None:
        min_periods = period
    return prices.rolling(window=period, min_periods=min(min_periods, len(prices))).mean()


def calculate_rsi(prices, period=21):
    delta = prices.diff()
    gain = delta.where(delta > 0, 0)
    loss = (-delta).where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_pe_percentile(pe_series, current_pe):
    valid_pe = pe_series.dropna()
    if len(valid_pe) == 0:
        return 50.0
    return round((valid_pe < current_pe).sum() / len(valid_pe) * 100, 1)


def calculate_drawdown(prices):
    high = prices.max()
    current = prices.iloc[-1]
    if high == 0:
        return 0.0
    return round((current - high) / high * 100, 2)


# ============ RSI+MA250 策略 ============
def get_zone(price, ma250):
    return "强势区" if price >= ma250 else "弱势区"


def get_invest_advice(rsi, zone):
    if rsi >= 72:
        return {"amount": "暂停", "detail": "极度过热·" + zone}
    elif rsi >= 65:
        return {"amount": "0.3份", "detail": "偏热·" + zone}
    elif rsi >= 55:
        return {"amount": "0.6份", "detail": "中性偏热·" + zone}
    elif rsi >= 48:
        return {"amount": "1份", "detail": "中性合理·" + zone}
    elif rsi >= 40:
        if zone == "强势区":
            return {"amount": "1.5份", "detail": "偏冷低估·强势区"}
        else:
            return {"amount": "1.2份", "detail": "偏冷低估·弱势区"}
    else:
        if zone == "强势区":
            return {"amount": "2份", "detail": "极度低估·强势区"}
        else:
            return {"amount": "1.5份", "detail": "极度低估·弱势区"}


def get_reduce_advice(rsi, zone):
    if zone != "强势区":
        return None
    if rsi >= 72:
        return {"action": "考虑减仓", "pct": "30%", "detail": "RSI≥72·强势区·卖出30%（需确认不在冷却期）"}
    elif rsi >= 65:
        return {"action": "考虑减仓", "pct": "15%", "detail": "RSI 65-71·强势区·卖出15%（需确认不在冷却期）"}
    return None


def get_rebuy_advice(rsi, zone):
    if zone != "强势区":
        return None
    if 59 <= rsi <= 63:
        return {"action": "考虑回补", "detail": "RSI 59-63·强势区·接回已减仓2/3（触发30天冷却）"}
    elif rsi < 50:
        return {"action": "考虑回补", "detail": "RSI<50·强势区·接回全部剩余（触发30天冷却）"}
    return None


def get_risk_signal(price, ma250, days_below, ma_trend):
    risk_active = days_below >= 15 and ma_trend == -1
    if risk_active:
        return "red", "持续弱势"
    elif price < ma250:
        return "yellow", "跌破MA250"
    else:
        return "green", "正常"


# ============ PE+回撤 策略（纳指100）============
def get_pe_zone(pe_percentile):
    if pe_percentile >= 85:
        return "极度高估"
    elif pe_percentile >= 70:
        return "高估"
    elif pe_percentile >= 50:
        return "合理"
    elif pe_percentile >= 30:
        return "低估"
    else:
        return "极度低估"


def get_drawdown_zone(drawdown):
    if drawdown > 15:
        return "深度回调"
    elif drawdown >= 8:
        return "中级回调"
    else:
        return "正常波动"


def get_nasdaq_invest_advice(pe_percentile, drawdown):
    deep_drawdown = drawdown > 15
    if pe_percentile >= 85:
        return {"amount": "暂停", "detail": "极度高估·暂停定投"}
    elif pe_percentile >= 70:
        base = "0.5份"
        detail = "高估·" + ("0.5份不加仓" if deep_drawdown else "0.5份")
        return {"amount": base, "detail": detail}
    elif pe_percentile >= 50:
        if deep_drawdown:
            return {"amount": "1.5份", "detail": "合理+深度回调·1+0.5份"}
        return {"amount": "1份", "detail": "合理估值·1份基准"}
    elif pe_percentile >= 30:
        if deep_drawdown:
            return {"amount": "2.3份", "detail": "低估+深度回调·1.8+0.5份"}
        return {"amount": "1.8份", "detail": "低估·1.8份"}
    else:
        return {"amount": "2.5份", "detail": "极度低估·2.5份封顶"}


def get_nasdaq_reduce_advice(pe_percentile, drawdown):
    if pe_percentile < 70:
        return None
    if drawdown > 15:
        return None
    if pe_percentile >= 85:
        return {"action": "考虑减仓", "pct": "30%", "detail": "PE≥85%·正常波动·卖出30%（需确认不在冷却期）"}
    elif pe_percentile >= 70:
        return {"action": "考虑减仓", "pct": "15%", "detail": "PE 70-84%·正常波动·卖出15%（需确认不在冷却期）"}
    return None


def get_nasdaq_rebuy_advice(pe_percentile, drawdown):
    if pe_percentile >= 70:
        return None
    if drawdown > 15:
        return None
    if 40 <= pe_percentile <= 50:
        return {"action": "考虑回补", "detail": "PE 40-50%·接回已减仓2/3（触发60天冷却）"}
    elif pe_percentile < 40:
        return {"action": "考虑回补", "detail": "PE<40%·接回全部剩余减仓（触发60天冷却）"}
    return None


def get_nasdaq_risk_signal(pe_percentile, drawdown):
    if drawdown > 15:
        return "red", "深度回调"
    elif pe_percentile >= 85:
        return "yellow", "极度高估"
    elif pe_percentile >= 70:
        return "yellow", "估值偏高"
    else:
        return "green", "正常"


# ============ 历史操作建议 ============
def _get_monthly_dates(n_months=12):
    dates = []
    today = datetime.now().date()
    for i in range(n_months, 0, -1):
        target = today - relativedelta.relativedelta(months=i)
        try:
            d = target.replace(day=21)
        except ValueError:
            continue
        if d > today:
            continue
        dates.append(d)
    return dates


def _find_nearest_trading_day(df, target_date):
    td = pd.Timestamp(target_date)
    mask = df['date'].dt.date == target_date
    if mask.any():
        return df[mask].index[-1]
    after = df[df['date'].dt.date > target_date]
    if len(after) > 0:
        return after.index[0]
    before = df[df['date'].dt.date < target_date]
    if len(before) > 0:
        return before.index[-1]
    return None


def calc_rsi_ma_history(df, ma250, rsi21, n_months=12):
    dates = _get_monthly_dates(n_months)
    history = []
    for d in dates:
        idx = _find_nearest_trading_day(df, d)
        if idx is None:
            continue
        price = float(df.iloc[idx]['close'])
        ma_val = float(ma250.iloc[idx]) if not pd.isna(ma250.iloc[idx]) else None
        rsi_val = float(rsi21.iloc[idx]) if not pd.isna(rsi21.iloc[idx]) else None
        if ma_val is None or rsi_val is None:
            continue
        zone = get_zone(price, ma_val)
        invest = get_invest_advice(rsi_val, zone)
        reduce_adv = get_reduce_advice(rsi_val, zone)
        rebuy_adv = get_rebuy_advice(rsi_val, zone)
        entry = {
            "date": d.strftime('%Y-%m-%d'),
            "price": round(price, 2),
            "rsi21": round(rsi_val, 1),
            "ma250": round(ma_val, 2),
            "zone": zone,
            "invest": invest['amount'],
            "invest_detail": invest['detail'],
        }
        if reduce_adv:
            entry["reduce"] = reduce_adv['action'] + ' ' + reduce_adv['pct']
        if rebuy_adv:
            entry["rebuy"] = rebuy_adv['action']
        history.append(entry)
    return history


def calc_pe_drawdown_history(pe_df, idx_df, n_months=12):
    dates = _get_monthly_dates(n_months)
    history = []
    for d in dates:
        pe_idx = _find_nearest_trading_day(pe_df, d)
        if pe_idx is None:
            continue
        pe_val = float(pe_df.iloc[pe_idx]['pe_ttm'])
        price_idx = _find_nearest_trading_day(idx_df, d)
        if price_idx is None:
            continue
        price = float(idx_df.iloc[price_idx]['close'])
        pe_before = pe_df[pe_df['date'].dt.date <= d]['pe_ttm'].dropna()
        if len(pe_before) < 50:
            continue
        pe_pct = round((pe_before < pe_val).sum() / len(pe_before) * 100, 1)
        prices_before = idx_df[idx_df['date'].dt.date <= d]['close'].astype(float)
        if len(prices_before) < 20:
            continue
        high = float(prices_before.max())
        dd = round((price - high) / high * 100, 2) if high != 0 else 0.0
        invest = get_nasdaq_invest_advice(pe_pct, dd)
        reduce_adv = get_nasdaq_reduce_advice(pe_pct, dd)
        rebuy_adv = get_nasdaq_rebuy_advice(pe_pct, dd)
        pe_zone = get_pe_zone(pe_pct)
        drawdown_zone = get_drawdown_zone(dd)
        entry = {
            "date": d.strftime('%Y-%m-%d'),
            "price": round(price, 2),
            "pe_ttm": round(pe_val, 1),
            "pe_percentile": pe_pct,
            "pe_zone": pe_zone,
            "drawdown": dd,
            "drawdown_zone": drawdown_zone,
            "invest": invest['amount'],
            "invest_detail": invest['detail'],
        }
        if reduce_adv:
            entry["reduce"] = reduce_adv['action'] + ' ' + reduce_adv['pct']
        if rebuy_adv:
            entry["rebuy"] = rebuy_adv['action']
        history.append(entry)
    return history


# ============ 处理RSI+MA250标的 ============
def process_rsi_ma(config):
    """处理RSI+MA250标的：931446 或 980081"""
    index_code = config['index_code']
    index_name = config['index_name']
    print(f"\n{'='*50}")
    print(f"处理: {index_name}({index_code}) [RSI+MA250]")
    print(f"{'='*50}")

    # 获取指数K线（支持多数据源回退）
    df, is_index_price = fetch_index_kline(config)
    if df is None or len(df) < 30:
        raise Exception(f"{index_name} 指数K线获取失败或数据不足")

    # 计算指标
    data_count = len(df)
    ma_min_periods = min(120, data_count)
    ma250 = calculate_ma(df['close'], MA_PERIOD, min_periods=ma_min_periods)
    rsi21 = calculate_rsi(df['close'], RSI_PERIOD)
    ma_data_available = data_count >= MA_PERIOD

    # MA250趋势
    if len(ma250.dropna()) >= 20:
        ma_recent = ma250.dropna().tail(20)
        daily_slope = (ma_recent.iloc[-1] - ma_recent.iloc[0]) / 20
        ma_trend = 1 if daily_slope > 0.0001 else (-1 if daily_slope < -0.0001 else 0)
    else:
        ma_trend = 0

    # 最新值
    current = df.iloc[-1]
    latest_price = float(current['close'])
    latest_ma250 = float(ma250.iloc[-1]) if not pd.isna(ma250.iloc[-1]) else latest_price
    latest_rsi = float(rsi21.iloc[-1]) if not pd.isna(rsi21.iloc[-1]) else 50.0

    # 涨跌幅
    prev_price = float(df.iloc[-2]['close'])
    price_change_pct = round((latest_price - prev_price) / prev_price * 100, 2)

    # 跌破/站上MA250天数
    days_below = 0
    for i in range(len(df) - 1, -1, -1):
        if pd.isna(ma250.iloc[i]):
            break
        if df.iloc[i]['close'] < ma250.iloc[i]:
            days_below += 1
        else:
            break

    days_above = 0
    for i in range(len(df) - 1, -1, -1):
        if pd.isna(ma250.iloc[i]):
            break
        if df.iloc[i]['close'] >= ma250.iloc[i]:
            days_above += 1
        else:
            break

    # 分区 + 策略
    zone = get_zone(latest_price, latest_ma250)
    invest = get_invest_advice(latest_rsi, zone)
    reduce_advice = get_reduce_advice(latest_rsi, zone)
    rebuy_advice = get_rebuy_advice(latest_rsi, zone)
    risk_signal, risk_label = get_risk_signal(latest_price, latest_ma250, days_below, ma_trend)

    print(f"  价格: {latest_price:.2f} ({'+' if price_change_pct >= 0 else ''}{price_change_pct}%)")
    print(f"  MA250: {latest_ma250:.2f}{'(参考)' if not ma_data_available else ''} | RSI21: {latest_rsi:.2f}")
    print(f"  分区: {zone} | 信号: {risk_signal}({risk_label})")
    print(f"  定投: {invest['amount']}")

    # 结果
    result = {
        "type": "rsi_ma",
        "etf_code": config['etf_code'],
        "etf_name": config['etf_name'],
        "index_code": index_code,
        "index_name": index_name,
        "fund_code": config['fund_code'],
        "fund_name": config['fund_name'],
        "weight": config['weight'],
        "current_price": round(latest_price, 2),
        "price_change_pct": price_change_pct,
        "ma250": round(latest_ma250, 2),
        "ma250_is_reference": not ma_data_available,
        "ma250_data_days": data_count,
        "rsi21": round(latest_rsi, 2),
        "zone": zone,
        "invest_amount": invest['amount'],
        "invest_detail": invest['detail'],
        "reduce_advice": reduce_advice,
        "rebuy_advice": rebuy_advice,
        "risk_signal": risk_signal,
        "risk_label": risk_label,
        "ma250_trend": "上升" if ma_trend == 1 else ("下降" if ma_trend == -1 else "横盘"),
        "days_below_ma250": int(days_below),
        "days_above_ma250": int(days_above),
        "price_vs_ma250_pct": round((latest_price / latest_ma250 - 1) * 100, 2) if latest_ma250 and latest_ma250 != 0 else None,
        "is_index_price": is_index_price,
        "market_date": current['date'].strftime('%Y-%m-%d'),
        "history_advice": calc_rsi_ma_history(df, ma250, rsi21),
    }

    # K线+指标数据（最近400条）
    tail_n = min(400, len(df))
    tail_df = df.tail(tail_n).reset_index(drop=True)
    tail_ma250 = ma250.iloc[-tail_n:].reset_index(drop=True)
    tail_rsi21 = rsi21.iloc[-tail_n:].reset_index(drop=True)

    klines = []
    ma_values = []
    rsi_values = []
    for i in range(len(tail_df)):
        klines.append({
            "time": tail_df.iloc[i]['date'].strftime('%Y-%m-%d'),
            "open": float(tail_df.iloc[i]['open']),
            "high": float(tail_df.iloc[i]['high']),
            "low": float(tail_df.iloc[i]['low']),
            "close": float(tail_df.iloc[i]['close']),
            "volume": float(tail_df.iloc[i]['volume']),
        })
        ma_values.append(None if pd.isna(tail_ma250.iloc[i]) else round(float(tail_ma250.iloc[i]), 2))
        rsi_values.append(None if pd.isna(tail_rsi21.iloc[i]) else round(float(tail_rsi21.iloc[i]), 2))

    chart_data = {
        "etf_code": config['etf_code'],
        "index_name": index_name,
        "klines": klines,
        "ma_values": ma_values,
        "rsi_values": rsi_values,
    }

    return result, chart_data


# ============ 处理纳指100标的（PE分位+回撤）============
def process_nasdaq(config):
    """处理纳指100：PE分位+回撤"""
    print(f"\n{'='*50}")
    print(f"处理: 纳斯达克100(NDX) [PE分位+回撤]")
    print(f"{'='*50}")

    pe_df = fetch_nasdaq_pe()
    if pe_df is None or len(pe_df) < 50:
        raise Exception("纳指100 PE数据获取失败")

    idx_df = fetch_nasdaq_price()
    if idx_df is None or len(idx_df) < 20:
        raise Exception("纳指100价格数据获取失败")

    # 指标计算
    current_pe = float(pe_df['pe_ttm'].iloc[-1])
    pe_data_days = len(pe_df)
    pe_years = round(pe_data_days / 252, 1)
    pe_percentile = calculate_pe_percentile(pe_df['pe_ttm'], current_pe)
    pe_zone = get_pe_zone(pe_percentile)

    drawdown = calculate_drawdown(idx_df['close'].astype(float))
    drawdown_zone = get_drawdown_zone(drawdown)
    high_price = float(idx_df['close'].astype(float).max())
    current_idx_price = float(idx_df['close'].astype(float).iloc[-1])
    prev_idx_price = float(idx_df['close'].astype(float).iloc[-2])
    price_change_pct = round((current_idx_price - prev_idx_price) / prev_idx_price * 100, 2)

    # 策略
    invest = get_nasdaq_invest_advice(pe_percentile, drawdown)
    reduce_advice = get_nasdaq_reduce_advice(pe_percentile, drawdown)
    rebuy_advice = get_nasdaq_rebuy_advice(pe_percentile, drawdown)
    risk_signal, risk_label = get_nasdaq_risk_signal(pe_percentile, drawdown)
    zone = pe_zone + "·" + drawdown_zone

    print(f"  PE(TTM): {current_pe:.2f} | 分位: {pe_percentile:.1f}% | {pe_zone}")
    print(f"  回撤: {drawdown:.2f}% | {drawdown_zone}")
    print(f"  定投: {invest['amount']}")

    result = {
        "type": "pe_drawdown",
        "etf_code": config['etf_code'],
        "etf_name": config['etf_name'],
        "index_code": "NDX",
        "index_name": "纳斯达克100",
        "fund_code": config['fund_code'],
        "fund_name": config['fund_name'],
        "weight": config['weight'],
        "current_price": round(current_idx_price, 2),
        "price_change_pct": price_change_pct,
        "pe_ttm": round(current_pe, 2),
        "pe_percentile": pe_percentile,
        "pe_years": pe_years,
        "pe_data_days": pe_data_days,
        "pe_zone": pe_zone,
        "drawdown": drawdown,
        "drawdown_zone": drawdown_zone,
        "high_price": round(high_price, 2),
        "zone": zone,
        "invest_amount": invest['amount'],
        "invest_detail": invest['detail'],
        "reduce_advice": reduce_advice,
        "rebuy_advice": rebuy_advice,
        "risk_signal": risk_signal,
        "risk_label": risk_label,
        "market_date": pe_df['date'].iloc[-1].strftime('%Y-%m-%d'),
        "history_advice": calc_pe_drawdown_history(pe_df, idx_df),
    }

    # PE历史
    pe_history = []
    for i in range(len(pe_df)):
        pe_history.append({
            "time": pe_df['date'].iloc[i].strftime('%Y-%m-%d'),
            "pe_ttm": round(float(pe_df['pe_ttm'].iloc[i]), 2),
        })

    # 价格历史（最近400条）
    idx_tail = idx_df.tail(400).reset_index(drop=True)
    idx_history = []
    for i in range(len(idx_tail)):
        idx_history.append({
            "time": idx_tail['date'].iloc[i].strftime('%Y-%m-%d'),
            "close": round(float(idx_tail['close'].iloc[i]), 2),
        })

    chart_data = {
        "etf_code": config['etf_code'],
        "index_name": "纳斯达克100",
        "pe_history": pe_history,
        "price_history": idx_history,
    }

    return result, chart_data


# ============ NaN清理 ============
def clean_nan(obj):
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan(item) for item in obj]
    elif isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


# ============ Git Push ============
def git_push():
    """提交并推送JSON到GitHub"""
    print(f"\n{'='*50}")
    print("Git Push")
    print(f"{'='*50}")
    try:
        # 检查是否有变更
        result = subprocess.run(
            ['git', 'status', '--porcelain', 'docs/'],
            capture_output=True, text=True, cwd=SCRIPT_DIR
        )
        if not result.stdout.strip():
            print("  没有数据变更，跳过push")
            return

        # git add
        subprocess.run(['git', 'add', 'docs/'], cwd=SCRIPT_DIR, check=True)

        # git commit
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        subprocess.run(
            ['git', 'commit', '-m', f'数据更新 {now}'],
            cwd=SCRIPT_DIR, check=True
        )

        # git push
        subprocess.run(['git', 'push'], cwd=SCRIPT_DIR, check=True)
        print(f"  ✓ Push成功!")
    except subprocess.CalledProcessError as e:
        print(f"  ✗ Git操作失败: {e}")
        print(f"  请手动执行: cd {SCRIPT_DIR} && git add docs/ && git commit -m '数据更新' && git push")
    except Exception as e:
        print(f"  ✗ Push异常: {str(e)[:80]}")


# ============ 主程序 ============
def main():
    no_push = '--no-push' in sys.argv
    print(f"[{datetime.now()}] ============ 价值投资控制台 本地更新 ============")
    print(f"AKShare版本: {ak.__version__}")
    if no_push:
        print("模式: 仅生成JSON（不push）")

    os.makedirs(DOCS_DIR, exist_ok=True)

    results = []
    chart_data_list = []

    for config in ETFS:
        try:
            if config['type'] == 'rsi_ma':
                result, chart_data = process_rsi_ma(config)
            elif config['type'] == 'pe_drawdown':
                result, chart_data = process_nasdaq(config)
            else:
                raise Exception(f"未知策略类型: {config['type']}")
            results.append(result)
            chart_data_list.append(chart_data)
        except Exception as e:
            print(f"  ✗ {config['etf_name']} 处理失败: {e}")
            # 错误占位
            results.append({
                "type": config['type'],
                "etf_code": config['etf_code'],
                "etf_name": config['etf_name'],
                "index_code": config['index_code'],
                "index_name": config['index_name'],
                "fund_code": config['fund_code'],
                "fund_name": config['fund_name'],
                "weight": config['weight'],
                "error": True,
                "is_index_price": config.get('is_index_price', True),
                "current_price": 0,
                "price_change_pct": 0,
                "zone": "数据异常",
                "invest_amount": "数据异常",
                "invest_detail": "获取失败",
                "risk_signal": "red",
                "risk_label": "数据获取失败",
                "market_date": "",
            })
            chart_data_list.append({"etf_code": config['etf_code'], "index_name": config['index_name']})

    # 输出各标的独立JSON（每个标的单独文件，互不影响）
    index_file_map = {"931446": "931446.json", "980081": "980081.json", "NDX": "nd100.json"}
    for i, config in enumerate(ETFS):
        fn = index_file_map.get(config['index_code'], f"{config['index_code']}.json")
        fp = os.path.join(DOCS_DIR, fn)
        ds_label = "AKShare 指数" if results[i].get('is_index_price', True) else "腾讯ETF"
        n_data = {
            "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "data_source": ds_label,
            "etf": results[i],
            "chart": chart_data_list[i],
        }
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump(clean_nan(n_data), f, ensure_ascii=False, indent=2)
        print(f"  ✓ {fn} 已写入 ({os.path.getsize(fp)} bytes)")

    # 输出 data.json（合并版，前端兼容格式）
    data_output = {
        "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "data_source": "AKShare 本地",
        "etfs": results,
    }
    data_path = os.path.join(DOCS_DIR, 'data.json')
    with open(data_path, 'w', encoding='utf-8') as f:
        json.dump(clean_nan(data_output), f, ensure_ascii=False, indent=2)
    print(f"  ✓ data.json 合并版已写入 ({os.path.getsize(data_path)} bytes)")

    # 汇总
    print(f"\n{'='*50}")
    print("数据更新完成!")
    for r in results:
        if r.get('type') == 'pe_drawdown':
            print(f"  {r.get('index_name','?')}: PE{r.get('pe_ttm',0):.1f} 分位{r.get('pe_percentile',0):.1f}% → 定投{r.get('invest_amount','?')}")
        else:
            p = r.get('current_price', 0)
            dec = 2 if p and p > 100 else 4
            print(f"  {r.get('index_name','?')}: 价格{p:.{dec}f} RSI{r.get('rsi21',0):.1f} → 定投{r.get('invest_amount','?')}")

    # Git Push
    if not no_push:
        git_push()
    else:
        print("\n(跳过push，使用 --no-push 模式)")


if __name__ == "__main__":
    main()
