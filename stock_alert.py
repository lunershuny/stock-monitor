#!/usr/bin/env python3
"""
台股選股通知：法人連買 ＋ 突破季線 ＋ 量增
資料來源：證交所公開資料（上市股票）
用法：盤後（建議 17:00 後）執行 python stock_alert.py
結果會推播到 Telegram，並產生監控網頁 docs/index.html
"""
import os
import json
import time
import datetime as dt

import requests
import pandas as pd

# ================== 參數設定 ==================
BUYER = "三大法人"      # 可改成 "外資" 或 "投信"
CONSEC_DAYS = 3        # 法人連續買超天數
MA_DAYS = 60           # 季線（60日均線）
VOL_AVG_DAYS = 5       # 量增比較基準：前 N 日均量
VOL_RATIO = 1.5        # 今日量 ≥ 前 N 日均量 × 倍數
MIN_VOL_LOTS = 500     # 今日成交量下限（張），過濾冷門股
STOCKS_ONLY = True     # True = 只看一般個股（排除 ETF 等 0 開頭代號）

HISTORY_KEEP = 30      # 網頁保留最近幾個交易日的結果
SPARK_DAYS = 40        # 網頁走勢小圖顯示天數

TG_TOKEN = os.getenv("TG_TOKEN")      # Telegram Bot Token（可不設）
TG_CHAT_ID = os.getenv("TG_CHAT_ID")  # Telegram Chat ID（可不設）
PAGE_URL = os.getenv("PAGE_URL", "")  # 監控網頁網址，會附在通知裡
# ==============================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "twse_cache")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
TEMPLATE = os.path.join(BASE_DIR, "template.html")
CONDITIONS = (f"{BUYER}連買{CONSEC_DAYS}日、收盤剛站上{MA_DAYS}日均線、"
              f"成交量達前{VOL_AVG_DAYS}日均量{VOL_RATIO}倍以上（至少{MIN_VOL_LOTS}張）")
HEADERS = {"User-Agent": "Mozilla/5.0"}
PRICE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={}&type=ALLBUT0999&response=json"
INST_URL = "https://www.twse.com.tw/rwd/zh/fund/T86?date={}&selectType=ALLBUT0999&response=json"


def num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def get_json(url):
    for _ in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print("連線失敗，重試中：", e)
            time.sleep(10)
    return None


def parse_price(js):
    for t in js.get("tables", []):
        f = t.get("fields") or []
        if "證券代號" in f and "收盤價" in f:
            ic, iname = f.index("證券代號"), f.index("證券名稱")
            ivol, iclose = f.index("成交股數"), f.index("收盤價")
            return {r[ic].strip(): [r[iname].strip(), num(r[iclose]), num(r[ivol])]
                    for r in t.get("data", [])}
    return {}


def parse_inst(js):
    f = js.get("fields") or []

    def col(key):
        for i, name in enumerate(f):
            if name.startswith(key):
                return i
        return None

    idx = {"外資": col("外陸資買賣超股數"), "投信": col("投信買賣超股數"),
           "三大法人": col("三大法人買賣超股數")}
    return {r[0].strip(): {k: (num(r[i]) if i is not None else None) for k, i in idx.items()}
            for r in js.get("data", [])}


def save(path, obj):
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(obj, fp, ensure_ascii=False)


def load_day(d):
    """回傳單日資料；非交易日或尚未公布則回傳 None。過去日期會快取。"""
    key = d.strftime("%Y%m%d")
    path = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fp:
            day = json.load(fp)
        return None if day.get("closed") else day

    print("下載", key)
    pj = get_json(PRICE_URL.format(key))
    time.sleep(3)  # 避免被證交所封鎖
    if pj is None:
        return None
    if pj.get("stat") != "OK":
        if d < dt.date.today():
            save(path, {"closed": True})
        return None

    ij = get_json(INST_URL.format(key))
    time.sleep(3)
    if not ij or ij.get("stat") != "OK":
        return None  # 法人資料尚未公布

    price = parse_price(pj)
    if not price:
        return None
    day = {"price": price, "inst": parse_inst(ij)}
    if d < dt.date.today() or dt.datetime.now().hour >= 17:
        save(path, day)
    return day


def collect(n):
    days, d = {}, dt.date.today()
    for _ in range(n * 2 + 30):
        if len(days) >= n:
            break
        if d.weekday() < 5:
            day = load_day(d)
            if day:
                days[d] = day
        d -= dt.timedelta(days=1)
    return dict(sorted(days.items()))


def build(days):
    close, vol, net, names = {}, {}, {}, {}
    for d, day in days.items():
        close[d] = {c: v[1] for c, v in day["price"].items()}
        vol[d] = {c: v[2] for c, v in day["price"].items()}
        net[d] = {c: v.get(BUYER) for c, v in day["inst"].items()}
        names.update({c: v[0] for c, v in day["price"].items()})

    def to_df(x):
        return pd.DataFrame.from_dict(x, orient="index").sort_index().astype(float)

    return to_df(close), to_df(vol), to_df(net), names


