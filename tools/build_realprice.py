#!/usr/bin/env python3
"""新竹市／新竹縣 實價登錄資料整理

從內政部不動產交易實價查詢服務網 (plvr.land.moi.gov.tw) 下載最近 N 季
開放資料（季資料 lvr_landcsv.zip），清理後輸出精簡 JSON 給 realprice.html 使用。

用法：
    python3 tools/build_realprice.py                # 自動抓最新 4 季
    python3 tools/build_realprice.py --seasons 115S3 115S2 115S1 114S4
    python3 tools/build_realprice.py --cache /tmp/realprice-cache --out data/realprice-hsinchu.json

清理規則：
  * 只取「房地」「房地+車位」「建物」交易；排除純土地、純車位。
  * 排除備註含特殊交易（親友/員工/特殊關係、急買急賣、協議價購、政府標售、
    畸零地、債權債務、瑕疵/凶宅、含其他標的價格、含租約、持分…）。
  * 排除多棟建物合併一筆、面積或價格為 0、預售屋已解約。
  * 中古/成屋(a)：只留交易日期在最早一季登記起日之後者（較舊日期多為預售屋交屋的分件登記）。
  * 單價（萬/坪）：車位價格與面積都有登記時 =(總價-車位價)/(建物面積-車位面積)，否則 = 總價/建物面積
    （與政府公布單價算法一致）。
  * 地址：去掉縣市與鄉鎮市區，保留路/街/段/巷，門牌號以 30 號為一區間（如 1~30號），不保留樓層與之幾。
"""
import argparse, csv, datetime as dt, io, json, os, re, statistics, sys, time, urllib.request, zipfile

URL = "https://plvr.land.moi.gov.tw/DownloadSeason?season={s}&type=zip&fileName=lvr_landcsv.zip"
CITIES = {"o": "新竹市", "j": "新竹縣"}
DISTRICTS = {
    "新竹縣": ["竹北市", "竹東鎮", "新埔鎮", "關西鎮", "湖口鄉", "新豐鄉", "芎林鄉", "橫山鄉", "北埔鄉", "寶山鄉", "峨眉鄉", "尖石鄉", "五峰鄉"],
    # 開放資料的新竹市「鄉鎮市區」欄位只有「新竹市」，門牌也多半不含東/北/香山區，故以全市計
    "新竹市": ["新竹市"],
}
TYPES = ["大樓", "華廈", "公寓", "透天", "套房", "其他"]
SPECIAL = re.compile(
    "親友|員工|特殊關係|較低|急買|急賣|二親等|夫妻|配偶|債權|債務|瑕疵|凶宅|毗鄰|畸零地|合併使用|"
    "政府機關|協議價購|標售|標讓售|拍賣|法拍|地上權|持分|含其他|包含其他|公共設施保留地|重建|都更|未辦繼承|含租約|僅車位")
PING = 0.3025          # 1 平方公尺 = 0.3025 坪
UNIT_PING = 3.30579    # 元/平方公尺 × 3.30579 = 元/坪
FW = str.maketrans("０１２３４５６７８９～", "0123456789~")
CN = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn2int(s):
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch in CN:
            cur = CN[ch]
        elif ch == "十":
            total += (cur or 1) * 10; cur = 0
        elif ch == "百":
            total += (cur or 1) * 100; cur = 0
        else:
            return None
    return total + cur


def floor_text(level, total):
    level = (level or "").replace("層", "")
    tot = cn2int((total or "").replace("層", ""))
    if not level:
        f = ""
    elif "全" in level:
        f = "全棟"
    else:
        parts = re.split("[，,、]", level)
        first = parts[0]
        under = first.startswith("地下")
        n = cn2int(first.replace("地下", ""))
        f = ("B%d" % n if under else "%d" % n) if n is not None else first[:4]
        if len(parts) > 1 and not parts[1].startswith("見"):
            f += "+"
    return f, tot


def btype(s):
    for k, v in (("住宅大樓", "大樓"), ("華廈", "華廈"), ("公寓", "公寓"), ("透天", "透天"), ("套房", "套房")):
        if k in s:
            return TYPES.index(v)
    return TYPES.index("其他")


