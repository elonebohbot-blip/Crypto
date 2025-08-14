import os, time, json, re, threading
import requests, feedparser
from datetime import datetime
import pytz
from dotenv import load_dotenv

# ================== CONFIG ==================
load_dotenv()
TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID_DEFAULT = os.getenv("CHAT_ID", "").strip()  # optionnel : si vide, on répond au chat de la commande
CP_TOKEN = os.getenv("CRYPTOPANIC_TOKEN", "").strip()  # optionnel
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))   # 300 = 5 min (met 900 pour 15 min en prod)

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN manquant. Définis-le dans les variables d'environnement.")

TZ = pytz.timezone("Europe/Paris")

ASSETS = ["BTC","ADA","ETH","SOL","LINK","AVAX"]
CG_IDS = {
    "BTC":"bitcoin",
    "ADA":"cardano",
    "ETH":"ethereum",
    "SOL":"solana",
    "LINK":"chainlink",
    "AVAX":"avalanche-2"
}

# Tes niveaux & règles (d’après ce qu’on a défini ensemble)
USER_ALERTS = {
    "BTC": {  # entrée ~118k$ / 103k€
        "warn_up":     [113000, 114000],
        "break_even":  [118000],
        "danger_down": [116000, 103000]
    },
    "ADA": {  # entrées 0.79$ & 0.92$
        "warn_up":     [0.95],
        "break_even":  [0.92],
        "danger_down": [0.83, 0.79]
    },
    "LINK": {
        "buy_zone":    [20.00, 20.50],   # zone entrée idéale (sur rebond confirmé)
        "danger_down": [19.50],
        "tp_zone":     [24.00, 24.50]
    },
    "AVAX": {
        "buy_zone":    [23.00, 23.00],   # tu as décidé d’acheter à 23$
        "danger_down": [22.00, 21.00],
        "tp_zone":     [26.00, 31.00]
    }
}

# Sources officielles + niches (RSS). X/Twitter non inclus (API requise).
FEEDS = {
    "BTC":[
        "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml&section=markets",
        "https://news.bitcoin.com/feed/",
        "https://mempool.space/blog/index.xml",
        "https://bitcoin.org/en/rss/announcements.rss"
    ],
    "ADA":[
        "https://www.essentialcardano.io/rss.xml",
        "https://iohk.io/en/blog.rss"
    ],
    "ETH":[
        "https://blog.ethereum.org/en/rss"
    ],
    "SOL":[
        "https://solana.com/news/rss.xml",
        "https://status.solana.com/history.rss"
    ],
    "LINK":[
        "https://blog.chain.link/feed/"
    ],
    "AVAX":[
        "https://medium.com/feed/avalancheavax"
    ],
    "_global":[
        "https://cointelegraph.com/rss",
        "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"
    ],
    "exchanges":[
        "https://blog.kraken.com/feed",
        "https://blog.coinbase.com/feed",
        "https://www.binance.com/en/blog?rss=en"
    ],
    "regulators":[
        "https://www.sec.gov/news/pressreleases.rss",
        "https://www.cftc.gov/PressRoom/PressReleases/rss.xml"
    ]
}

KW_BUY  = ["etf","listing","listed","partnership","partenariat","integration","upgrade","hard fork",
           "mainnet","testnet","adoption","institutional","scalability","roadmap","regulation","approval"]
KW_SELL = ["hack","exploit","security breach","breach","lawsuit","ban","delist","delisting","halted withdrawals"]

SEEN_ITEMS_FILE   = "seen_items.json"
SEEN_TARGETS_FILE = "seen_targets.json"
PRED_FILE         = "predictions.json"
LAST_UPDATE_ID_FILE = "last_update_id.json"  # pour les commandes Telegram (getUpdates)

# ================== HELPERS ==================
def now_paris():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def credibility(url):
    if any(s in url for s in ["blog.ethereum.org","iohk.io","essentialcardano.io","solana.com","blog.chain.link","avalancheavax","mempool.space","bitcoin.org"]):
        return "High (official)"
    if any(s in url for s in ["coindesk.com","cointelegraph.com","theblock.co","reuters.com","bloomberg.com"]):
        return "Medium-High (journalistic)"
    if any(s in url for s in ["kraken.com","coinbase.com","binance.com"]):
        return "Medium-High (exchange)"
    if any(s in url for s in ["sec.gov","cftc.gov"]):
        return "High (regulator)"
    return "Medium"

