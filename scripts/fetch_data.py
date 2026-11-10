"""Refresh data.json for wearefractured.com.

Pulls each creator's latest YouTube uploads (public RSS), the season timeline
playlist (public playlist page, falling back to RSS) and the Discord member
count (public invite endpoint). Runs hourly from GitHub Actions. If a source
fails, the previous value in data.json is kept so the site never goes blank.
"""
import base64
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

CHANNELS = {
    "@acehardware67": "UCHTt0mp5tpZawosRCKyVkew",
    "@prep4cringe": "UCQDBI-yd7S_o0uhqgsnyI-Q",
    "@undercard594": "UCR9LCP-0SG9FwoAmRD4_AwA",
    "@vexan22": "UC2tuFSyCnEmABhFiPDONhnQ",
    "@fireblade-yt": "UCpfQlWwMvzBjeDWhyLLTb4A",
    "@h2ozero": "UCN95epHVNMAwhwUedVrv9Ag",
}
# Minecraft account name for each creator (checked 2026-11-09: skins match their channel art)
IGN = {
    "@acehardware67": "AceHardware67",
    "@prep4cringe": "Prep4Cringe",
    "@undercard594": "UNDERcard594",
    "@vexan22": "_Vexan_",
    "@fireblade-yt": "Fireblade_1",
    "@h2ozero": "h2ozer0",
}
NEWS_RSS = "https://www.minecraft.net/en-us/feeds/community-content/rss"
PLAYLIST = "PLmzftq-PMGGLPC9Xkheqq36Jjy7xlGWXQ"
INVITE = "RD2mYCt4y8"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def clean(title):
    # drop trailing hashtag spam from Shorts titles, strip HTML-significant characters, cap length
    t = re.sub(r"(\s+#\S+)+\s*$", "", title or "").strip() or (title or "").strip()
    t = re.sub(r"[<>\"'`&]", "", t)
    return t[:140]


def valid(v):
    return bool(v.get("id") and ID_RE.match(v["id"])) and (v.get("published") is None or DATE_RE.match(v["published"] or ""))


def rss(url):
    root = ET.fromstring(get(url))
    out = []
    for e in root.findall("a:entry", NS):
        out.append({
            "id": e.find("yt:videoId", NS).text,
            "title": clean(e.find("a:title", NS).text),
            "published": e.find("a:published", NS).text[:10],
        })
    return out


def playlist_page():
    html = get(f"https://www.youtube.com/playlist?list={PLAYLIST}").decode("utf-8", "replace")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    found = []

    def walk(o):
        if isinstance(o, dict):
            r = o.get("playlistVideoRenderer")
            if r and r.get("videoId"):
                runs = r.get("title", {}).get("runs") or [{}]
                found.append({"id": r["videoId"], "title": clean(runs[0].get("text", ""))})
            lv = o.get("lockupViewModel")
            if lv and lv.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO" and lv.get("contentId"):
                t = lv.get("metadata", {}).get("lockupMetadataViewModel", {}).get("title", {}).get("content", "")
                found.append({"id": lv["contentId"], "title": clean(t)})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    seen, out = set(), []
    for v in found:
        if v["id"] not in seen and v["title"] not in ("[Private video]", "[Deleted video]"):
            seen.add(v["id"])
            out.append(v)
    return out


def upload_date(vid):
    try:
        html = get(f"https://www.youtube.com/watch?v={vid}").decode("utf-8", "replace")
        m = re.search(r'"(?:uploadDate|publishDate)":"(\d{4}-\d{2}-\d{2})', html)
        return m.group(1) if m else None
    except Exception:
        return None


def live_now(cid):
    """Return {id, title} if the channel is live right now, else None."""
    html = get(f"https://www.youtube.com/channel/{cid}/live").decode("utf-8", "replace")
    can = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})"', html)
    if not can or '"isLive":true' not in html or '"isUpcoming":true' in html:
        return None
    t = re.search(r'<meta name="title" content="([^"]*)"', html)
    return {"id": can.group(1), "title": clean(unescape(t.group(1)) if t else "Live now")}


