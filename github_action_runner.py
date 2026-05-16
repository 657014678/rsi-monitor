"""
GitHub Actions Runner - 价值投资控制台
三标的策略：012708(RSI21+MA250) + 025497(RSI21+MA250) + 159941(PE分位+回撤)
定时获取ETF/指数数据并更新docs目录下的JSON文件
"""
import json
import os
import sys
import time
import math
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dateutil import relativedelta

# ============ 三标的配置 ============
ETFS = [
    {
        "type": "rsi_ma",            # 策略类型：RSI+MA250
        "etf_code": "512890",
        "etf_name": "红利低波ETF",
        "index_code": "931446",
        "index_name": "东证红利低波",
        "fund_code": "012708",
        "fund_name": "东方红红利低波A",
        "weight": "40%",
        "yahoo_suffix": ".SS",
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
        "yahoo_suffix": ".SZ",
    },
    {
        "type": "pe_drawdown",       # 策略类型：PE分位+回撤
        "etf_code": "159941",
        "etf_name": "纳指100ETF联接A",
        "index_code": "NDX",
        "index_name": "纳斯达克100",
        "fund_code": "159941",
        "fund_name": "广发纳指100ETF联接A",
        "weight": "20%",
        "yahoo_suffix": None,        # 纳指不用Yahoo
    },
]

RSI_PERIOD = 21
MA_PERIOD = 250

# ============ 数据获取 ============
def rename_columns(df):
    """统一列名映射"""
    column_mappings = {
        '日期': 'date', 'date': 'date', '日期时间': 'date', '日期 ': 'date',
        '开盘': 'open', '开盘价': 'open', 'open': 'open',
        '收盘': 'close', '收盘价': 'close', 'close': 'close', 'close ': 'close',
        '最高': 'high', '最高价': 'high', 'high': 'high', 'high ': 'high',
        '最低': 'low', '最低价': 'low', 'low': 'low', 'low ': 'low',
        '成交量': 'volume', '成交数量': 'volume', 'volume': 'volume', 'vol': 'volume',
        '成交额': 'amount', 'amount': 'amount', '总市值': 'amount',
        '涨跌幅': 'change_pct', '涨跌额': 'change',
        '换手率': 'turnover'
    }
    df = df.rename(columns=column_mappings)
    return df

def standardize_columns(df):
    """确保必要的列存在并标准化"""
    actual_cols = list(df.columns)
    print(f"  原始列名：{actual_cols}")

    df.columns = df.columns.str.strip()
    required_cols = ['date', 'close']
    missing_cols = [col for col in required_cols if col not in df.columns]

    if missing_cols:
        raise Exception(f"数据缺少必要列：{missing_cols}")

    for col in ['open', 'high', 'low', 'volume']:
        if col not in df.columns:
            if col == 'volume':
                df[col] = 0
            else:
                df[col] = df['close']

    return df