def send(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode":"HTML", "disable_web_page_preview": False},
            timeout=20
        )
    except Exception as e:
        print("Telegram error:", e)

def broadcast(text, fallback_chat_id=None):
    # envoie à CHAT_ID_DEFAULT si défini, sinon à fallback_chat_id (issu d'une commande)
    target = CHAT_ID_DEFAULT or fallback_chat_id
    if target:
        send(target, text)

def get_prices():
    ids = ",".join(CG_IDS[a] for a in ASSETS)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd,eur"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code==200:
            return r.json()
    except Exception as e:
        print("Price error:", e)
    return {}

def norm(s):
    return re.sub(r"\s+"," ", (s or "")).strip()

def detect_asset(title, summary):
    t = f"{title} {summary}".lower()
    if "bitcoin" in t or "btc" in t: return "BTC"
    if "cardano" in t or "ada" in t: return "ADA"
    if "ethereum" in t or "eth" in t: return "ETH"
    if "solana" in t or "sol " in t or t.endswith(" sol"): return "SOL"
    if "chainlink" in t or "link" in t: return "LINK"
    if "avalanche" in t or "avax" in t: return "AVAX"
    return None

def classify_action(title, summary):
    txt = f"{title} {summary}".lower()
    if any(k in txt for k in KW_SELL):
        return "Prendre des profits / Réduire", "Signal négatif (sécurité/régulation)."
    if any(k in txt for k in KW_BUY):
        return "Acheter +", "Catalyseur haussier (ETF/listing/upgrade/adoption)."
    return "Hold", "Pas de catalyseur clair."

# ================== NEWS SCAN ==================
def scan_feeds(seen, prices, fallback_chat_id=None):
    for group, urls in FEEDS.items():
        for url in urls:
            try:
                feed = feedparser.parse(url)
            except Exception:
                continue
            for e in feed.entries[:10]:
                uid = e.get("id") or e.get("link") or e.get("title")
                if not uid:
                    continue
                key = f"{group}:{uid}"
                if key in seen:
                    continue

                title   = norm(e.get("title"))
                link    = e.get("link","")
                summary = norm(e.get("summary") or e.get("description") or "")
                target  = group if group not in ["_global","exchanges","regulators"] else detect_asset(title, summary)

                # Si global/exchanges/regulators sans actif détecté => on pousse quand même (info générale)
                asset_label = target if target else group

                cg_id = CG_IDS.get(target) if target else None
                eur = usd = None
                if cg_id:
                    px = prices.get(cg_id, {})
                    eur = px.get("eur")
                    usd = px.get("usd")

                action, why = classify_action(title, summary)
                cred        = credibility(link)
                price_line  = f"Prix: {eur:.2f} € / ${usd:.2f}" if (eur and usd) else "Prix: n/a"

                msg = (
                    f"📰 <b>{asset_label}</b> — {now_paris()}\n"
                    f"<b>{title}</b>\n{link}\n"
                    f"{price_line}\n\n"
                    f"Action: <b>{action}</b>\nRaison: {why}\nCrédibilité: {cred}"
                )
                broadcast(msg, fallback_chat_id)
                seen[key] = True

def scan_cryptopanic(seen, prices, fallback_chat_id=None):
    if not CP_TOKEN:
        return
    mapping = {"BTC":"bitcoin","ADA":"cardano","ETH":"ethereum","SOL":"solana","LINK":"chainlink","AVAX":"avalanche"}
    for symbol, slug in mapping.items():
        url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CP_TOKEN}&currencies={slug}&public=true"
        try:
            r = requests.get(url, timeout=20)
            if r.status_code!=200:
                continue
            data = r.json().get("results", [])
        except:
            continue

        for item in data[:10]:
            uid = item.get("id")
            key = f"cp:{symbol}:{uid}"
            if key in seen:
                continue
            title = norm(item.get("title"))
            link  = item.get("url","")
            cg_id = CG_IDS.get(symbol)
            eur = usd = None
            if cg_id:
                px = prices.get(cg_id, {})
                eur = px.get("eur")
                usd = px.get("usd")
            action, why = classify_action(title, "")
            price_line  = f"Prix: {eur:.2f} € / ${usd:.2f}" if (eur and usd) else "Prix: n/a"
            msg = (
                f"📰 <b>{symbol}</b> — {now_paris()}\n"
                f"<b>{title}</b>\n{link}\n"
                f"{price_line}\n\n"
                f"Action: <b>{action}</b>\nRaison: {why}\nCrédibilité: Medium-High (aggregator)"
            )
            broadcast(msg, fallback_chat_id)
            seen[key] = True