def mask_addr(addr, city, dist):
    a = addr.translate(FW).replace(" ", "")
    a = re.sub("[\ue000-\uf8ff]", "○", a)  # 政府造字（私用區字元）手機無法顯示
    changed = True
    while changed:  # 新竹市資料常見「新竹市新竹市」重複前綴，也可能帶東區/北區
        changed = False
        for p in (city, dist, "東區", "北區", "香山區"):
            if p and a.startswith(p):
                a = a[len(p):]; changed = True
    a = re.sub(r"^\D{1,3}[里村](?=\d+鄰)", "", a)
    a = re.sub(r"\d+鄰", "", a)
    m = re.search(r"(\d+)(?:[之\-－]\d+)?號", a)
    if not m:
        return re.sub(r"\d.*$", "", a)[:20]
    pre = re.sub(r"[\d、，,之\-－]+$", "", a[:m.start()])  # 多個門牌號（215、216、…）只留區間
    pre = re.sub(r"(\d+弄)?(\d+衖)?$", "", pre)
    n = int(m.group(1))
    lo = (n - 1) // 30 * 30 + 1
    return "%s%d~%d號" % (pre, lo, lo + 29)


def roc_int(s):
    s = (s or "").strip()
    if not re.fullmatch(r"\d{6,7}", s):
        return None
    s = s.zfill(7)
    return int(s)