def fetch_etf_data(config, days=400):
    """获取ETF历史数据 - yfinance为主，AKShare/腾讯财经备用"""
    import yfinance as yf

    etf_code = config['etf_code']
    etf_name = config['etf_name']
    yahoo_code = f"{etf_code}{config['yahoo_suffix']}" if config.get('yahoo_suffix') else None
    # 腾讯财经代码格式: sh512890 / sz159263
    tencent_code = None
    if config.get('yahoo_suffix'):
        tencent_prefix = 'sh' if config['yahoo_suffix'] == '.SS' else 'sz'
        tencent_code = f"{tencent_prefix}{etf_code}"

    print(f"  数据源目标: {etf_name}({etf_code}) → Yahoo: {yahoo_code}, 腾讯: {tencent_code}")

    # 1. yfinance
    def fetch_yf():
        if not yahoo_code:
            return None
        ticker = yf.Ticker(yahoo_code)
        df = ticker.history(period="2y", interval="1d")
        if df is not None and len(df) > 0:
            df = df.reset_index()
            df.columns = [col.lower().replace(' ', '_') for col in df.columns]
            return df
        return None

    # 2. 腾讯财经(绕代理，国内稳定)
    def fetch_tencent():
        import urllib.request
        import json as _json
        if not tencent_code:
            return None

        all_data = []
        batch_starts = ['2018-01-01', '2021-01-01', '2024-01-01']
        for start in batch_starts:
            url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tencent_code},day,{start},2099-12-31,800,qfq'
            try:
                proxy_handler = urllib.request.ProxyHandler({})
                opener = urllib.request.build_opener(proxy_handler)
                req = urllib.request.Request(url)
                resp = opener.open(req, timeout=15)
                data = _json.loads(resp.read().decode('utf-8'))

                key = tencent_code
                if key not in data.get('data', {}):
                    continue
                inner = data['data'][key]
                day_list = inner.get('qfqday', inner.get('day', []))
                if not day_list:
                    continue

                for item in day_list:
                    all_data.append({
                        'date': item[0],
                        'open': float(item[1]),
                        'close': float(item[2]),
                        'high': float(item[3]),
                        'low': float(item[4]),
                        'volume': float(item[5]) if len(item) > 5 else 0,
                    })
            except Exception as e:
                print(f"    腾讯批次{start}失败: {str(e)[:60]}")
                continue

        if not all_data:
            return None
        df = pd.DataFrame(all_data)
        df = df.drop_duplicates(subset='date', keep='last')
        df = df.sort_values('date').reset_index(drop=True)
        return df

    # 3. 东方财富(AKShare)
    def fetch_em():
        df = ak.fund_etf_hist_em(symbol=etf_code, period="daily", adjust="qfq")
        return df

    # 4. 新浪(AKShare)
    def fetch_sina():
        df = ak.fund_etf_hist_sina(symbol=etf_code)
        return df

    data_sources = [
        ("Yahoo Finance", fetch_yf),
        ("腾讯财经", fetch_tencent),
        ("东方财富", fetch_em),
        ("新浪", fetch_sina),
    ]

    def fetch_with_retry(source_name, fetch_func, retries=2):
        for i in range(retries):
            try:
                print(f"  {source_name} 尝试 {i+1}/{retries}...")
                df = fetch_func()
                if df is not None and len(df) > 10:
                    print(f"  {source_name} 获取 {len(df)} 条数据")
                    return df
                print(f"  {source_name} 数据不足")
            except Exception as e:
                print(f"  {source_name} 失败: {str(e)[:80]}")
            if i < retries - 1:
                time.sleep(3)
        return None

    df = None
    source_used = ""
    for source_name, fetch_func in data_sources:
        print(f"  >>> 尝试数据源: {source_name}")
        result = fetch_with_retry(source_name, fetch_func)
        if result is not None and len(result) > 10:
            df = result
            source_used = source_name
            print(f"  ✓ {source_name} 成功")
            break
        print(f"  ✗ {source_name} 最终失败")

    if df is None or len(df) == 0:
        raise Exception(f"{etf_name}({etf_code}) 所有数据源都失败了")

    # 标准化
    df = rename_columns(df)
    df = standardize_columns(df)

    # 转换日期
    try:
        df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
    except Exception as e:
        print(f"  日期转换失败: {e}")
        raise

    df = df.sort_values('date').reset_index(drop=True)

    if len(df) < 250:
        print(f"  ⚠ 数据量不足250条（{len(df)}条），MA250计算可能不准确")

    result = df.tail(days).reset_index(drop=True)
    print(f"  最终数据：{len(result)} 条，{result['date'].min().strftime('%Y-%m-%d')} → {result['date'].max().strftime('%Y-%m-%d')}")

    return result, source_used

# ============ 纳指100专属：PE分位+回撤数据 ============
def fetch_index_data(config, days=400):
    """获取A股指数历史数据（AKShare）"""
    index_code = config['index_code']
    index_name = config['index_name']
    print(f"  数据源目标: 指数 {index_name}({index_code})")

    df = None
    for i in range(3):
        try:
            print(f"  AKShare指数 尝试 {i+1}/3...")
            raw = ak.index_zh_a_hist(symbol=index_code, period="daily", start_date="20200101", end_date="20991231")
            if raw is not None and len(raw) > 10:
                df = raw
                print(f"  AKShare指数 获取 {len(df)} 条数据")
                break
        except Exception as e:
            print(f"  AKShare指数 失败: {str(e)[:80]}")
        if i < 2:
            time.sleep(3)

    if df is None or len(df) == 0:
        raise Exception(f"{index_name}({index_code}) 指数数据获取失败")

    df = rename_columns(df)
    df = standardize_columns(df)
    df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
    df = df.sort_values('date').reset_index(drop=True)

    result = df.tail(days).reset_index(drop=True)
    print(f"  最终数据：{len(result)} 条，{result['date'].min().strftime('%Y-%m-%d')} → {result['date'].max().strftime('%Y-%m-%d')}")

    return result