# ================== PREDICTIONS & NIVEAUX ==================
def check_predictions(prices, seen_targets, fallback_chat_id=None):
    preds = load_json(PRED_FILE, {})
    for asset, items in preds.items():
        cg = CG_IDS.get(asset)
        if not cg or cg not in prices:
            continue
        cur_usd = prices[cg].get("usd")
        cur_eur = prices[cg].get("eur")
        for i, p in enumerate(items):
            key = f"{asset}:{i}:{p.get('target')}:{p.get('currency','USD')}"
            state = seen_targets.get(key, {"reached": False, "noted": False})
            target  = p.get("target")
            ccy     = p.get("currency","USD").upper()
            cur     = cur_eur if ccy=="EUR" else cur_usd
            if cur is None or target is None:
                continue

            # Atteinte
            if cur >= target and not state["reached"]:
                msg = (
                    f"🎯 <b>{asset}</b> — Objectif atteint ({ccy} {target}) — {now_paris()}\n"
                    f"Prix actuel: {cur:.2f} {ccy}\n"
                    f"Source: {p.get('source','N/A')}\n"
                    f"Conseil: Prendre profits partiels si momentum faiblit; sinon laisser courir avec stop suiveur.\n"
                    f"Note: {p.get('note','')}"
                )
                broadcast(msg, fallback_chat_id)
                state["reached"] = True

            # Proche (≤3%)
            if not state["noted"] and target>0 and abs(cur-target)/target <= 0.03 and not state["reached"]:
                msg = (
                    f"👀 <b>{asset}</b> — Proche de l'objectif ({ccy} {target}) — {now_paris()}\n"
                    f"Prix: {cur:.2f} {ccy} (≤3%)\n"
                    f"Action: Prépare une prise de profit ou un renfort selon cassure."
                )
                broadcast(msg, fallback_chat_id)
                state["noted"] = True

            seen_targets[key] = state
    save_json(SEEN_TARGETS_FILE, seen_targets)

def check_user_levels(prices, fallback_chat_id=None):
    for asset, cfg in USER_ALERTS.items():
        cg = CG_IDS.get(asset)
        if not cg or cg not in prices:
            continue
        usd = prices[cg].get("usd")
        eur = prices[cg].get("eur")
        if usd is None:
            continue

        def ping(txt):
            broadcast(
                f"⚙️ <b>{asset}</b> — {txt} — {now_paris()}\n"
                f"Prix actuel: {eur:.2f} € / ${usd:.2f}",
                fallback_chat_id
            )

        for lvl in cfg.get("warn_up", []):
            if usd >= lvl:
                ping(f"A atteint la zone de réduction de pertes ({lvl}$)")
        for lvl in cfg.get("break_even", []):
            if usd >= lvl:
                ping(f"Retour à l'équilibre (~{lvl}$)")
        for lvl in cfg.get("danger_down", []):
            if usd <= lvl:
                ping(f"⚠️ Alerte danger: sous {lvl}$")

        bz = cfg.get("buy_zone")
        if bz and len(bz)==2:
            low, high = min(bz), max(bz)
            if low <= usd <= high:
                ping(f"Zone d'achat ({low}$–{high}$) — attendre confirmation (bougie verte/volume)")

# ================== SCHEDULER (news + prix) ==================
def scheduler_loop():
    seen = load_json(SEEN_ITEMS_FILE, {})
    seen_targets = load_json(SEEN_TARGETS_FILE, {})
    broadcast(f"✅ Bot crypto DÉMARRÉ — {now_paris()} (vérif toutes {POLL_SECONDS//60} min)")

    while True:
        prices = get_prices()
        scan_feeds(seen, prices)
        scan_cryptopanic(seen, prices)
        check_predictions(prices, seen_targets)
        check_user_levels(prices)
        save_json(SEEN_ITEMS_FILE, seen)
        time.sleep(POLL_SECONDS)

