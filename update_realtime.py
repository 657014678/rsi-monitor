"""
update_realtime.py - 实时行情更新脚本（轻量版）
每30分钟运行一次，只更新ETF/指数最新价格，不重新计算日线指标（RSI/MA/PE）
日线指标由 daily_update.py 每天收盘更新一次

使用方式：
  python update_realtime.py           # 正常运行
  python update_realtime.py --no-push # 只更新JSON，不push

数据流：
  Tencent API → ETF最新2条K线 → 更新 etf_price/etf_price_change_pct
  新浪API    → 纳指最新2条K线 → 更新 current_price/price_change_pct
  → 写入 data.json + 各标的独立JSON → git push（有变化时）
"""
import json
import os
import sys
import math
import time
import subprocess
import urllib.request
from datetime import datetime, timedelta

# ============ 三标的配置（与 daily_update.py 保持一致） ============
ETFS = [
    {
        "type": "rsi_ma",
        "etf_code": "563020",
        "etf_name": "红利低波ETF",
        "index_code": "H30269",
        "json_file": "h30269.json",
        "tencent_prefix": "sh",
    },
    {
        "type": "rsi_ma",
        "etf_code": "159263",
        "etf_name": "价值100ETF",
        "index_code": "980081",
        "json_file": "980081.json",
        "tencent_prefix": "sz",
    },
    {
        "type": "pe_drawdown",
        "etf_code": "159696",
        "etf_name": "纳指ETF",
        "index_code": "NDX",
        "json_file": "nd100.json",
        "tencent_prefix": "sz",
    },
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, 'docs')