def fetch_nasdaq_pe_data():
    """获取纳指100历史PE(TTM)数据 - 乐咕乐股"""
    print("  获取纳指100 PE数据（乐咕乐股）...")
    try:
        df = ak.stock_market_pe_lg(symbol='纳指100')
        df = df.rename(columns={'日期': 'date', '市盈率': 'pe_ttm', '总市值': 'market_cap'})
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        print(f"  纳指100 PE数据：{len(df)} 条，{df['date'].min().strftime('%Y-%m-%d')} → {df['date'].max().strftime('%Y-%m-%d')}")
        return df
    except Exception as e:
        print(f"  乐咕PE数据失败: {str(e)[:100]}")
        raise

def fetch_nasdaq_index_data():
    """获取纳指100指数价格 - 新浪财经"""
    print("  获取纳指100指数价格（新浪）...")
    try:
        df = ak.index_us_stock_sina(symbol='.NDX')
        df = rename_columns(df)
        df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
        df = df.sort_values('date').reset_index(drop=True)
        print(f"  纳指100价格数据：{len(df)} 条，{df['date'].min().strftime('%Y-%m-%d')} → {df['date'].max().strftime('%Y-%m-%d')}")
        return df
    except Exception as e:
        print(f"  新浪纳指价格失败: {str(e)[:100]}")
        raise

# ============ 技术指标 ============
def calculate_ma(prices, period, min_periods=None):
    """计算移动平均线，数据不足时用min_periods计算参考值"""
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
    """计算PE在历史序列中的百分位"""
    valid_pe = pe_series.dropna()
    if len(valid_pe) == 0:
        return 50.0
    return round((valid_pe < current_pe).sum() / len(valid_pe) * 100, 1)

def calculate_drawdown(prices):
    """计算当前价格相对阶段最高点的回撤幅度"""
    high = prices.max()
    current = prices.iloc[-1]
    if high == 0:
        return 0.0
    return round((current - high) / high * 100, 2)

# ============ RSI+MA250 策略逻辑（012708/025497共用）============
def get_zone(price, ma250):
    """判断市场分区：强势区/弱势区"""
    return "强势区" if price >= ma250 else "弱势区"

def get_invest_advice(rsi, zone):
    """月度定投建议（基于RSI和市场分区）"""
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
    """减仓建议（仅强势区+RSI≥65）"""
    if zone != "强势区":
        return None
    if rsi >= 72:
        return {"action": "考虑减仓", "pct": "30%", "detail": "RSI≥72·强势区·卖出30%（需确认不在冷却期）"}
    elif rsi >= 65:
        return {"action": "考虑减仓", "pct": "15%", "detail": "RSI 65-71·强势区·卖出15%（需确认不在冷却期）"}
    return None

def get_rebuy_advice(rsi, zone):
    """回补建议（仅强势区）"""
    if zone != "强势区":
        return None
    if 59 <= rsi <= 63:
        return {"action": "考虑回补", "detail": "RSI 59-63·强势区·接回已减仓2/3（触发30天冷却）"}
    elif rsi < 50:
        return {"action": "考虑回补", "detail": "RSI<50·强势区·接回全部剩余（触发30天冷却）"}
    return None

def get_risk_signal(price, ma250, days_below, ma_trend):
    """风险信号灯"""
    risk_active = days_below >= 15 and ma_trend == -1
    if risk_active:
        return "red", "持续弱势"
    elif price < ma250:
        return "yellow", "跌破MA250"
    else:
        return "green", "正常"