def skin(name):
    """Download the creator's current Minecraft skin; return 'slim' or 'default'."""
    uid = json.loads(get(f"https://api.mojang.com/users/profiles/minecraft/{name}"))["id"]
    prof = json.loads(get(f"https://sessionserver.mojang.com/session/minecraft/profile/{uid}"))
    tex = json.loads(base64.b64decode(prof["properties"][0]["value"]))["textures"].get("SKIN")
    if not tex or not tex.get("url", "").startswith(("http://textures.minecraft.net/", "https://textures.minecraft.net/")):
        return None
    png = get(tex["url"].replace("http://", "https://", 1))
    if png[:8] != b"\x89PNG\r\n\x1a\n" or len(png) > 200_000:
        return None
    path = os.path.join(ROOT, "img", f"skin-{name.lower()}.png")
    old = open(path, "rb").read() if os.path.exists(path) else None
    if old != png:
        with open(path, "wb") as f:
            f.write(png)
    return "slim" if tex.get("metadata", {}).get("model") == "slim" else "default"


IMG_RE = re.compile(r"^https://www\.minecraft\.net/content/[A-Za-z0-9/_.%-]+\.(jpe?g|png|webp)$", re.I)


def article_image(url):
    html = get(url).decode("utf-8", "replace")
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    img = m.group(1) if m else ""
    return img if IMG_RE.match(img) else None


def local_image(src):
    """Copy a minecraft.net article image into img/news/ so it loads from this site."""
    import hashlib
    ext = src.rsplit(".", 1)[-1].lower().replace("jpeg", "jpg")
    name = hashlib.sha1(src.encode()).hexdigest()[:16] + "." + ext
    path = os.path.join(ROOT, "img", "news", name)
    if not os.path.exists(path):
        body = get(src)
        if len(body) > 1_500_000 or not (body[:3] == b"\xff\xd8\xff" or body[:8] == b"\x89PNG\r\n\x1a\n" or body[8:12] == b"WEBP"):
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(body)
    return "img/news/" + name


def tag_for(title):
    t = title.lower()
    if any(k in t for k in ("snapshot", "preview", "changelog", "pre-release", "release candidate", "java edition", "update")):
        return "update"
    if "marketplace" in t:
        return "marketplace"
    if "realms" in t:
        return "realms"
    return "news"


