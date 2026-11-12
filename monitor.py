#!/usr/bin/env python3
"""HMT Watches stock monitor. Run every 15 min by GitHub Actions. State: state.json"""
import html, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = "https://www.hmtwatches.in"
LISTINGS = {"/mens": "Men's", "/womens": "Women's", "/pair": "Pair", "/unisex": "Unisex", "/watches": "Unclassified"}
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
UA = {"User-Agent": "Mozilla/5.0 (HMT stock monitor; personal use)"}
NEG = re.compile(r"coming soon|out of stock|sold out|currently unavailable|notify me", re.I)
POS = re.compile(r"<button(?![^>]*disabled)[^>]*>\s*(?:<[^>]+>\s*)*(add to cart|buy now)", re.I)

def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            print(f"fetch fail {url[:80]}: {e}", file=sys.stderr)
            time.sleep(5 * (i + 1))
    return None

def product_links(page):
    return {html.unescape(m) for m in re.findall(r'href="(https://www\.hmtwatches\.in/product_overview\?id=[^"]+)"', page)}

def page_links(page):
    return {html.unescape(m) for m in re.findall(r'href="([^"]*[?&]page=\d+[^"]*)"', page)}

def crawl():
    """Returns {url: category}. Product ids are encrypted and change per render, so URLs are only used within one run."""
    found, ok = {}, True
    for path, cat in LISTINGS.items():
        queue, seen = [BASE + path], set()
        while queue:
            u = queue.pop()
            if u in seen: continue
            seen.add(u)
            pg = get(u)
            if pg is None:
                ok = False; continue
            for p in product_links(pg):
                found.setdefault(p, cat)
                if cat in ("Men's", "Women's"): found[p] = cat
            queue += [urllib.parse.urljoin(u, l) for l in page_links(pg)]
    return found, ok

def inspect(url):
    pg = get(url)
    if pg is None: return None
    text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", pg)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text)
    t = re.search(r"<h[12][^>]*>\s*([^<]*HMT[^<]*)<", pg) or re.search(r"<title>([^<]+)</title>", pg)
    title = (t.group(1) if t else url).strip()
    price = re.search(r"(?:₹|Rs\.?)\s*([\d,]{3,})", text, re.I)
    model = re.search(r"\b([A-Z]{2,5}\s?\d{1,3}[A-Z0-9]*(?:\s?\d{2,})?)\b", title)
    if NEG.search(text): status = "unavailable"
    elif POS.search(pg): status = "in_stock"
    else: status = "unconfirmed"
    return {"title": title, "model": model.group(1) if model else "n/a",
            "price": "Rs. " + price.group(1) if price else "n/a", "status": status}

def telegram(msg):
    tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    data = urllib.parse.urlencode({"chat_id": chat, "text": msg, "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=30)

def main():
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    old = json.dumps(state, sort_keys=True)
    first_run = not state          # baseline run: don't announce every existing listing as "new"
    now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    links, complete = crawl()
    if not links:
        print("No products found (site down or layout changed); no state change."); sys.exit(1 if not complete else 0)
    alerts, seen_now = [], {}
    for url, cat in links.items():
        info = inspect(url)
        if info is None: continue                       # fetch error != stock change
        key = info["title"].lower()
        if key in seen_now: continue
        seen_now[key] = 1
        prev = state.get(key, {}).get("status")
        is_new = key not in state
        s = info["status"]
        label = None
        if s == "in_stock" and prev != "in_stock":
            label = "NEW LISTING - IN STOCK" if is_new and not first_run else "IN STOCK"
        elif s == "unconfirmed" and prev not in ("unconfirmed", "in_stock"):
            label = "UNCONFIRMED availability (check manually)"
        elif is_new and not first_run:
            label = "NEW LISTING (not yet purchasable: %s)" % ("coming soon / unavailable" if s == "unavailable" else s)
        if label:
            alerts.append(f"{info['title']} | Model: {info['model']}\nCategory: {cat}\nPrice: {info['price']}\n"
                          f"Status: {label}\n{url}\nDetected: {now}")
        state[key] = {**info, "category": cat}
    if json.dumps(state, sort_keys=True) != old:         # only touch the file when something changed
        json.dump(state, open(STATE, "w"), indent=1, ensure_ascii=False)
    if alerts:
        msg = "HMT watch alert\n\n" + "\n\n".join(alerts)
        for i in range(0, len(msg), 3800): telegram(msg[i:i + 3800])
    print(f"{now}: {len(seen_now)} products checked, {len(alerts)} alerts, first_run={first_run}")

if __name__ == "__main__":
    main()