# ============ PE+回撤 策略逻辑（纳指100专属）============
def get_pe_zone(pe_percentile):
    """估值分区"""
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
    """回撤分区"""
    if drawdown > 15:
        return "深度回调"
    elif drawdown >= 8:
        return "中级回调"
    else:
        return "正常波动"

def get_nasdaq_invest_advice(pe_percentile, drawdown):
    """纳指定投建议"""
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
    """纳指减仓建议"""
    # 减仓禁区
    if pe_percentile < 70:
        return None
    if drawdown > 15:
        return None  # 深度回调，即使高估也只暂停不减仓
    # 触发条件：PE≥70 + 回撤<8% + 需确认不在冷却期
    if pe_percentile >= 85:
        return {"action": "考虑减仓", "pct": "30%", "detail": "PE≥85%·正常波动·卖出30%（需确认不在冷却期）"}
    elif pe_percentile >= 70:
        return {"action": "考虑减仓", "pct": "15%", "detail": "PE 70-84%·正常波动·卖出15%（需确认不在冷却期）"}
    return None

def get_nasdaq_rebuy_advice(pe_percentile, drawdown):
    """纳指回补建议"""
    # 回补禁区
    if pe_percentile >= 70:
        return None
    if drawdown > 15:
        return None
    # 标准回补
    if 40 <= pe_percentile <= 50:
        return {"action": "考虑回补", "detail": "PE 40-50%·接回已减仓2/3（触发60天冷却）"}
    elif pe_percentile < 40:
        return {"action": "考虑回补", "detail": "PE<40%·接回全部剩余减仓（触发60天冷却）"}
    return None

def get_nasdaq_risk_signal(pe_percentile, drawdown):
    """纳指风险信号"""
    if drawdown > 15:
        return "red", "深度回调"
    elif pe_percentile >= 85:
        return "yellow", "极度高估"
    elif pe_percentile >= 70:
        return "yellow", "估值偏高"
    else:
        return "green", "正常"

# ============ 处理RSI+MA250标的 ============
def process_etf_rsi_ma(config):
    """处理RSI+MA250标的：获取数据→计算指标→生成建议"""
    etf_name = config['etf_name']
    etf_code = config['etf_code']
    print(f"\n{'='*50}")
    print(f"处理标的: {etf_name}({etf_code}) [RSI+MA250]")
    print(f"{'='*50}")

    # 获取数据（ETF价格紧贴指数，用于指标计算）
    df, source_used = fetch_etf_data(config)

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

    # 最新数据
    current = df.iloc[-1]
    latest_price = float(current['close'])
    latest_ma250 = float(ma250.iloc[-1]) if not pd.isna(ma250.iloc[-1]) else latest_price
    latest_rsi = float(rsi21.iloc[-1]) if not pd.isna(rsi21.iloc[-1]) else 50.0

    # 涨跌
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

    # 市场分区
    zone = get_zone(latest_price, latest_ma250)

    # 策略建议
    invest = get_invest_advice(latest_rsi, zone)
    reduce_advice = get_reduce_advice(latest_rsi, zone)
    rebuy_advice = get_rebuy_advice(latest_rsi, zone)
    risk_signal, risk_label = get_risk_signal(latest_price, latest_ma250, days_below, ma_trend)

    print(f"  价格: {latest_price:.4f} ({'+' if price_change_pct >= 0 else ''}{price_change_pct}%)")
    print(f"  MA250: {latest_ma250:.4f}{'(参考)' if not ma_data_available else ''} | RSI21: {latest_rsi:.2f}")
    print(f"  分区: {zone} | 信号: {risk_signal}({risk_label})")
    print(f"  定投: {invest['amount']} | 减仓: {reduce_advice} | 回补: {rebuy_advice}")

    # 构建结果
    result = {
        "type": "rsi_ma",
        "is_index": True,
        "etf_code": etf_code,
        "etf_name": config['etf_name'],
        "index_code": config['index_code'],
        "index_name": config['index_name'],
        "fund_code": config['fund_code'],
        "fund_name": config['fund_name'],
        "weight": config['weight'],
        "current_price": round(latest_price, 2),
        "price_change_pct": price_change_pct,
        "ma250": round(latest_ma250, 2),
        "ma250_is_reference": not ma_data_available,
        "ma250_data_days": int(data_count),
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
        "price_vs_ma250_pct": round((latest_price / latest_ma250 - 1) * 100, 2),
        "market_date": current['date'].strftime('%Y-%m-%d'),
        "history_advice": _calc_rsi_ma_history(df, ma250, rsi21),
    }

    # K线+指标历史
    klines = []
    ma_values = []
    rsi_values = []
    for i in range(len(df)):
        klines.append({
            "time": df.iloc[i]['date'].strftime('%Y-%m-%d'),
            "open": float(df.iloc[i]['open']),
            "high": float(df.iloc[i]['high']),
            "low": float(df.iloc[i]['low']),
            "close": float(df.iloc[i]['close']),
            "volume": float(df.iloc[i]['volume'])
        })
        ma_values.append(None if pd.isna(ma250.iloc[i]) else round(float(ma250.iloc[i]), 2))
        rsi_values.append(None if pd.isna(rsi21.iloc[i]) else round(float(rsi21.iloc[i]), 2))

    history = {
        "etf_code": etf_code,
        "index_name": config['index_name'],
        "klines": klines,
        "ma_values": ma_values,
        "rsi_values": rsi_values,
    }

    return result, history, source_used

