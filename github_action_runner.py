"""
GitHub Actions Runner - 价值投资控制台
双标的 RSI21+MA250 策略：012708(东证红利低波) + 025497(国证价值100)
定时获取ETF数据并更新docs目录下的JSON文件
"""
import json
import os
import sys
import time
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime

# ============ 双标的配置 ============
ETFS = [
    {
        "etf_code": "512890",        # 数据源ETF代码
        "etf_name": "红利低波ETF",    # 数据源ETF名称
        "index_code": "931446",       # 跟踪指数代码（仅展示用）
        "index_name": "东证红利低波",  # 跟踪指数名称
        "fund_code": "012708",        # 基金代码
        "fund_name": "东方红红利低波A",
        "yahoo_suffix": ".SS",        # 上海交易所
    },
    {
        "etf_code": "159263",        # 数据源ETF代码
        "etf_name": "价值100ETF",     # 数据源ETF名称
        "index_code": "980081",       # 跟踪指数代码（仅展示用）
        "index_name": "国证价值100",   # 跟踪指数名称
        "fund_code": "025497",        # 基金代码
        "fund_name": "易方达价值100A",
        "yahoo_suffix": ".SZ",        # 深圳交易所
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
    yahoo_code = f"{etf_code}{config['yahoo_suffix']}"
    # 腾讯财经代码格式: sh512890 / sz159263
    tencent_prefix = 'sh' if config['yahoo_suffix'] == '.SS' else 'sz'
    tencent_code = f"{tencent_prefix}{etf_code}"

    print(f"  数据源目标: {etf_name}({etf_code}) → Yahoo: {yahoo_code}, 腾讯: {tencent_code}")

    # 1. yfinance
    def fetch_yf():
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

        # 腾讯qfq API每次最多返回约800条，需分批获取
        all_data = []
        # 分批从更早开始获取，确保有足够历史数据计算MA250
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
        # 去重（按日期）
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

# ============ 策略逻辑 ============
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
    """减仓建议（仅强势区+RSI≥65）
    注意：实际操作需用户自行判断是否在冷却期，系统仅显示建议"""
    if zone != "强势区":
        return None
    if rsi >= 72:
        return {"action": "考虑减仓", "pct": "30%", "detail": "RSI≥72·强势区·卖出30%（需确认不在冷却期）"}
    elif rsi >= 65:
        return {"action": "考虑减仓", "pct": "15%", "detail": "RSI 65-71·强势区·卖出15%（需确认不在冷却期）"}
    return None

def get_rebuy_advice(rsi, zone):
    """回补建议（仅强势区）
    注意：实际操作需用户自行判断冷却期和豁免条件，系统仅显示建议"""
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

# ============ 单标的处理 ============
def process_etf(config):
    """处理单个标的：获取数据→计算指标→生成建议"""
    etf_name = config['etf_name']
    etf_code = config['etf_code']
    print(f"\n{'='*50}")
    print(f"处理标的: {etf_name}({etf_code})")
    print(f"{'='*50}")

    # 获取数据
    df, source_used = fetch_etf_data(config)

    # 计算指标
    # MA250：数据不足时用 min_periods=120 计算参考值
    data_count = len(df)
    ma_min_periods = min(120, data_count)  # 至少120天才开始计算MA参考值
    ma250 = calculate_ma(df['close'], MA_PERIOD, min_periods=ma_min_periods)
    rsi21 = calculate_rsi(df['close'], RSI_PERIOD)
    ma_data_available = data_count >= MA_PERIOD  # MA250是否已满数据

    # MA250趋势（20日斜率）
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

    # 跌破MA250天数
    days_below = 0
    for i in range(len(df) - 1, -1, -1):
        if pd.isna(ma250.iloc[i]):
            break
        if df.iloc[i]['close'] < ma250.iloc[i]:
            days_below += 1
        else:
            break

    # 站上MA250天数
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
        "etf_code": etf_code,
        "etf_name": config['etf_name'],
        "index_code": config['index_code'],
        "index_name": config['index_name'],
        "fund_code": config['fund_code'],
        "fund_name": config['fund_name'],
        "current_price": round(latest_price, 4),
        "price_change_pct": price_change_pct,
        "ma250": round(latest_ma250, 4),
        "ma250_is_reference": not ma_data_available,  # MA250是否为参考值（数据不足）
        "ma250_data_days": int(data_count),  # 可用数据天数
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
        ma_values.append(None if pd.isna(ma250.iloc[i]) else round(float(ma250.iloc[i]), 4))
        rsi_values.append(None if pd.isna(rsi21.iloc[i]) else round(float(rsi21.iloc[i]), 2))

    history = {
        "etf_code": etf_code,
        "index_name": config['index_name'],
        "klines": klines,
        "ma_values": ma_values,
        "rsi_values": rsi_values,
    }

    return result, history, source_used

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
            result, history, source_used = process_etf(config)
            results.append(result)
            histories.append(history)
            sources.append(source_used)
        except Exception as e:
            print(f"[{datetime.now()}] ✗ {config['etf_name']} 处理失败: {e}")
            # 标的失败时添加错误占位
            results.append({
                "etf_code": config['etf_code'],
                "etf_name": config['etf_name'],
                "index_code": config['index_code'],
                "index_name": config['index_name'],
                "fund_code": config['fund_code'],
                "fund_name": config['fund_name'],
                "error": str(e),
                "current_price": 0,
                "price_change_pct": 0,
                "ma250": 0,
                "ma250_is_reference": True,
                "ma250_data_days": 0,
                "rsi21": 50,
                "zone": "数据异常",
                "invest_amount": "数据异常",
                "invest_detail": "获取失败",
                "reduce_advice": None,
                "rebuy_advice": None,
                "risk_signal": "red",
                "risk_label": "数据获取失败",
                "ma250_trend": "未知",
                "days_below_ma250": 0,
                "days_above_ma250": 0,
                "price_vs_ma250_pct": 0,
                "market_date": "",
            })
            histories.append({
                "etf_code": config['etf_code'],
                "index_name": config['index_name'],
                "klines": [],
                "ma_values": [],
                "rsi_values": [],
            })
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
        json.dump(data_output, f, ensure_ascii=False, indent=2)

    with open('docs/history.json', 'w', encoding='utf-8') as f:
        json.dump(history_output, f, ensure_ascii=False)

    print(f"\n[{datetime.now()}] ============ 数据更新完成! ============")
    for r in results:
        print(f"  {r.get('index_name', '?')}: 价格{r.get('current_price', 0):.4f} RSI{r.get('rsi21', 0):.1f} {r.get('zone', '?')} → 定投{r.get('invest_amount', '?')}")

if __name__ == "__main__":
    main()