def news(old):
    cache = {n["url"]: n.get("image") for n in old if isinstance(n, dict) and n.get("url") and str(n.get("image") or "").startswith("img/news/")}
    root = ET.fromstring(get(NEWS_RSS))
    out = []
    for it in root.findall("./channel/item"):
        link = it.find("{http://www.w3.org/2005/Atom}link")
        url = link.get("href", "") if link is not None else (it.findtext("link") or "")
        title = clean(it.findtext("title") or "")
        try:
            date = datetime.strptime((it.findtext("pubDate") or "")[:16], "%a, %d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            date = None
        if title and re.match(r"^https://www\.minecraft\.net/[A-Za-z0-9/_.-]+$", url):
            desc = re.sub(r"<[^>]+>", " ", unescape(it.findtext("description") or ""))
            desc = clean(re.sub(r"\s+", " ", desc).strip())[:220]
            img = cache.get(url)
            if img is None:
                try:
                    src = article_image(url)
                    img = local_image(src) if src else None
                except Exception as e:
                    print("news image failed", url, e)
            out.append({"title": title, "url": url, "published": date, "desc": desc, "image": img, "tag": tag_for(title)})
        if len(out) == 6:
            break
    keep = {os.path.basename(n["image"]) for n in out if n.get("image")}
    d = os.path.join(ROOT, "img", "news")
    if keep and os.path.isdir(d):
        for f in os.listdir(d):
            if f not in keep:
                os.remove(os.path.join(d, f))
    return out


def episode_thumbs(ids):
    """Keep a local copy of each episode thumbnail (some phones block i.ytimg.com)."""
    d = os.path.join(ROOT, "img", "ep")
    os.makedirs(d, exist_ok=True)
    for vid in ids:
        path = os.path.join(d, vid + ".jpg")
        if os.path.exists(path):
            continue
        try:
            body = get(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
            if body[:3] == b"\xff\xd8\xff" and len(body) < 600_000:
                with open(path, "wb") as f:
                    f.write(body)
        except Exception as e:
            print("thumb failed", vid, e)
    keep = {v + ".jpg" for v in ids}
    for f in os.listdir(d):
        if f not in keep:
            os.remove(os.path.join(d, f))


def crew_online():
    """Names of creators on the Minecraft server right now (mcstatus.io, 60s cache)."""
    d = json.loads(get("https://api.mcstatus.io/v2/status/java/fracturedsmp.datho.st"))
    if not d.get("online"):
        return set()
    names = {str(p.get("name_clean", "")).lower() for p in (d.get("players") or {}).get("list") or []}
    return {h for h, ign in IGN.items() if ign.lower() in names}


def main():
    try:
        with open(OUT) as f:
            data = json.load(f)
    except Exception:
        data = {"creators": {}, "episodes": [], "members": None, "online": None}

    for handle, cid in CHANNELS.items():
        try:
            vids = rss(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
            # keep one entry per distinct title so repeated uploads don't fill the row
            seen, uniq = set(), []
            for v in vids:
                if v["title"] not in seen:
                    seen.add(v["title"])
                    uniq.append(v)
            uniq = [v for v in uniq if valid(v)]
            if uniq:
                data["creators"][handle] = uniq[:3]
        except Exception as e:
            print("channel failed", handle, e)

    try:
        feed = rss(f"https://www.youtube.com/feeds/videos.xml?playlist_id={PLAYLIST}")
        dates = {v["id"]: v["published"] for v in feed}
        try:
            eps = playlist_page()
        except Exception as e:
            print("playlist page failed", e)
            eps = []
        if len(eps) < len(feed):
            eps = [{"id": v["id"], "title": v["title"]} for v in feed]
        old = {x["id"]: x.get("published") for x in data.get("episodes", [])}
        for v in eps:
            v["published"] = dates.get(v["id"]) or old.get(v["id"]) or upload_date(v["id"])
        eps = [v for v in eps if valid(v)]
        if eps:
            data["episodes"] = eps
    except Exception as e:
        print("playlist failed", e)

    try:
        inv = json.loads(get(f"https://discord.com/api/v10/invites/{INVITE}?with_counts=true"))
        m, o = inv.get("approximate_member_count"), inv.get("approximate_presence_count")
        data["members"] = m if isinstance(m, int) else data.get("members")
        data["online"] = o if isinstance(o, int) else data.get("online")
    except Exception as e:
        print("discord failed", e)

    live = {}
    for handle, cid in CHANNELS.items():
        try:
            v = live_now(cid)
            if v and ID_RE.match(v["id"]):
                live[handle] = v
        except Exception as e:
            print("live check failed", handle, e)
    data["live"] = live

    skins = data.get("skins", {})
    for handle, name in IGN.items():
        try:
            m = skin(name)
            if m:
                skins[handle] = {"name": name, "model": m}
        except Exception as e:
            print("skin failed", handle, e)
    data["skins"] = skins

    try:
        n = news(data.get("news", []))
        if n:
            data["news"] = n
    except Exception as e:
        print("news failed", e)

    try:
        ids = [e["id"] for e in data.get("episodes", []) if ID_RE.match(e.get("id", ""))]
        if ids:
            episode_thumbs(ids)
    except Exception as e:
        print("thumbs failed", e)

    seen = data.get("last_seen", {})
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        for h in crew_online():
            seen[h] = stamp
    except Exception as e:
        print("crew check failed", e)
    data["last_seen"] = {h: t for h, t in seen.items() if h in IGN}

    data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    with open(OUT, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("episodes", len(data["episodes"]), "members", data["members"], "creators", {k: len(v) for k, v in data["creators"].items()}, "live", list(live), "skins", len(skins), "news", len(data.get("news", [])))


if __name__ == "__main__":
    main()