# ============ 处理纳指100标的（PE分位+回撤）============
def process_nasdaq(config):
    """处理纳指100标的：获取PE+指数数据→计算分位回撤→生成建议"""
    print(f"\n{'='*50}")
    print(f"处理标的: {config['index_name']}({config['etf_code']}) [PE分位+回撤]")
    print(f"{'='*50}")

    # 获取PE数据
    pe_df = fetch_nasdaq_pe_data()
    # 获取指数价格数据
    idx_df = fetch_nasdaq_index_data()

    # 当前PE和PE分位
    current_pe = float(pe_df['pe_ttm'].iloc[-1])
    pe_data_days = len(pe_df)
    pe_years = round(pe_data_days / 252, 1)
    pe_percentile = calculate_pe_percentile(pe_df['pe_ttm'], current_pe)
    pe_zone = get_pe_zone(pe_percentile)

    # 计算回撤（用全部历史数据的阶段高点）
    drawdown = calculate_drawdown(idx_df['close'].astype(float))
    drawdown_zone = get_drawdown_zone(drawdown)

    # 阶段高点信息
    high_price = float(idx_df['close'].astype(float).max())
    current_idx_price = float(idx_df['close'].astype(float).iloc[-1])

    # 涨跌（相对前一天）
    prev_idx_price = float(idx_df['close'].astype(float).iloc[-2])
    price_change_pct = round((current_idx_price - prev_idx_price) / prev_idx_price * 100, 2)

    # 策略建议
    invest = get_nasdaq_invest_advice(pe_percentile, drawdown)
    reduce_advice = get_nasdaq_reduce_advice(pe_percentile, drawdown)
    rebuy_advice = get_nasdaq_rebuy_advice(pe_percentile, drawdown)
    risk_signal, risk_label = get_nasdaq_risk_signal(pe_percentile, drawdown)

    # 估值分区作为zone
    zone = pe_zone + "·" + drawdown_zone

    print(f"  PE(TTM): {current_pe:.2f} | 近{pe_years}年分位: {pe_percentile:.1f}% | 估值: {pe_zone}")
    print(f"  回撤: {drawdown:.2f}% | 回撤分区: {drawdown_zone}")
    print(f"  定投: {invest['amount']} | 减仓: {reduce_advice} | 回补: {rebuy_advice}")

    # 构建结果
    result = {
        "type": "pe_drawdown",
        "is_index": True,
        "etf_code": config['etf_code'],
        "etf_name": config['etf_name'],
        "index_code": config['index_code'],
        "index_name": config['index_name'],
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
        "history_advice": _calc_pe_drawdown_history(pe_df, idx_df),
    }

    # PE历史数据（用于前端图表）
    pe_history = []
    for i in range(len(pe_df)):
        pe_history.append({
            "time": pe_df['date'].iloc[i].strftime('%Y-%m-%d'),
            "pe_ttm": round(float(pe_df['pe_ttm'].iloc[i]), 2),
        })

    # 指数价格历史（最近400条）
    idx_tail = idx_df.tail(400).reset_index(drop=True)
    idx_history = []
    for i in range(len(idx_tail)):
        idx_history.append({
            "time": idx_tail['date'].iloc[i].strftime('%Y-%m-%d'),
            "close": round(float(idx_tail['close'].iloc[i]), 2),
        })

    history = {
        "etf_code": config['etf_code'],
        "index_name": config['index_name'],
        "pe_history": pe_history,
        "price_history": idx_history,
    }

    return result, history, "乐咕+新浪"

