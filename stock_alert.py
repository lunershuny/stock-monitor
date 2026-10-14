#!/usr/bin/env python3
"""
台股選股監控（上市股票，資料來源：證交所）
策略 1：法人連買 ＋ 突破季線 ＋ 量增
策略 A：均線多頭排列（MA5>MA10>MA20 且向上）＋ 回測 MA10
策略 B：爆量長紅 K 突破盤整區
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
MIN_VOL_LOTS = 500     # 當日成交量下限（張），所有策略共用，過濾冷門股
STOCKS_ONLY = True     # True = 只看一般個股（排除 ETF 等 0 開頭代號）

# 策略 1：法人連買＋突破季線＋量增
BUYER = "三大法人"      # 可改成 "外資" 或 "投信"
CONSEC_DAYS = 3        # 法人連續買超天數
MA_DAYS = 60           # 季線
VOL_AVG_DAYS = 5       # 量增比較基準：前 N 日均量
VOL_RATIO = 1.5        # 今日量 ≥ 前 N 日均量 × 倍數

# 策略 A：多頭排列＋回測 MA10
PULLBACK_TOL = 0.01    # 最低價碰到 MA10 上方 1% 以內就算回測
RISING_DAYS = 1        # 均線斜率向上：今天均線 > N 天前

# 策略 B：爆量突破盤整
BOX_DAYS = 20          # 盤整區：前 N 個交易日
BOX_MAX_WIDTH = 0.15   # 盤整區高低差 ≤ 15%
BURST_RATIO = 2.0      # 爆量：今日量 ≥ 盤整期均量 × 倍數
LONG_RED_PCT = 0.03    # 長紅K：(收盤-開盤)/開盤 ≥ 3%

HISTORY_KEEP = 30      # 網頁保留最近幾個交易日的結果
SPARK_DAYS = 40        # 網頁走勢小圖顯示天數

TG_TOKEN = os.getenv("TG_TOKEN")      # Telegram Bot Token（可不設）
TG_CHAT_ID = os.getenv("TG_CHAT_ID")  # Telegram Chat ID（可不設）
PAGE_URL = os.getenv("PAGE_URL", "")  # 監控網頁網址，會附在通知裡
# ==============================================

STRATEGIES = [
    {"id": "inst", "name": "法人突破季線", "ref_name": "季線",
     "desc": f"{BUYER}連買{CONSEC_DAYS}日、收盤剛站上{MA_DAYS}日均線、"
             f"成交量達前{VOL_AVG_DAYS}日均量{VOL_RATIO:g}倍以上"},
    {"id": "pullback", "name": "多頭回測十日線", "ref_name": "MA10",
     "desc": f"MA5＞MA10＞MA20 且三線都向上，當天最低價回測到 MA10（{PULLBACK_TOL:.0%} 內），"
             f"收盤仍守在 MA10 之上"},
    {"id": "breakout", "name": "爆量突破盤整", "ref_name": "盤整區上緣",
     "desc": f"前{BOX_DAYS}日高低差在{BOX_MAX_WIDTH:.0%}以內，今天長紅K（實體≥{LONG_RED_PCT:.0%}）"
             f"收盤突破區間高點，成交量達盤整期均量{BURST_RATIO:g}倍以上"},
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "twse_cache")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
TEMPLATE = os.path.join(BASE_DIR, "template.html")
HEADERS = {"User-Agent": "Mozilla/5.0"}
PRICE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={}&type=ALLBUT0999&response=json"
INST_URL = "https://www.twse.com.tw/rwd/zh/fund/T86?date={}&selectType=ALLBUT0999&response=json"
# 每檔價格欄位：[名稱, 收盤, 成交股數, 開盤, 最高, 最低]
PRICE_COLS = 6


# ---------------- 資料下載 ----------------
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
            i = {k: f.index(k) for k in ["證券代號", "證券名稱", "收盤價", "成交股數", "開盤價", "最高價", "最低價"]}
            return {r[i["證券代號"]].strip(): [r[i["證券名稱"]].strip(), num(r[i["收盤價"]]), num(r[i["成交股數"]]),
                                              num(r[i["開盤價"]]), num(r[i["最高價"]]), num(r[i["最低價"]])]
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
        if day.get("closed"):
            return None
        first = next(iter(day["price"].values()), [])
        if len(first) >= PRICE_COLS:      # 舊版快取沒有開高低價，重新下載
            return day

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
    cols = {"close": 1, "vol": 2, "open": 3, "high": 4, "low": 5}
    raw = {k: {} for k in cols}
    raw["net"], names = {}, {}
    for d, day in days.items():
        for k, i in cols.items():
            raw[k][d] = {c: v[i] for c, v in day["price"].items()}
        raw["net"][d] = {c: v.get(BUYER) for c, v in day["inst"].items()}
        names.update({c: v[0] for c, v in day["price"].items()})
    f = {k: pd.DataFrame.from_dict(v, orient="index").sort_index().astype(float) for k, v in raw.items()}
    for k in f:
        f[k] = f[k].reindex(index=f["close"].index, columns=f["close"].columns)
    return f, names


# ---------------- 策略 ----------------
def series(s):
    return [None if pd.isna(v) else round(float(v), 2) for v in s.iloc[-SPARK_DAYS:]]


def make_row(code, names, f, ref, stats, sort_key):
    return {"code": code, "name": names.get(code, ""), "close": round(float(f["close"][code].iloc[-1]), 2),
            "spark": series(f["close"][code]), "ref": series(ref), "stats": stats, "_sort": sort_key}


def pct(x):
    return f"{x * 100:+.2f}%"


def s_inst(f, names, base):
    close, vol, net = f["close"], f["vol"], f["net"]
    ma = close.rolling(MA_DAYS).mean()
    c0, c1, m0, m1 = close.iloc[-1], close.iloc[-2], ma.iloc[-1], ma.iloc[-2]
    v0, vavg = vol.iloc[-1], vol.iloc[-1 - VOL_AVG_DAYS:-1].mean()
    recent = net.iloc[-CONSEC_DAYS:]
    hit = base & (c0 > m0) & (c1 <= m1) & (v0 >= vavg * VOL_RATIO) & (recent > 0).all()
    rows = []
    for code in hit[hit].index:
        lots = recent[code].sum() / 1000
        rows.append(make_row(code, names, f, ma[code], [
            ["高於季線", pct(c0[code] / m0[code] - 1), True],
            ["量比", f"{v0[code] / vavg[code]:.1f} 倍", False],
            [f"法人{CONSEC_DAYS}日買超", f"{lots:,.0f} 張", False]], -lots))
    return rows


def s_pullback(f, names, base):
    close, low, vol = f["close"], f["low"], f["vol"]
    ma5, ma10, ma20 = (close.rolling(n).mean() for n in (5, 10, 20))
    n = RISING_DAYS
    aligned = (ma5.iloc[-1] > ma10.iloc[-1]) & (ma10.iloc[-1] > ma20.iloc[-1])
    rising = ((ma5.iloc[-1] > ma5.iloc[-1 - n]) & (ma10.iloc[-1] > ma10.iloc[-1 - n])
              & (ma20.iloc[-1] > ma20.iloc[-1 - n]))
    m10 = ma10.iloc[-1]
    touch = (low.iloc[-1] <= m10 * (1 + PULLBACK_TOL)) & (close.iloc[-1] >= m10)
    hit = base & aligned & rising & touch
    rows = []
    for code in hit[hit].index:
        gap = close[code].iloc[-1] / m10[code] - 1
        slope = ma20[code].iloc[-1] / ma20[code].iloc[-6] - 1
        rows.append(make_row(code, names, f, ma10[code], [
            ["收盤距 MA10", pct(gap), True],
            ["MA20 五日斜率", pct(slope), False],
            ["成交量", f"{vol[code].iloc[-1] / 1000:,.0f} 張", False]], gap))
    return rows


def s_breakout(f, names, base):
    close, opn, high, low, vol = f["close"], f["open"], f["high"], f["low"], f["vol"]
    win = slice(-1 - BOX_DAYS, -1)
    box_hi, box_lo = high.iloc[win].max(), low.iloc[win].min()
    width = box_hi / box_lo - 1
    c0, o0, c1 = close.iloc[-1], opn.iloc[-1], close.iloc[-2]
    v0, vavg = vol.iloc[-1], vol.iloc[win].mean()
    full = high.iloc[win].notna().all() & low.iloc[win].notna().all()
    hit = (base & full & (width <= BOX_MAX_WIDTH) & (c0 > box_hi)
           & ((c0 - o0) / o0 >= LONG_RED_PCT) & (c0 > c1) & (v0 >= vavg * BURST_RATIO))
    rows = []
    for code in hit[hit].index:
        ratio = v0[code] / vavg[code]
        ref = pd.Series([None] * len(close), index=close.index, dtype=float)
        ref.iloc[-1 - BOX_DAYS:] = box_hi[code]
        rows.append(make_row(code, names, f, ref, [
            ["突破幅度", pct(c0[code] / box_hi[code] - 1), True],
            ["量比", f"{ratio:.1f} 倍", False],
            ["盤整區高低差", f"{width[code] * 100:.1f}%", False]], -ratio))
    return rows


RUNNERS = {"inst": s_inst, "pullback": s_pullback, "breakout": s_breakout}


def run_all(f, names):
    if len(f["close"]) < MA_DAYS + 2:
        raise SystemExit(f"歷史資料不足（只有 {len(f['close'])} 天）")
    codes = f["close"].columns
    ok = pd.Series([(not STOCKS_ONLY) or (len(c) == 4 and c.isdigit() and c[0] != "0") for c in codes], index=codes)
    base = ok & (f["vol"].iloc[-1] >= MIN_VOL_LOTS * 1000)
    out = {}
    for s in STRATEGIES:
        rows = sorted(RUNNERS[s["id"]](f, names, base), key=lambda r: r.pop("_sort"))
        out[s["id"]] = rows
    return out


# ---------------- 輸出 ----------------
def format_msg(date, results):
    lines = [f"📈 {date:%Y-%m-%d} 選股監控"]
    if date != dt.date.today():
        lines.append("⚠️ 今日資料尚未公布，以下為最近交易日")
    for s in STRATEGIES:
        rows = results[s["id"]]
        lines.append(f"\n【{s['name']}】{len(rows)} 檔")
        lines += [f"  {r['code']} {r['name']}  {r['close']:g}" for r in rows[:15]]
        if len(rows) > 15:
            lines.append(f"  …另有 {len(rows) - 15} 檔")
    if PAGE_URL:
        lines.append(f"\n🔗 {PAGE_URL}")
    return "\n".join(lines)


def notify(text):
    print(text)
    if not (TG_TOKEN and TG_CHAT_ID):
        print("\n（未設定 TG_TOKEN / TG_CHAT_ID，只印在畫面上）")
        return
    for i in range(0, len(text), 3500):  # Telegram 單則上限 4096 字
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": text[i:i + 3500]}, timeout=30)


def update_page(date, results):
    """把結果存進 docs/data.json 並產生 docs/index.html；回傳是否為新的交易日。"""
    os.makedirs(DOCS_DIR, exist_ok=True)
    path = os.path.join(DOCS_DIR, "data.json")
    hist = {"days": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fp:
            hist = json.load(fp)
    ds = date.strftime("%Y-%m-%d")
    is_new = all(d["date"] != ds for d in hist["days"])
    days = [d for d in hist["days"] if d["date"] != ds and "strategies" in d]  # 舊格式不保留
    days.append({"date": ds, "strategies": results})
    hist = {
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "strategies": [{k: s[k] for k in ("id", "name", "desc", "ref_name")} for s in STRATEGIES],
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
    for name in os.listdir(CACHE_DIR):
        if name[:8] < cutoff:
            os.remove(os.path.join(CACHE_DIR, name))


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    days = collect(MA_DAYS + VOL_AVG_DAYS + 2)
    f, names = build(days)
    date = f["close"].index[-1]
    results = run_all(f, names)
    if update_page(date, results):
        notify(format_msg(date, results))
    else:
        print(f"{date} 已處理過，只更新網頁、不重複通知")
    prune_cache()


if __name__ == "__main__":
    main()
