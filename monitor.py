#!/usr/bin/env python3
"""HMT Watches stock monitor (headless browser version). Alerts via Telegram. State: state.json"""
import json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = "https://www.hmtwatches.in"
PAGES = [("/mens", "Men's"), ("/womens", "Women's"), ("/pair", "Pair"), ("/unisex", "Unisex"),
         ("/watches", None), ("/", None)]
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NEG = re.compile(r"coming soon|out of stock|sold out|currently unavailable|notify me", re.I)
SEL = "a[href*='product_overview?id=']"

# Runs inside the browser: for every product link, find the card that holds only that product.
CARD_JS = """() => {
  const sel = "a[href*='product_overview?id=']", out = [];
  const distinct = el => new Set([...el.querySelectorAll(sel)].map(x => x.href)).size;
  document.querySelectorAll(sel).forEach(a => {
    let el = a;
    while (el.parentElement && el.parentElement !== document.body && distinct(el.parentElement) <= 1) el = el.parentElement;
    const im = el.querySelector("img");
    out.push({href: a.href, text: el.innerText || "", img: im ? im.src : ""});
  });
  return out;
}"""

def settle(page, ms=15000):
    try: page.wait_for_load_state("networkidle", timeout=ms)
    except Exception: pass

def goto(page, url):
    for i in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            settle(page)
            return True
        except Exception as e:
            print(f"load fail {url[:70]}: {str(e)[:100]}", file=sys.stderr)
            time.sleep(5 * (i + 1))
    return False

def scrape_listing(page, path):
    if not goto(page, BASE + path): return None
    if path != "/":
        for how in (lambda: page.get_by_label("Include out of Stock").check(timeout=3000),
                    lambda: page.get_by_text("Include out of Stock").first.click(timeout=3000)):
            try: how(); break
            except Exception: pass
        settle(page)
    last = -1
    for _ in range(30):                                  # scroll / load-more until no new products appear
        page.mouse.wheel(0, 5000); page.wait_for_timeout(1000)
        for t in ("Load More", "Load more", "Show More"):
            try:
                loc = page.get_by_text(t)
                if loc.count(): loc.first.click(timeout=1000)
            except Exception: pass
        n = page.locator(SEL).count()
        if n == last: break
        last = n
    return page.evaluate(CARD_JS)

def parse_card(c):
    lines = [l.strip() for l in c["text"].splitlines() if l.strip()]
    text = " ".join(lines)
    price = re.search(r"(?:₹|RS\.?)\s*([\d,]{3,})", text, re.I)
    name = next((l for l in lines if l.upper().startswith("HMT")), None) or \
           next((l for l in lines if re.search(r"[A-Za-z]{3}", l) and not NEG.fullmatch(l)), "")
    img = os.path.basename(urllib.parse.urlparse(c["img"]).path)
    return {"name": name, "price": "Rs. " + price.group(1) if price else "n/a",
            "neg": bool(NEG.search(text)), "img": img, "href": c["href"]}

def guess_category(name):
    n = name.lower()
    if re.search(r"women|ladies|lady", n): return "Women's"
    if re.search(r"\bmen|gents", n): return "Men's"
    return "Unclassified"

def inspect(page, url):
    if not goto(page, url): return None
    try: title = page.locator("h1:visible, h2:visible").first.inner_text(timeout=3000).strip()
    except Exception: title = page.title()
    body = page.inner_text("body")
    price = re.search(r"(?:₹|RS\.?)\s*([\d,]{3,})", body, re.I)
    btns = page.locator("button:visible, a:visible").filter(has_text=re.compile(r"^\s*(add to cart|buy now)\s*$", re.I))
    can_buy = any(btns.nth(i).is_enabled() for i in range(btns.count()))
    neg = bool(NEG.search(body))
    status = "unconfirmed" if (can_buy and neg) else "in_stock" if can_buy else "unavailable" if neg else "unconfirmed"
    model = re.search(r"\b([A-Z]{2,5}\s?\d{1,3}[A-Z0-9]*(?:\s?\d{2,})?)\b", title)
    return {"title": title, "model": model.group(1) if model else "n/a",
            "price": "Rs. " + price.group(1) if price else "n/a", "status": status}

def telegram(msg):
    tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    data = urllib.parse.urlencode({"chat_id": chat, "text": msg, "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=30)

def decide(status, prev, is_new, first_run):
    if status == "in_stock" and prev != "in_stock":
        return "NEW LISTING - IN STOCK" if is_new and not first_run else "IN STOCK"
    if status == "unconfirmed" and prev not in ("unconfirmed", "in_stock"):
        return "UNCONFIRMED availability (check manually)"
    if is_new and not first_run:
        return "NEW LISTING (not yet purchasable)"
    return None

def main():
    from playwright.sync_api import sync_playwright
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    old = json.dumps(state, sort_keys=True)
    first_run = not state
    now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    cards, alerts, new_state = {}, [], dict(state)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=UA, locale="en-IN", timezone_id="Asia/Kolkata",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        for path, cat in PAGES:
            res = scrape_listing(page, path)
            print(f"{path}: {'FAILED' if res is None else str(len(res)) + ' product links'}")
            for c in res or []:
                d = parse_card(c)
                key = f"{d['name']}|{d['img']}".lower()
                if key not in cards: cards[key] = {**d, "category": cat}
                elif cat and not cards[key]["category"]: cards[key]["category"] = cat
        if not cards:
            print("NO PRODUCTS FOUND. Page title:", page.title(), "| links on page:", page.locator("a").count())
            print(page.inner_text("body")[:400]); sys.exit(1)
        for key, d in cards.items():
            cat = d["category"] or guess_category(d["name"])
            prev = state.get(key, {}).get("status")
            info = None
            if d["neg"]: status = "unavailable"
            elif prev == "in_stock": status = "in_stock"        # already announced; card still shows no sold-out label
            else:
                info = inspect(page, d["href"])
                if info is None: continue                       # load error != stock change
                status = info["status"]
            label = decide(status, prev, key not in state, first_run)
            if label:
                info = info or inspect(page, d["href"]) or {}
                alerts.append(f"{info.get('title') or d['name']} | Model: {info.get('model', 'n/a')}\n"
                              f"Category: {cat}\nPrice: {info.get('price') or d['price']}\n"
                              f"Status: {label}\n{d['href']}\nDetected: {now}")
            new_state[key] = {"name": d["name"], "status": status, "category": cat}
        browser.close()
    if alerts:                                                  # state is saved only after alerts are delivered
        msg = "HMT watch alert\n\n" + "\n\n".join(alerts)
        for i in range(0, len(msg), 3800): telegram(msg[i:i + 3800])
    if json.dumps(new_state, sort_keys=True) != old:
        json.dump(new_state, open(STATE, "w"), indent=1, ensure_ascii=False)
    print(f"{now}: {len(cards)} products checked, {len(alerts)} alerts, first_run={first_run}")

if __name__ == "__main__":
    main()
