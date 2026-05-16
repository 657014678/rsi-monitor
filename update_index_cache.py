"""
本地缓存生成脚本 - 在本地（国内网络）运行
从东方财富API获取指数K线数据，保存到 data/cache/ 目录
供 GitHub Actions 在海外无法访问东方财富时使用
"""
import json
import os
import sys
import time
import pandas as pd
import numpy as np
from datetime import datetime

# 添加项目根目录
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用 github_action_runner 中的配置
from github_action_runner import ETFS, fetch_index_price_em

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'cache')

def update_cache_for_rsi_ma(config):
    """为RSI+MA250标的更新指数K线缓存"""
    index_code = config['index_code']
    index_name = config['index_name']

    if config['type'] != 'rsi_ma':
        return None

    print(f"\n{'='*40}")
    print(f"更新缓存: {index_name}({index_code})")
    print(f"{'='*40}")

    # 调用现有的数据获取函数
    index_data = fetch_index_price_em(config)
    kline_df = index_data.get('kline_df') if index_data else None

    if kline_df is None or len(kline_df) == 0:
        print(f"  ✗ {index_name} K线数据获取失败")
        return None

    # 保存为CSV
    cache_file = os.path.join(CACHE_DIR, f'{index_code}.csv')
    kline_df['date'] = kline_df['date'].dt.strftime('%Y-%m-%d')
    kline_df.to_csv(cache_file, index=False, encoding='utf-8')

    print(f"  ✓ 缓存已保存: {cache_file}")
    print(f"  数据范围: {kline_df['date'].iloc[0]} → {kline_df['date'].iloc[-1]} ({len(kline_df)}条)")

    # 同时保存实时行情
    if index_data.get('realtime_price'):
        rt_file = os.path.join(CACHE_DIR, f'{index_code}_realtime.json')
        rt_data = {
            'price': index_data['realtime_price'],
            'change_pct': index_data.get('realtime_change_pct'),
            'prev_close': index_data.get('realtime_prev_close'),
            'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        with open(rt_file, 'w', encoding='utf-8') as f:
            json.dump(rt_data, f, ensure_ascii=False, indent=2)
        print(f"  ✓ 实时行情已保存: {rt_file}")

    return cache_file

def main():
    print(f"[{datetime.now()}] ============ 指数K线缓存更新 ============")
    os.makedirs(CACHE_DIR, exist_ok=True)

    updated = []
    for config in ETFS:
        if config['type'] == 'rsi_ma':
            cache_file = update_cache_for_rsi_ma(config)
            if cache_file:
                updated.append(os.path.basename(cache_file))
        else:
            print(f"\n跳过 {config['index_name']}（非RSI+MA250标的，GitHub Actions可直接获取）")

    print(f"\n{'='*40}")
    print(f"缓存更新完成！更新了 {len(updated)} 个文件:")
    for f in updated:
        print(f"  - data/cache/{f}")
    print(f"\n下次推送后，GitHub Actions 将在API失败时使用缓存数据")

if __name__ == '__main__':
    main()