# ============ 数据获取 ============
def _build_no_proxy_opener():
    """创建无代理的urllib opener"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    urllib.request.install_opener(opener)
    return opener


def fetch_etf_latest_price(etf_code, prefix):
    """获取ETF最新2条K线，用于计算最新价和涨跌幅
    返回: (latest_price, prev_price, change_pct, date_str) 或 None
    """
    """获取ETF最新2条K线，用于计算最新价和涨跌幅
    返回: (latest_price, prev_price, change_pct) 或 None
    """
    full_code = f"{prefix}{etf_code}"
    try:
        _build_no_proxy_opener()
        url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={full_code},day,,,2,qfq'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode('utf-8'))

        stock_data = data.get('data', {}).get(full_code, {})
        field = 'qfqday' if 'qfqday' in stock_data else 'day'
        klines = stock_data.get(field, [])

        if not klines or len(klines) < 2:
            # 不足2条时，只返回最新价
            if len(klines) == 1:
                latest = float(klines[0][2])
                date_str = klines[0][0]
                return (latest, latest, 0.0, date_str)
                latest = float(klines[0][2])
                return (latest, latest, 0.0)
            return None

        latest = float(klines[-1][2])
        prev = float(klines[-2][2])
        change_pct = round((latest - prev) / prev * 100, 2)
        date_str = klines[-1][0]  # 格式: "2026-05-19"
        return (latest, prev, change_pct, date_str)

    except Exception as e:
        print(f"  ⚠ 腾讯 {full_code} 失败: {str(e)[:80]}")
        return None


def fetch_nasdaq_latest_price():
    """获取纳指100最新2条K线 - 新浪API精简版
    返回: (latest_price, prev_price, change_pct, date_str) 或 None
    """
    """获取纳指100最新2条K线 - 新浪API精简版"""
    try:
        _build_no_proxy_opener()
        # AKShare 底层调用的就是新浪接口，我们直接用精简方式
        import akshare as ak
        df = ak.index_us_stock_sina(symbol='.NDX')
        if df is None or len(df) < 2:
            return None
        # 取最近2条
        df = df.tail(2).reset_index(drop=True)
        # 列名可能是 '收盘' 或 'close'
        close_col = '收盘' if '收盘' in df.columns else 'close'
        latest = float(df[close_col].iloc[-1])
        prev = float(df[close_col].iloc[-2])
        change_pct = round((latest - prev) / prev * 100, 2)
        date_str = klines[-1][0]  # 格式: "2026-05-19"
        return (latest, prev, change_pct, date_str)
    except Exception as e:
        print(f"  ⚠ 新浪纳指失败: {str(e)[:80]}")
        return None


# ============ JSON 更新 ============
def _round_price(val):
    """统一价格精度：ETF价格保留4位，指数价格保留2位"""
    if val is None:
        return None
    # ETF一般在1-2元区间，用4位小数
    return round(val, 2)


def _deep_has_diff(old, new_fields):
    """检查新字段与旧数据是否有差异（递归比较部分字段）"""
    for key, new_val in new_fields.items():
        old_val = old.get(key)
        if old_val != new_val:
            return True
    return False


def update_json_file(filepath, new_fields):
    """读取JSON文件，更新指定字段，写回"""
    if not os.path.exists(filepath):
        print(f"  ⚠ 文件不存在: {filepath}")
        return False

    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 字段在 etf 节点下
    etf = data.get('etf', {})
    if not _deep_has_diff(etf, new_fields):
        return False

    # 更新字段
    data['update_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for key, val in new_fields.items():
        etf[key] = val

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True


def update_data_json(filepath, etf_index, new_fields):
    """更新 data.json 中第 etf_index 个标的的字段"""
    if not os.path.exists(filepath):
        print(f"  ⚠ 文件不存在: {filepath}")
        return False

    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    etfs = data.get('etfs', [])
    if etf_index >= len(etfs):
        return False

    etf = etfs[etf_index]
    if not _deep_has_diff(etf, new_fields):
        return False

    data['update_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for key, val in new_fields.items():
        etf[key] = val

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True


# ============ Git Push ============
def git_push():
    """提交并推送JSON到GitHub"""
    print(f"\n{'='*40}")
    print("Git Push (实时更新)")
    print(f"{'='*40}")
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain', 'docs/'],
            capture_output=True, text=True, cwd=SCRIPT_DIR
        )
        if not result.stdout.strip():
            print("  没有数据变更，跳过push")
            return

        subprocess.run(['git', 'add', 'docs/'], cwd=SCRIPT_DIR, check=True)
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        subprocess.run(
            ['git', 'commit', '-m', f'实时行情 {now}'],
            cwd=SCRIPT_DIR, check=True
        )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                subprocess.run(['git', 'push'], cwd=SCRIPT_DIR, check=True, timeout=60)
                print(f"  ✓ Push成功!")
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                if attempt < max_retries - 1:
                    wait = 10 * (attempt + 1)
                    print(f"  ⚠ Push第{attempt+1}次失败，{wait}秒后重试...")
                    time.sleep(wait)
                else:
                    raise
    except Exception as e:
        print(f"  ✗ Git操作失败: {str(e)[:80]}")
        print(f"  请手动执行: cd {SCRIPT_DIR} && git add docs/ && git commit -m '行情更新' && git push")


# ============ 主程序 ============
def main():
    no_push = '--no-push' in sys.argv
    print(f"[{datetime.now()}] ============ 实时行情更新 ============")
    if no_push:
        print("模式: 仅更新JSON（不push）")

    any_change = False

    for i, config in enumerate(ETFS):
        etf_code = config['etf_code']
        etf_name = config['etf_name']
        prefix = config['tencent_prefix']

        print(f"\n--- {etf_name}({etf_code}) ---")

        # 1. ETF实时价格（腾讯API）
        etf_result = fetch_etf_latest_price(etf_code, prefix)
        if etf_result:
            etf_latest, etf_prev, etf_change, etf_date = etf_result
            etf_latest, etf_prev, etf_change = etf_result
            new_fields = {
                "etf_price": round(etf_latest, 4),
                "etf_price_change_pct": etf_change,
                "market_date": etf_date,
                'etf_price': round(etf_latest, 4),
                'etf_price_change_pct': etf_change,
            }
            print(f"  ETF价格: {new_fields['etf_price']} ({'+' if etf_change >= 0 else ''}{etf_change}%)")

            # 更新独立JSON
            fn = config['json_file']
            fp = os.path.join(DOCS_DIR, fn)
            if update_json_file(fp, new_fields):
                any_change = True
                print(f"  ✓ {fn} 已更新")

            # 更新 data.json
            dp = os.path.join(DOCS_DIR, 'data.json')
            if update_data_json(dp, i, new_fields):
                any_change = True
                print(f"  ✓ data.json #{i} 已更新")
        else:
            print(f"  ⚠ 获取失败，跳过")

        # 2. 纳指额外获取指数最新价（新浪）
        if config['type'] == 'pe_drawdown':
            ndx_result = fetch_nasdaq_latest_price()
            if ndx_result:
                ndx_latest, ndx_prev, ndx_change, ndx_date = ndx_result
                ndx_latest, ndx_prev, ndx_change = ndx_result
                index_fields = {
                    "current_price": round(ndx_latest, 2),
                    "price_change_pct": ndx_change,
                    "market_date": ndx_date,
                    'current_price': round(ndx_latest, 2),
                    'price_change_pct': ndx_change,
                }
                print(f"  纳指指数: {index_fields['current_price']} ({'+' if ndx_change >= 0 else ''}{ndx_change}%)")

                # 更新 nd100.json
                fn = config['json_file']
                fp = os.path.join(DOCS_DIR, fn)
                if update_json_file(fp, index_fields):
                    any_change = True
                    print(f"  ✓ {fn} 指数价格已更新")

                # 更新 data.json
                dp = os.path.join(DOCS_DIR, 'data.json')
                if update_data_json(dp, i, index_fields):
                    any_change = True
                    print(f"  ✓ data.json #{i} 指数价格已更新")

    print(f"\n{'='*40}")
    if any_change:
        print("数据已更新 ✅")
        if not no_push:
            git_push()
        else:
            print("(跳过push，使用 --no-push 模式)")
    else:
        print("数据无变化，跳过更新")

    print(f"完成: {datetime.now()}")


if __name__ == "__main__":
    main()
