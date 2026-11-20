#!/usr/bin/env python3
"""Scrape the company listings from https://zhanyihouse-news.netlify.app/ into data/listings.json.

The source page embeds its listings as a JS array (`const PROPERTIES = [...]`) in the
HTML, there is no JSON endpoint. Run from the repo root:  python3 tools/build_listings.py
Photos are hotlinked from yccdn.yungching.com.tw (only the image key is stored).
Hsinchu City sub-district (東區/北區/香山區) is inferred from the road name with
tools/hsinchu_city_roads.json; roads that cross districts stay as plain 新竹市.
"""
import json, re, sys, urllib.request, pathlib, datetime

SRC = "https://zhanyihouse-news.netlify.app/"
ROOT = pathlib.Path(__file__).resolve().parent.parent
IMG_PREFIX = "https://yccdn.yungching.com.tw/v1/image/?key="


def fetch():
    req = urllib.request.Request(SRC, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8")


def parse_properties(html):
    i = html.index("const PROPERTIES = [")
    j = html.index("\n];", i)
    js = html[html.index("[", i):j + 2]
    js = re.sub(r"(?m)^(\s+)(\w+):", r'\1"\2":', js)
    js = re.sub(r",(\s*[\]}])", r"\1", js)  # trailing commas
    return json.loads(js)


def main():
    html = fetch()
    props = parse_properties(html)
    m = re.search(r'id="siteUpdated">([^<]+)<', html)
    updated = m.group(1).strip() if m else ""
    roads = json.loads((ROOT / "tools" / "hsinchu_city_roads.json").read_text("utf-8"))
    dists, single = roads["dists"], roads["single"]

    out, seen = [], set()
    for p in props:
        m = re.search(r"/house/(\d+)", p["url"])
        if not m or m.group(1) in seen or not p["img"].startswith(IMG_PREFIX):
            continue
        seen.add(m.group(1))
        price = float(p["price"].replace(",", "").replace("萬", ""))
        sub = ""
        if p["city"] == "新竹市" and p["addr"] in single:
            sub = dists[single[p["addr"]]]
        out.append({
            "id": m.group(1),
            "n": re.sub(r"\s+", " ", p["name"]).strip(),
            "a": p["addr"],
            "c": p["district"] if p["city"] == "新竹縣" else "新竹市",
            "sd": sub,
            "p": p["ping"], "mb": p["mb"], "r": p["rooms"], "pr": price,
            "t": p["type"], "s": p["section"],
            "img": p["img"][len(IMG_PREFIX):].split("&")[0],
        })
    out.sort(key=lambda x: x["id"], reverse=True)
    data = {"source": SRC, "updated": updated,
            "built": datetime.date.today().isoformat(), "list": out}
    dest = ROOT / "data" / "listings.json"
    dest.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"{len(out)} listings, source updated {updated} -> {dest} ({dest.stat().st_size//1024} KB)")


if __name__ == "__main__":
    sys.exit(main())