def roc_to_date(r):
    y, m, d = r // 10000 + 1911, r // 100 % 100, r % 100
    try:
        return dt.date(y, max(m, 1), max(d, 1))
    except ValueError:
        return dt.date(y, max(min(m, 12), 1), 1)


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def fetch(season, cache):
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, season + ".zip")
    if os.path.exists(path) and zipfile.is_zipfile(path):
        return path
    req = urllib.request.Request(URL.format(s=season), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    if not data.startswith(b"PK"):
        return None  # 該季尚未發布（伺服器回傳 HTML 訊息頁）
    with open(path, "wb") as f:
        f.write(data)
    return path


def latest_seasons(n, cache):
    today = dt.date.today()
    y, q = today.year - 1911, (today.month - 1) // 3 + 1
    out = []
    for _ in range(n + 4):
        s = "%dS%d" % (y, q)
        if fetch(s, cache):
            out.append(s)
            if len(out) == n:
                break
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


def read_csv(z, name):
    rows = list(csv.DictReader(io.StringIO(z.read(name).decode("utf-8-sig"))))
    return rows[1:]  # 第二列是英文欄名


def reg_window(z):
    t = z.read("build_time.xml").decode("utf-8", "replace")
    m = re.search(r"登記日期\s*(\d+)年(\d+)月(\d+)日至\s*(\d+)年(\d+)月(\d+)日", t)
    return tuple(int(x) for x in m.groups()) if m else None


def build(seasons, cache):
    dist_list = [[c, d] for c in ("新竹縣", "新竹市") for d in DISTRICTS[c]]
    dist_idx = {(c, d): i for i, (c, d) in enumerate(dist_list)}
    stats = {"raw_a": 0, "raw_b": 0, "kept_a": 0, "kept_b": 0, "land_or_park": 0, "special": 0,
             "multi_bldg": 0, "bad_value": 0, "old_trade_date": 0, "cancelled": 0, "dup": 0}
    A, B, names, name_idx, seen = [], [], [], {}, set()
    windows, published = [], None
    zips = {s: zipfile.ZipFile(fetch(s, cache)) for s in seasons}
    for s, z in zips.items():
        w = reg_window(z)
        if w:
            windows.append(w)
        if s == seasons[0]:
            published = dt.date(*z.infolist()[0].date_time[:3])
    first_reg = min(windows)[:3] if windows else None
    # 最早一季登記起日的「當月 1 日」(民國 yyyymmdd)：早於此日期成交者多為預售屋交屋分件登記
    a_min_date = (first_reg[0] * 10000 + first_reg[1] * 100 + 1) if first_reg else 0

    for s, z in zips.items():
        for p, city in CITIES.items():
            for kind in "ab":
                for x in read_csv(z, "%s_lvr_land_%s.csv" % (p, kind)):
                    stats["raw_" + kind] += 1
                    target = x["交易標的"]
                    if not ("房地" in target or target == "建物"):
                        stats["land_or_park"] += 1; continue
                    sid = x["編號"]
                    if sid in seen:
                        stats["dup"] += 1; continue
                    seen.add(sid)
                    if SPECIAL.search(x["備註"]):
                        stats["special"] += 1; continue
                    if kind == "b" and x.get("解約情形", "").strip():
                        stats["cancelled"] += 1; continue
                    m = re.search(r"建物(\d+)", x["交易筆棟數"])
                    if m and int(m.group(1)) > 1:
                        stats["multi_bldg"] += 1; continue
                    total = fnum(x["總價元"]); area = fnum(x["建物移轉總面積平方公尺"])
                    pprice = fnum(x["車位總價元"]); parea = fnum(x["車位移轉總面積平方公尺"])
                    date = roc_int(x["交易年月日"])
                    if total <= 0 or area <= 0 or not date:
                        stats["bad_value"] += 1; continue
                    if kind == "a" and date < a_min_date:
                        stats["old_trade_date"] += 1; continue
                    if pprice > 0 and parea > 0 and area - parea > 0:
                        unit = (total - pprice) / ((area - parea) * PING); park = 1
                    else:
                        unit = total / (area * PING); park = 2 if (parea > 0 or "車位" in target) else 0
                    dist = x["鄉鎮市區"].strip()
                    di = dist_idx.get((city, dist))
                    if di is None:
                        dist_list.append([city, dist]); di = len(dist_list) - 1; dist_idx[(city, dist)] = di
                    fl, tot = floor_text(x["移轉層次"], x["總樓層數"])
                    age = None
                    built = roc_int(x.get("建築完成年月", ""))
                    if kind == "a" and built and built > 100:
                        age = round(max(0.0, (roc_to_date(date) - roc_to_date(built)).days / 365.25), 1)
                    rooms = int(fnum(x["建物現況格局-房"]))
                    row = [di, date, mask_addr(x["土地位置建物門牌"], city, dist), btype(x["建物型態"]), fl, tot or 0,
                           round(area * PING, 1), age, round(total / 1e4, 1), round(unit / 1e4, 2),
                           park, round(pprice / 1e4, 1) if pprice else 0, rooms]
                    if kind == "b":
                        nm = x.get("建案名稱", "").strip()
                        if nm not in name_idx:
                            name_idx[nm] = len(names); names.append(nm)
                        row.append(name_idx[nm])
                        B.append(row)
                    else:
                        A.append(row)
                    stats["kept_" + kind] += 1
    A.sort(key=lambda r: -r[1]); B.sort(key=lambda r: -r[1])
    def span(rows):
        return [min(r[1] for r in rows), max(r[1] for r in rows)] if rows else None
    w0, w1 = min(windows), max(windows)
    today = dt.date.today()
    return {
        "meta": {
            "seasons": seasons,
            "reg": "%d/%d/%d–%d/%d/%d" % (w0[0], w0[1], w0[2], w1[3], w1[4], w1[5]),
            "spanA": span(A), "spanB": span(B),
            "published": "%d/%d/%d" % (published.year - 1911, published.month, published.day) if published else "",
            "updated": "%d/%d/%d" % (today.year - 1911, today.month, today.day),
            "stats": stats,
            "cols": ["區", "日期", "地址", "型態", "樓層", "總樓層", "建坪", "屋齡", "總價萬", "單價萬坪", "車位(0無1分離2含)", "車位價萬", "房", "建案"],
        },
        "d": dist_list, "t": TYPES, "n": names, "a": A, "b": B,
    }


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--seasons", nargs="*")
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--cache", default="/tmp/realprice-cache")
    ap.add_argument("--out", default=os.path.join(here, "..", "data", "realprice-hsinchu.json"))
    a = ap.parse_args()
    seasons = a.seasons or latest_seasons(a.n, a.cache)
    seasons = sorted(seasons, key=lambda s: (int(s.split("S")[0]), int(s.split("S")[1])), reverse=True)
    for s in seasons:
        if not fetch(s, a.cache):
            sys.exit("season %s not available" % s)
    data = build(seasons, a.cache)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print("seasons", seasons, "stats", data["meta"]["stats"], "bytes", os.path.getsize(a.out))


if __name__ == "__main__":
    main()