# ================== COMMANDES TELEGRAM (getUpdates) ==================
def tg_get_updates(offset=None, timeout=25):
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    params = {"timeout": timeout}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=timeout+5)
        return r.json()
    except Exception as e:
        print("getUpdates error:", e)
        return {}

def prices_snapshot_text():
    prices = get_prices()
    lines = ["📊 <b>Prix EUR / USD</b> — " + now_paris()]
    for a in ASSETS:
        gid = CG_IDS[a]
        px  = prices.get(gid, {})
        eur = px.get("eur"); usd = px.get("usd")
        if eur and usd:
            lines.append(f"• {a}: {eur:.2f} € / ${usd:.2f}")
        else:
            lines.append(f"• {a}: n/a")
    return "\n".join(lines)

def latest_news_text(asset_filter=None, limit_per_feed=3):
    prices = get_prices()
    lines = [f"📰 <b>News instantanées</b> — {now_paris()}"]
    # on ne marque pas seen ici (juste un pull à la demande)
    for group, urls in FEEDS.items():
        for url in urls:
            try:
                feed = feedparser.parse(url)
            except:
                continue
            count = 0
            for e in feed.entries[:limit_per_feed]:
                title = norm(e.get("title"))
                link  = e.get("link","")
                summary = norm(e.get("summary") or e.get("description") or "")
                target = group if group not in ["_global","exchanges","regulators"] else detect_asset(title, summary)
                label  = target if target else group
                if asset_filter and label != asset_filter:
                    continue
                cred = credibility(link)
                lines.append(f"• [{label}] {title} — {cred}\n  {link}")
                count += 1
                if count >= limit_per_feed:
                    break
    return "\n".join(lines)

def handle_command(chat_id, text):
    t = text.strip().lower()

    if t.startswith("/start"):
        send(chat_id, "👋 Bot crypto prêt. Utilise /news (option: /news BTC) • /status • /levels")
        return
    if t.startswith("/status"):
        send(chat_id, prices_snapshot_text())
        return
    if t.startswith("/levels"):
        msg = ["🔔 <b>Niveaux surveillés</b>"]
        for a, cfg in USER_ALERTS.items():
            parts = []
            if cfg.get("warn_up"):     parts.append(f"warn_up: {cfg['warn_up']}")
            if cfg.get("break_even"):  parts.append(f"break_even: {cfg['break_even']}")
            if cfg.get("danger_down"): parts.append(f"danger: {cfg['danger_down']}")
            if cfg.get("buy_zone"):    parts.append(f"buy_zone: {cfg['buy_zone']}")
            if cfg.get("tp_zone"):     parts.append(f"tp_zone: {cfg['tp_zone']}")
            msg.append(f"• {a}: " + " | ".join(parts))
        send(chat_id, "\n".join(msg))
        return
    if t.startswith("/news"):
        # /news ou /news BTC (ADA, ETH, SOL, LINK, AVAX)
        parts = text.split()
        asset = None
        if len(parts) >= 2:
            asset = parts[1].upper()
            if asset not in ASSETS:
                send(chat_id, "Usage: /news [BTC|ADA|ETH|SOL|LINK|AVAX] (optionnel). Sans argument = toutes.")
                return
        send(chat_id, latest_news_text(asset_filter=asset))
        return

    # fallback
    send(chat_id, "Commandes: /news [ASSET] • /status • /levels")

def commands_loop():
    state = load_json(LAST_UPDATE_ID_FILE, {"offset": None})
    offset = state.get("offset")
    while True:
        data = tg_get_updates(offset=offset, timeout=25)
        if not data or not data.get("ok"):
            time.sleep(2)
            continue
        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            text = msg.get("text","")
            if chat_id and text:
                handle_command(str(chat_id), text)
        save_json(LAST_UPDATE_ID_FILE, {"offset": offset})

# ================== MAIN ==================
if __name__ == "__main__":
    # 2 threads : 1) scheduler périodique (news/prix/predictions/alertes),
    #             2) écoute des commandes Telegram (/news, /status, /levels)
    th1 = threading.Thread(target=scheduler_loop, daemon=True)
    th2 = threading.Thread(target=commands_loop, daemon=True)
    th1.start(); th2.start()
    # boucle principale pour garder le process vivant
    while True:
        time.sleep(3600)
