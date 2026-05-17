"""
回测脚本：三标的按权重每月定投收益
  - 中证红利低波动 40%: 3200元/月
  - 国证价值100 40%: 3200元/月
  - 纳指100 20%: 1600元/月

统一起始月 2016-02（国证价值100 2016-01-04 成立的次月）
每月21日按当日收盘价买入，计算累计投入、当前市值、最大回撤、总收益率
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import math
import urllib.request
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
import numpy as np
from dateutil import relativedelta

# ============ 配置 ============
TOTAL_MONTHLY = 8000  # 每月总投资
START_YEAR = 2016
START_MONTH = 2       # 2016-01-04 最晚指数成立次月

TODAY = datetime.now()


def _build_no_proxy_opener():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    urllib.request.install_opener(opener)
    return opener


# ============ 获取指数全部历史价格 ============
def fetch_hongli_all():
    _build_no_proxy_opener()
    print("获取 中证红利低波动(指数H30269) 全部历史...")
    today_str = datetime.now().strftime('%Y%m%d')
    df = ak.stock_zh_index_hist_csindex(
        symbol='H30269', start_date='20160101', end_date=today_str
    )
    col_map = {'日期': 'date', '收盘': 'close', '开盘': 'open',
               '最高': 'high', '最低': 'low', '成交量': 'volume'}
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
    df = df.sort_values('date').reset_index(drop=True)
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    print(f"  ✓ {len(df)}条, {df['date'].min().strftime('%Y-%m-%d')} → {df['date'].max().strftime('%Y-%m-%d')}")
    return df


def fetch_jiazhi_all():
    _build_no_proxy_opener()
    print("获取 国证价值100(全收益480081) 全部历史...")
    today_str = datetime.now().strftime('%Y%m%d')
    df = ak.index_hist_cni(
        symbol='480081', start_date='20160101', end_date=today_str
    )
    df = df.rename(columns={'日期': 'date', '收盘价': 'close'})
    df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
    df = df.sort_values('date').reset_index(drop=True)
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    print(f"  ✓ {len(df)}条, {df['date'].min().strftime('%Y-%m-%d')} → {df['date'].max().strftime('%Y-%m-%d')}")
    return df


def fetch_nasdaq_price_all():
    _build_no_proxy_opener()
    print("获取 纳指100 全部价格历史...")
    df = ak.index_us_stock_sina(symbol='.NDX')
    col_map = {'日期': 'date', '日期时间': 'date', '收盘': 'close'}
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
    df = df.sort_values('date').reset_index(drop=True)
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    print(f"  ✓ {len(df)}条, {df['date'].min().strftime('%Y-%m-%d')} → {df['date'].max().strftime('%Y-%m-%d')}")
    return df


# ============ 回测核心逻辑 ============
def find_nearest_trading_day(df, target_date):
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


def backtest_single(df, monthly_amount, start_date, name):
    """
    单个指数的每月定投回测
    从 start_date（年月）开始，每月21日投入
    返回: (总投入, 当前市值, 交易记录, 每月持仓市值记录)
    """
    df = df.copy().reset_index(drop=True)
    last_date = df['date'].max()
    
    # 开始月份
    current = datetime(start_date.year, start_date.month, 1)
    total_invested = 0.0
    total_shares = 0.0
    trades = []
    
    while current <= last_date:
        try:
            target_day = current.replace(day=21)
        except ValueError:
            current = current + relativedelta.relativedelta(months=1)
            continue
        
        if target_day > last_date:
            break
        
        idx = find_nearest_trading_day(df, target_day.date())
        if idx is None:
            current = current + relativedelta.relativedelta(months=1)
            continue
        
        price = float(df.iloc[idx]['close'])
        if price <= 0:
            current = current + relativedelta.relativedelta(months=1)
            continue
        
        shares_bought = monthly_amount / price
        total_invested += monthly_amount
        total_shares += shares_bought
        
        trade_date = df.iloc[idx]['date'].strftime('%Y-%m-%d')
        current_value = total_shares * price
        
        trades.append({
            "date": trade_date,
            "price": round(price, 2),
            "shares_bought": round(shares_bought, 4),
            "cumulative_shares": round(total_shares, 4),
            "cumulative_invested": round(total_invested, 2),
            "cumulative_value": round(current_value, 2),
            "monthly_pnl_pct": round((current_value - total_invested) / total_invested * 100, 2),
        })
        
        current = current + relativedelta.relativedelta(months=1)
    
    latest_price = float(df.iloc[-1]['close'])
    current_value = total_shares * latest_price
    
    return total_invested, current_value, trades, latest_price, total_shares


# ============ 带再平衡的回测（三标的同步推进） ============
def backtest_with_rebalance(dfs, amounts, weights, names, start_date, rebalance_months=None):
    """
    三标的同步定投 + 定期再平衡
    dfs: [df_hongli, df_jiazhi, df_nasdaq]
    amounts: [3200, 3200, 1600]
    weights: [0.4, 0.4, 0.2]
    names: ["红利低波", "价值100", "纳指100"]
    rebalance_months: 再平衡执行的月份列表，如 [12] 表示每年12月，[6,12] 表示半年
    """
    if rebalance_months is None:
        rebalance_months = [12]  # 默认每年底
    n = len(dfs)
    dfs = [d.copy().reset_index(drop=True) for d in dfs]
    last_dates = [d['date'].max() for d in dfs]
    last_date = max(last_dates)
    
    current = datetime(start_date.year, start_date.month, 1)
    
    # 每个标的的份额和累计投入
    shares = [0.0] * n
    total_invested = [0.0] * n
    
    # 每个标的的交易记录
    all_trades = [[] for _ in range(n)]
    # 再平衡记录
    rebalance_log = []
    # 每月组合快照
    portfolio_history = []
    
    peak_value = 0
    max_drawdown = 0.0
    max_dd_start = ""
    max_dd_end = ""
    
    while current <= last_date:
        try:
            target_day = current.replace(day=21)
        except ValueError:
            current += relativedelta.relativedelta(months=1)
            continue
        
        if target_day > last_date:
            break
        
        # 获取三个标的21日最近交易日的价格
        prices = []
        indices = []
        for i in range(n):
            idx = find_nearest_trading_day(dfs[i], target_day.date())
            if idx is None:
                prices.append(None)
                indices.append(None)
            else:
                prices.append(float(dfs[i].iloc[idx]['close']))
                indices.append(idx)
        
        # 任一标的缺数据则跳过该月
        if any(p is None or p <= 0 for p in prices):
            current += relativedelta.relativedelta(months=1)
            continue
        
        trade_date = dfs[0].iloc[indices[0]]['date'].strftime('%Y-%m-%d') if indices[0] else target_day.strftime('%Y-%m-%d')
        
        # === 定投买入 ===
        for i in range(n):
            shares_bought = amounts[i] / prices[i]
            total_invested[i] += amounts[i]
            shares[i] += shares_bought
            
            current_value = shares[i] * prices[i]
            all_trades[i].append({
                "date": trade_date,
                "price": round(prices[i], 2),
                "shares_bought": round(shares_bought, 4),
                "cumulative_shares": round(shares[i], 4),
                "cumulative_invested": round(total_invested[i], 2),
                "cumulative_value": round(current_value, 2),
                "monthly_pnl_pct": round((current_value - total_invested[i]) / total_invested[i] * 100, 2),
            })
        
        # === 定期再平衡（rebalance_months 指定的月份） ===
        if current.month in rebalance_months:
            current_values = [shares[i] * prices[i] for i in range(n)]
            total_value = sum(current_values)
            target_values = [total_value * w for w in weights]
            
            rebal_records = []
            for i in range(n):
                diff = current_values[i] - target_values[i]
                if abs(diff) > 0.01:
                    old_shares = shares[i]
                    shares[i] = target_values[i] / prices[i]
                    delta_shares = shares[i] - old_shares
                    rebal_records.append({
                        "name": names[i],
                        "before_value": round(current_values[i], 2),
                        "target_value": round(target_values[i], 2),
                        "diff": round(diff, 2),
                        "delta_shares": round(delta_shares, 4),
                        "action": "卖出" if diff > 0 else "买入",
                    })
                    # 再平衡后更新交易记录中的份额和市值
                    new_value = shares[i] * prices[i]
                    if all_trades[i]:
                        all_trades[i][-1]["cumulative_shares"] = round(shares[i], 4)
                        all_trades[i][-1]["cumulative_value"] = round(new_value, 2)
                        all_trades[i][-1]["monthly_pnl_pct"] = round((new_value - total_invested[i]) / total_invested[i] * 100, 2)
            
            if rebal_records:
                rebalance_log.append({
                    "date": trade_date,
                    "details": rebal_records,
                })
                print(f"  🔄 再平衡 {trade_date}:")
                for r in rebal_records:
                    print(f"     {r['name']}: {r['action']} {abs(r['diff']):>8,.0f}元 ({r['before_value']:>8,.0f}→{r['target_value']:>8,.0f})")
        
        # === 组合快照 ===
        v_list = [shares[i] * prices[i] for i in range(n)]
        inv_list = total_invested
        total_v = sum(v_list)
        total_inv = sum(inv_list)
        pnl_pct = round((total_v - total_inv) / total_inv * 100, 2) if total_inv > 0 else 0
        
        if total_v > peak_value:
            peak_value = round(total_v, 2)
        
        dd = (total_v - peak_value) / peak_value * 100 if peak_value > 0 else 0
        if dd < max_drawdown:
            max_drawdown = dd
            max_dd_end = trade_date
            # 回溯找峰值月份
            for prev in portfolio_history:
                if prev['portfolio_value'] == peak_value:
                    max_dd_start = prev['ym'] + "-21"
                    break
        
        portfolio_history.append({
            "ym": trade_date[:7],
            "trade_date": trade_date,
            "portfolio_value": round(total_v, 2),
            "total_invested": round(total_inv, 2),
            "pnl_pct": pnl_pct,
            "drawdown": round(dd, 2),
            "weights": {names[i]: round(v_list[i]/total_v*100, 1) for i in range(n)},
        })
        
        current += relativedelta.relativedelta(months=1)
    
    # 最新价格和市值
    latest_value = sum(shares[i] * float(dfs[i].iloc[-1]['close']) for i in range(n))
    latest_prices = [float(d.iloc[-1]['close']) for d in dfs]
    
    # 汇总
    summary = {
        "start_date": f"{START_YEAR}-{START_MONTH:02d}",
        "end_date": TODAY.strftime('%Y-%m'),
        "monthly_total": TOTAL_MONTHLY,
        "months": len(portfolio_history),
        "weights_desc": "40/40/20",
        "rebalance_months": rebalance_months,
        "rebalance_desc": f"每年{'、'.join(str(m) for m in rebalance_months)}月21日",
        "total_invested": round(sum(total_invested), 2),
        "current_value": round(latest_value, 2),
        "total_return_pct": round((latest_value - sum(total_invested)) / sum(total_invested) * 100, 2),
        "max_drawdown": round(max_drawdown, 2),
        "max_dd_start": max_dd_start,
        "max_dd_end": max_dd_end,
        "latest_prices": {names[i]: round(latest_prices[i], 2) for i in range(n)},
    }
    
    # 各标的详情
    items = []
    final_values = [shares[i] * float(dfs[i].iloc[-1]['close']) for i in range(n)]
    for i in range(n):
        inv = total_invested[i]
        val = final_values[i]
        ret = (val - inv) / inv * 100 if inv > 0 else 0
        items.append({
            "name": names[i],
            "monthly": amounts[i],
            "trades": len(all_trades[i]),
            "invested": round(inv, 2),
            "value": round(val, 2),
            "return_pct": round(ret, 2),
        })
    summary["items"] = items
    
    # 当前权重
    tv = sum(final_values)
    summary["current_weights"] = {names[i]: round(final_values[i]/tv*100, 1) for i in range(n)}
    
    # 找谷底详情
    dd_details = [p for p in portfolio_history if p['drawdown'] < 0]
    worst_dd = min(dd_details, key=lambda x: x['drawdown']) if dd_details else None
    if worst_dd:
        summary["max_dd_valley"] = {
            "portfolio_value": worst_dd['portfolio_value'],
            "total_invested": worst_dd['total_invested'],
            "pnl_pct": worst_dd['pnl_pct'],
            "date": worst_dd['trade_date'],
        }
    
    # 近3年最大回撤
    three_years_ago = (TODAY - relativedelta.relativedelta(years=3)).strftime('%Y-%m')
    recent_dd_list = [p for p in portfolio_history if p['ym'] >= three_years_ago and p['drawdown'] < 0]
    if recent_dd_list:
        worst_recent = min(recent_dd_list, key=lambda x: x['drawdown'])
        if worst_recent != worst_dd:
            summary["recent_3y_max_dd"] = {
                "drawdown": round(worst_recent['drawdown'], 2),
                "date": worst_recent['trade_date'],
                "pnl_pct": worst_recent['pnl_pct'],
            }
    
    # 历年收益
    yearly = {}
    for p in portfolio_history:
        yr = p['ym'][:4]
        if yr not in yearly:
            yearly[yr] = []
        yearly[yr].append(p)
    
    yearly_data = []
    for yr in sorted(yearly.keys()):
        entries = yearly[yr]
        last = entries[-1]
        yr_dd = min(e['drawdown'] for e in entries)
        prev_yr = str(int(yr) - 1)
        if prev_yr in yearly:
            prev_last = yearly[prev_yr][-1]
            yr_inv = last['total_invested'] - prev_last['total_invested']
            # 每年收益率 = (年末市值 - 年初市值 - 当年投入) / 年初市值
            yr_ret = round((last['portfolio_value'] - prev_last['portfolio_value'] - yr_inv) / prev_last['portfolio_value'] * 100, 2)
        else:
            yr_inv = last['total_invested']
            yr_ret = last['pnl_pct']  # 第一年与累计收益率相同
        yearly_data.append({
            "year": yr,
            "invested": round(yr_inv, 0),
            "value": round(last['portfolio_value'], 0),
            "return_pct": last['pnl_pct'],
            "year_return_pct": yr_ret,
            "max_dd": round(yr_dd, 2),
        })
    summary["yearly"] = yearly_data
    summary["rebalance_log"] = rebalance_log
    
    return summary, portfolio_history, all_trades


# ============ 敏感性分析 ============
def sensitivity_analysis(dfs, names, start_dt):
    """运行多种权重+再平衡频率组合，比较结果"""
    configs = [
        ("年度再平衡 40/40/20", [0.4, 0.4, 0.2], [12]),
        ("半年度再平衡 40/40/20", [0.4, 0.4, 0.2], [6, 12]),
        ("半年度 30/50/20", [0.3, 0.5, 0.2], [6, 12]),
        ("半年度 50/30/20", [0.5, 0.3, 0.2], [6, 12]),
        ("半年度 35/35/30", [0.35, 0.35, 0.3], [6, 12]),
    ]
    
    results = []
    for label, w, r_months in configs:
        amounts = [int(TOTAL_MONTHLY * w[i]) for i in range(3)]
        adj_amounts = []
        remaining = TOTAL_MONTHLY
        for i in range(2):
            adj_amounts.append(amounts[i])
            remaining -= amounts[i]
        adj_amounts.append(remaining)
        
        summary, _, _ = backtest_with_rebalance(dfs, adj_amounts, w, names, start_dt, rebalance_months=r_months)
        results.append({
            "label": label,
            "weights": f"{int(w[0]*100)}/{int(w[1]*100)}/{int(w[2]*100)}",
            "total_return": summary["total_return_pct"],
            "max_dd": summary["max_drawdown"],
            "current_value": summary["current_value"],
            "total_invested": summary["total_invested"],
        })
    
    print(f"\n{'='*60}")
    print("📊 权重敏感性分析对比")
    print(f"{'='*60}")
    sep = "-" * 65
    print(sep)
    print(f"  {'配置':<24} {'收益率':>8} {'最大回撤':>10} {'市值':>12} {'投入':>10}")
    print(sep)
    for r in results:
        ret = r['total_return']
        dd = r['max_dd']
        val = r['current_value']
        inv = r['total_invested']
        print(f"  {r['label']:<24} {ret:>+7.2f}% {dd:>9.2f}% {val:>12,.0f} {inv:>10,.0f}")
    print(sep)
    return results


# ============ 子功能：运行一个回测配置 ============
def run_one_backtest(dfs, names, start_dt, weights=None, rebalance_months=None, print_results=True, save_json=True):
    """运行单个回测配置并可选保存"""
    if weights is None:
        weights = [0.4, 0.4, 0.2]
    if rebalance_months is None:
        rebalance_months = [6, 12]  # 默认改为半年度
    
    n = len(weights)
    amounts = []
    remaining = TOTAL_MONTHLY
    for i in range(n - 1):
        amt = int(TOTAL_MONTHLY * weights[i])
        amounts.append(amt)
        remaining -= amt
    amounts.append(remaining)
    
    freq_label = "半年(6+12月)" if 6 in rebalance_months and 12 in rebalance_months else \
                 "每年12月" if rebalance_months == [12] else f"每月{','.join(str(m) for m in rebalance_months)}"
    
    w_desc = "/".join(str(int(w*100)) for w in weights)
    
    if print_results:
        print("=" * 60)
        print(f"回测配置: 权重{w_desc} | 再平衡: {freq_label}")
        print(f"每月总投入: {TOTAL_MONTHLY}元")
        print(f"统一起始月: {START_YEAR}-{START_MONTH:02d}")
        print(f"回测日: {TODAY.strftime('%Y-%m-%d')}")
        print("=" * 60)
        print()
    
    summary, portfolio_history, all_trades = backtest_with_rebalance(
        dfs, amounts, weights, names, start_dt, rebalance_months=rebalance_months
    )
    
    if print_results:
        s = summary
        print(f"{'='*60}")
        print(f"📊 回测结果")
        print(f"{'='*60}")
        
        print(f"\n📌 汇总")
        print(f"  {'再平衡':>10}: {freq_label}")
        print(f"  {'定投月数':>10}: {s['months']} 个月 ({s['start_date']} ~ {s['end_date']})")
        print(f"  {'总投入':>10}: {s['total_invested']:>10,.0f} 元")
        print(f"  {'当前市值':>10}: {s['current_value']:>10,.0f} 元")
        print(f"  {'总收益额':>10}: {s['current_value'] - s['total_invested']:>+10,.0f} 元")
        print(f"  {'总收益率':>10}: {s['total_return_pct']:>+8.2f}%")
        
        print(f"\n📉 最大回撤")
        print(f"  {'最大回撤':>10}: {s['max_drawdown']:.2f}%")
        print(f"  {'峰值日期':>10}: {s['max_dd_start']}")
        print(f"  {'谷底日期':>10}: {s['max_dd_end']}")
        if 'max_dd_valley' in s:
            v = s['max_dd_valley']
            print(f"  {'谷底市值':>10}: {v['portfolio_value']:>10,.0f} 元")
            print(f"  {'谷底投入':>10}: {v['total_invested']:>10,.0f} 元")
            print(f"  {'谷底收益':>10}: {v['pnl_pct']:>+8.2f}%")
        
        if 'recent_3y_max_dd' in s:
            r = s['recent_3y_max_dd']
            print(f"\n  近3年最大回撤: {r['drawdown']:.2f}%（{r['date']}）")
            print(f"  近3年谷底收益: {r['pnl_pct']:+.2f}%")
        
        print(f"\n📈 各标的详细")
        sep_line = "-" * 65
        print(sep_line)
        print(f"  {'标的':<12} {'月投':>6} {'定投月数':>8} {'投入':>10} {'市值':>10} {'收益':>10} {'收益率':>8}")
        print(sep_line)
        for item in s['items']:
            val = item['value']
            inv = item['invested']
            print(f"  {item['name']:<12} {item['monthly']:>6} {item['trades']:>8} {inv:>10,.0f} {val:>10,.0f} {val-inv:>+10,.0f} {item['return_pct']:>+7.2f}%")
        print(sep_line)
        
        print(f"\n📊 当前配置比例")
        for name, pct in s['current_weights'].items():
            target = 40 if name != "纳指100" else 20
            arrow = "↑" if pct > target else "↓"
            print(f"  {name:<12}: {pct:.1f}%（目标{target}%）{arrow}")
        
        print(f"\n💰 当前指数价格")
        for name, price in s['latest_prices'].items():
            print(f"  {name}: {price:>10.2f}")
        
        print(f"\n📅 历年收益率")
        print(f"  {'年份':>6} {'投入':>8} {'市值':>10} {'收益率':>8} {'最大回撤':>8}")
        print("-" * 45)
        for yd in s['yearly']:
            print(f"  {yd['year']:>6} {yd['invested']:>8,.0f} {yd['value']:>10,.0f} {yd['return_pct']:>+7.2f}% {yd['max_dd']:>7.2f}%")
        print("-" * 45)
    
    # 保存 JSON
    if save_json:
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docs', 'data', 'backtest_result.json')
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        output = {
            "summary": s,
            "rebalance_count": len(s.get("rebalance_log", [])),
            "portfolio_history": [{
                "ym": p['ym'],
                "date": p['trade_date'],
                "value": p['portfolio_value'],
                "invested": p['total_invested'],
                "pnl_pct": p['pnl_pct'],
                "dd": p['drawdown'],
            } for p in portfolio_history],
        }
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 结果已保存: {output_path}")
    
    return summary, portfolio_history, all_trades


# ============ 主程序 ============
def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="三标的定投回测（含再平衡）")
    parser.add_argument('--freq', choices=['annual', 'semi'], default='semi',
                        help='再平衡频率: annual(每年底) / semi(半年, 默认)')
    parser.add_argument('--weights', type=str, default='40,40,20',
                        help='权重比例, 用逗号分隔, 如 40,40,20')
    parser.add_argument('--sensitivity', action='store_true',
                        help='运行敏感性分析(多种权重组合对比)')
    args = parser.parse_args()
    
    rebalance_months = [12] if args.freq == 'annual' else [6, 12]
    w_parts = [float(x.strip())/100 for x in args.weights.split(',')]
    # 归一化
    w_sum = sum(w_parts)
    weights = [w / w_sum for w in w_parts]
    
    # 获取各指数全部历史
    df_hongli = fetch_hongli_all()
    df_jiazhi = fetch_jiazhi_all()
    df_nasdaq = fetch_nasdaq_price_all()
    dfs = [df_hongli, df_jiazhi, df_nasdaq]
    names = ["中证红利低波", "价值100", "纳指100"]
    start_dt = datetime(START_YEAR, START_MONTH, 1)
    
    if args.sensitivity:
        # 只做敏感性分析，不覆盖主结果
        sensitivity_analysis(dfs, names, start_dt)
        print("\n(敏感性分析不保存到 backtest_result.json)")
        print("如需更新正式数据，请不带 --sensitivity 运行:")
        print("  python backtest_dingtou.py --freq semi --weights 40,40,20")
    else:
        # 正常运行并保存JSON
        run_one_backtest(dfs, names, start_dt, weights=weights, rebalance_months=rebalance_months)


if __name__ == "__main__":
    main()