# ============ 统一错误占位 ============
def make_error_result(config):
    """标的失败时的错误占位"""
    is_pe = config['type'] == 'pe_drawdown'
    result = {
        "type": config['type'],
        "etf_code": config['etf_code'],
        "etf_name": config['etf_name'],
        "index_code": config['index_code'],
        "index_name": config['index_name'],
        "fund_code": config['fund_code'],
        "fund_name": config['fund_name'],
        "weight": config['weight'],
        "error": True,
        "current_price": 0,
        "price_change_pct": 0,
        "zone": "数据异常",
        "invest_amount": "数据异常",
        "invest_detail": "获取失败",
        "reduce_advice": None,
        "rebuy_advice": None,
        "risk_signal": "red",
        "risk_label": "数据获取失败",
        "market_date": "",
    }
    if not is_pe:
        result.update({
            "ma250": 0, "ma250_is_reference": True, "ma250_data_days": 0,
            "rsi21": 50, "ma250_trend": "未知",
            "days_below_ma250": 0, "days_above_ma250": 0, "price_vs_ma250_pct": 0,
        })
    else:
        result.update({
            "pe_ttm": 0, "pe_percentile": 50, "pe_years": 0, "pe_data_days": 0,
            "pe_zone": "数据异常", "drawdown": 0, "drawdown_zone": "数据异常", "high_price": 0,
        })

    history = {
        "etf_code": config['etf_code'],
        "index_name": config['index_name'],
    }
    if not is_pe:
        history.update({"klines": [], "ma_values": [], "rsi_values": []})
    else:
        history.update({"pe_history": [], "price_history": []})

    return result, history

# ============ 近6个月历史操作建议 ============
def _get_monthly_dates(n_months=12):
    """获取近n个月每月21日日期列表（从旧到新）"""
    dates = []
    today = datetime.now().date()
    for i in range(n_months, 0, -1):
        # 每月21日
        target = today - relativedelta.relativedelta(months=i)
        try:
            d = target.replace(day=21)
        except ValueError:
            # 2月没有21日不可能，但防御一下
            continue
        # 如果21日在今天之后，跳过
        if d > today:
            continue
        dates.append(d)
    return dates

def _find_nearest_trading_day(df, target_date):
    """在DataFrame中找到离target_date最近的交易日（优先当天，其次之后，最后之前）"""
    td = pd.Timestamp(target_date)
    # 优先找当天
    mask = df['date'].dt.date == target_date
    if mask.any():
        return df[mask].index[-1]
    # 找之后的
    after = df[df['date'].dt.date > target_date]
    if len(after) > 0:
        return after.index[0]
    # 找之前的
    before = df[df['date'].dt.date < target_date]
    if len(before) > 0:
        return before.index[-1]
    return None

def _calc_rsi_ma_history(df, ma250, rsi21, n_months=12):
    """计算RSI+MA250标的近n个月每月21日的操作建议"""
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
        }
        if reduce_adv:
            entry["reduce"] = reduce_adv['action'] + ' ' + reduce_adv['pct']
        if rebuy_adv:
            entry["rebuy"] = rebuy_adv['action']
        history.append(entry)
    return history