def screen(close, vol, net, names):
    if len(close) < MA_DAYS + 1:
        raise SystemExit(f"歷史資料不足（只有 {len(close)} 天）")

    ma = close.rolling(MA_DAYS).mean()
    c0, c1, m0, m1 = close.iloc[-1], close.iloc[-2], ma.iloc[-1], ma.iloc[-2]
    breakout = (c0 > m0) & (c1 <= m1)                      # 今天剛站上季線

    v0 = vol.iloc[-1]
    vavg = vol.iloc[-1 - VOL_AVG_DAYS:-1].mean()
    vol_up = (v0 >= vavg * VOL_RATIO) & (v0 >= MIN_VOL_LOTS * 1000)

    recent = net.reindex(index=close.index, columns=close.columns).iloc[-CONSEC_DAYS:]
    buying = (recent > 0).all()                             # 連續 N 日買超

    hit = breakout & vol_up & buying
    rows = []
    for code in hit[hit].index:
        if STOCKS_ONLY and not (len(code) == 4 and code.isdigit() and code[0] != "0"):
            continue
        def series(df):
            return [None if pd.isna(v) else round(float(v), 2) for v in df[code].iloc[-SPARK_DAYS:]]
        rows.append({
            "code": code, "name": names.get(code, ""),
            "close": round(float(c0[code]), 2), "ma": round(float(m0[code]), 2),
            "pct": round(float(c0[code] / m0[code] - 1) * 100, 2),
            "ratio": round(float(v0[code] / vavg[code]), 2),
            "net_lots": round(float(recent[code].sum()) / 1000),
            "spark": series(close), "ma_line": series(ma),
        })
    return sorted(rows, key=lambda r: r["net_lots"], reverse=True)


def format_msg(date, rows):
    head = (f"📈 {date:%Y-%m-%d} 法人連買＋突破季線＋量增\n"
            f"條件：{BUYER}連買{CONSEC_DAYS}日｜站上{MA_DAYS}MA｜"
            f"量≥{VOL_AVG_DAYS}日均量{VOL_RATIO}倍\n")
    if date != dt.date.today():
        head += "⚠️ 今日資料尚未公布，以下為最近交易日\n"
    if PAGE_URL:
        head += f"🔗 {PAGE_URL}\n"
    if not rows:
        return head + "\n今天沒有符合條件的股票。"
    lines = [head + f"共 {len(rows)} 檔\n"]
    for r in rows:
        lines.append(f"{r['code']} {r['name']}  收 {r['close']:g}（季線 {r['ma']:.2f}）\n"
                     f"  量比 {r['ratio']:.1f}x｜{BUYER}{CONSEC_DAYS}日買超 {r['net_lots']:,.0f} 張")
    return "\n".join(lines)


def notify(text):
    print(text)
    if not (TG_TOKEN and TG_CHAT_ID):
        print("\n（未設定 TG_TOKEN / TG_CHAT_ID，只印在畫面上）")
        return
    for i in range(0, len(text), 3500):  # Telegram 單則上限 4096 字
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": text[i:i + 3500]}, timeout=30)


def update_page(date, rows):
    """把結果存進 docs/data.json 並產生 docs/index.html；回傳是否為新的交易日。"""
    os.makedirs(DOCS_DIR, exist_ok=True)
    path = os.path.join(DOCS_DIR, "data.json")
    hist = {"days": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fp:
            hist = json.load(fp)
    ds = date.strftime("%Y-%m-%d")
    is_new = all(d["date"] != ds for d in hist["days"])
    days = [d for d in hist["days"] if d["date"] != ds] + [{"date": ds, "rows": rows}]
    hist = {
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "conditions": CONDITIONS,
        "days": sorted(days, key=lambda d: d["date"], reverse=True)[:HISTORY_KEEP],
    }
    save(path, hist)
    with open(TEMPLATE, encoding="utf-8") as fp:
        html = fp.read()
    data = json.dumps(hist, ensure_ascii=False).replace("</", "<\\/")
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as fp:
        fp.write(html.replace("/*DATA*/null", data))
    return is_new


def prune_cache(keep_days=150):
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).strftime("%Y%m%d")
    for f in os.listdir(CACHE_DIR):
        if f[:8] < cutoff:
            os.remove(os.path.join(CACHE_DIR, f))


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    days = collect(MA_DAYS + VOL_AVG_DAYS + 2)
    close, vol, net, names = build(days)
    date = close.index[-1]
    rows = screen(close, vol, net, names)
    if update_page(date, rows):
        notify(format_msg(date, rows))
    else:
        print(f"{date} 已處理過，只更新網頁、不重複通知")
    prune_cache()


if __name__ == "__main__":
    main()