def _calc_pe_drawdown_history(pe_df, idx_df, n_months=12):
    """计算PE+回撤标的近n个月每月21日的操作建议"""
    dates = _get_monthly_dates(n_months)
    history = []
    for d in dates:
        # 找最近的PE数据
        pe_idx = _find_nearest_trading_day(pe_df, d)
        if pe_idx is None:
            continue
        pe_val = float(pe_df.iloc[pe_idx]['pe_ttm'])

        # 找最近的价格数据
        price_idx = _find_nearest_trading_day(idx_df, d)
        if price_idx is None:
            continue
        price = float(idx_df.iloc[price_idx]['close'])

        # PE分位：用目标日期之前的PE数据计算
        pe_before = pe_df[pe_df['date'].dt.date <= d]['pe_ttm'].dropna()
        if len(pe_before) < 50:
            continue
        pe_pct = round((pe_before < pe_val).sum() / len(pe_before) * 100, 1)

        # 回撤：用目标日期之前的价格计算阶段高点
        prices_before = idx_df[idx_df['date'].dt.date <= d]['close'].astype(float)
        if len(prices_before) < 20:
            continue
        high = float(prices_before.max())
        dd = round((price - high) / high * 100, 2) if high != 0 else 0.0

        invest = get_nasdaq_invest_advice(pe_pct, dd)
        reduce_adv = get_nasdaq_reduce_advice(pe_pct, dd)
        rebuy_adv = get_nasdaq_rebuy_advice(pe_pct, dd)
        pe_zone = get_pe_zone(pe_pct)

        entry = {
            "date": d.strftime('%Y-%m-%d'),
            "price": round(price, 2),
            "pe_ttm": round(pe_val, 1),
            "pe_percentile": pe_pct,
            "pe_zone": pe_zone,
            "drawdown": dd,
            "invest": invest['amount'],
        }
        if reduce_adv:
            entry["reduce"] = reduce_adv['action'] + ' ' + reduce_adv['pct']
        if rebuy_adv:
            entry["rebuy"] = rebuy_adv['action']
        history.append(entry)
    return history

# ============ NaN 清理工具 ============
def clean_nan(obj):
    """递归清理字典/列表中的 NaN/Infinity，替换为 None（JSON null）"""
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan(item) for item in obj]
    elif isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj

# ============ 主程序 ============
def main():
    print(f"[{datetime.now()}] ============ 价值投资控制台 ============")
    print(f"[{datetime.now()}] Python版本: {sys.version}")
    print(f"[{datetime.now()}] AKShare版本: {ak.__version__}")

    results = []
    histories = []
    sources = []

    for config in ETFS:
        try:
            if config['type'] == 'rsi_ma':
                result, history, source_used = process_etf_rsi_ma(config)
            elif config['type'] == 'pe_drawdown':
                result, history, source_used = process_nasdaq(config)
            else:
                raise Exception(f"未知策略类型: {config['type']}")
            results.append(result)
            histories.append(history)
            sources.append(source_used)
        except Exception as e:
            print(f"[{datetime.now()}] ✗ {config['etf_name']} 处理失败: {e}")
            result, history = make_error_result(config)
            results.append(result)
            histories.append(history)
            sources.append("失败")

    # 输出合并JSON
    data_output = {
        "update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "data_source": " / ".join(set(s for s in sources if s != "失败")) or "全部失败",
        "etfs": results,
    }

    history_output = {
        "etfs": histories,
    }

    os.makedirs('docs', exist_ok=True)

    with open('docs/data.json', 'w', encoding='utf-8') as f:
        json.dump(clean_nan(data_output), f, ensure_ascii=False, indent=2)

    with open('docs/history.json', 'w', encoding='utf-8') as f:
        json.dump(clean_nan(history_output), f, ensure_ascii=False)

    print(f"\n[{datetime.now()}] ============ 数据更新完成! ============")
    for r in results:
        if r.get('type') == 'pe_drawdown':
            print(f"  {r.get('index_name', '?')}: PE{r.get('pe_ttm', 0):.1f} 分位{r.get('pe_percentile', 0):.1f}% {r.get('pe_zone', '?')} → 定投{r.get('invest_amount', '?')}")
        else:
            print(f"  {r.get('index_name', '?')}: 价格{r.get('current_price', 0):.4f} RSI{r.get('rsi21', 0):.1f} {r.get('zone', '?')} → 定投{r.get('invest_amount', '?')}")

if __name__ == "__main__":
    main()
