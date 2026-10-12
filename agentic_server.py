#!/usr/bin/env python3
"""
agentic_server.py  -  everything Agentic needs, in one process.

    python3 agentic_server.py                  # start everything
    python3 agentic_server.py /path/Crescent.py   # tell it where Crescent.py lives
    python3 agentic_server.py --install        # start automatically at login
    python3 agentic_server.py --uninstall

Put it in the same folder as index.html (that folder is what gets served).
Ctrl+C stops all of it. This replaces Agentic.sh, crescent_server.py,
kokoro_server.py, whisper_server.py and browser_proxy.py. Ports are unchanged,
so index.html needs no edits:

    :8080    web server        http://localhost:8080/index.html
    :47722   Crescent bridge   music, Linux-terminal link, backups, tools
    :47723   browser proxy     lets every website load in Agentic's browser
    :5050    Kokoro TTS        POST /speak            (voice output)
    :5051    Whisper STT       POST /transcribe       (voice input)
    :11434   offline LLM       built-in llama.cpp model server, Ollama-compatible
             (falls back to starting Ollama if llama-cpp-python isn't installed)

Every service is optional: if one can't start (missing model, missing package,
port already taken) it is skipped with a message and the rest keep running.
Python standard library only for the core; extras are only needed per feature:
    voice out   pip install kokoro-onnx numpy      + ~/VEO-x/kokoro-v1.0.onnx, voices-v1.0.bin
    voice in    pip install faster-whisper
    music       yt-dlp and mpv on PATH
    offline LLM pip install llama-cpp-python   (models: --models to list, --pull <repo> <file> to download)

Environment: AGENTIC_WEB_PORT, AGENTIC_WEB_HOST (default 0.0.0.0), AGENTIC_WEB_ROOT,
AGENTIC_PROXY_PORT, AGENTIC_PROXY_ALLOW_PRIVATE=1, CRESCENT_PORT, CRESCENT_PY,
AGENTIC_WHISPER_MODEL (base.en), AGENTIC_WHISPER_DEVICE (cpu), AGENTIC_WHISPER_COMPUTE (int8),
AGENTIC_LLM=builtin|ollama, AGENTIC_LLM_GPU=1, AGENTIC_LLM_MODEL, AGENTIC_LLM_CTX, AGENTIC_LLM_THREADS,
AGENTIC_NO=web,llm,ollama,kokoro,whisper,proxy,crescent  (comma list of services to skip)
                      AGENTIC_NO_OPEN=1  (don't open the browser automatically)
"""
import sys, os, re, json, gzip, zlib, socket, ipaddress, html, base64
import fcntl, hashlib, pty, select, shutil, signal, struct, subprocess, tempfile, termios
import threading, time, functools
import email.utils
import urllib.request, urllib.parse, urllib.error, http.cookiejar
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, SimpleHTTPRequestHandler

ThreadingHTTPServer.daemon_threads = True

# ════════════════════════════════════════════════════════════════════════════
#  Crescent bridge  (was crescent_server.py)  - port 47722
# ════════════════════════════════════════════════════════════════════════════
CRESCENT_HOST = os.environ.get("CRESCENT_HOST", "127.0.0.1")
CRESCENT_PORT = int(os.environ.get("CRESCENT_PORT", "47722"))
HERE = os.path.dirname(os.path.abspath(__file__))


def find_crescent():
    """Where's Crescent.py? In order: a path given on the command line
    (python3 crescent_server.py ~/somewhere/Crescent.py), $CRESCENT_PY, next
    to this file, the current folder, on PATH, then a search of your home
    folder (a few levels deep, skipping hidden/huge folders)."""
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    cands = [os.path.expanduser(args[0])] if args else []
    if os.environ.get("CRESCENT_PY"):
        cands.append(os.path.expanduser(os.environ["CRESCENT_PY"]))
    for name in ("Crescent.py", "crescent.py"):
        cands += [os.path.join(HERE, name), os.path.join(os.getcwd(), name)]
        w = shutil.which(name)
        if w:
            cands.append(w)
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    home = os.path.expanduser("~")
    skip = {"node_modules", "venv", ".venv", "__pycache__", "snap", "go", "Music", "Videos", "Pictures"}
    base = home.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(home):
        depth = root.count(os.sep) - base
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip] if depth < 5 else []
        for name in ("Crescent.py", "crescent.py"):
            if name in files:
                return os.path.join(root, name)
    return cands[0] if cands else os.path.join(HERE, "Crescent.py")


# The (possibly slow) search for Crescent.py and its import now run in a background
# thread started by main() AFTER the web server is up, so a slow home-folder scan or
# a Crescent.py that hangs on import can never keep the site from loading.
CRESCENT_PY = os.path.join(HERE, "Crescent.py")
DL_DIR = os.path.expanduser("~/Music/Crescent")
COOKIES = os.path.expanduser("~/.crescent_cookies.txt")
KDECONNECT = "kdeconnect-cli"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ── Reuse Crescent.py's own helpers where we can ────────────────────────────
# Importing runs its module-level setup (but not main()). If that fails — e.g.
# mpv isn't installed, which makes Crescent.py exit — we fall back to simple
# built-in versions so the browser player still works.
cr = None


def _load_crescent():
    global cr, CRESCENT_PY
    try:
        CRESCENT_PY = find_crescent()
    except BaseException as e:
        log(f"note: Crescent.py search failed ({e!s:.120})")
        return
    try:
        sys.path.insert(0, os.path.dirname(CRESCENT_PY))
        import importlib.util
        _spec = importlib.util.spec_from_file_location("crescent_mod", CRESCENT_PY)
        mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(mod)
        cr = mod
    except BaseException as e:  # SystemExit included
        log(f"note: couldn't import Crescent.py helpers ({e!s:.120}); using built-in fallbacks")
        cr = None
    if os.path.isfile(CRESCENT_PY):
        log("Crescent.py: %s" % CRESCENT_PY)
    else:
        log("Crescent.py not found (music terminal link off) - tell me where: python3 agentic_server.py /path/to/Crescent.py")


def ytdlp(args, timeout=45):
    """Run yt-dlp; returns (stdout, error_string)."""
    exe = shutil.which("yt-dlp")
    cmd = [exe] if exe else [sys.executable, "-m", "yt_dlp"]
    if os.path.exists(COOKIES):
        cmd += ["--cookies", COOKIES]
    try:
        r = subprocess.run(cmd + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", "yt-dlp timed out"
    except FileNotFoundError:
        return "", "yt-dlp not installed (pip install yt-dlp)"
    if r.returncode != 0:
        lines = [l for l in r.stderr.splitlines() if l.strip()]
        if "No module named yt_dlp" in r.stderr:
            return "", "yt-dlp not installed (pip install yt-dlp)"
        return r.stdout, (lines[-1][:240] if lines else f"yt-dlp exited {r.returncode}")
    return r.stdout, ""


# ── HTTP endpoints ───────────────────────────────────────────────────────────
def ep_audio_url(q):
    url = q.get("url", "")
    if not url:
        return {"error": "missing url"}
    out, err = ytdlp(["--no-playlist", "-f", "bestaudio[ext=m4a]/bestaudio/best",
                      "-j", "--no-warnings", url])
    line = next((l for l in out.splitlines() if l.startswith("{")), "")
    if not line:
        return {"error": err or "no stream found"}
    info = json.loads(line)
    stream = info.get("url")
    if not stream and info.get("requested_formats"):
        stream = info["requested_formats"][0].get("url")
    if not stream:
        return {"error": "no audio stream in yt-dlp output"}
    return {
        "url": stream,
        "title": info.get("title") or url,
        "webpage_url": info.get("webpage_url") or url,
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
    }


def ep_recommend(q):
    url = q.get("url", "")
    if not url:
        return {"results": [], "error": "missing url"}
    if cr and hasattr(cr, "get_related_videos"):
        urls, err = cr.get_related_videos(url, limit=10)
    else:
        out, err = ytdlp(["--no-playlist", "--skip-download", "-j", url])
        title = ""
        try:
            title = json.loads(out.splitlines()[0]).get("title", "")
        except Exception:
            pass
        urls = []
        if title:
            out2, err = ytdlp(["--flat-playlist", "-j", f"ytsearch15:{title}"])
            for l in out2.splitlines():
                try:
                    it = json.loads(l)
                except Exception:
                    continue
                u = it.get("url") or (it.get("id") and f"https://www.youtube.com/watch?v={it['id']}")
                if u and it.get("id") not in url:
                    urls.append(u)
            urls = urls[:10]
    return {"results": [{"url": u} for u in urls], "error": err or ""}


def ep_download(q):
    url = q.get("url", "")
    if not url:
        return {"ok": False, "error": "missing url"}
    os.makedirs(DL_DIR, exist_ok=True)
    out, err = ytdlp(["--no-playlist", "-x", "--audio-format", "mp3",
                      "--embed-thumbnail", "--add-metadata",
                      "-o", os.path.join(DL_DIR, "%(title)s.%(ext)s"),
                      "--print", "after_move:filepath", url], timeout=600)
    path = (out.strip().splitlines() or [""])[-1]
    if err and not path:
        return {"ok": False, "error": err}
    return {"ok": True, "path": path or DL_DIR}


def _inbox_dirs():
    if cr and hasattr(cr, "handoff_inbox_dirs"):
        return cr.handoff_inbox_dirs()
    ds = [os.path.expanduser("~/storage/downloads"), "/sdcard/Download",
          os.path.expanduser("~/Downloads"), os.path.expanduser("~/kdeconnect")]
    return [d for d in dict.fromkeys(ds) if os.path.isdir(d)]


def ep_handoff_send(q):
    if not shutil.which(KDECONNECT):
        return {"ok": False, "error": "kdeconnect-cli not found — install KDE Connect for handoff"}
    try:
        queue = json.loads(q.get("queue") or "[]")
    except Exception:
        queue = []
    snap = {"url": q.get("url", ""), "name": q.get("title") or q.get("url", ""),
            "position": float(q.get("position") or 0), "queue": queue, "ts": time.time()}
    if not snap["url"]:
        return {"ok": False, "error": "nothing playing to hand off"}
    # Named crescent_handoff*.json so the receiving side (Crescent.py or this
    # server) recognises it in its Downloads/kdeconnect folder.
    path = os.path.join(tempfile.gettempdir(), "crescent_handoff.json")
    with open(path, "w") as f:
        json.dump(snap, f)
    try:
        ids = subprocess.run([KDECONNECT, "-a", "--id-only"], capture_output=True,
                             text=True, timeout=5).stdout.split()
    except Exception:
        ids = []
    if not ids:
        return {"ok": False, "error": "no reachable KDE Connect device found"}
    try:
        subprocess.run([KDECONNECT, "-d", ids[0], "--share", path], timeout=10)
    except Exception as e:
        return {"ok": False, "error": f"handoff share failed: {e}"}
    return {"ok": True, "message": f"handed off '{snap['name']}' @ {int(snap['position'])}s to {ids[0]}"}


def ep_handoff_receive(q):
    found = []
    for d in _inbox_dirs():
        try:
            for fn in os.listdir(d):
                if fn.lstrip(".").startswith("crescent_handoff") and fn.endswith(".json"):
                    p = os.path.join(d, fn)
                    found.append((os.path.getmtime(p), p))
        except Exception:
            pass
    if not found:
        return {"ok": False, "error": "no incoming handoff found"}
    path = max(found)[1]
    try:
        with open(path) as f:
            snap = json.load(f)
        os.remove(path)
    except Exception as e:
        return {"ok": False, "error": f"couldn't read handoff file: {e}"}
    return {"ok": True, "snapshot": snap}


# ── KDE Connect for the Agentic browser ─────────────────────────────────────
# Send pages, tab lists and downloads to your phone, ring it, or ping it.
# Uses the same kdeconnect-cli as handoff; falls back to the KDE Connect Flatpak.
KDE_FLATPAK = "org.kde.kdeconnect"


def _kde_cli():
    if shutil.which(KDECONNECT):
        return [KDECONNECT]
    if shutil.which("flatpak"):
        try:
            if subprocess.run(["flatpak", "info", KDE_FLATPAK], capture_output=True,
                              timeout=10).returncode == 0:
                return ["flatpak", "run", "--command=kdeconnect-cli", KDE_FLATPAK]
        except Exception:
            pass
    return None


def _kde_run(args, timeout=15):
    cli = _kde_cli()
    if not cli:
        return False, "kdeconnect-cli not found. Install KDE Connect and pair your phone."
    try:
        p = subprocess.run(cli + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "KDE Connect took too long to answer"
    except Exception as e:
        return False, f"couldn't run kdeconnect-cli: {e}"
    if p.returncode != 0:
        lines = (p.stderr or p.stdout or "").strip().splitlines()
        return False, lines[-1] if lines else f"kdeconnect-cli failed (exit {p.returncode})"
    return True, p.stdout


def _kde_parse(out):
    devs = []
    for line in (out or "").splitlines():
        line = line.strip()
        if " " in line:
            dev_id, name = line.split(" ", 1)
            devs.append({"id": dev_id, "name": name.strip()})
    return devs


def ep_kde_devices(q):
    ok, paired = _kde_run(["--list-devices", "--id-name-only"])
    if not ok:
        return {"ok": False, "error": paired}
    ok2, avail = _kde_run(["--list-available", "--id-name-only"])
    reach = {d["id"] for d in _kde_parse(avail if ok2 else "")}
    return {"ok": True, "devices": [dict(d, reachable=d["id"] in reach) for d in _kde_parse(paired)]}


def _kde_done(res, msg):
    ok, out = res
    return {"ok": True, "message": msg} if ok else {"ok": False, "error": out}


def ep_kde_share(q):
    dev, url, fpath = q.get("id", ""), q.get("url", ""), q.get("path", "")
    if not dev:
        return {"ok": False, "error": "no device picked"}
    if url:
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "only web links can be sent"}
        return _kde_done(_kde_run(["-d", dev, "--share", url]), "sent")
    if fpath:
        fpath = os.path.expanduser(fpath)
        if not os.path.isfile(fpath):
            return {"ok": False, "error": "that file isn't there anymore"}
        return _kde_done(_kde_run(["-d", dev, "--share", fpath], timeout=60), "sending")
    return {"ok": False, "error": "nothing to send"}


def ep_kde_share_text(q):
    dev, text = q.get("id", ""), (q.get("text") or "")[:20000]
    if not dev or not text:
        return {"ok": False, "error": "nothing to send"}
    return _kde_done(_kde_run(["-d", dev, "--share-text", text]), "sent")


def ep_kde_upload(q, body):
    """A file picked or dropped in Agentic: save it to a temp folder, then send it."""
    dev = q.get("id", "")
    if not dev:
        return {"ok": False, "error": "no device picked"}
    name = os.path.basename(q.get("name") or "file").strip() or "file"
    name = re.sub(r"[^\w.\- ()]+", "_", name)[:120] or "file"
    folder = os.path.join(tempfile.gettempdir(), "agentic_kdeconnect")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(body)
    return _kde_done(_kde_run(["-d", dev, "--share", path], timeout=120), "sending " + name)


def ep_kde_ring(q):
    if not q.get("id"):
        return {"ok": False, "error": "no device picked"}
    dev = q["id"]
    # kdeconnect-cli --ring reports success even when "Ring your phone" is switched
    # off, so check the plugin first and give a real answer.
    if shutil.which("gdbus"):
        p = subprocess.run(["gdbus", "call", "--session", "--dest", "org.kde.kdeconnect",
                            "--object-path", "/modules/kdeconnect/devices/" + dev,
                            "--method", "org.kde.kdeconnect.device.hasPlugin", "kdeconnect_findmyphone"],
                           capture_output=True, text=True, timeout=5)
        if p.returncode == 0 and "false" in p.stdout:
            return {"ok": False, "error": "Ringing is switched off. On the computer open KDE Connect → your phone → "
                    "turn on 'Ring your phone'; on the phone open KDE Connect → Plugin settings → turn on 'Find my phone'."}
        # ring straight through D-Bus (same thing the CLI does, but errors aren't swallowed)
        p = subprocess.run(["gdbus", "call", "--session", "--dest", "org.kde.kdeconnect",
                            "--object-path", "/modules/kdeconnect/devices/%s/findmyphone" % dev,
                            "--method", "org.kde.kdeconnect.device.findmyphone.ring"],
                           capture_output=True, text=True, timeout=5)
        if p.returncode == 0:
            return {"ok": True, "message": "ringing"}
    return _kde_done(_kde_run(["-d", dev, "--ring"]), "ringing")


# "Agentic alerts off" switch you flip from the phone. Kept in a file so it
# survives restarts and works even when no browser tab is open.
ALERTS_MUTE_FILE = os.path.join(os.path.expanduser("~"), ".agentic_phone_alerts_off")


def phone_alerts_muted():
    return os.path.exists(ALERTS_MUTE_FILE)


def set_phone_alerts_muted(off):
    try:
        if off:
            open(ALERTS_MUTE_FILE, "w").write(str(time.time()))
        elif os.path.exists(ALERTS_MUTE_FILE):
            os.remove(ALERTS_MUTE_FILE)
    except Exception:
        pass


def ep_kde_ping(q):
    if not q.get("id"):
        return {"ok": False, "error": "no device picked"}
    if q.get("alert") and phone_alerts_muted():
        return {"ok": True, "message": "skipped (Agentic alerts are off on your phone)", "muted": True}
    msg = (q.get("msg") or "Hello from Agentic")[:200]
    return _kde_done(_kde_run(["-d", q["id"], "--ping-msg", msg]), "pinged")


# KDE Connect extras: battery, phone notifications, SMS, phone camera, refresh/pair.
# Battery and notifications come from KDE Connect's D-Bus service through gdbus.
def _kde_dbus(obj, iface, prop=None, method=None):
    if not shutil.which("gdbus"):
        return None
    base = ["gdbus", "call", "--session", "--dest", "org.kde.kdeconnect", "--object-path", obj]
    args = base + (["--method", "org.freedesktop.DBus.Properties.Get", iface, prop] if prop else ["--method", iface + "." + method])
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None


def _kde_battery(dev):
    obj = "/modules/kdeconnect/devices/%s/battery" % dev
    iface = "org.kde.kdeconnect.device.battery"
    charge = _kde_dbus(obj, iface, prop="charge") or _kde_dbus(obj, iface, method="charge")
    charging = _kde_dbus(obj, iface, prop="isCharging") or _kde_dbus(obj, iface, method="isCharging")
    m = re.search(r"-?\d+", charge or "")
    if not m or int(m.group()) < 0:
        return None
    return {"charge": int(m.group()), "charging": "true" in (charging or "")}


def _kde_signal(dev):
    obj = "/modules/kdeconnect/devices/%s/connectivity_report" % dev
    iface = "org.kde.kdeconnect.device.connectivity_report"
    strength = _kde_dbus(obj, iface, prop="cellularNetworkStrength")
    kind = _kde_dbus(obj, iface, prop="cellularNetworkType")
    m = re.search(r"-?\d+", strength or "")
    if not m or int(m.group()) < 0:
        return None
    t = re.search(r"'([^']*)'", kind or "")
    return {"bars": max(0, min(4, int(m.group()))), "type": t.group(1) if t else ""}


def ep_kde_status(q):
    """devices plus battery for the reachable ones"""
    res = ep_kde_devices(q)
    if res.get("ok"):
        for d in res["devices"]:
            if d.get("reachable"):
                b = _kde_battery(d["id"])
                if b:
                    d["battery"] = b
                sig = _kde_signal(d["id"])
                if sig:
                    d["signal"] = sig
    return res


_NOTIF_CACHE = {}


def _kde_notifs_base(q):
    dev = q.get("id", "")
    if not dev:
        return {"ok": False, "error": "no device picked"}
    obj = "/modules/kdeconnect/devices/%s/notifications" % dev
    raw = _kde_dbus(obj, "org.kde.kdeconnect.device.notifications", method="activeNotifications")
    items = []
    if raw is not None:
        nif = "org.kde.kdeconnect.device.notifications.notification"
        ids = re.findall(r"'([^']+)'", raw)[:40]
        for nid in ids:
            ck = (dev, nid)
            if ck in _NOTIF_CACHE:
                items.append(_NOTIF_CACHE[ck])
                continue
            no = obj + "/" + nid
            def prop(name):
                v = _kde_dbus(no, nif, prop=name) or ""
                m = re.search(r"'((?:[^'\\]|\\.)*)'", v)
                return m.group(1).replace("\\'", "'").replace("\\\\", "\\") if m else ""
            title, text, app = prop("title"), prop("text"), prop("appName")
            if title or text:
                dis = _kde_dbus(no, nif, prop="dismissable") or ""
                it = {"id": nid, "app": app, "title": title, "text": text,
                      "repliable": bool(prop("replyId")), "dismissable": "true" in dis}
                _NOTIF_CACHE[ck] = it
                items.append(it)
        for ck in [k for k in _NOTIF_CACHE if k[0] == dev and k[1] not in ids]:
            _NOTIF_CACHE.pop(ck, None)
        return {"ok": True, "notifications": items}
    ok, out = _kde_run(["-d", dev, "--list-notifications"])
    if not ok:
        return {"ok": False, "error": out}
    for i, line in enumerate(l.strip(" -") for l in out.splitlines() if l.strip()):
        parts = [x.strip() for x in line.split(":", 2)]
        items.append({"id": "l%d:%s" % (i, line[:60]), "app": parts[0] if len(parts) > 1 else "",
                      "title": parts[1] if len(parts) > 2 else (parts[1] if len(parts) > 1 else line),
                      "text": parts[2] if len(parts) > 2 else ""})
    return {"ok": True, "notifications": items}


PHONE_NOTIFS = []


def ep_kde_notifications(q):
    """Phone notifications: KDE Connect's own list, plus any it popped up on this desktop (caught live)."""
    _watch_phone_pings()
    res = _kde_notifs_base(q)
    base = res.get("notifications", []) if res.get("ok") else []
    have = {(n.get("title", ""), n.get("text", "")) for n in base}
    now = time.time()
    extra = []
    with KDE_ACT_LOCK:
        PHONE_NOTIFS[:] = [n for n in PHONE_NOTIFS if now - n["t"] < 900][-40:]
        for n in PHONE_NOTIFS:
            if (n["title"], n["text"]) not in have:
                extra.append({"id": n["id"], "app": n["app"], "title": n["title"], "text": n["text"],
                              "repliable": False, "dismissable": False})
    if not res.get("ok") and not extra:
        return res
    return {"ok": True, "notifications": base + extra[::-1]}


def ep_kde_sms(q):
    dev, to, text = q.get("id", ""), (q.get("to") or "").strip(), (q.get("text") or "").strip()
    if not dev or not to or not text:
        return {"ok": False, "error": "need a phone number and a message"}
    if not re.fullmatch(r"[+0-9 ()\-.]{3,24}", to):
        return {"ok": False, "error": "that doesn't look like a phone number"}
    return _kde_done(_kde_run(["-d", dev, "--send-sms", text[:1500], "--destination", to]), "text sent")


def ep_kde_photo(q):
    """Asks the phone to take a photo; waits up to 90 s for it to arrive, then returns it."""
    dev = q.get("id", "")
    if not dev:
        return {"ok": False, "error": "no device picked"}
    folder = os.path.join(tempfile.gettempdir(), "agentic_kdeconnect")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "phone_photo_%d.jpg" % int(time.time()))
    ok, out = _kde_run(["-d", dev, "--photo", path], timeout=20)
    if not ok:
        return {"ok": False, "error": out}
    for _ in range(180):
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            time.sleep(0.8)
            with open(path, "rb") as f:
                data = f.read()
            if len(data) > 15 * 1024 * 1024:
                return {"ok": True, "path": path, "message": "photo saved (too big to preview)"}
            return {"ok": True, "path": path, "image": "data:image/jpeg;base64," + base64.b64encode(data).decode()}
        time.sleep(0.5)
    return {"ok": False, "error": "no photo came back (take it on the phone within 90 seconds)"}


def _gv_str(t):
    """a GVariant string literal for gdbus arguments"""
    return "'" + t.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _kde_notif_call(q, method, *args):
    dev, nid = q.get("id", ""), q.get("nid", "")
    if not dev or not nid or "/" in nid or not shutil.which("gdbus"):
        return None
    obj = "/modules/kdeconnect/devices/%s/notifications/%s" % (dev, nid)
    cmd = ["gdbus", "call", "--session", "--dest", "org.kde.kdeconnect", "--object-path", obj,
           "--method", "org.kde.kdeconnect.device.notifications.notification." + method] + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return p.returncode == 0, (p.stderr or "").strip()
    except Exception as e:
        return False, str(e)


def ep_kde_reply(q):
    text = (q.get("text") or "").strip()[:1500]
    if not text:
        return {"ok": False, "error": "write a reply first"}
    res = _kde_notif_call(q, "sendReply", _gv_str(text))
    if res is None:
        return {"ok": False, "error": "can't reply to that notification"}
    return {"ok": True, "message": "reply sent"} if res[0] else {"ok": False, "error": res[1] or "reply failed"}


def ep_kde_dismiss(q):
    res = _kde_notif_call(q, "dismiss")
    if res is None:
        return {"ok": False, "error": "can't dismiss that notification"}
    return {"ok": True, "message": "dismissed"} if res[0] else {"ok": False, "error": res[1] or "dismiss failed"}


def ep_kde_contacts(q):
    """Phone contacts synced by KDE Connect's contacts plugin (vCards)."""
    dev = q.get("id", "")
    if not dev or "/" in dev:
        return {"ok": False, "error": "no device picked"}
    _kde_dbus("/modules/kdeconnect/devices/%s/contacts" % dev, "org.kde.kdeconnect.device.contacts", method="synchronizeRemoteWithLocal")
    folder = os.path.join(os.path.expanduser("~/.local/share/kpeoplevcard"), "kdeconnect-" + dev)
    out = []
    if os.path.isdir(folder):
        for fn in sorted(os.listdir(folder))[:3000]:
            if not fn.endswith(".vcf"):
                continue
            try:
                with open(os.path.join(folder, fn), encoding="utf-8", errors="ignore") as f:
                    card = f.read()
            except Exception:
                continue
            name = re.search(r"^FN[^:]*:(.+)$", card, re.M)
            nums = [n.strip() for n in re.findall(r"^TEL[^:]*:(.+)$", card, re.M) if n.strip()]
            if name and nums:
                out.append({"name": name.group(1).strip(), "numbers": nums[:4]})
    out.sort(key=lambda c: c["name"].lower())
    return {"ok": True, "contacts": out,
            "note": "" if out else "No contacts synced yet. Turn on Contacts sync in the KDE Connect app on your phone."}


# Phone buttons: KDE Connect "Run command" entries call /kdeconnect/run?act=...
# and the Agentic app picks the action up from /kdeconnect/actions.
KDE_ACTIONS = []
KDE_ACT_LOCK = threading.Lock()
KDE_ACT_NAMES = {
    # --- Crescent (the music player in Agentic) ---
    "cplay": "Music: Play",
    "cpause": "Music: Pause",
    "cskip": "Music: Skip",
    "cback": "Music: Back",
    "crestart": "Music: Restart song",
    "cloop": "Music: Loop",
    "shuffle": "Music: Shuffle all",
    "shufflefav": "Music: Shuffle favorites",
    "favorite": "Music: Favorite this song",
    "crecommend": "Music: Recommend a song",
    "cstats": "Music: Stats",
    "sleeptimer": "Music: Sleep timer",
    # --- Media (whatever is playing on the computer: YouTube, Spotify...) ---
    "playpause": "Media: Play/pause",
    "next": "Media: Next",
    "prev": "Media: Previous",
    "mediastop": "Media: Stop",
    "nowplaying": "Media: What's playing",
    "volup": "Media: Volume up",
    "voldown": "Media: Volume down",
    "vol50": "Media: Volume 50%",
    "mute": "Media: Mute",
    "micmute": "Media: Mic mute",
    # --- PC (the computer itself) ---
    "pcstatus": "PC: Status",
    "pctemp": "PC: Temperature",
    "pctop": "PC: What's using CPU",
    "screenshot": "PC: Screenshot to phone",
    "clipboard": "PC: Clipboard to phone",
    "lastdownload": "PC: Last download to phone",
    "ring": "PC: Ring (find it)",
    "stopring": "PC: Stop ringing",
    "lock": "PC: Lock",
    "screenoff": "PC: Screen off",
    "stayawake": "PC: Stay awake 1 hour",
    "brightup": "PC: Brighter",
    "brightdown": "PC: Dimmer",
    "wifi": "PC: Wi-Fi on/off",
    "bluetooth": "PC: Bluetooth on/off",
    "suspend": "PC: Sleep",
    "pcip": "PC: IP address",
    "pcpublicip": "PC: Public IP",
    "pctime": "PC: Date and time",
    "pcuptime": "PC: Uptime",
    "pcdisk": "PC: Disk space",
    # --- Agentic ---
    "reply": "Agentic: Last reply",
    "ask": "Agentic: Ask (copied text)",
    "newchat": "Agentic: New chat",
    "brief": "Agentic: Daily brief",
    "weather": "Agentic: Weather",
    "recommend": "Agentic: Recommend",
    "lyra": "Agentic: Lyra news",
    "openlyra": "Agentic: Open Lyra",
    "alertsoff": "Agentic: Alerts OFF",
    "alertson": "Agentic: Alerts ON",
    # --- Look up (uses the text you copied on the phone) ---
    "lookup": "Look: Look up",
    "define": "Look: Define",
    "explain": "Look: Explain",
    "translate": "Look: Translate",
    "factcheck": "Look: Fact-check",
    # --- Web (Agentic's browser) ---
    "navgo": "Web: Open copied link",
    "page": "Web: Send page to phone",
    "summary": "Web: Summarize page",
    "tabs": "Web: Send tabs to phone",
    "navback": "Web: Back",
    "navreload": "Web: Reload",
    "bookmark": "Web: Bookmark page",
    # --- Settings ---
    "custdark": "Set: Dark theme",
    "custlight": "Set: Light theme",
    "custnotifsound": "Set: Notification sound on/off",
    "custringsound": "Set: Ring sound on/off",
    "custlyranotifs": "Set: Phone notifs in Agentic",
}
# left off the phone by default so the Run command list stays short; send all=1 to add every button
PHONE_BTN_HIDDEN = {
    "cstats", "crestart", "cloop",
    "mediastop", "micmute", "vol50",
    "pcip", "pcpublicip", "pctime", "pcuptime", "pcdisk", "brightup", "brightdown",
    "openlyra",
    "bookmark", "navreload",
    "custnotifsound", "custringsound", "custlyranotifs",
}
# Buttons that need a helper program on this computer. If none of the programs is
# installed, the button is left off the phone instead of silently doing nothing.
ACT_NEEDS = {
    "playpause": ["playerctl"], "next": ["playerctl"], "prev": ["playerctl"], "mediastop": ["playerctl"], "nowplaying": ["playerctl"],
    "volup": ["pactl", "wpctl"], "voldown": ["pactl", "wpctl"], "vol50": ["pactl", "wpctl"], "mute": ["pactl", "wpctl"], "micmute": ["pactl", "wpctl"],
    "brightup": ["brightnessctl", "light"], "brightdown": ["brightnessctl", "light"],
    "screenshot": ["spectacle", "grim", "gnome-screenshot", "scrot", "import"],
    "lock": ["loginctl", "xdg-screensaver"], "screenoff": ["xset", "loginctl"],
    "wifi": ["nmcli", "rfkill"], "bluetooth": ["rfkill"], "suspend": ["systemctl"], "stayawake": ["systemd-inhibit"],
    "clipboard": ["gdbus", "wl-paste", "xclip", "xsel"],
}


def unavailable_acts():
    return {a for a, progs in ACT_NEEDS.items() if not any(shutil.which(p) for p in progs)}


def ep_kde_check(q):
    """Which phone buttons can't work on this computer, and what to install."""
    out = []
    for a in sorted(unavailable_acts()):
        out.append({"act": a, "name": KDE_ACT_NAMES.get(a, a), "install": ACT_NEEDS[a][0]})
    return {"ok": True, "missing": out}

# these run right here on the server; everything else is handed to the Agentic app
KDE_SERVER_ACTS = {"stopring", "weather", "pcstatus", "screenshot", "clipboard", "playpause", "next",
                   "volup", "voldown", "mute", "lock", "prev", "mediastop", "micmute", "brightup", "brightdown",
                   "suspend", "pcip", "pctime", "wifi", "bluetooth", "screenoff", "pcuptime", "pcdisk",
                   "pcpublicip", "nowplaying", "alertsoff", "alertson",
                   "vol50", "pctemp", "pctop", "lastdownload", "unlock", "screenon", "stayawake"}


def _first_phone():
    ok, out = _kde_run(["--list-available", "--id-only"])
    ids = out.split() if ok else []
    return ids[0] if ids else ""


def _phone_say(msg, dev=None):
    dev = dev or _first_phone()
    if dev:
        _kde_run(["-d", dev, "--ping-msg", msg[:200]])


def _phone_text(text, dev=None):
    dev = dev or _first_phone()
    if dev:
        _kde_run(["-d", dev, "--share-text", text[:20000]])


def _sh(cmds, timeout=8):
    """run the first command whose program exists; True if it worked"""
    for c in cmds:
        if shutil.which(c[0]):
            try:
                if subprocess.run(c, capture_output=True, timeout=timeout).returncode == 0:
                    return True
            except Exception:
                pass
    return False


def _pc_status():
    parts = []
    try:
        for b in sorted(os.listdir("/sys/class/power_supply")):
            cap = os.path.join("/sys/class/power_supply", b, "capacity")
            if os.path.isfile(cap):
                st = open(os.path.join("/sys/class/power_supply", b, "status")).read().strip()
                parts.append("battery %s%% (%s)" % (open(cap).read().strip(), st.lower()))
                break
    except Exception:
        pass
    try:
        l1 = os.getloadavg()[0]
        parts.append("CPU load %.1f on %d cores" % (l1, os.cpu_count() or 1))
    except Exception:
        pass
    try:
        mi = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo") if l.split()[0].rstrip(":") in ("MemTotal", "MemAvailable")}
        used = (mi["MemTotal"] - mi["MemAvailable"]) / 1048576
        parts.append("memory %.1f of %.1f GB" % (used, mi["MemTotal"] / 1048576))
    except Exception:
        pass
    try:
        du = shutil.disk_usage(os.path.expanduser("~"))
        parts.append("disk %d GB free" % (du.free // 10**9))
    except Exception:
        pass
    try:
        up = float(open("/proc/uptime").read().split()[0])
        parts.append("up %dh %dm" % (up // 3600, up % 3600 // 60))
    except Exception:
        pass
    return "PC: " + (", ".join(parts) or "running")


def _do_server_act(act):
    if act in ("alertsoff", "alertson"):
        set_phone_alerts_muted(act == "alertsoff")
        # confirmation always goes through, even when muting
        _phone_say("Agentic alerts are now " + ("OFF. Tap 'Agentic alerts ON' to turn them back on." if act == "alertsoff" else "ON."))
        return
    if act == "stopring":
        RING_STOP.set()
        return
    if act == "vol50":
        if not _sh([["pactl", "set-sink-volume", "@DEFAULT_SINK@", "50%"], ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.5"]]):
            _phone_say("Volume control needs pactl or wpctl")
        return
    if act == "pctemp":
        temps = []
        try:
            for z in sorted(os.listdir("/sys/class/thermal")):
                f = os.path.join("/sys/class/thermal", z, "temp")
                if z.startswith("thermal_zone") and os.path.isfile(f):
                    t = int(open(f).read().strip()) / 1000
                    if 0 < t < 130:
                        temps.append(t)
        except Exception:
            pass
        _phone_say(("CPU temperature: %.0f°C (%.0f°F)" % (max(temps), max(temps) * 9 / 5 + 32)) if temps else "Couldn't read the temperature")
        return
    if act == "pctop":
        try:
            out = subprocess.run(["ps", "-eo", "comm,%cpu,%mem", "--sort=-%cpu"], capture_output=True, text=True, timeout=5).stdout.splitlines()[1:6]
            _phone_say("Top CPU: " + "; ".join("%s %s%% cpu" % (l.split()[0], l.split()[1]) for l in out if l.split()))
        except Exception:
            _phone_say("Couldn't read running programs")
        return
    if act == "lastdownload":
        folder = os.path.expanduser("~/Downloads")
        try:
            files = [os.path.join(folder, f) for f in os.listdir(folder) if not f.startswith(".") and not f.endswith((".part", ".crdownload"))]
            files = [f for f in files if os.path.isfile(f)]
            newest = max(files, key=os.path.getmtime) if files else None
        except Exception:
            newest = None
        dev = _first_phone()
        if newest and dev:
            _kde_run(["-d", dev, "--share", newest], timeout=120)
        else:
            _phone_say("No downloads found", dev)
        return
    if act == "unlock":
        if not _sh([["loginctl", "unlock-session"]]):
            _phone_say("Couldn't unlock the screen")
        return
    if act == "screenon":
        if not _sh([["xset", "dpms", "force", "on"], ["xdotool", "mousemove_relative", "1", "1"]]):
            _phone_say("Couldn't wake the screen")
        return
    if act == "stayawake":
        try:
            subprocess.Popen(["systemd-inhibit", "--what=idle:sleep", "--who=Agentic", "--why=Stay awake from phone", "sleep", "3600"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _phone_say("The computer will stay awake for the next hour")
        except Exception:
            _phone_say("Couldn't keep the computer awake (needs systemd-inhibit)")
        return
    if act == "weather":
        try:
            req = urllib.request.Request("https://wttr.in/?format=%l:+%c+%t+(feels+%f),+wind+%w,+%h+humidity",
                                         headers={"User-Agent": "curl/8"})
            txt = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore").strip()
            _phone_say(("Weather " + txt) if txt else "Couldn't get the weather")
        except Exception:
            _phone_say("Couldn't get the weather right now")
        return
    if act == "pcstatus":
        _phone_say(_pc_status())
        return
    if act == "clipboard":
        clip = _desktop_clipboard()
        if clip:
            _phone_text(clip)
        else:
            _phone_say("The computer's clipboard is empty")
        return
    if act == "screenshot":
        folder = os.path.join(tempfile.gettempdir(), "agentic_kdeconnect")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "screenshot_%d.png" % int(time.time()))
        ok = _sh([["spectacle", "-b", "-n", "-f", "-o", path], ["grim", path], ["gnome-screenshot", "-f", path],
                  ["scrot", path], ["import", "-window", "root", path]], timeout=20)
        dev = _first_phone()
        if ok and os.path.isfile(path) and dev:
            _kde_run(["-d", dev, "--share", path], timeout=60)
        else:
            _phone_say("Couldn't take a screenshot (install spectacle, grim or scrot)", dev)
        return
    if act in ("playpause", "next"):
        if not _sh([["playerctl", "play-pause" if act == "playpause" else "next"]]):
            _phone_say("Media control needs playerctl installed")
        return
    if act in ("volup", "voldown", "mute"):
        arg = {"volup": "+5%", "voldown": "-5%"}.get(act)
        cmds = ([["pactl", "set-sink-volume", "@DEFAULT_SINK@", arg], ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%" + arg[0]]]
                if arg else [["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"], ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"]])
        if not _sh(cmds):
            _phone_say("Volume control needs pactl or wpctl")
        return
    if act == "lock":
        if not _sh([["loginctl", "lock-session"], ["xdg-screensaver", "lock"]]):
            _phone_say("Couldn't lock the screen")
        return
    if act in ("prev", "mediastop"):
        if not _sh([["playerctl", "previous" if act == "prev" else "stop"]]):
            _phone_say("Media control needs playerctl installed")
        return
    if act == "micmute":
        if not _sh([["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "toggle"], ["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "toggle"]]):
            _phone_say("Microphone control needs pactl or wpctl")
        return
    if act in ("brightup", "brightdown"):
        up = act == "brightup"
        if not _sh([["brightnessctl", "set", "10%+" if up else "10%-"], ["light", "-A" if up else "-U", "10"]]):
            _phone_say("Brightness control needs brightnessctl installed")
        return
    if act == "suspend":
        if not _sh([["systemctl", "suspend"]]):
            _phone_say("Couldn't put the computer to sleep")
        return
    if act == "pcip":
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.connect(("8.8.8.8", 80))
            ip = sk.getsockname()[0]
            sk.close()
            _phone_say("Computer IP address: " + ip)
        except Exception:
            _phone_say("Couldn't work out the IP address")
        return
    if act == "pctime":
        _phone_say(time.strftime("%A, %B %d, %Y, %I:%M %p"))
        return
    if act in ("wifi", "bluetooth"):
        radio = "wifi" if act == "wifi" else "bluetooth"
        try:
            state = subprocess.run(["nmcli", "radio", "wifi"], capture_output=True, text=True, timeout=5).stdout.strip() if act == "wifi" else ""
        except Exception:
            state = ""
        if act == "wifi" and state in ("enabled", "disabled"):
            ok = _sh([["nmcli", "radio", "wifi", "off" if state == "enabled" else "on"]])
        else:
            ok = _sh([["rfkill", "toggle", radio]])
        if not ok:
            _phone_say("Couldn't toggle " + ("Wi-Fi" if act == "wifi" else "Bluetooth") + " (needs nmcli or rfkill)")
        return
    if act == "screenoff":
        if not _sh([["xset", "dpms", "force", "off"], ["loginctl", "lock-session"]]):
            _phone_say("Couldn't turn the screen off")
        return
    if act == "pcuptime":
        try:
            secs = float(open("/proc/uptime").read().split()[0])
            d, r = divmod(int(secs), 86400)
            h, r = divmod(r, 3600)
            _phone_say("Computer has been on for %dd %dh %dm" % (d, h, r // 60))
        except Exception:
            _phone_say("Couldn't read the uptime")
        return
    if act == "pcdisk":
        try:
            u = shutil.disk_usage("/")
            _phone_say("Disk: %.1f GB free of %.1f GB" % (u.free / 1e9, u.total / 1e9))
        except Exception:
            _phone_say("Couldn't read disk space")
        return
    if act == "pcpublicip":
        try:
            req = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "curl/8"})
            _phone_say("Public IP address: " + urllib.request.urlopen(req, timeout=8).read().decode().strip())
        except Exception:
            _phone_say("Couldn't get the public IP address")
        return
    if act == "nowplaying":
        try:
            out = subprocess.run(["playerctl", "metadata", "--format", "{{artist}} - {{title}}"], capture_output=True, text=True, timeout=5).stdout.strip()
            _phone_say(("Playing: " + out) if out else "Nothing is playing")
        except Exception:
            _phone_say("Needs playerctl installed")
        return

RING_SOUNDS = ["/usr/share/sounds/freedesktop/stereo/phone-incoming-call.oga",
               "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
               "/usr/share/sounds/freedesktop/stereo/bell.oga",
               "/usr/share/sounds/ocean/stereo/alarm-clock-elapsed.oga"]
RING_STOP = threading.Event()


def _ring_computer(seconds=12):
    """Plays a loud sound on this computer so you can find it (stops early via /kdeconnect/ring-stop)."""
    snd = next((f for f in RING_SOUNDS if os.path.isfile(f)), None)
    player = next((p for p in ("paplay", "pw-play", "canberra-gtk-play", "aplay") if shutil.which(p)), None)
    if not snd or not player:
        return
    RING_STOP.clear()

    def loop():
        end = time.time() + seconds
        while time.time() < end and not RING_STOP.is_set():
            args = [player, "-f", snd] if player == "canberra-gtk-play" else [player, snd]
            try:
                subprocess.run(args, capture_output=True, timeout=10)
            except Exception:
                break
    threading.Thread(target=loop, daemon=True).start()


# ── Watchers: ping the phone when something happens on the computer ─────
# Turn each one off with an empty file ~/.agentic_watch_off_<name> (hot, download, battery).
WATCH_HOT_C = int(os.environ.get("AGENTIC_WATCH_HOT_C", "85"))


def _watch_on(name):
    return not os.path.exists(os.path.join(os.path.expanduser("~"), ".agentic_watch_off_" + name)) and not phone_alerts_muted()


def _cpu_temp():
    best = None
    try:
        for z in os.listdir("/sys/class/thermal"):
            f = os.path.join("/sys/class/thermal", z, "temp")
            if z.startswith("thermal_zone") and os.path.isfile(f):
                t = int(open(f).read().strip()) / 1000
                if 0 < t < 130:
                    best = t if best is None else max(best, t)
    except Exception:
        pass
    return best


def _watchers():
    dl = os.path.expanduser("~/Downloads")
    seen = set(os.listdir(dl)) if os.path.isdir(dl) else set()
    last_hot = 0
    low_warned = full_warned = False
    tick = 0
    while True:
        time.sleep(30)
        tick += 1
        try:
            # 1. computer running hot (at most every 15 min)
            t = _cpu_temp()
            if t and t >= WATCH_HOT_C and time.time() - last_hot > 900 and _watch_on("hot"):
                last_hot = time.time()
                _phone_say("Heads up: the computer is running hot (%.0f°C)" % t)
            # 2. a download finished
            if os.path.isdir(dl):
                now = set(os.listdir(dl))
                done = [f for f in now - seen if not f.startswith(".") and not f.endswith((".part", ".crdownload", ".tmp"))]
                seen = {f for f in now if not f.endswith((".part", ".crdownload", ".tmp"))}
                if done and _watch_on("download"):
                    _phone_say("Download finished: " + (done[0] if len(done) == 1 else "%s and %d more" % (done[0], len(done) - 1)))
            # 3. phone battery low / full (every 2 min)
            if tick % 4 == 0 and _watch_on("battery"):
                dev = _first_phone()
                b = _kde_battery(dev) if dev else None
                if b:
                    if b["charge"] <= 15 and not b["charging"] and not low_warned:
                        low_warned = True
                        _phone_say("Phone battery is low (%d%%). Plug it in." % b["charge"], dev)
                    elif b["charge"] > 25 or b["charging"]:
                        low_warned = False
                    if b["charge"] >= 100 and b["charging"] and not full_warned:
                        full_warned = True
                        _phone_say("Phone is fully charged.", dev)
                    elif b["charge"] < 95:
                        full_warned = False
        except Exception:
            pass


threading.Thread(target=_watchers, daemon=True).start()


def ep_kde_pcinfo(q):
    """Quick facts about this computer for chat questions."""
    info = {"status": _pc_status(), "time": time.strftime("%A, %B %d, %Y, %I:%M %p")}
    t = _cpu_temp()
    if t:
        info["temp_c"] = round(t)
    try:
        u = shutil.disk_usage("/")
        info["disk_free_gb"], info["disk_total_gb"] = round(u.free / 1e9, 1), round(u.total / 1e9, 1)
    except Exception:
        pass
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sk.connect(("8.8.8.8", 80)); info["ip"] = sk.getsockname()[0]; sk.close()
    except Exception:
        pass
    try:
        secs = int(float(open("/proc/uptime").read().split()[0])); info["uptime"] = "%dd %dh %dm" % (secs // 86400, secs % 86400 // 3600, secs % 3600 // 60)
    except Exception:
        pass
    try:
        out = subprocess.run(["ps", "-eo", "comm,%cpu", "--sort=-%cpu"], capture_output=True, text=True, timeout=5).stdout.splitlines()[1:6]
        info["top"] = [{"name": l.split()[0], "cpu": l.split()[1]} for l in out if len(l.split()) >= 2]
    except Exception:
        pass
    if shutil.which("playerctl"):
        try:
            p = subprocess.run(["playerctl", "metadata", "--format", "{{artist}} - {{title}}"], capture_output=True, text=True, timeout=4).stdout.strip()
            if p:
                info["playing"] = p
        except Exception:
            pass
    return {"ok": True, **info}


def ep_kde_ring_stop(q):
    RING_STOP.set()
    return {"ok": True, "message": "stopped"}


# The phone app's own "Send ping" button: KDE Connect shows it as a desktop
# notification, so we watch for that notification and pass it to Agentic.
_PING_WATCH = {"on": False}


def _watch_phone_pings():
    if _PING_WATCH["on"] or not shutil.which("dbus-monitor"):
        return
    _PING_WATCH["on"] = True

    def run():
        try:
            p = subprocess.Popen(["dbus-monitor", "--session",
                                  "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except Exception:
            _PING_WATCH["on"] = False
            return
        block = []
        for line in p.stdout:
            if line.startswith("method call"):
                block = []
            block.append(line)
            if re.match(r"^\s*int32 -?\d+\s*$", line):   # expire_timeout: last Notify argument
                _check_ping_block(block)
                block = []
            elif len(block) > 400:
                block = []
        _PING_WATCH["on"] = False
    threading.Thread(target=run, daemon=True).start()


def _check_ping_block(block):
    # only the top-level Notify arguments (app, icon, summary, body) - stop before the
    # actions array / hints dict, whose keys like "x-kde-eventId" used to leak in as text
    top = []
    for ln in block:
        if re.match(r"^\s*array \[", ln):
            break
        top.append(ln)
    strs = re.findall(r'^\s*string "(.*)"\s*$', "".join(top), re.M)
    text = "\n".join(block)
    if not strs or not re.search(r"kde ?connect", text, re.I):
        return
    # Notify(app_name, replaces_id, app_icon, summary, body, ...)
    app = strs[0] if strs else ""
    summary = strs[2] if len(strs) > 2 else ""
    body = strs[3] if len(strs) > 3 else ""
    if "pingReceived" in text or body.strip() in ("Ping!", "Ping"):
        with KDE_ACT_LOCK:
            KDE_ACTIONS.append({"act": "phoneping", "t": time.time(), "text": body, "from": summary or app})
            del KDE_ACTIONS[:-20]
        return
    # any other KDE Connect popup (phone notifications mirrored to this desktop)
    if (summary or body) and not re.search(r"^(Agentic|Hello from Agentic)", summary + body) \
            and not re.match(r"^x-kde-", summary) and not re.match(r"^x-kde-", body):
        nid = "dm%d" % int(time.time() * 1000)
        with KDE_ACT_LOCK:
            if not any(n["title"] == summary and n["text"] == body and time.time() - n["t"] < 5 for n in PHONE_NOTIFS):
                PHONE_NOTIFS.append({"id": nid, "t": time.time(), "app": "", "title": summary or app, "text": body})


def _desktop_clipboard():
    """Text on this computer's clipboard (KDE Connect syncs the phone's clipboard here)."""
    tries = []
    if shutil.which("gdbus"):
        tries.append(["gdbus", "call", "--session", "--dest", "org.kde.klipper", "--object-path", "/klipper",
                      "--method", "org.kde.klipper.klipper.getClipboardContents"])
    if shutil.which("wl-paste"):
        tries.append(["wl-paste", "--no-newline"])
    if shutil.which("xclip"):
        tries.append(["xclip", "-o", "-selection", "clipboard"])
    if shutil.which("xsel"):
        tries.append(["xsel", "--clipboard", "--output"])
    for cmd in tries:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
        except Exception:
            continue
        out = p.stdout if p.returncode == 0 else ""
        if cmd[0] == "gdbus":
            m = re.match(r"^\('((?:[^'\\]|\\.)*)',\)\s*$", out.strip(), re.S)
            out = m.group(1).replace("\\n", "\n").replace("\\'", "'").replace("\\\\", "\\") if m else ""
        if out.strip():
            return out.strip()[:6000]
    return ""


def ep_kde_run(q):
    act = q.get("act", "")
    if act not in KDE_ACT_NAMES and not re.fullmatch(r"custom-[a-z0-9_-]{1,44}", act):
        return {"ok": False, "error": "unknown action"}
    item = {"act": act, "t": time.time()}
    if act in ("ask", "navgo", "lookup", "define", "explain", "translate", "factcheck", "wikilookup", "synonyms", "proscons"):
        item["text"] = _desktop_clipboard()
    if act == "ring":
        pass  # the page plays your Customize ring tone itself; no second system sound
    if act in KDE_SERVER_ACTS:
        threading.Thread(target=_do_server_act, args=(act,), daemon=True).start()
        return {"ok": True, "message": "done"}
    with KDE_ACT_LOCK:
        KDE_ACTIONS.append(item)
        del KDE_ACTIONS[:-20]
    return {"ok": True, "message": "queued"}


def ep_kde_actions(q):
    _watch_phone_pings()
    with KDE_ACT_LOCK:
        acts = [a for a in KDE_ACTIONS if time.time() - a["t"] < 120]
        KDE_ACTIONS.clear()
    return {"ok": True, "actions": acts}


def _kde_config_dirs(dev):
    """Where KDE Connect keeps per-device settings (normal install and Flatpak)."""
    return [os.path.expanduser("~/.config/kdeconnect/%s" % dev),
            os.path.expanduser("~/.var/app/org.kde.kdeconnect/config/kdeconnect/%s" % dev)]


def _runcommand_read(old):
    """Parse the commands= line however Qt wrote it (quoted + escaped, or bare)."""
    m = re.search(r"^commands=(.*)$", old, re.M)
    if not m:
        return {}
    v = m.group(1).strip()
    if v.startswith('"') and v.endswith('"'):
        v = v[1:-1]
        out, i = [], 0
        while i < len(v):
            c = v[i]
            if c == "\\" and i + 1 < len(v):
                n = v[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(n, n))
                i += 2
                continue
            out.append(c)
            i += 1
        v = "".join(out)
    if v.startswith("@ByteArray(") and v.endswith(")"):
        v = v[len("@ByteArray("):-1]
    try:
        d = json.loads(v)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _runcommand_line(cmds):
    """commands= in the quoted, escaped form KDE Connect itself writes."""
    raw = "@ByteArray(" + json.dumps(cmds, separators=(",", ":")) + ")"
    return 'commands="' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _restart_kdeconnect():
    """KDE Connect only re-reads the command list when it (re)connects, so restart its daemon."""
    for q in ("kquitapp6", "kquitapp5"):
        if shutil.which(q):
            subprocess.run([q, "kdeconnectd"], capture_output=True, timeout=10)
            break
    else:
        subprocess.run(["pkill", "-x", "kdeconnectd"], capture_output=True, timeout=5)
    time.sleep(1.5)
    exe = shutil.which("kdeconnectd") or next((x for x in (
        "/usr/lib/x86_64-linux-gnu/libexec/kdeconnectd", "/usr/libexec/kdeconnectd",
        "/usr/lib/libexec/kdeconnectd", "/usr/lib64/libexec/kdeconnectd") if os.path.isfile(x)), None)
    try:
        if exe:
            subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        elif shutil.which("flatpak"):
            subprocess.Popen(["flatpak", "run", "--command=kdeconnectd", "org.kde.kdeconnect"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(2)
        _kde_run(["--refresh"])   # also wakes it through D-Bus if it wasn't started above
        return True
    except Exception:
        return False


def ep_kde_setup_commands(q):
    """Adds the Agentic buttons to KDE Connect's Run command list for this device."""
    dev = q.get("id", "")
    if not dev or "/" in dev or ".." in dev:
        return {"ok": False, "error": "no device picked"}
    dirs = _kde_config_dirs(dev)
    base = next((d for d in dirs if os.path.isdir(d)), dirs[0])
    folder = os.path.join(base, "kdeconnect_runcommand")
    path = os.path.join(folder, "config")
    old = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="ignore") as f:
            old = f.read()
    existing = _runcommand_read(old)
    # drop any Agentic entries from an older version, then add the current set
    existing = {k: v for k, v in existing.items() if not str(k).startswith("{agentic-")}
    off = {a for a in (q.get("off") or "").split(",") if a}
    if q.get("all") != "1":
        off |= PHONE_BTN_HIDDEN
    off |= unavailable_acts()
    for act, name in KDE_ACT_NAMES.items():
        if act in off:
            continue
        existing["{agentic-%s}" % act] = {"name": name, "command": "curl -s 'http://127.0.0.1:%d/kdeconnect/run?act=%s' >/dev/null" % (CRESCENT_PORT, act)}
    try:
        customs = json.loads(q.get("custom") or "[]")
    except Exception:
        customs = []
    for c in customs[:400] if isinstance(customs, list) else []:
        cid = str((c or {}).get("id", ""))
        cname = str((c or {}).get("name", "")).strip()[:70]
        if re.fullmatch(r"[a-z0-9_-]{1,44}", cid) and cname and ("custom-" + cid) not in off:
            existing["{agentic-custom-%s}" % cid] = {"name": cname if cid.startswith("cz-") else "My: " + cname, "command": "curl -s 'http://127.0.0.1:%d/kdeconnect/run?act=custom-%s' >/dev/null" % (CRESCENT_PORT, cid)}
    os.makedirs(folder, exist_ok=True)
    if old:
        shutil.copyfile(path, path + ".agentic-backup")
    line = _runcommand_line(existing)
    if re.search(r"^commands=", old, re.M):
        new = re.sub(r"^commands=.*$", lambda _: line, old, flags=re.M)
    elif "[General]" in old:
        new = old.replace("[General]", "[General]\n" + line, 1)
    else:
        new = old + ("\n" if old and not old.endswith("\n") else "") + "[General]\n" + line + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    restarted = _restart_kdeconnect() if q.get("restart", "1") != "0" else False
    mine = sum(1 for k in existing if str(k).startswith("{agentic-"))
    return {"ok": True, "path": path, "count": mine,
            "message": "%d Agentic buttons saved%s. On your phone open KDE Connect, tap your computer, then Run command (pull down to refresh)."
                       % (mine, " and KDE Connect restarted" if restarted else ". Restart KDE Connect (or log out and back in)")}


def ep_kde_button_list(q):
    return {"ok": True, "buttons": [{"act": a, "name": n} for a, n in KDE_ACT_NAMES.items()]}


def ep_kde_refresh(q):
    return _kde_done(_kde_run(["--refresh"]), "looking for devices")


def ep_kde_pair(q):
    if not q.get("id"):
        return {"ok": False, "error": "no device picked"}
    return _kde_done(_kde_run(["-d", q["id"], "--pair"]), "pair request sent, accept it on the phone")


# ── Persistent Crescent.py session on a pty ─────────────────────────────────
class CrescentSession:
    def __init__(self):
        self.lock = threading.Lock()
        self.pid = None
        self.fd = None
        self.client = None          # current WebSocket connection (only one at a time)
        self.client_lock = threading.Lock()
        self.size = (30, 100)

    def alive(self):
        if not self.pid:
            return False
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            if pid == 0:
                return True
        except ChildProcessError:
            pass
        self.pid = None
        try:
            os.close(self.fd)
        except Exception:
            pass
        self.fd = None
        return False

    def ensure(self):
        with self.lock:
            if self.alive():
                return True
            if not os.path.isfile(CRESCENT_PY):
                return False
            pid, fd = pty.fork()
            if pid == 0:  # child
                os.chdir(os.path.dirname(CRESCENT_PY))
                env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor",
                           LANG=os.environ.get("LANG") or "C.UTF-8")
                os.execvpe(sys.executable, [sys.executable, CRESCENT_PY], env)
            self.pid, self.fd = pid, fd
            self.resize(*self.size)
            threading.Thread(target=self._pump, args=(fd,), daemon=True).start()
            log(f"started Crescent.py (pid {pid})")
            return True

    def _pump(self, fd):
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 1.0)
                if not r:
                    if self.fd != fd:
                        return
                    continue
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                self.send(b"\r\n\x1b[2m[Crescent.py exited - reconnect to start it again]\x1b[0m\r\n")
                self.alive()
                return
            self.send(data)

    def send(self, data):
        with self.client_lock:
            c = self.client
        if c:
            try:
                c.ws_send(data, binary=True)
            except Exception:
                pass

    def write(self, data):
        if self.fd is not None:
            try:
                os.write(self.fd, data)
            except OSError:
                pass

    def resize(self, rows, cols):
        self.size = (max(5, int(rows)), max(20, int(cols)))
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.size[0], self.size[1], 0, 0))
                os.kill(self.pid, signal.SIGWINCH)
            except Exception:
                pass

    def redraw(self):
        # curses redraws everything on a resize; nudge it so a reattached
        # browser panel gets a full screen instead of a blank one.
        r, c = self.size
        self.resize(r, c - 1)
        time.sleep(0.05)
        self.resize(r, c)

    def _children(self):
        kids = []
        for d in os.listdir("/proc") if os.path.isdir("/proc") else []:
            if d.isdigit() and _ppid(int(d)) == self.pid:
                kids.append(int(d))
        return kids

    def kill(self):
        with self.lock:
            if not self.alive():
                return False
            kids = self._children()  # its mpv — only this copy's, not yours
            try:
                os.kill(self.pid, signal.SIGTERM)
                for _ in range(20):
                    time.sleep(0.1)
                    if not self.alive():
                        break
                else:
                    os.kill(self.pid, signal.SIGKILL)
                    self.alive()
            except Exception:
                pass
            for k in kids:
                try:
                    os.kill(k, signal.SIGTERM)
                except Exception:
                    pass
            return True


SESSION = CrescentSession()


def mpv_sock_path():
    """Crescent.py's mpv socket — wherever a running Crescent.py put it."""
    cands = [os.path.join(os.environ.get("TMPDIR", "/tmp"), "play-mpv.sock"), "/tmp/play-mpv.sock",
             os.path.expanduser("~/../usr/tmp/play-mpv.sock")]  # last one: Termux
    for c in cands:
        if os.path.exists(c):
            return c
    return cands[0]


def mpv_alive():
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(mpv_sock_path())
        s.close()
        return True
    except Exception:
        return False


def mpv_cmd(*command):
    """Fire one mpv IPC command; returns mpv's reply dict."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(mpv_sock_path())
    try:
        s.sendall((json.dumps({"command": list(command), "request_id": 7}) + "\n").encode())
        buf = b""
        deadline = time.time() + 2
        while time.time() < deadline:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("request_id") == 7:
                    return msg
    finally:
        s.close()
    return {}


def mpv_get(props):
    """Read properties from Crescent.py's mpv over its IPC socket."""
    sock_path = mpv_sock_path()
    out = {}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(sock_path)
    try:
        for i, p in enumerate(props, 1):
            s.sendall((json.dumps({"command": ["get_property", p], "request_id": i}) + "\n").encode())
        buf = b""
        deadline = time.time() + 2
        while len(out) < len(props) and time.time() < deadline:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                rid = msg.get("request_id")
                if rid and 1 <= rid <= len(props):
                    out[props[rid - 1]] = msg.get("data")
    finally:
        s.close()
    return out


def ep_crescent_now(q):
    try:
        d = mpv_get(["path", "time-pos", "media-title", "pause"])
    except Exception:
        return {"ok": False, "error": "standalone Crescent.py isn't playing anything (mpv not running)"}
    url = d.get("path")
    if not url:
        return {"ok": False, "error": "nothing playing in standalone Crescent.py"}
    return {"ok": True, "url": url, "position": d.get("time-pos") or 0,
            "title": d.get("media-title") or url, "paused": bool(d.get("pause"))}


# ── Controlling a Crescent.py you started yourself ──────────────────────────
# 1. tmux: if Crescent.py is running inside any tmux pane, commands are typed
#    into it with `tmux send-keys` — full control, every Crescent command works.
#    Start it that way with:  tmux new -s crescent python3 Crescent.py
# 2. player: if it's running in a plain terminal, we can't type into that
#    window, so playback commands go straight to its mpv over the IPC socket.
# 3. session: otherwise, the background Crescent.py this server runs itself.

def _crescent_pids():
    pids = set()
    for d in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except Exception:
            continue
        if any(a.endswith(b"Crescent.py") or a.endswith(b"crescent.py") for a in args[:3]):
            pids.add(int(d))
    if SESSION.pid:
        pids.discard(SESSION.pid)  # our own background copy isn't "yours"
    return pids


def _ppid(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except Exception:
        return 0


def find_tmux_pane():
    """The tmux pane (e.g. '%3') running a Crescent.py, or None."""
    if not shutil.which("tmux"):
        return None
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    panes = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            panes[int(parts[1])] = parts[0]
    if not panes:
        return None
    for pid in _crescent_pids():
        p, hops = pid, 0
        while p > 1 and hops < 12:
            if p in panes:
                return panes[p]
            p, hops = _ppid(p), hops + 1
    return None


def tmux_type(pane, text):
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], timeout=3)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], timeout=3)


def _parse_seek(a):
    a = a.strip()
    if a.endswith("%"):
        return [float(a[:-1]), "absolute-percent"]
    rel = a[:1] in "+-"
    sign = -1 if a.startswith("-") else 1
    body = a.lstrip("+-")
    parts = body.split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return [sign * secs, "relative"] if rel else [secs, "absolute"]


def player_command(cmd):
    """Translate a Crescent command into mpv IPC. Returns (ok, message)."""
    parts = cmd.split(None, 1)
    w, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
    if w == "pause":
        mpv_cmd("set_property", "pause", True); return True, "paused"
    if w in ("play", "resume") and not arg:
        mpv_cmd("set_property", "pause", False); return True, "playing"
    if w in ("toggle", "pp"):
        mpv_cmd("cycle", "pause"); return True, "toggled pause"
    if w == "stop":
        mpv_cmd("stop"); return True, "stopped"
    if w in ("skip", "next"):
        mpv_cmd("playlist-next", "force"); return True, "skipped"
    if w in ("back", "reverse", "prev"):
        mpv_cmd("playlist-prev", "force"); return True, "back"
    if w in ("vol", "volume") and arg:
        v = max(0, min(130, float(arg)))
        mpv_cmd("set_property", "volume", v); return True, f"volume {v:.0f}"
    if w in ("seek", "ff", "rw") and arg:
        mpv_cmd("seek", *_parse_seek(arg)); return True, f"seek {arg}"
    if w in ("play", "queue") and arg:
        src = arg if re.match(r"^(https?://|ytdl://|/)", arg) else "ytdl://ytsearch1:" + arg
        if src.startswith("ytsearch"):
            src = "ytdl://" + src
        mpv_cmd("loadfile", src, "replace" if w == "play" else "append-play")
        return True, ("playing " if w == "play" else "queued ") + arg
    if re.match(r"^https?://", cmd):
        mpv_cmd("loadfile", cmd, "replace"); return True, "playing " + cmd
    return False, (f"'{w}' needs full control — Crescent.py is in a plain terminal, so only "
                   "play/pause/skip/back/stop/vol/seek reach it. For everything, start it with: "
                   "tmux new -s crescent python3 Crescent.py")


def crescent_target():
    yours = _crescent_pids()
    if yours and SESSION.alive():
        # Two Crescent.py's would fight over the same mpv socket — once you're
        # running your own, the background copy steps aside.
        log("your Crescent.py is running — stopping the background copy")
        SESSION.kill()
    pane = find_tmux_pane()
    if pane:
        return "tmux", pane
    if yours:
        return "player", None
    if SESSION.alive():
        return "session", None
    return "session", None


def ep_crescent_send(q):
    """Send Crescent commands to the real Crescent.py — yours if it's running
    (tmux: full control, plain terminal: playback via mpv), else a background
    one this server starts. cmd may hold several commands separated by
    newlines; delay= waits (seconds) between them so a seek lands after play
    has loaded."""
    cmds = [c.strip() for c in (q.get("cmd") or "").split("\n") if c.strip()]
    if not cmds:
        return {"ok": False, "error": "missing cmd"}
    delay = min(float(q.get("delay") or 0), 15)
    mode, pane = crescent_target()

    if mode == "player":
        if not mpv_alive():
            return {"ok": False, "mode": mode, "error": "your Crescent.py is running but its player (mpv) isn't answering"}
        results = []
        for i, c in enumerate(cmds):
            if i and delay:
                time.sleep(delay)
            try:
                ok, msg = player_command(c)
            except Exception as e:
                ok, msg = False, f"player error: {e}"
            if not ok:
                return {"ok": False, "mode": mode, "error": msg, "done": results}
            results.append(msg)
        return {"ok": True, "mode": mode, "sent": cmds, "done": results}

    started = False
    if mode == "session":
        was_running = SESSION.alive()
        if not SESSION.ensure():
            return {"ok": False, "error": f"Crescent.py not found at {CRESCENT_PY}"}
        started = not was_running
        if started:
            time.sleep(2.0)  # let curses + mpv come up before typing

    def typer():
        for i, c in enumerate(cmds):
            if i and delay:
                time.sleep(delay)
            if mode == "tmux":
                tmux_type(pane, c)
            else:
                SESSION.write(c.encode() + b"\r")
            time.sleep(0.15)
    threading.Thread(target=typer, daemon=True).start()
    return {"ok": True, "mode": mode, "pane": pane, "started": started, "sent": cmds}


# ── Mailbox: Crescent.py in your Linux terminal → Agentic ───────────────────
# The browser can't be called into, so Agentic polls /agentic/poll every
# second: it picks up queued commands, runs them in its built-in Crescent,
# posts each reply to /agentic/result, and reports what it's playing.
AG_LOCK = threading.Condition()
AG = {"queue": [], "results": {}, "state": {}, "seen": 0.0, "next_id": 1}


def ag_online():
    return time.time() - AG["seen"] < 4


def ep_agentic_send(q):
    cmd = (q.get("cmd") or "").strip()
    if not cmd:
        return {"ok": False, "error": "missing cmd"}
    with AG_LOCK:
        cid = AG["next_id"]; AG["next_id"] += 1
        AG["queue"].append({"id": cid, "cmd": cmd})
        online = ag_online()
    if not online:
        return {"ok": True, "id": cid, "online": False,
                "reply": "queued — Agentic isn't open right now; it'll run when you open Agentic"}
    wait = min(float(q.get("wait") or 0), 10)
    deadline = time.time() + wait
    with AG_LOCK:
        while wait and cid not in AG["results"] and time.time() < deadline:
            AG_LOCK.wait(deadline - time.time())
        reply = AG["results"].pop(cid, None)
    return {"ok": True, "id": cid, "online": True, "reply": reply}


def ep_agentic_state(q):
    with AG_LOCK:
        return {"ok": True, "online": ag_online(), "state": AG["state"]}


# One player at a time: whichever starts playing last pauses the other, so
# Agentic and Crescent.py never play over each other. CRESCENT_EXCLUSIVE=0
# turns this off.
EXCLUSIVE = os.environ.get("CRESCENT_EXCLUSIVE", "1") != "0"
PLAYING = {"agentic": False, "crescent": False}


def crescent_playing():
    if not os.path.exists(mpv_sock_path()):
        return False
    try:
        d = mpv_get(["pause", "idle-active"])
    except Exception:
        return False
    return d.get("pause") is False and not d.get("idle-active")


def ep_agentic_poll(q):
    with AG_LOCK:
        AG["seen"] = time.time()
        if q.get("state"):
            try:
                AG["state"] = json.loads(q["state"])
            except Exception:
                pass
        ag_now = bool(AG["state"].get("playing"))
    cr_now = crescent_playing() if EXCLUSIVE else False
    extra = []
    if EXCLUSIVE and ag_now and cr_now:
        ag_started = not PLAYING["agentic"]
        cr_started = not PLAYING["crescent"]
        if ag_started and not cr_started:
            try:
                mpv_cmd("set_property", "pause", True)  # Agentic started → pause Crescent.py
                cr_now = False
                log("Agentic started playing — paused Crescent.py")
            except Exception:
                pass
        else:
            extra.append({"id": 0, "cmd": "__pause"})  # Crescent.py started → pause Agentic
            ag_now = False
            log("Crescent.py started playing — paused Agentic")
    PLAYING["agentic"], PLAYING["crescent"] = ag_now, cr_now
    with AG_LOCK:
        cmds, AG["queue"] = AG["queue"], []
    return {"ok": True, "cmds": extra + cmds}


def ep_agentic_result(q):
    try:
        cid = int(q.get("id") or 0)
    except ValueError:
        return {"ok": False}
    with AG_LOCK:
        AG["results"][cid] = q.get("text", "")
        if len(AG["results"]) > 200:
            for k in sorted(AG["results"])[:100]:
                AG["results"].pop(k, None)
        AG_LOCK.notify_all()
    return {"ok": True}


# ── HTTP + minimal WebSocket handler ────────────────────────────────────────
ROUTES = {
    "/audio-url": ep_audio_url,
    "/recommend": ep_recommend,
    "/download": ep_download,
    "/handoff/send": ep_handoff_send,
    "/handoff/receive": ep_handoff_receive,
    "/kdeconnect/devices": ep_kde_devices,
    "/kdeconnect/share": ep_kde_share,
    "/kdeconnect/share-text": ep_kde_share_text,
    "/kdeconnect/ring": ep_kde_ring,
    "/kdeconnect/ping": ep_kde_ping,
    "/kdeconnect/status": ep_kde_status,
    "/kdeconnect/notifications": ep_kde_notifications,
    "/kdeconnect/sms": ep_kde_sms,
    "/kdeconnect/photo": ep_kde_photo,
    "/kdeconnect/refresh": ep_kde_refresh,
    "/kdeconnect/pair": ep_kde_pair,
    "/kdeconnect/reply": ep_kde_reply,
    "/kdeconnect/dismiss": ep_kde_dismiss,
    "/kdeconnect/contacts": ep_kde_contacts,
    "/kdeconnect/run": ep_kde_run,
    "/kdeconnect/actions": ep_kde_actions,
    "/kdeconnect/ring-stop": ep_kde_ring_stop,
    "/kdeconnect/setup-commands": ep_kde_setup_commands,
    "/kdeconnect/button-list": ep_kde_button_list,
    "/kdeconnect/pcinfo": ep_kde_pcinfo,
    "/kdeconnect/check": ep_kde_check,
    "/agentic/send": ep_agentic_send,
    "/agentic/state": ep_agentic_state,
    "/agentic/poll": ep_agentic_poll,
    "/agentic/result": ep_agentic_result,
}


class CrescentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/agentic/poll" in self.path or "/health" in self.path:
            return  # Agentic polls every second; keep the log readable
        log(self.address_string(), fmt % args)

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*, Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        # Only pages on this computer may write files (not random websites).
        origin = self.headers.get("Origin") or ""
        host = urllib.parse.urlparse(origin).hostname if origin and origin != "null" else ""
        if host and host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1") and not host.endswith(".localhost"):
            return self.reply({"ok": False, "error": "forbidden origin"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 200 * 1024 * 1024:
                return self.reply({"ok": False, "error": "bad size"}, 400)
            body = self.rfile.read(n)
            if path == "/backup/save":
                return self.reply(save_backup(body))
            if path == "/tools/pycheck":
                return self.reply(py_check(body))
            if path == "/ai/chat":
                # Other free AI providers, same idea as /glm/chat: fixed addresses only, key passed through.
                q = {k: v[-1] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
                p = q.get("p", "")
                auth = (self.headers.get("Authorization") or "").strip()
                key = auth[7:].strip() if auth.lower().startswith("bearer ") else auth
                eps = {"mistral": "https://api.mistral.ai/v1/chat/completions",
                       "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions",
                       "cerebras": "https://api.cerebras.ai/v1/chat/completions"}
                if p in eps:
                    ep = eps[p]
                else:
                    return self.reply({"error": {"message": "unknown provider"}}, 400)
                # Cloudflare-fronted APIs (Cerebras) block Python's default User-Agent
                hdrs = {"Content-Type": "application/json", "Authorization": "Bearer " + key,
                        "User-Agent": "Agentic/1.0", "Accept": "application/json, text/event-stream"}
                for _attempt in range(2):
                    try:
                        r = urllib.request.urlopen(urllib.request.Request(ep, data=body, method="POST", headers=hdrs), timeout=180)
                        code, out, ctype = r.status, r.read(), r.headers.get("Content-Type", "application/json")
                        break
                    except urllib.error.HTTPError as he:
                        code, out, ctype = he.code, he.read(), "application/json"
                        if he.code not in (429, 503) or _attempt:
                            break
                        time.sleep(2)
                    except Exception as ex:
                        code, out, ctype = 502, json.dumps({"error": {"message": "couldn't reach %s: %s" % (p, str(ex)[:120])}}).encode(), "application/json"
                        break
                self.send_response(code)
                self.cors()
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            if path == "/glm/chat":
                # z.ai blocks direct browser calls (CORS), so Agentic's GLM requests go through here.
                # A key only works on the endpoint it was made for: normal z.ai keys, GLM Coding Plan
                # keys, and bigmodel.cn (China) keys each have their own. Try each until one accepts it.
                auth = (self.headers.get("Authorization") or "").strip()
                if auth and not auth.lower().startswith("bearer "):
                    auth = "Bearer " + auth
                auth = "Bearer " + auth[7:].strip().strip('"').strip("'")
                code, out, tried = 0, b"", []
                # free GLM models are often "temporarily overloaded" (code 1305): wait and retry,
                # then try the other free model before giving up
                try:
                    _bj = json.loads(body)
                except Exception:
                    _bj = None
                # GLM 4.5+ "thinks" before answering by default; that hidden reasoning is thrown away by the
                # app and just adds wait. Turn it off unless the request asked for it explicitly.
                if isinstance(_bj, dict) and "thinking" not in _bj and os.environ.get("AGENTIC_GLM_THINKING", "0") != "1":
                    _bj["thinking"] = {"type": "disabled"}
                    body = json.dumps(_bj).encode()
                _stream = isinstance(_bj, dict) and bool(_bj.get("stream"))
                for _attempt in range(2):
                    if _attempt and _bj is not None:
                        time.sleep(1.5)
                        body = json.dumps(_bj).encode()
                    code, out, tried = 0, b"", []
                    for ep in ("https://api.z.ai/api/paas/v4/chat/completions",
                               "https://api.z.ai/api/coding/paas/v4/chat/completions",
                               "https://open.bigmodel.cn/api/paas/v4/chat/completions"):
                        req = urllib.request.Request(ep, data=body, method="POST",
                                                     headers={"Content-Type": "application/json", "Authorization": auth})
                        try:
                            r = urllib.request.urlopen(req, timeout=180)
                            if _stream and r.status == 200:
                                # pass the reply through word by word as it arrives
                                self.send_response(200)
                                self.cors()
                                self.send_header("Content-Type", r.headers.get("Content-Type") or "text/event-stream")
                                self.send_header("Cache-Control", "no-cache")
                                self.send_header("Connection", "close")
                                self.end_headers()
                                try:
                                    while True:
                                        chunk = r.read1(4096) if hasattr(r, "read1") else r.read(1024)
                                        if not chunk:
                                            break
                                        self.wfile.write(chunk)
                                        self.wfile.flush()
                                except (BrokenPipeError, ConnectionResetError):
                                    pass   # the app stopped listening (Stop pressed)
                                self.close_connection = True
                                return
                            code, out = r.status, r.read()
                            break
                        except urllib.error.HTTPError as he:
                            code, out = he.code, he.read()
                            try:
                                m = json.loads(out).get("error", {}).get("message", "")
                            except Exception:
                                m = out[:120].decode("utf-8", "ignore")
                            tried.append("%s: %s" % (urllib.parse.urlparse(ep).netloc + ("/coding" if "/coding/" in ep else ""), m))
                            if he.code not in (401, 403):
                                break
                        except Exception as ex:
                            code, out = 502, json.dumps({"error": {"message": "couldn't reach z.ai: %s" % str(ex)[:120]}}).encode()
                    if not (code in (429, 500, 503) or b'"1305"' in out or b'"1302"' in out):
                        break
                if code in (401, 403) and tried:
                    key = auth[7:]
                    shape = "key looks like %d characters%s" % (len(key), ", with a dot in it" if "." in key else ", with no dot (z.ai keys normally have one)")
                    out = json.dumps({"error": {"message": "every GLM address rejected the key (%s). %s" % (shape, " | ".join(tried))}}).encode()
                self.send_response(code)
                self.cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            if path == "/kdeconnect/upload":
                q = {k: v[-1] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
                return self.reply(ep_kde_upload(q, body))
            return self.reply({"error": "not found"}, 404)
        except Exception as e:
            return self.reply({"ok": False, "error": str(e)[:200]}, 500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        path = u.path.rstrip("/") or "/"
        try:
            if path == "/crescent-pty":
                return self.handle_pty()
            if path == "/crescent-kill":
                return self.reply({"killed": SESSION.kill()})
            if path == "/crescent-now":
                return self.reply(ep_crescent_now(q))
            if path == "/crescent-send":
                return self.reply(ep_crescent_send(q))
            if path in ("/", "/health"):
                return self.reply({"ok": True, "crescent_py": CRESCENT_PY,
                                   "found": os.path.isfile(CRESCENT_PY),
                                   "session_running": SESSION.alive(),
                                   "your_crescent": crescent_target()[0] if _crescent_pids() else None,
                                   "tmux_pane": find_tmux_pane(),
                                   "yt_dlp": bool(shutil.which("yt-dlp")),
                                   "mpv": bool(shutil.which("mpv"))})
            if path.startswith("/kdeconnect/"):
                # only pages on this computer may push things to your phone
                origin = self.headers.get("Origin") or ""
                host = urllib.parse.urlparse(origin).hostname if origin and origin != "null" else ""
                if host and host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1") and not host.endswith(".localhost"):
                    return self.reply({"ok": False, "error": "forbidden origin"}, 403)
            fn = ROUTES.get(path)
            if not fn:
                return self.reply({"error": "not found"}, 404)
            return self.reply(fn(q))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("error:", repr(e))
            try:
                self.reply({"ok": False, "error": str(e)[:240], "results": []}, 500)
            except Exception:
                pass

    # -- WebSocket (RFC 6455, just what xterm.js needs) --
    def handle_pty(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
            return self.reply({"error": "websocket upgrade required"}, 400)
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        self._send_lock = threading.Lock()

        if not SESSION.ensure():
            self.ws_send(f"\r\nCrescent.py not found at {CRESCENT_PY}\r\n"
                         "Put crescent_server.py in the same folder as Crescent.py.\r\n".encode())
            self.ws_close()
            return

        with SESSION.client_lock:
            old = SESSION.client
            SESSION.client = self
        if old:
            try:
                old.ws_close()
            except Exception:
                pass
        log("terminal attached")
        SESSION.redraw()
        try:
            while True:
                op, payload = self.ws_recv()
                if op is None or op == 0x8:
                    break
                if op == 0x9:
                    self.ws_send(payload, opcode=0xA)
                    continue
                if op == 0x1:  # text: control messages (resize)
                    try:
                        msg = json.loads(payload.decode("utf-8", "replace"))
                        if msg.get("type") == "resize":
                            SESSION.resize(msg.get("rows", 30), msg.get("cols", 100))
                            continue
                    except Exception:
                        pass
                    SESSION.write(payload)
                elif op == 0x2:  # binary: keystrokes
                    SESSION.write(payload)
                if not SESSION.alive():
                    break
        except Exception:
            pass
        finally:
            with SESSION.client_lock:
                if SESSION.client is self:
                    SESSION.client = None
            log("terminal detached (Crescent.py keeps running)")

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.rfile.read(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def ws_recv(self):
        try:
            b1, b2 = self._read_exact(2)
        except Exception:
            return None, b""
        op = b1 & 0x0F
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if b2 & 0x80 else b"\0\0\0\0"
        data = bytearray(self._read_exact(n))
        for i in range(n):
            data[i] ^= mask[i % 4]
        return op, bytes(data)

    def ws_send(self, data, binary=True, opcode=None):
        op = opcode if opcode is not None else (0x2 if binary else 0x1)
        n = len(data)
        if n < 126:
            hdr = struct.pack(">BB", 0x80 | op, n)
        elif n < 65536:
            hdr = struct.pack(">BBH", 0x80 | op, 126, n)
        else:
            hdr = struct.pack(">BBQ", 0x80 | op, 127, n)
        with self._send_lock:
            self.wfile.write(hdr + data)
            self.wfile.flush()

    def ws_close(self):
        try:
            self.ws_send(b"\x03\xe8", opcode=0x8)
        except Exception:
            pass
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass


# ── Agent tool: Python syntax check (parses only — never runs the code) ─────
def py_check(body):
    import ast, warnings
    src = body.decode("utf-8", "replace")
    if len(src) > 200_000:
        return {"ok": False, "error": "too large"}
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        try:
            ast.parse(src)
        except SyntaxError as e:
            return {"ok": True, "valid": False, "error": f"{type(e).__name__}: {e.msg}", "line": e.lineno, "col": e.offset,
                    "text": (e.text or "").rstrip()}
    return {"ok": True, "valid": True, "warnings": [str(x.message) for x in w][:5]}


# ── Automatic backups (Agentic sends one a day) ─────────────────────────────
BACKUP_DIR = os.path.expanduser(os.environ.get("AGENTIC_BACKUP_DIR", "~/Agentic/backups"))
BACKUP_KEEP = 14


def save_backup(body):
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("agenticBackup"):
        return {"ok": False, "error": "not an Agentic backup"}
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, "agentic-backup-" + time.strftime("%Y-%m-%d") + ".json")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, path)
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("agentic-backup-") and f.endswith(".json"))
    for old in files[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except Exception:
            pass
    log(f"saved automatic backup → {path}")
    return {"ok": True, "path": path, "kept": min(len(files), BACKUP_KEEP)}


# ── Start automatically at login:  python3 crescent_server.py --install ─────
SERVICE = "agentic-server"


def _cmdline():
    return [sys.executable, os.path.abspath(__file__)] + ([CRESCENT_PY] if os.path.isfile(CRESCENT_PY) else [])


def install_autostart(remove=False):
    home = os.path.expanduser("~")
    cmd = _cmdline()
    path_env = os.environ.get("PATH", "/usr/bin:/bin")
    if os.environ.get("TERMUX_VERSION"):
        boot = os.path.join(home, ".termux", "boot", SERVICE + ".sh")
        if remove:
            if os.path.exists(boot): os.remove(boot)
            return "removed Termux boot script"
        os.makedirs(os.path.dirname(boot), exist_ok=True)
        with open(boot, "w") as f:
            f.write("#!/data/data/com.termux/files/usr/bin/sh\ntermux-wake-lock\n" + " ".join(f"'{c}'" for c in cmd) + " >/dev/null 2>&1 &\n")
        os.chmod(boot, 0o755)
        return f"installed {boot} — it starts at boot (needs the Termux:Boot app); for now, run it normally"
    if sys.platform == "darwin":
        plist = os.path.join(home, "Library", "LaunchAgents", "com.agentic." + SERVICE + ".plist")
        if remove:
            subprocess.run(["launchctl", "unload", "-w", plist], capture_output=True)
            if os.path.exists(plist): os.remove(plist)
            return "removed LaunchAgent"
        os.makedirs(os.path.dirname(plist), exist_ok=True)
        args = "".join(f"<string>{c}</string>" for c in cmd)
        with open(plist, "w") as f:
            f.write(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.agentic.{SERVICE}</string>
<key>ProgramArguments</key><array>{args}</array>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>{path_env}</string></dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
''')
        subprocess.run(["launchctl", "load", "-w", plist], capture_output=True)
        return f"installed and started {plist}"
    if shutil.which("systemctl") and subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True).returncode == 0:
        unit = os.path.join(home, ".config", "systemd", "user", SERVICE + ".service")
        if remove:
            subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE], capture_output=True)
            if os.path.exists(unit): os.remove(unit)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            return "removed systemd user service"
        os.makedirs(os.path.dirname(unit), exist_ok=True)
        with open(unit, "w") as f:
            f.write(f"""[Unit]
Description=Agentic server (web, voice, Crescent bridge, browser proxy)
After=network.target sound.target

[Service]
ExecStart={" ".join(cmd)}
WorkingDirectory={os.path.dirname(os.path.abspath(__file__))}
Environment=PATH={path_env}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
""")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        r = subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE], capture_output=True, text=True)
        if r.returncode != 0:
            return "wrote " + unit + " but couldn't enable it: " + (r.stderr.strip() or "unknown error")
        return f"installed and started systemd user service '{SERVICE}' (check: systemctl --user status {SERVICE})"
    desk = os.path.join(home, ".config", "autostart", SERVICE + ".desktop")
    if remove:
        if os.path.exists(desk): os.remove(desk)
        return "removed autostart entry"
    os.makedirs(os.path.dirname(desk), exist_ok=True)
    with open(desk, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Crescent server\nExec=" + " ".join(cmd) + "\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n")
    return f"installed {desk} — it'll start next time you log in"




# ════════════════════════════════════════════════════════════════════════════
#  Browser proxy  (was browser_proxy.py)  - port 47723
#    /page /raw /read /search /yt /embed - see the Browser > Settings page
# ════════════════════════════════════════════════════════════════════════════
PROXY_PORT = int(os.environ.get('AGENTIC_PROXY_PORT') or 47723)
ALLOW_PRIVATE = os.environ.get('AGENTIC_PROXY_ALLOW_PRIVATE') == '1'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/126.0.0.0 Safari/537.36')
MAX_BYTES = 40 * 1024 * 1024

# ───────────────────────── networking ─────────────────────────

def host_is_public(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split('%')[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def check_url(url):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ('http', 'https') or not p.hostname:
        raise ValueError('only http(s) addresses are supported')
    if not ALLOW_PRIVATE and not host_is_public(p.hostname):
        raise ValueError('blocked: %s is a private/local address' % p.hostname)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR), SafeRedirect)


def fetch(url, method='GET', body=None, headers=None, timeout=20):
    """-> (status, headers, bytes, final_url)"""
    check_url(url)
    h = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
    }
    if headers:
        h.update(headers)
    host = urllib.parse.urlparse(url).hostname or ''
    if host.endswith('youtube.com') and 'Cookie' not in h:
        h['Cookie'] = 'CONSENT=YES+1; SOCS=CAI'
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        r = OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        r = e
    data = r.read(MAX_BYTES + 1)
    enc = (r.headers.get('Content-Encoding') or '').lower()
    try:
        if 'gzip' in enc:
            data = gzip.decompress(data)
        elif 'deflate' in enc:
            try:
                data = zlib.decompress(data)
            except zlib.error:
                data = zlib.decompress(data, -15)
    except Exception:
        pass
    status = getattr(r, 'status', None) or getattr(r, 'code', 200)
    return status, r.headers, data, (r.geturl() or url)


def decode(data, headers):
    m = re.search(r'charset=["\']?([\w\-]+)', headers.get('Content-Type', '') or '', re.I)
    cs = m.group(1) if m else None
    if not cs:
        m = re.search(rb'<meta[^>]+charset=["\']?([\w\-]+)', data[:4096], re.I)
        cs = m.group(1).decode('ascii', 'ignore') if m else 'utf-8'
    try:
        return data.decode(cs, 'replace')
    except LookupError:
        return data.decode('utf-8', 'replace')


def is_html(headers, data):
    ct = (headers.get('Content-Type') or '').lower()
    if 'html' in ct:
        return True
    if not ct:
        return data[:512].lstrip().lower().startswith((b'<!doctype html', b'<html'))
    return False


def origin_ok(origin):
    if origin == 'null':
        return True
    try:
        host = urllib.parse.urlparse(origin).hostname or ''
    except Exception:
        return False
    if host in ('localhost',) or host.endswith('.local'):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False

# ───────────────────────── page rewriting ─────────────────────────

INJECT = r"""
(function(){
var P=location.origin,B=__B__,F=__F__,N=__N__;
function abs(u){try{return new URL(u,document.baseURI).href}catch(e){return u}}
function mine(a){return a.indexOf(P+'/')===0}
function viaRaw(u){var a=abs(String(u));return(/^https?:/i.test(a)&&!mine(a))?P+'/raw?url='+encodeURIComponent(a):u}
function page(u){return P+'/page?n='+encodeURIComponent(N)+'&url='+encodeURIComponent(u)}
function tell(o){o.ab=1;o.n=N;try{parent.postMessage(o,'*')}catch(e){}}
function nav(u){if(/^https?:/i.test(u))location.href=page(u)}
// Agentic's AI agent: look / click / type on this page. Only the Agentic app itself
// (the parent window, served from this computer) may send commands.
addEventListener('message',function(e){
 var d=e.data; if(!d||d.abAgent!==1||e.source!==parent)return;
 if(!/^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$/i.test(e.origin))return;
 var r; try{r=(0,eval)(String(d.code))}catch(x){r={ok:false,msg:'Page script failed: '+x.message}}
 try{r=JSON.parse(JSON.stringify(r))}catch(x){r={ok:false,msg:'Bad result'}}
 try{parent.postMessage({abAgentReply:1,id:d.id,result:r},e.origin)}catch(x){}
});
try{
 var of=window.fetch;
 window.fetch=function(i,o){
  try{ if(typeof i==='string'||i instanceof URL)i=viaRaw(String(i)); else if(i&&i.url)i=new Request(viaRaw(i.url),i);}catch(e){}
  return of.call(this,i,o)};
 var xo=XMLHttpRequest.prototype.open;
 XMLHttpRequest.prototype.open=function(m,u){try{arguments[1]=viaRaw(String(u))}catch(e){}return xo.apply(this,arguments)};
}catch(e){}
['pushState','replaceState'].forEach(function(k){var o=history[k];history[k]=function(){try{return o.apply(this,arguments)}catch(e){}}});
window.open=function(u){if(u)nav(abs(u));return null};
document.addEventListener('click',function(e){
 if(e.defaultPrevented||e.button>0)return;
 var a=e.target&&e.target.closest?e.target.closest('a[href],area[href]'):null; if(!a)return;
 var h=(a.getAttribute('href')||'').trim(); if(!h)return;
 if(h.charAt(0)==='#'){e.preventDefault();var id=decodeURIComponent(h.slice(1));
  var t=id&&(document.getElementById(id)||document.getElementsByName(id)[0]);
  if(t)t.scrollIntoView();else if(!id)scrollTo(0,0);return}
 if(/^(javascript:|mailto:|tel:|data:|blob:)/i.test(h))return;
 var u=abs(a.href); if(!/^https?:/i.test(u))return;
 e.preventDefault();
 if(e.ctrlKey||e.metaKey||e.shiftKey)tell({type:'newtab',url:u});else nav(u);
},true);
document.addEventListener('submit',function(e){
 var f=e.target; if(!f||f.tagName!=='FORM'||e.defaultPrevented)return;
 var act=abs(f.getAttribute('action')||B),m=(f.getAttribute('method')||'get').toLowerCase();
 if(m==='post'){f.setAttribute('action',page(act));f.setAttribute('target','_self');return}
 e.preventDefault();
 var q;try{q=new URLSearchParams(new FormData(f,e.submitter||undefined)).toString()}
 catch(x){q=new URLSearchParams(new FormData(f)).toString()}
 nav(act.split('#')[0].split('?')[0]+(q?'?'+q:''));
},true);
function report(){tell({type:'nav',url:F,title:document.title})}
function refresh(){var m=document.querySelector('meta[http-equiv="refresh" i]');if(!m)return;
 var c=(m.getAttribute('content')||'').match(/^\s*(\d+)\s*;\s*url\s*=\s*['"]?([^'"]+)/i);
 if(c)setTimeout(function(){nav(abs(c[2]))},Math.min(+c[1],10)*1000)}
if(document.readyState==='complete'){report();refresh()}
else addEventListener('load',function(){report();refresh()});
setTimeout(report,1500);
})();
"""


def js_str(s):
    return json.dumps(s).replace('</', '<\\/')


def make_page(text, final, nonce):
    text = re.sub(r'<meta[^>]+http-equiv=["\']?content-security-policy[^>]*>', '', text, flags=re.I)
    base = final
    m = re.search(r'<base[^>]+href=["\']([^"\']+)["\'][^>]*>', text, re.I)
    if m:
        base = urllib.parse.urljoin(final, m.group(1))
        text = re.sub(r'<base\b[^>]*>', '', text, flags=re.I)
    script = INJECT.replace('__B__', js_str(base)).replace('__F__', js_str(final)).replace('__N__', js_str(nonce))
    block = ('<base href="%s"><meta name="referrer" content="no-referrer"><script>%s</script>'
             % (html.escape(base, quote=True), script))
    m = re.search(r'<head(?:\s[^>]*)?>', text, re.I) or re.search(r'<html(?:\s[^>]*)?>', text, re.I)
    if m:
        return text[:m.end()] + block + text[m.end():]
    return block + text

# ───────────────────────── html -> markdown ─────────────────────────

class MD(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'svg', 'iframe', 'head', 'nav', 'footer', 'aside',
            'form', 'template', 'button', 'select', 'canvas', 'audio', 'video'}
    BLOCK = {'p', 'div', 'section', 'article', 'main', 'header', 'ul', 'ol', 'table', 'tr',
             'blockquote', 'figure', 'figcaption', 'dl', 'dt', 'dd', 'details', 'summary'}

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.out, self.skip, self.pre, self.links = base, [], 0, 0, []

    def nl(self, n=1):
        tail = ''.join(self.out[-3:])
        have = len(tail) - len(tail.rstrip('\n'))
        if not tail:
            return
        self.out.append('\n' * max(0, n - have))

    def url(self, u):
        u = (u or '').strip()
        if not u or u.startswith(('javascript:', 'mailto:', 'tel:', '#', 'data:')):
            return ''
        return urllib.parse.urljoin(self.base, u)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if re.fullmatch(r'h[1-6]', tag):
            self.nl(2); self.out.append('#' * int(tag[1]) + ' ')
        elif tag in self.BLOCK:
            self.nl(2 if tag in ('p', 'blockquote', 'table', 'ul', 'ol') else 1)
            if tag == 'blockquote':
                self.out.append('> ')
        elif tag == 'br':
            self.out.append('\n')
        elif tag == 'hr':
            self.nl(2); self.out.append('---'); self.nl(2)
        elif tag == 'li':
            self.nl(1); self.out.append('- ')
        elif tag in ('td', 'th'):
            self.out.append(' ')
        elif tag == 'pre':
            self.nl(2); self.out.append('```\n'); self.pre += 1
        elif tag == 'code' and not self.pre:
            self.out.append('`')
        elif tag in ('strong', 'b'):
            self.out.append('**')
        elif tag == 'a':
            self.links.append((len(self.out), self.url(a.get('href'))))
        elif tag == 'img':
            src = self.url(a.get('src') or a.get('data-src') or a.get('data-original'))
            if src:
                alt = (a.get('alt') or '').replace(']', ' ').replace('[', ' ').strip()
                self.out.append(' ![%s](%s) ' % (alt, src))

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if re.fullmatch(r'h[1-6]', tag) or tag in self.BLOCK:
            self.nl(2 if tag in ('p', 'blockquote', 'table', 'ul', 'ol') or tag[0] == 'h' else 1)
        elif tag == 'pre':
            self.pre = max(0, self.pre - 1); self.nl(1); self.out.append('```'); self.nl(2)
        elif tag == 'code' and not self.pre:
            self.out.append('`')
        elif tag in ('strong', 'b'):
            self.out.append('**')
        elif tag == 'a' and self.links:
            idx, href = self.links.pop()
            seg = ''.join(self.out[idx:])
            del self.out[idx:]
            if href and seg.strip():
                self.out.append('[%s](%s)' % (seg.strip().replace('\n', ' '), href))
            else:
                self.out.append(seg)

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre:
            self.out.append(data)
        else:
            t = re.sub(r'\s+', ' ', data)
            if t.strip() or (self.out and not self.out[-1].endswith((' ', '\n'))):
                self.out.append(t)


def to_markdown(text, base):
    m = re.search(r'<title[^>]*>(.*?)</title>', text, re.I | re.S)
    title = html.unescape(re.sub(r'\s+', ' ', m.group(1))).strip() if m else ''
    p = MD(base)
    try:
        p.feed(text); p.close()
    except Exception:
        pass
    body = re.sub(r'[ \t]+\n', '\n', ''.join(p.out))
    body = re.sub(r'\n{3,}', '\n\n', body).strip()
    return title, body

# ───────────────────────── search ─────────────────────────

def clean(s):
    return html.unescape(re.sub(r'<[^>]+>', '', s or '')).strip()


def host_short(u):
    try:
        return (urllib.parse.urlparse(u).hostname or '').replace('www.', '')
    except Exception:
        return ''


TIME_DDG = {'d': 'd', 'w': 'w', 'm': 'm', 'y': 'y'}
TIME_BING = {'d': 'ex1%3a%22ez1%22', 'w': 'ex1%3a%22ez2%22', 'm': 'ex1%3a%22ez3%22'}
TIME_BING_NEWS = {'d': 'interval%3d%227%22', 'w': 'interval%3d%228%22', 'm': 'interval%3d%229%22'}


def search_ddg(q, t=''):
    df = ('&df=' + TIME_DDG[t]) if t in TIME_DDG else ''
    _, h, data, _ = fetch('https://html.duckduckgo.com/html/?q=%s&kl=us-en%s' % (urllib.parse.quote(q), df))
    page = decode(data, h)
    if 'anomaly-modal' in page or 'bots use DuckDuckGo' in page:
        raise ValueError('DuckDuckGo asked for a bot check')
    out = []
    for m in re.finditer(r'<a\b([^>]*class="[^"]*result__a[^"]*"[^>]*)>(.*?)</a>', page, re.S):
        hm = re.search(r'href="([^"]+)"', m.group(1))
        if not hm:
            continue
        url = html.unescape(hm.group(1))
        if url.startswith('//'):
            url = 'https:' + url
        if 'duckduckgo.com/y.js' in url:
            continue  # ad
        um = re.search(r'[?&]uddg=([^&]+)', url)
        if um:
            url = urllib.parse.unquote(um.group(1))
        if not url.startswith('http'):
            continue
        sm = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', page[m.end():m.end() + 3000], re.S)
        out.append({'title': clean(m.group(2)), 'url': url, 'snippet': clean(sm.group(1))[:320] if sm else '',
                    'shown': host_short(url)})
    return out


def bing_unwrap(u):
    try:
        p = urllib.parse.urlparse(u)
        if p.hostname and p.hostname.endswith('bing.com') and p.path.startswith('/ck/a'):
            v = urllib.parse.parse_qs(p.query).get('u', [''])[0]
            if v.startswith('a1'):
                v = v[2:]
                return base64.urlsafe_b64decode(v + '=' * (-len(v) % 4)).decode('utf-8', 'replace')
    except Exception:
        pass
    return u


def search_bing(q, t=''):
    if t == 'y':
        import datetime
        today = (datetime.date.today() - datetime.date(1970, 1, 1)).days
        f = '&filters=ex1%%3a%%22ez5_%d_%d%%22' % (today - 365, today)
    else:
        f = ('&filters=' + TIME_BING[t]) if t in TIME_BING else ''
    _, h, data, _ = fetch('https://www.bing.com/search?q=%s&setlang=en-US&cc=us%s' % (urllib.parse.quote(q), f))
    page = decode(data, h)
    out = []
    for blk in re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', page, re.S):
        a = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if not a:
            continue
        url = bing_unwrap(html.unescape(a.group(1)))
        if not url.startswith('http'):
            continue
        p = re.search(r'<p[^>]*>(.*?)</p>', blk, re.S)
        out.append({'title': clean(a.group(2)), 'url': url, 'snippet': clean(p.group(1))[:320] if p else '',
                    'shown': host_short(url)})
    return out


def _relevant(rows, q):
    # DuckDuckGo sometimes answers a throttled request with results for only
    # part of the query; treat results that miss every later word as a miss.
    words = [w for w in re.findall(r'\w+', q.lower()) if len(w) > 2][1:]
    if not words:
        return bool(rows)
    for r in rows[:6]:
        t = (r.get('title', '') + ' ' + r.get('snippet', '') + ' ' + r.get('url', '')).lower()
        if any(w in t for w in words):
            return True
    return False


def search(q, engine, t=''):
    # Bing first (reliable from a home connection), DuckDuckGo as the backup.
    order = [search_bing, search_ddg]
    rows = []
    for fn in order:
        try:
            rows = fn(q, t)
        except Exception:
            rows = []
        if rows and _relevant(rows, q):
            return rows
    return rows or []


def news_search(q, t=''):
    """Bing News RSS -> [{title, url, src, t, snippet, thumb}]"""
    f = ('&qft=' + TIME_BING_NEWS[t]) if t in TIME_BING_NEWS else ''
    _, h, data, _ = fetch('https://www.bing.com/news/search?q=%s&format=rss&setlang=en-US&cc=us%s' % (urllib.parse.quote(q), f))
    page = decode(data, h)
    out = []
    for item in re.findall(r'<item>(.*?)</item>', page, re.S):
        def tag(name):
            m = re.search(r'<%s[^>]*>(.*?)</%s>' % (name, name), item, re.S)
            v = m.group(1) if m else ''
            v = re.sub(r'^<!\[CDATA\[|\]\]>$', '', v.strip())
            return html.unescape(v).strip()
        link = tag('link')
        try:
            pq = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
            if pq.get('url'):
                link = pq['url'][0]
        except Exception:
            pass
        if not link.startswith('http'):
            continue
        thumb = tag('News:Image')
        if thumb:
            thumb = thumb.replace('&amp;', '&').replace('http://', 'https://', 1)
            thumb += ('&' if '?' in thumb else '?') + 'w=240&h=160&c=7'
        ts = 0
        try:
            ts = int(email.utils.parsedate_to_datetime(tag('pubDate')).timestamp() * 1000)
        except Exception:
            pass
        out.append({'title': clean(tag('title')), 'url': link, 'src': clean(tag('News:Source')),
                    'snippet': clean(tag('description'))[:300], 'thumb': thumb, 't': ts})
    return out


def walk(o, key, acc):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key and isinstance(v, dict):
                acc.append(v)
            else:
                walk(v, key, acc)
    elif isinstance(o, list):
        for v in o:
            walk(v, key, acc)


def yt_search(q):
    _, h, data, _ = fetch('https://www.youtube.com/results?search_query=%s&sp=EgIQAQ%%253D%%253D' % urllib.parse.quote(q))
    page = decode(data, h)
    i = page.find('ytInitialData')
    j = page.find('{', i) if i >= 0 else -1
    if j < 0:
        raise ValueError('YouTube returned no data')
    obj, _ = json.JSONDecoder().raw_decode(page[j:])
    vids = []
    walk(obj, 'videoRenderer', vids)
    items = []
    for v in vids:
        vid = v.get('videoId')
        if not vid:
            continue
        dur = 0
        t = (v.get('lengthText') or {}).get('simpleText') or ''
        if re.fullmatch(r'(\d+:)*\d+', t):
            for part in t.split(':'):
                dur = dur * 60 + int(part)
        title = ''.join(r.get('text', '') for r in (v.get('title') or {}).get('runs', []))
        owner = ''.join(r.get('text', '') for r in (v.get('ownerText') or {}).get('runs', []))
        items.append({'url': '/watch?v=' + vid, 'title': title, 'duration': dur, 'uploaderName': owner,
                      'uploadedDate': (v.get('publishedTimeText') or {}).get('simpleText', ''),
                      'thumbnail': 'https://i.ytimg.com/vi/%s/mqdefault.jpg' % vid})
        if len(items) >= 20:
            break
    return {'items': items}

# ───────────────────────── full YouTube pages ─────────────────────────

def _yt_obj(page, marker):
    i = page.find(marker)
    if i < 0:
        return None
    j = page.find('{', i)
    if j < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(page[j:])
        return obj
    except ValueError:
        return None


def _yt_text(o):
    if not isinstance(o, dict):
        return ''
    if 'simpleText' in o:
        return o['simpleText']
    if 'content' in o and isinstance(o['content'], str):
        return o['content']
    return ''.join(r.get('text', '') for r in o.get('runs', []) if isinstance(r, dict))


def _dur_secs(t):
    t = (t or '').strip()
    if not re.fullmatch(r'(\d+:)*\d+', t):
        return 0
    n = 0
    for part in t.split(':'):
        n = n * 60 + int(part)
    return n


def _first(o, key):
    acc = []
    walk(o, key, acc)
    return acc[0] if acc else None


def _yt_items(d, limit=40):
    """videoRenderer / gridVideoRenderer / lockupViewModel -> client video items"""
    out, seen = [], set()

    def add(vid, title, dur, owner, when, views):
        if not vid or vid in seen or not title:
            return
        seen.add(vid)
        out.append({'url': '/watch?v=' + vid, 'title': title, 'duration': dur, 'uploaderName': owner,
                    'uploadedDate': when, 'views': views,
                    'thumbnail': 'https://i.ytimg.com/vi/%s/mqdefault.jpg' % vid})

    def visit(o):
        if len(out) >= limit:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ('videoRenderer', 'gridVideoRenderer', 'compactVideoRenderer') and isinstance(v, dict):
                    add(v.get('videoId'), _yt_text(v.get('title')),
                        _dur_secs(_yt_text(v.get('lengthText'))),
                        _yt_text(v.get('ownerText') or v.get('shortBylineText') or v.get('longBylineText')),
                        _yt_text(v.get('publishedTimeText')),
                        _yt_text(v.get('shortViewCountText') or v.get('viewCountText')))
                elif k == 'lockupViewModel' and isinstance(v, dict):
                    if v.get('contentType') not in (None, 'LOCKUP_CONTENT_TYPE_VIDEO'):
                        continue
                    meta = ((v.get('metadata') or {}).get('lockupMetadataViewModel') or {})
                    rows = (((meta.get('metadata') or {}).get('contentMetadataViewModel') or {}).get('metadataRows') or [])
                    parts = [[_yt_text(p.get('text')) for p in (r.get('metadataParts') or []) if isinstance(p, dict)] for r in rows]
                    owner = parts[0][0] if parts and parts[0] else ''
                    rest = parts[1] if len(parts) > 1 else []
                    badge = _first(v.get('contentImage') or {}, 'thumbnailBadgeViewModel') or {}
                    views = rest[0] if rest else ''
                    if views and re.fullmatch(r'[\d.,]+[KMB]?', views):
                        views += ' views'
                    add(v.get('contentId'), _yt_text(meta.get('title')), _dur_secs(badge.get('text')), owner,
                        rest[1] if len(rest) > 1 else '', views)
                else:
                    visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)
    visit(d)
    return out


def yt_watch(v):
    if not re.fullmatch(r'[\w-]{6,20}', v or ''):
        raise ValueError('bad video id')
    _, h, data, _ = fetch('https://www.youtube.com/watch?v=%s&hl=en&gl=US' % v)
    page = decode(data, h)
    pr = _yt_obj(page, 'var ytInitialPlayerResponse = ') or {}
    d = _yt_obj(page, 'var ytInitialData = ') or {}
    vd = pr.get('videoDetails') or {}
    mf = ((pr.get('microformat') or {}).get('playerMicroformatRenderer') or {})
    owner = _first(d, 'videoOwnerRenderer') or {}
    thumbs = ((owner.get('thumbnail') or {}).get('thumbnails') or [])
    chan_url = (((owner.get('navigationEndpoint') or {}).get('browseEndpoint') or {}).get('canonicalBaseUrl') or '')
    prim = _first(d, 'videoPrimaryInfoRenderer') or {}
    related = [x for x in _yt_items(d.get('contents', {}).get('twoColumnWatchNextResults', {}).get('secondaryResults', d), 30)
               if not x['url'].endswith(v)]
    return {
        'id': v,
        'title': vd.get('title') or _yt_text(prim.get('title')),
        'channel': vd.get('author') or _yt_text(owner.get('title')),
        'channelUrl': ('https://www.youtube.com' + chan_url) if chan_url else '',
        'channelThumb': thumbs[-1]['url'] if thumbs else '',
        'subs': _yt_text(owner.get('subscriberCountText')),
        'views': int(vd.get('viewCount') or 0),
        'likes': int(mf.get('likeCount') or 0),
        'date': mf.get('publishDate') or mf.get('uploadDate') or '',
        'relDate': _yt_text(prim.get('relativeDateText')),
        'description': vd.get('shortDescription') or '',
        'length': int(vd.get('lengthSeconds') or 0),
        'live': bool(vd.get('isLiveContent')),
        'related': related,
    }


YT_FEEDS = {
    'news': 'https://www.youtube.com/channel/UCYfdidRxbB8Qhf0Nx7ioOYw',
    'music': 'https://www.youtube.com/channel/UC-9-kyTW8ZkZNDHQJ6FgpwQ',
    'gaming': 'https://www.youtube.com/channel/UCOpNcN46UbXVtpKMrmU4Abg',
    'sports': 'https://www.youtube.com/channel/UCEgdi0XIXXZ-qJOFPf4JSKw',
}


def yt_feed(kind):
    url = YT_FEEDS.get(kind)
    if not url:
        raise ValueError('unknown feed')
    _, h, data, _ = fetch(url + '?hl=en&gl=US')
    d = _yt_obj(decode(data, h), 'var ytInitialData = ') or {}
    return {'items': _yt_items(d, 24)}


YT_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fff;--ink:#0f0f0f;--mute:#606060;--chip:#f2f2f2;--chip2:#e5e5e5;--line:#e5e5e5}
:root.dark{--bg:#0f0f0f;--ink:#f1f1f1;--mute:#aaa;--chip:#272727;--chip2:#3f3f3f;--line:#3f3f3f}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);font:14px Roboto,Arial,sans-serif}
a{color:inherit;text-decoration:none}
.top{display:flex;justify-content:center;padding:10px 16px 0}
.top form{display:flex;width:min(640px,100%)}
.top input{flex:1;min-width:0;height:40px;border:1px solid var(--line);border-right:0;border-radius:40px 0 0 40px;padding:0 16px;font:16px Roboto,Arial,sans-serif;background:var(--bg);color:var(--ink);outline:none}
.top input:focus{border-color:#1c62b9}
.top button{width:64px;height:40px;border:1px solid var(--line);border-radius:0 40px 40px 0;background:var(--chip);color:var(--ink);cursor:pointer}
.wrap{display:flex;gap:24px;padding:20px 24px 40px;max-width:1750px;margin:0 auto}
.main{flex:1;min-width:0}
.player{position:relative;width:100%;aspect-ratio:16/9;background:#000;border-radius:12px;overflow:hidden}
.player iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
h1{font-size:20px;line-height:28px;font-weight:700;margin:12px 0 8px;word-break:break-word}
.row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.ch{display:flex;align-items:center;gap:12px;min-width:0;margin-right:12px}
.ch img{width:40px;height:40px;border-radius:50%;background:var(--chip)}
.ch b{display:block;font-size:16px;font-weight:500}
.ch small{color:var(--mute);font-size:12px}
.pill{display:inline-flex;align-items:center;gap:6px;height:36px;padding:0 16px;border-radius:18px;background:var(--chip);font-weight:500;cursor:pointer;border:0;color:var(--ink);font-size:14px}
.pill:hover{background:var(--chip2)}
.pill svg{width:20px;height:20px}
.sub{background:var(--ink);color:var(--bg)}
.sub:hover{background:var(--ink);opacity:.85}
.grow{flex:1}
.desc{background:var(--chip);border-radius:12px;padding:12px;margin-top:12px;cursor:pointer;white-space:pre-wrap;word-break:break-word;line-height:20px}
.desc b{display:block;margin-bottom:4px}
.desc.short .txt{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.desc .more{font-weight:500;margin-top:4px}
.side{width:402px;flex-shrink:0}
.side h2{font-size:16px;margin:0 0 12px}
.rv{display:flex;gap:8px;margin-bottom:8px;cursor:pointer;border-radius:8px}
.rv .th{position:relative;flex:0 0 168px;height:94px;border-radius:8px;overflow:hidden;background:var(--chip)}
.rv img{width:100%;height:100%;object-fit:cover;display:block}
.rv .d{position:absolute;right:4px;bottom:4px;background:rgba(0,0,0,.8);color:#fff;font-size:12px;font-weight:500;padding:1px 4px;border-radius:4px}
.rv .t{font-size:14px;font-weight:500;line-height:20px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:4px}
.rv .m{color:var(--mute);font-size:12px;line-height:18px}
.rv:hover .t{opacity:.85}
@media (max-width:1000px){.wrap{flex-direction:column;padding:12px}.side{width:100%}}
@media (max-width:520px){.rv .th{flex-basis:140px;height:79px}h1{font-size:18px}}
</style></head><body>
<div class="top"><form id="sf"><input name="q" placeholder="Search YouTube" autocomplete="off"><button type="submit" aria-label="Search"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></button></form></div>
<div class="wrap"><div class="main">
<div class="player"><iframe src="__SRC__" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share; fullscreen" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>
<div id="info"></div></div><aside class="side" id="side"></aside></div>
<script>
var D=__DATA__,N=__N__,V=__V__;
if(__DARK__)document.documentElement.classList.add('dark');
function esc(t){return String(t==null?'':t).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function tell(o){o.ab=1;o.n=N;try{parent.postMessage(o,'*')}catch(e){}}
function num(n){if(n>=1e9)return(n/1e9).toFixed(1).replace(/\.0$/,'')+'B';if(n>=1e6)return(n/1e6).toFixed(1).replace(/\.0$/,'')+'M';if(n>=1e3)return(n/1e3).toFixed(n>=1e4?0:1).replace(/\.0$/,'')+'K';return String(n)}
function dur(s){s=+s||0;if(!s)return'';var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return(h?h+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0')}
function fdate(d){if(!d)return'';var t=new Date(/^\d{4}-\d\d-\d\d$/.test(d)?d+'T12:00:00':d);return isNaN(t)?d:t.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'})}
var link=function(u){return 'https://www.youtube.com'+u};
if(D){
 var i=document.getElementById('info'),like='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M7 11v9H4v-9zM7 11l4-8c1.5 0 2.5 1 2.5 2.5V9h5a2 2 0 0 1 2 2.3l-1.2 7A2 2 0 0 1 17.3 20H7"/></svg>';
 i.innerHTML='<h1>'+esc(D.title)+'</h1><div class="row"><a class="ch" href="'+esc(D.channelUrl||'#')+'" data-ext="1">'+(D.channelThumb?'<img src="'+esc(D.channelThumb)+'" alt="" referrerpolicy="no-referrer">':'')+'<span><b>'+esc(D.channel)+'</b><small>'+esc(D.subs)+'</small></span></a>'+
  (D.channelUrl?'<a class="pill sub" href="'+esc(D.channelUrl)+'" data-ext="1">Subscribe</a>':'')+'<span class="grow"></span>'+(D.likes?'<span class="pill">'+like+num(D.likes)+'</span>':'')+
  '<a class="pill" href="https://www.youtube.com/watch?v='+esc(D.id)+'" data-ext="1">Open on YouTube</a></div>'+
  '<div class="desc short" id="desc"><b>'+(D.views?D.views.toLocaleString()+' views':'')+(D.date?' &nbsp;'+esc(fdate(D.date)):'')+'</b><div class="txt">'+esc(D.description||'No description.')+'</div><div class="more">...more</div></div>';
 var de=document.getElementById('desc');de.onclick=function(e){if(e.target.closest('a'))return;de.classList.toggle('short');de.querySelector('.more').textContent=de.classList.contains('short')?'...more':'Show less'};
 document.getElementById('side').innerHTML=(D.related&&D.related.length?'<h2>Up next</h2>':'')+(D.related||[]).map(function(x){return '<a class="rv" href="'+esc(link(x.url))+'" data-go="1"><div class="th"><img src="'+esc(x.thumbnail)+'" alt="" loading="lazy" referrerpolicy="no-referrer">'+(x.duration?'<span class="d">'+dur(x.duration)+'</span>':'')+'</div><div><div class="t">'+esc(x.title)+'</div><div class="m">'+esc(x.uploaderName)+'</div><div class="m">'+esc([x.views,x.uploadedDate].filter(Boolean).join(' • '))+'</div></div></a>'}).join('');
}
document.addEventListener('click',function(e){var a=e.target.closest('a');if(!a)return;var h=a.getAttribute('href');if(!h||h==='#'){e.preventDefault();return}
 e.preventDefault();if(a.hasAttribute('data-ext')){tell({type:'newtab',url:h});return}
 if(e.ctrlKey||e.metaKey||e.button===1)tell({type:'newtab',url:h});else tell({type:'go',url:h})},true);
document.getElementById('sf').onsubmit=function(e){e.preventDefault();var q=this.q.value.trim();if(q)tell({type:'go',url:'agentic://yt/'+encodeURIComponent(q)})};
tell({type:'nav',url:'https://www.youtube.com/watch?v='+V,title:(D&&D.title?D.title+' - YouTube':'YouTube')});
</script></body></html>"""


def yt_page(v, nonce, t='', lst='', dark=False, origin=''):
    if not re.fullmatch(r'[\w-]{6,20}', v or ''):
        raise ValueError('bad video id')
    try:
        data = yt_watch(v)
    except Exception:
        data = None
    src = 'https://www.youtube-nocookie.com/embed/%s?rel=0&playsinline=1&autoplay=1&origin=%s' % (v, urllib.parse.quote(origin, safe=''))
    if lst and re.fullmatch(r'[\w-]{6,80}', lst):
        src += '&list=' + lst
    if t and t.isdigit() and int(t) > 0:
        src += '&start=' + t
    title = (data or {}).get('title') or 'YouTube'
    return (YT_PAGE.replace('__TITLE__', html.escape(title + ' - YouTube'))
            .replace('__SRC__', html.escape(src, quote=True))
            .replace('__DARK__', 'true' if dark else 'false')
            .replace('__DATA__', js_str(data))
            .replace('__N__', js_str(nonce or ''))
            .replace('__V__', js_str(v)))

# ───────────────────────── http handler ─────────────────────────

def error_page(msg, url=''):
    return ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<body style="font:15px system-ui,sans-serif;max-width:560px;margin:12vh auto;padding:0 20px;color:#444">'
            '<h2 style="color:#111">Couldn\'t load this page</h2><p>%s</p><p style="word-break:break-all;color:#888;font-size:13px">%s</p>'
            '<p style="font-size:13px">Try reader view (the lines icon in the toolbar) or open it in a real tab.</p></body>'
            % (html.escape(msg), html.escape(url)))


STRIP = {'x-frame-options', 'content-security-policy', 'content-security-policy-report-only', 'content-length',
         'content-encoding', 'transfer-encoding', 'connection', 'keep-alive', 'set-cookie', 'strict-transport-security',
         'cross-origin-embedder-policy', 'cross-origin-opener-policy', 'cross-origin-resource-policy',
         'report-to', 'nel', 'alt-svc', 'content-range', 'accept-ranges', 'location', 'refresh'}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'AgenticProxy/1.0'

    def log_message(self, fmt, *args):
        pass

    # -- helpers
    def cors(self):
        o = self.headers.get('Origin')
        if o and origin_ok(o):
            self.send_header('Access-Control-Allow-Origin', o)
            self.send_header('Vary', 'Origin')

    def reply(self, code, body, ctype, extra=(), cors=False):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if cors:
            self.cors()
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200):
        self.reply(code, json.dumps(obj), 'application/json; charset=utf-8', cors=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    # -- routing
    def route(self, method):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        get = lambda k: (q.get(k) or [''])[0]
        body = None
        n = int(self.headers.get('Content-Length') or 0)
        if n:
            body = self.rfile.read(n)
        elif method == 'POST':
            body = b''
        path = u.path
        try:
            if path == '/health':
                self.json({'ok': True, 'name': 'agentic-browser-proxy', 'v': 1})
            elif path == '/page':
                self.page(method, get('url'), get('n'), body)
            elif path == '/raw':
                self.raw(method, get('url'), body)
            elif path == '/read':
                self.read(get('url'))
            elif path == '/search':
                self.json(search(get('q'), get('engine'), get('t')))
            elif path == '/news':
                self.json(news_search(get('q'), get('t')))
            elif path == '/yt':
                self.json(yt_search(get('q')))
            elif path == '/ytwatch':
                self.json(yt_watch(get('v')))
            elif path == '/ytfeed':
                self.json(yt_feed(get('kind')))
            elif path == '/ytpage':
                origin = 'http://' + (self.headers.get('Host') or '127.0.0.1:%d' % PROXY_PORT)
                self.reply(200, yt_page(get('v'), get('n'), get('t'), get('list'), get('dark') == '1', origin),
                           'text/html; charset=utf-8')
            elif path == '/embed':
                self.embed(get('v'), get('list'), get('t'))
            else:
                self.reply(404, 'not found', 'text/plain')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            msg = str(e) or e.__class__.__name__
            try:
                if path in ('/page', '/embed', '/ytpage'):
                    self.reply(502, error_page(msg, get('url')), 'text/html; charset=utf-8')
                else:
                    self.json({'error': msg}, 502)
            except Exception:
                pass

    def fwd_headers(self, target):
        h = {}
        for k in ('Content-Type', 'Accept', 'Authorization', 'X-Requested-With', 'Range'):
            v = self.headers.get(k)
            if v:
                h[k] = v
        p = urllib.parse.urlparse(target)
        site = '%s://%s' % (p.scheme, p.netloc)
        h['Referer'] = site + '/'
        if self.command == 'POST':
            h['Origin'] = site
        return h

    # -- endpoints
    def page(self, method, url, nonce, body):
        if url and not re.match(r'^[a-z][a-z0-9+.-]*:', url, re.I):
            url = 'http://' + url
        status, hdr, data, final = fetch(url, method, body, self.fwd_headers(url))
        if len(data) > MAX_BYTES:
            raise ValueError('file too large for the proxy')
        extra = [('X-Final-Url', final)]
        if is_html(hdr, data):
            out = make_page(decode(data, hdr), final, nonce).encode('utf-8')
            self.reply(status, out, 'text/html; charset=utf-8', extra)
        else:
            for k in ('Content-Disposition', 'Last-Modified', 'ETag'):
                if hdr.get(k):
                    extra.append((k, hdr[k]))
            self.reply(status, data, hdr.get('Content-Type') or 'application/octet-stream', extra)

    def raw(self, method, url, body):
        status, hdr, data, final = fetch(url, method, body, self.fwd_headers(url))
        extra = []
        for k in ('Content-Disposition', 'Last-Modified', 'ETag'):
            if hdr.get(k):
                extra.append((k, hdr[k]))
        self.reply(status, data, hdr.get('Content-Type') or 'application/octet-stream', extra)

    def read(self, url):
        status, hdr, data, final = fetch(url)
        if not is_html(hdr, data):
            name = urllib.parse.unquote(urllib.parse.urlparse(final).path.split('/')[-1]) or final
            txt = 'Title: %s\n\nURL Source: %s\n\nMarkdown Content:\n(This is a %s file, not a web page. Use "Open in a real browser tab" to view it.)' % (
                name, final, hdr.get('Content-Type', 'binary'))
        else:
            title, md = to_markdown(decode(data, hdr), final)
            txt = 'Title: %s\n\nURL Source: %s\n\nMarkdown Content:\n%s' % (title, final, md)
        self.reply(200, txt, 'text/plain; charset=utf-8', cors=True)

    def embed(self, v, lst, t):
        if v and not re.fullmatch(r'[\w-]{6,20}', v):
            raise ValueError('bad video id')
        if lst and not re.fullmatch(r'[\w-]{6,80}', lst):
            lst = ''
        if not v and not lst:
            raise ValueError('no video given')
        origin = 'http://' + (self.headers.get('Host') or '127.0.0.1:%d' % PROXY_PORT)
        src = 'https://www.youtube-nocookie.com/embed/%s?rel=0&playsinline=1&origin=%s' % (
            v or 'videoseries', urllib.parse.quote(origin, safe=''))
        if lst:
            src += '&list=' + lst
        if t and t.isdigit() and int(t) > 0:
            src += '&start=' + t
        page = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<style>html,body{margin:0;height:100%%;background:#000}iframe{border:0;width:100%%;height:100%%}</style>'
                '<iframe src="%s" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; '
                'picture-in-picture; web-share; fullscreen" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe>'
                % html.escape(src, quote=True))
        self.reply(200, page, 'text/html; charset=utf-8')




# ════════════════════════════════════════════════════════════════════════════
#  Small helpers shared by the voice services
# ════════════════════════════════════════════════════════════════════════════
class ServiceHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def send_bytes(self, code, body, ctype):
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reply(self, obj, code=200):
        self.send_bytes(code, json.dumps(obj).encode(), "application/json")

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_body(self, limit=200 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            return None
        return self.rfile.read(n)


def parse_multipart(body, ctype):
    """{field name: bytes} from a multipart/form-data body."""
    m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', ctype or "")
    if not m:
        return {}
    sep = b"--" + (m.group(1) or m.group(2)).encode()
    out = {}
    for part in body.split(sep):
        head, _, data = part.partition(b"\r\n\r\n")
        nm = re.search(rb'name="([^"]+)"', head)
        if not nm or not _:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        out[nm.group(1).decode("utf-8", "replace")] = data
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Kokoro TTS  (was kokoro_server.py)  - port 5050
# ════════════════════════════════════════════════════════════════════════════
KOKORO_DIR = os.path.expanduser(os.environ.get("AGENTIC_KOKORO_DIR", "~/VEO-x"))
KOKORO_ONNX = os.path.join(KOKORO_DIR, "kokoro-v1.0.onnx")
KOKORO_VOICES = os.path.join(KOKORO_DIR, "voices-v1.0.bin")
KOKORO_PORT = 5050
# Optional int8 model (AGENTIC_KOKORO_FAST=1). Smaller download, but on most CPUs it's
# actually slower than the full model (measured 6.9s vs 1.9s per sentence), so it's off by default.
KOKORO_FAST = os.environ.get("AGENTIC_KOKORO_FAST", "0") == "1"
KOKORO_ONNX_FAST = os.path.join(KOKORO_DIR, "kokoro-v1.0.int8.onnx")
KOKORO_URLS = {
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}
_kokoro = None
_kokoro_err = ""
_kokoro_lock = threading.Lock()
_kokoro_fetch_lock = threading.Lock()
_kokoro_cache = {}
_kokoro_progress = ""


def _kokoro_find(name):
    """Look for a Kokoro model file in the usual places, then (shallowly) under home."""
    home = os.path.expanduser("~")
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (KOKORO_DIR, here, os.getcwd(), os.path.join(home, "Downloads"), home,
              os.path.join(home, "kokoro"), os.path.join(home, "VEO-x"), os.path.join(home, ".cache", "kokoro")):
        f = os.path.join(d, name)
        if os.path.isfile(f) and os.path.getsize(f) > 1000000:
            return f
    base_depth = home.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(home):
        if root.count(os.sep) - base_depth >= 4:
            dirs[:] = []
            continue
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("node_modules", "__pycache__", "proc")]
        if name in files:
            f = os.path.join(root, name)
            if os.path.getsize(f) > 1000000:
                return f
    return None


def _kokoro_download(name, dest):
    global _kokoro_progress
    tmp = dest + ".part"
    log("[kokoro] downloading", name)
    with urllib.request.urlopen(KOKORO_URLS[name], timeout=60) as r, open(tmp, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if total:
                _kokoro_progress = "downloading %s %d%%" % (name, got * 100 // total)
    os.replace(tmp, dest)
    log("[kokoro] downloaded", name)


def kokoro_ensure_files():
    """Make sure both model files exist in KOKORO_DIR: copy them from wherever they
    ended up, otherwise download them. Safe to call repeatedly."""
    global KOKORO_ONNX, KOKORO_VOICES, _kokoro_err, _kokoro_progress
    with _kokoro_fetch_lock:
        os.makedirs(KOKORO_DIR, exist_ok=True)
        for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            dest = os.path.join(KOKORO_DIR, name)
            if os.path.isfile(dest) and os.path.getsize(dest) > 1000000:
                continue
            found = _kokoro_find(name)
            if found and os.path.abspath(found) != os.path.abspath(dest):
                import shutil
                log("[kokoro] found", name, "at", found, "- copying to", KOKORO_DIR)
                shutil.copyfile(found, dest)
                continue
            try:
                _kokoro_download(name, dest)
            except Exception as e:
                _kokoro_err = "Couldn't download %s (%s). Put kokoro-v1.0.onnx and voices-v1.0.bin in %s" % (name, e, KOKORO_DIR)
                _kokoro_progress = ""
                raise RuntimeError(_kokoro_err)
        _kokoro_progress = ""
        KOKORO_ONNX = os.path.join(KOKORO_DIR, "kokoro-v1.0.onnx")
        KOKORO_VOICES = os.path.join(KOKORO_DIR, "voices-v1.0.bin")


def _kokoro_import():
    try:
        from kokoro_onnx import Kokoro
        return Kokoro
    except ImportError:
        log("[kokoro] kokoro-onnx not installed - installing it")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "kokoro-onnx", "numpy"],
                       check=False, timeout=600)
        try:
            import importlib, site
            importlib.invalidate_caches()
            try:
                sp = site.getusersitepackages()
                if sp not in sys.path:
                    sys.path.append(sp)
            except Exception:
                pass
            from kokoro_onnx import Kokoro
            return Kokoro
        except ImportError:
            return None


def get_kokoro():
    global _kokoro, _kokoro_err
    if _kokoro is not None:
        return _kokoro
    kokoro_ensure_files()
    with _kokoro_lock:
        if _kokoro is None:
            Kokoro = _kokoro_import()
            if Kokoro is None:
                _kokoro_err = "kokoro-onnx isn't installed and auto-install failed (pip install kokoro-onnx numpy)"
                raise RuntimeError(_kokoro_err)
            model_path = KOKORO_ONNX
            if KOKORO_FAST:
                try:
                    if not (os.path.isfile(KOKORO_ONNX_FAST) and os.path.getsize(KOKORO_ONNX_FAST) > 1000000):
                        found = _kokoro_find("kokoro-v1.0.int8.onnx")
                        if found:
                            import shutil
                            shutil.copyfile(found, KOKORO_ONNX_FAST)
                        else:
                            _kokoro_download("kokoro-v1.0.int8.onnx", KOKORO_ONNX_FAST)
                    model_path = KOKORO_ONNX_FAST
                except Exception as e:
                    log("[kokoro] fast model unavailable, using full model:", e)
            try:
                # Tune onnxruntime for this CPU: use the real cores, run graph ops in parallel
                import onnxruntime as rt
                so = rt.SessionOptions()
                cores = max(1, (os.cpu_count() or 2))
                so.intra_op_num_threads = cores
                so.inter_op_num_threads = 1
                so.graph_optimization_level = rt.GraphOptimizationLevel.ORT_ENABLE_ALL
                so.execution_mode = rt.ExecutionMode.ORT_SEQUENTIAL
                sess = rt.InferenceSession(model_path, sess_options=so, providers=["CPUExecutionProvider"])
                _kokoro = Kokoro.from_session(sess, KOKORO_VOICES)
            except Exception as e:
                log("[kokoro] tuned session failed, using default:", e)
                _kokoro = Kokoro(model_path, KOKORO_VOICES)
            _kokoro_err = ""
            log("[kokoro] model loaded:", os.path.basename(model_path))
            try:  # warm-up so the first real reply isn't slow
                _kokoro.create("Ready.", voice="af_heart", speed=1.0, lang="en-us")
            except Exception:
                pass
        return _kokoro


def kokoro_warmup():
    """Fetch/load the model in the background at startup so the first reply isn't slow."""
    def run():
        try:
            get_kokoro()
        except Exception as e:
            log("[kokoro] warmup failed:", e)
    threading.Thread(target=run, daemon=True).start()


# ════════════════════════════════════════════════════════════════════════════
#  Piper — the FAST voice engine (used by default for voice mode)
#  ~13x faster than Kokoro on CPU (0.16s vs 2.07s for the same sentence in testing),
#  so replies start almost immediately on slower machines. Kokoro stays available
#  as the "natural" engine. Each constellation keeps a male or female voice.
# ════════════════════════════════════════════════════════════════════════════
TTS_DEFAULT_ENGINE = os.environ.get("AGENTIC_TTS", "fast")      # fast (Piper) | natural (Kokoro)
PIPER_DIR = os.path.join(KOKORO_DIR, "piper")
PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/{lc}/{name}/{q}/{voice}{ext}?download=true"
# Kokoro voice id -> matching Piper voice (same gender / accent)
PIPER_FOR_KOKORO = {
    "af_heart": "en_US-lessac-medium",   "af_sky": "en_US-amy-medium",
    "af_bella": "en_US-hfc_female-medium", "af_river": "en_US-kristin-medium",
    "af_aoede": "en_US-ljspeech-medium", "af_kore": "en_GB-alba-medium",
    "af_nova": "en_US-amy-medium",       "af_nicole": "en_US-lessac-medium",
    "af_sarah": "en_US-kristin-medium",  "af_jessica": "en_US-hfc_female-medium",
    "bf_emma": "en_GB-jenny_dioco-medium", "bf_isabella": "en_GB-alba-medium",
    "am_michael": "en_US-ryan-medium",   "am_fenrir": "en_US-joe-medium",
    "am_liam": "en_US-bryce-medium",     "am_eric": "en_US-john-medium",
    "am_puck": "en_US-hfc_male-medium",  "am_adam": "en_US-norman-medium",
    "am_onyx": "en_US-kusal-medium",     "am_echo": "en_US-norman-medium",
    "bm_lewis": "en_GB-alan-medium",     "bm_george": "en_GB-northern_english_male-medium",
    "bm_daniel": "en_GB-alan-medium",
}
_piper_voices = {}
_piper_locks = {}
_piper_err = ""
_piper_lock = threading.Lock()


def pip_install(*pkgs):
    """Install packages for this Python, retrying the ways Linux distros require. Returns error text or ''."""
    last = ""
    for extra in ([], ["--break-system-packages"], ["--user"], ["--user", "--break-system-packages"]):
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet"] + extra + list(pkgs),
                           capture_output=True, text=True, timeout=900)
        if r.returncode == 0:
            try:
                import importlib, site
                importlib.invalidate_caches()
                sp = site.getusersitepackages()
                if sp not in sys.path:
                    sys.path.append(sp)
            except Exception:
                pass
            return ""
        last = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["pip failed"]
        last = last[0][:200]
    return last


def _piper_import():
    try:
        from piper import PiperVoice
        return PiperVoice
    except ImportError:
        log("[piper] piper-tts not installed - installing it")
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "piper-tts"], check=False, timeout=900)
        try:
            import importlib, site
            importlib.invalidate_caches()
            try:
                sp = site.getusersitepackages()
                if sp not in sys.path:
                    sys.path.append(sp)
            except Exception:
                pass
            from piper import PiperVoice
            return PiperVoice
        except ImportError:
            return None


def piper_voice_name(kokoro_voice):
    v = (kokoro_voice or "").strip()
    if v in PIPER_FOR_KOKORO:
        return PIPER_FOR_KOKORO[v]
    if v.startswith("en_") and v.count("-") == 2:      # already a Piper voice name
        return v
    if v[:2] in ("am", "bm"):
        return "en_US-ryan-medium" if v[0] == "a" else "en_GB-alan-medium"
    return "en_US-lessac-medium" if v[:1] != "b" else "en_GB-jenny_dioco-medium"


def get_piper(voice):
    """Load (downloading the first time, ~60MB per voice) and cache a Piper voice."""
    global _piper_err
    with _piper_lock:
        if voice in _piper_voices:
            return _piper_voices[voice], _piper_locks[voice]
        PiperVoice = _piper_import()
        if PiperVoice is None:
            _piper_err = "piper-tts isn't installed and auto-install failed (pip install piper-tts)"
            raise RuntimeError(_piper_err)
        os.makedirs(PIPER_DIR, exist_ok=True)
        model = os.path.join(PIPER_DIR, voice + ".onnx")
        lc, name, q = voice.split("-")
        for ext in (".onnx.json", ".onnx"):
            dest = model + (".json" if ext == ".onnx.json" else "")
            if not (os.path.isfile(dest) and os.path.getsize(dest) > 100):
                log("[piper] downloading voice", voice + ext)
                url = PIPER_URL.format(lc=lc, name=name, q=q, voice=voice, ext=ext)
                with urllib.request.urlopen(url, timeout=120) as r, open(dest + ".part", "wb") as out:
                    shutil.copyfileobj(r, out)
                os.replace(dest + ".part", dest)
        pv = PiperVoice.load(model)
        _piper_voices[voice] = pv
        _piper_locks[voice] = threading.Lock()
        _piper_err = ""
        log("[piper] voice ready:", voice)
        return pv, _piper_locks[voice]


def piper_wav(text, kokoro_voice, speed):
    import io, wave
    pv, lock = get_piper(piper_voice_name(kokoro_voice))
    buf = io.BytesIO()
    with lock, wave.open(buf, "wb") as wf:
        try:
            from piper.config import SynthesisConfig
            pv.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=1.0 / max(0.5, speed)))
        except ImportError:   # older piper-tts API
            pv.synthesize(text, wf, length_scale=1.0 / max(0.5, speed))
    return buf.getvalue()


def piper_warmup():
    """Fetch every constellation's Piper voice in the background so none of them stall later."""
    def run():
        for v in sorted(set(PIPER_FOR_KOKORO.values())):
            try:
                piper_wav("Ready.", v, 1.0)
            except Exception as e:
                log("[piper] warmup", v, "failed:", e)
                break
    threading.Thread(target=run, daemon=True).start()


def samples_to_wav(samples, sample_rate):
    import io, wave
    import numpy as np
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


class KokoroHandler(ServiceHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path in ("", "/health"):
            return self.reply({"status": "ok" if (_kokoro or _piper_voices) else (_kokoro_progress or "loading"), "model": KOKORO_ONNX,
                               "error": _kokoro_err, "engine": TTS_DEFAULT_ENGINE, "piper_voices": sorted(_piper_voices), "piper_error": _piper_err})
        if path == "/voices":
            try:
                voices = sorted(get_kokoro().get_voices())
            except Exception:
                voices = ["af_heart", "af_sky", "af_bella", "af_sarah", "af_river", "af_kore", "af_aoede",
                          "am_adam", "am_michael", "am_eric", "am_liam", "am_fenrir", "am_puck",
                          "bf_emma", "bm_george", "bm_lewis"]
            return self.reply({"voices": voices})
        self.reply({"error": "not found"}, 404)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path.rstrip("/") != "/speak":
            return self.reply({"error": "not found"}, 404)
        try:
            data = json.loads(self.read_body(1024 * 1024) or b"{}")
        except Exception:
            data = None
        if not data or "text" not in data:
            return self.reply({"error": "Missing text"}, 400)
        text = str(data["text"]).strip()
        if not text:
            return self.reply({"error": "Empty text"}, 400)
        voice = data.get("voice", "af_heart")
        speed = float(data.get("speed", 0.9))
        engine = (data.get("engine") or TTS_DEFAULT_ENGINE).lower()
        if len(text) > 500:  # keep generation fast
            text = text[:500].rsplit(" ", 1)[0] + "..."
        try:
            key = (engine, text, voice, round(speed, 2))
            wav = _kokoro_cache.get(key)
            if wav is None and engine == "fast":
                try:
                    wav = piper_wav(text, voice, speed)
                    _kokoro_cache[key] = wav
                except Exception as e:
                    log("[piper] failed, using Kokoro:", e)
            if wav is None:
                k = get_kokoro()
                with _kokoro_lock:
                    samples, sr = k.create(text, voice=voice, speed=speed, lang="en-us")
                wav = samples_to_wav(samples, sr)
                _kokoro_cache[key] = wav
                if len(_kokoro_cache) > 200:
                    _kokoro_cache.pop(next(iter(_kokoro_cache)))
            self.send_bytes(200, wav, "audio/wav")
        except Exception as e:
            log("[kokoro] error:", e)
            self.reply({"error": str(e)}, 503 if not _kokoro else 500)


# ════════════════════════════════════════════════════════════════════════════
#  Whisper STT  (was whisper_server.py)  - port 5051
# ════════════════════════════════════════════════════════════════════════════
WHISPER_PORT = 5051
# tiny.en is ~2-3x faster than base.en on CPU and fine for short spoken prompts.
# Set AGENTIC_WHISPER_MODEL=base.en (or small.en) for more accuracy.
WHISPER_MODEL = os.environ.get("AGENTIC_WHISPER_MODEL", "tiny.en")
# Words it should expect (names it would otherwise mishear). Comma-separated, add your own.
WHISPER_HINTS = os.environ.get("AGENTIC_WHISPER_HINTS",
    "Agentic, Lyra, Orion, Carina, Gemini, Auriga, Vela, Apus, Apollo, Sirius, Cosmic, Astra, Cosmos, "
    "Crescent, Ollama, Kokoro, Paducah, Kentucky, Cunningham, Bitcord")
WHISPER_DEVICE = os.environ.get("AGENTIC_WHISPER_DEVICE", "cpu")        # "cuda" with an NVIDIA GPU
WHISPER_COMPUTE = os.environ.get("AGENTIC_WHISPER_COMPUTE", "int8")     # "float16" on cuda
_whisper = None
_whisper_err = ""
_whisper_lock = threading.Lock()


def get_whisper():
    global _whisper, _whisper_err
    with _whisper_lock:
        if _whisper is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                _whisper_err = "faster-whisper isn't installed (pip install faster-whisper)"
                raise RuntimeError(_whisper_err)
            log("[whisper] loading '%s' on %s (first run downloads the model)..." % (WHISPER_MODEL, WHISPER_DEVICE))
            _whisper = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE, cpu_threads=max(1, (os.cpu_count() or 2) - 1))
            _whisper_err = ""
            log("[whisper] ready")
        return _whisper


# ════════════════════════════════════════════════════════════════════════════
#  Moonshine — the FAST speech-to-text engine (default for voice mode)
#  Whisper always processes a fixed 30-second window, so even a 2-second question
#  costs ~3s on a slower CPU. Moonshine's work scales with how long you actually
#  talk: 150-240ms per question in testing, with the same accuracy (it even got
#  "Paducah" right). Whisper stays as the fallback (AGENTIC_STT=whisper to force it).
# ════════════════════════════════════════════════════════════════════════════
STT_DEFAULT_ENGINE = os.environ.get("AGENTIC_STT", "fast")
MOONSHINE_NAME = os.environ.get("AGENTIC_MOONSHINE", "sherpa-onnx-moonshine-base-en-int8")   # base = accurate (default); ...-tiny-en-int8 = a little faster
MOONSHINE_DIR = os.path.join(KOKORO_DIR, MOONSHINE_NAME)
MOONSHINE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/%s.tar.bz2" % MOONSHINE_NAME
_moon = None
_moon_err = ""
_moon_lock = threading.Lock()
_moon_fail_at = 0.0


def get_moonshine():
    global _moon, _moon_err
    with _moon_lock:
        if _moon is not None:
            return _moon
        try:
            import sherpa_onnx
        except ImportError:
            log("[moonshine] sherpa-onnx not installed - installing it")
            why = pip_install("sherpa-onnx")
            try:
                import sherpa_onnx
            except ImportError as e:
                _moon_err = "couldn't install sherpa-onnx (%s). Run: %s -m pip install sherpa-onnx" % (why or str(e)[:120], sys.executable)
                raise RuntimeError(_moon_err)
        if not os.path.isfile(os.path.join(MOONSHINE_DIR, "tokens.txt")):
            import tarfile
            os.makedirs(KOKORO_DIR, exist_ok=True)
            tmp = MOONSHINE_DIR + ".tar.bz2.part"
            log("[moonshine] downloading", MOONSHINE_NAME, "(~120MB, one time)")
            with urllib.request.urlopen(MOONSHINE_URL, timeout=120) as r, open(tmp, "wb") as out:
                shutil.copyfileobj(r, out)
            with tarfile.open(tmp, "r:bz2") as tf:
                tf.extractall(KOKORO_DIR)
            os.remove(tmp)
        d = MOONSHINE_DIR
        _moon = sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=os.path.join(d, "preprocess.onnx"),
            encoder=os.path.join(d, "encode.int8.onnx"),
            uncached_decoder=os.path.join(d, "uncached_decode.int8.onnx"),
            cached_decoder=os.path.join(d, "cached_decode.int8.onnx"),
            tokens=os.path.join(d, "tokens.txt"),
            num_threads=max(1, min(4, os.cpu_count() or 2)))
        _moon_err = ""
        log("[moonshine] ready:", MOONSHINE_NAME)
        return _moon


_HINT_SKIP = {"bitcoin", "aria", "astro", "opus", "apple", "cosmic", "cosmos", "carbon", "orient", "origin", "agency",
              "serious", "series", "cereal", "carina", "career", "carry", "carrier", "vella", "village", "apples",
              "agents", "agenda", "agentive", "crescents", "kentuckian", "cunning", "lyrical", "lyrics", "auricle",
              "apostle", "apology", "sirens", "orange", "orions", "geminis", "gemini", "bitcoins", "kokomo"}


def fix_names(text):
    """Snap near-misses of your names (WHISPER_HINTS) to the right spelling, e.g. Padjuca -> Paducah.
    Conservative: only words of 5+ letters that are very close (>= 0.8 similar) and not common words."""
    import difflib
    names = [n.strip() for n in WHISPER_HINTS.split(",") if n.strip()]
    low = {n.lower(): n for n in names}
    def fix(m):
        w = m.group(0)
        lw = w.lower()
        if len(lw) < 5 or lw in low or lw in _HINT_SKIP:
            return w
        best = max(names, key=lambda n: difflib.SequenceMatcher(None, lw, n.lower()).ratio())
        r = difflib.SequenceMatcher(None, lw, best.lower()).ratio()
        # very close, or fairly close AND same first letter + similar length (Padjuka -> Paducah, BitGrid -> Bitcord)
        ok = r >= 0.8 or (r >= 0.7 and len(lw) >= 6 and lw[0] == best[0].lower() and abs(len(lw) - len(best)) <= 2)
        return best if ok else w
    return re.sub(r"[A-Za-z]+", fix, text)


def moonshine_transcribe(wav_bytes):
    """16-bit mono WAV (the browser sends 16 kHz) -> text."""
    import io, wave
    import numpy as np
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    rec = get_moonshine()
    st = rec.create_stream()
    st.accept_waveform(sr, pcm)
    with _moon_lock:
        rec.decode_stream(st)
    return fix_names((st.result.text or "").strip())


class WhisperHandler(ServiceHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path in ("", "/health"):
            return self.reply({"status": "ok" if (_whisper or _moon) else "loading", "model": WHISPER_MODEL, "error": _whisper_err,
                               "engine": "moonshine" if (STT_DEFAULT_ENGINE == "fast" and _moon) else "whisper",
                               "moonshine": MOONSHINE_NAME, "moonshine_error": _moon_err})
        self.reply({"error": "not found"}, 404)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path.rstrip("/") != "/transcribe":
            return self.reply({"error": "not found"}, 404)
        body = self.read_body()
        audio = parse_multipart(body, self.headers.get("Content-Type")).get("audio") if body else None
        if not audio:
            return self.reply({"transcript": ""}, 400)
        if STT_DEFAULT_ENGINE == "fast" and audio[:4] == b"RIFF" and (time.time() - _moon_fail_at > 300):
            text = None
            try:
                text = moonshine_transcribe(audio)
            except Exception as e:
                globals()["_moon_fail_at"] = time.time()   # don't re-try on every question; retry in 5 min
                globals()["_moon_err"] = "transcribe failed: %s: %s" % (type(e).__name__, str(e)[:200])
                import traceback
                log("[moonshine] failed, using Whisper:", traceback.format_exc()[-600:])
            if text is not None:
                # The browser hanging up early (cancelled / new question) is not a Moonshine problem
                try:
                    return self.reply({"transcript": text, "engine": "moonshine"})
                except (BrokenPipeError, ConnectionResetError):
                    return
        try:
            model = get_whisper()
            suffix = ".wav" if audio[:4] == b"RIFF" else ".webm"   # browser now sends 16 kHz WAV (no decode step)
            with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
                tmp.write(audio)
                tmp.flush()
                opts = dict(beam_size=1, best_of=1, language="en", vad_filter=True,
                            condition_on_previous_text=False, without_timestamps=True, temperature=0.0)
                with _whisper_lock:
                    try:
                        segments, _info = model.transcribe(tmp.name, hotwords=WHISPER_HINTS, **opts)
                    except TypeError:   # older faster-whisper without hotwords
                        segments, _info = model.transcribe(tmp.name, initial_prompt=WHISPER_HINTS, **opts)
                    text = "".join(s.text for s in segments).strip()
            self.reply({"transcript": text, "engine": "whisper", "moonshine_error": _moon_err})
        except Exception as e:
            log("[whisper] error:", e)
            self.reply({"transcript": "", "error": str(e)}, 503 if not _whisper else 500)


# ════════════════════════════════════════════════════════════════════════════
#  Web server + Ollama + launcher  (was Agentic.sh)
# ════════════════════════════════════════════════════════════════════════════
WEB_PORT = int(os.environ.get("AGENTIC_WEB_PORT", "8080"))
WEB_HOST = os.environ.get("AGENTIC_WEB_HOST", "0.0.0.0")
WEB_ROOT = os.path.expanduser(os.environ.get("AGENTIC_WEB_ROOT", HERE))
SKIP = {s.strip().lower() for s in os.environ.get("AGENTIC_NO", "").split(",") if s.strip()}


class WebHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def serve(name, handler, port, host="127.0.0.1"):
    """Start one HTTP service on a background thread. Never raises."""
    if name.lower() in SKIP:
        log("- %-8s skipped (AGENTIC_NO)" % name)
        return None
    try:
        srv = ThreadingHTTPServer((host, port), handler)
    except OSError as e:
        log("! %-8s not started on :%d (%s) - something else may already be running there" % (name, port, e.strerror or e))
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("+ %-8s up on :%d" % (name, port))
    return srv


def start_ollama():
    if "ollama" in SKIP:
        return
    if port_open(11434):
        log("+ ollama   already running on :11434")
        return
    exe = shutil.which("ollama")
    if not exe:
        log("- ollama   not installed (skipping)")
        return
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    out = open(os.path.join(HERE, "logs", "ollama.log"), "ab")
    # new session, so Ctrl+C on this script leaves Ollama running (same as the old launcher)
    env = dict(os.environ)
    env.setdefault("OLLAMA_KEEP_ALIVE", "30m")   # keep the model in memory between voice turns
    subprocess.Popen([exe, "serve"], stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    log("+ ollama   started")


# ══════════════════════════════════════════════════════════════════════════
#  Offline LLM  - port 11434 (or 11435 if Ollama already has 11434)
#  Runs GGUF models directly with llama.cpp (pip install llama-cpp-python) and
#  answers the same API as Ollama, so index.html needs no edits. Models you
#  already pulled with Ollama are found automatically; or put .gguf files in
#  ./models, or:  python3 agentic_server.py --pull <hf-repo> <file.gguf>
#  Env: AGENTIC_LLM=builtin|ollama  AGENTIC_LLM_PORT  AGENTIC_LLM_MODELS (folder)
#       AGENTIC_LLM_MODEL (default model)  AGENTIC_LLM_CTX (4096)
#       AGENTIC_LLM_THREADS  AGENTIC_LLM_GPU=1  AGENTIC_LLM_KEEP (idle minutes, 10)
# ══════════════════════════════════════════════════════════════════════════
import datetime as _dt
import glob
from urllib.parse import urlparse
LLM_VERSION = '0.1.0-agentic'

# ───────────────────────────── model discovery ─────────────────────────────

def _llm_ollama_dirs():
    env = os.environ.get('OLLAMA_MODELS')
    cands = [env] if env else []
    home = os.path.expanduser('~')
    cands += [os.path.join(home, '.ollama', 'models'), '/usr/share/ollama/.ollama/models', '/var/lib/ollama/.ollama/models']
    return [d for d in cands if d and os.path.isdir(os.path.join(d, 'manifests'))]


def _llm_param_size(n_bytes, quant):
    # rough parameter guess from file size + quant bits, only for display
    bits = {'Q2': 2.6, 'Q3': 3.5, 'Q4': 4.6, 'Q5': 5.5, 'Q6': 6.6, 'Q8': 8.5, 'F16': 16, 'BF16': 16, 'F32': 32}
    b = next((v for k, v in bits.items() if quant.upper().startswith(k)), 5)
    p = n_bytes * 8 / b
    return f'{p / 1e9:.1f}B' if p >= 1e9 else f'{p / 1e6:.0f}M'


def _llm_quant(name):
    m = re.search(r'(I?Q\d(?:_[A-Z0-9]+)*|F16|BF16|F32)', name.upper())
    return m.group(1) if m else ''


def llm_discover(models_dir):
    """Return {name: {path, size, modified, family, quant, source}}."""
    found = {}
    # 1. loose .gguf files
    for p in sorted(glob.glob(os.path.join(models_dir, '**', '*.gguf'), recursive=True)):
        base = os.path.basename(p)
        if re.search(r'mmproj|projector', base, re.I):
            continue
        name = os.path.splitext(base)[0]
        st = os.stat(p)
        q = _llm_quant(name)
        found[name] = dict(path=p, size=st.st_size, modified=st.st_mtime, family=name.split('-')[0].lower(), quant=q, source='file')
    # 2. models already downloaded by Ollama (GGUF blobs + JSON manifests)
    for root in _llm_ollama_dirs():
        man_root = os.path.join(root, 'manifests')
        for man in glob.glob(os.path.join(man_root, '*', '*', '*', '*')):
            try:
                with open(man, encoding='utf-8') as f:
                    data = json.load(f)
                layer = next((l for l in data.get('layers', []) if l.get('mediaType') == 'application/vnd.ollama.image.model'), None)
                if not layer:
                    continue
                blob = os.path.join(root, 'blobs', layer['digest'].replace(':', '-'))
                if not os.path.isfile(blob):
                    continue
                reg, ns, model, tag = man.replace(man_root + os.sep, '').split(os.sep)[-4:]
                name = (f'{model}:{tag}' if ns == 'library' else f'{ns}/{model}:{tag}')
                if reg != 'registry.ollama.ai':
                    name = f'{reg}/{name}'
                if name in found:
                    continue
                cfg = {}
                try:
                    cdig = data.get('config', {}).get('digest', '')
                    with open(os.path.join(root, 'blobs', cdig.replace(':', '-')), encoding='utf-8') as f:
                        cfg = json.load(f)
                except Exception:
                    pass
                st = os.stat(blob)
                found[name] = dict(path=blob, size=layer.get('size', st.st_size), modified=os.stat(man).st_mtime,
                                   family=cfg.get('model_family', model.split('.')[0]), quant=cfg.get('file_type', ''), source='ollama')
            except Exception:
                continue
    return found


# ───────────────────────────── model runtime ─────────────────────────────

class LLMEngine:
    """Holds one chat model in memory at a time (plus an optional embedding copy)."""

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()          # one generation at a time, like Ollama with 1 slot
        self.llm = None
        self.name = None
        self.loaded_at = 0
        self.last_used = 0
        self.emb = None
        self.emb_name = None
        self.catalog = {}
        self.refresh()
        threading.Thread(target=self._reaper, daemon=True).start()

    def refresh(self):
        self.catalog = llm_discover(self.args.models_dir)
        return self.catalog

    def resolve(self, name):
        if not name:
            name = self.args.default_model or self.name or next(iter(self.catalog), None)
        if name in self.catalog:
            return name
        self.refresh()
        if name in self.catalog:
            return name
        # forgiving matches: "llama3.2" → "llama3.2:latest", case-insensitive, prefix
        low = (name or '').lower()
        for cand in (f'{name}:latest',):
            if cand in self.catalog:
                return cand
        for k in self.catalog:
            if k.lower() == low or k.lower().startswith(low):
                return k
        return None

    def _load_kwargs(self, embedding=False):
        a = self.args
        kw = dict(n_ctx=a.ctx, n_threads=a.threads, n_batch=a.batch, n_gpu_layers=-1 if a.gpu else 0, verbose=a.verbose)
        if embedding:
            kw['embedding'] = True
        if a.flash_attn:
            kw['flash_attn'] = True
        return kw

    def get(self, name):
        """Return a loaded Llama for `name`, swapping models if needed. Call with self.lock held."""
        from llama_cpp import Llama
        real = self.resolve(name)
        if not real:
            raise KeyError(f"model '{name}' not found — run: python3 agentic_server.py --models")
        if self.llm is None or self.name != real:
            self.unload()
            log(f'loading {real} …')
            t0 = time.time()
            self.llm = Llama(model_path=self.catalog[real]['path'], **self._load_kwargs())
            self.name, self.loaded_at = real, time.time()
            log(f'loaded {real} in {time.time() - t0:.1f}s (ctx {self.args.ctx}, threads {self.args.threads}, gpu {"on" if self.args.gpu else "off"})')
        self.last_used = time.time()
        return self.llm, real

    def get_embedder(self, name):
        from llama_cpp import Llama
        real = self.resolve(name)
        if not real:
            raise KeyError(f"model '{name}' not found")
        if self.emb is None or self.emb_name != real:
            self.emb = None
            self.emb = Llama(model_path=self.catalog[real]['path'], **self._load_kwargs(embedding=True))
            self.emb_name = real
        self.last_used = time.time()
        return self.emb

    def unload(self):
        if self.llm is not None:
            log(f'unloading {self.name}')
            try:
                self.llm.close()
            except Exception:
                pass
        self.llm, self.name = None, None

    def _reaper(self):
        while True:
            time.sleep(20)
            if self.args.keep_alive > 0 and self.llm is not None and time.time() - self.last_used > self.args.keep_alive * 60:
                if self.lock.acquire(blocking=False):
                    try:
                        self.unload(); self.emb = None
                    finally:
                        self.lock.release()


# ───────────────────────────── helpers ─────────────────────────────

def llm_now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def llm_clean(msgs):
    """Text only (images are dropped — this server has no vision support), roles normalised."""
    out = []
    for m in msgs or []:
        role = m.get('role', 'user')
        c = m.get('content', '')
        if isinstance(c, list):  # OpenAI content parts
            c = ''.join(p.get('text', '') for p in c if isinstance(p, dict))
        if role == 'tool':
            role, c = 'user', 'Tool result:\n' + str(c)
        if role not in ('system', 'user', 'assistant'):
            role = 'user'
        if out and out[-1]['role'] == role and role != 'system':
            out[-1]['content'] += '\n\n' + str(c)   # some templates require alternating roles
        else:
            out.append({'role': role, 'content': str(c)})
    return out


def llm_fold(msgs):
    """For chat templates that reject a system role: merge it into the first user turn."""
    sys_txt = '\n\n'.join(m['content'] for m in msgs if m['role'] == 'system')
    rest = [dict(m) for m in msgs if m['role'] != 'system']
    if sys_txt:
        if rest and rest[0]['role'] == 'user':
            rest[0]['content'] = sys_txt + '\n\n' + rest[0]['content']
        else:
            rest.insert(0, {'role': 'user', 'content': sys_txt})
    return rest


def llm_gen_params(opts, top=None):
    """Map Ollama `options` (or OpenAI top-level fields) to llama-cpp-python arguments."""
    o = dict(opts or {})
    if top:
        for k in ('temperature', 'top_p', 'stop', 'seed', 'max_tokens', 'presence_penalty', 'frequency_penalty'):
            if top.get(k) is not None:
                o[k] = top[k]
    p = {}
    n = o.get('num_predict', o.get('max_tokens'))
    p['max_tokens'] = None if n in (None, -1, -2) else int(n)
    for src, dst in (('temperature', 'temperature'), ('top_p', 'top_p'), ('top_k', 'top_k'), ('min_p', 'min_p'),
                     ('repeat_penalty', 'repeat_penalty'), ('presence_penalty', 'presence_penalty'),
                     ('frequency_penalty', 'frequency_penalty'), ('seed', 'seed')):
        if o.get(src) is not None:
            p[dst] = o[src]
    if o.get('stop'):
        p['stop'] = o['stop'] if isinstance(o['stop'], list) else [o['stop']]
    return p


# ───────────────────────────── HTTP server ─────────────────────────────

class LLMHandler(BaseHTTPRequestHandler):
    server_version = 'AgenticLLM/' + LLM_VERSION
    protocol_version = 'HTTP/1.1'
    engine: "LLMEngine" = None
    allow_origins = []

    def log_message(self, fmt, *a):
        if self.engine.args.verbose:
            log(fmt % a)

    # CORS: only pages on this computer may call the server (same rule Ollama uses by default)
    def _origin_ok(self):
        o = self.headers.get('Origin')
        if not o or o == 'null':
            return True
        h = urlparse(o).hostname or ''
        return h in ('localhost', '127.0.0.1', '::1', '0.0.0.0') or h.endswith('.localhost') or o in self.allow_origins or '*' in self.allow_origins

    def _cors(self):
        o = self.headers.get('Origin')
        if o and self._origin_ok():
            self.send_header('Access-Control-Allow-Origin', o)
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, DELETE')
            self.send_header('Access-Control-Allow-Private-Network', 'true')

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json(code, {'error': msg})

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        try:
            return json.loads(raw or b'{}')
        except Exception:
            return None

    def _start_stream(self, ctype):
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()

    def _chunk(self, data: bytes):
        self.wfile.write(f'{len(data):X}\r\n'.encode() + data + b'\r\n')
        self.wfile.flush()

    def _gone(self):
        """True once the page has hung up (Stop button, navigated away). Checked every token."""
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            return bool(r) and self.connection.recv(1, socket.MSG_PEEK) == b''
        except Exception:
            return True

    def _stopper(self, llm):
        """Runs on every generated token: if the page hung up, force end-of-text so the model is freed at once."""
        from llama_cpp import LogitsProcessorList
        eos = llm.token_eos()
        def proc(ids, scores):
            if self._gone():
                scores[:] = -1e30
                scores[eos] = 0
            return scores
        return LogitsProcessorList([proc])

    def _end_stream(self):
        try:
            self.wfile.write(b'0\r\n\r\n'); self.wfile.flush()
        except Exception:
            pass

    # ── routes ──
    def do_OPTIONS(self):
        self.send_response(204 if self._origin_ok() else 403)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200); self._cors(); self.send_header('Content-Length', '0'); self.end_headers()

    def do_GET(self):
        if not self._origin_ok():
            return self._err(403, 'origin not allowed')
        path = urlparse(self.path).path.rstrip('/') or '/'
        E = self.engine
        if path == '/':
            body = b'Agentic LLM is running (Ollama-compatible)'
            self.send_response(200); self._cors(); self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path == '/api/version':
            return self._json(200, {'version': LLM_VERSION})
        if path == '/api/tags':
            E.refresh()
            return self._json(200, {'models': [{
                'name': n, 'model': n, 'modified_at': _dt.datetime.fromtimestamp(m['modified'], _dt.timezone.utc).isoformat(),
                'size': m['size'], 'digest': os.path.basename(m['path']).replace('sha256-', '')[:64],
                'details': {'format': 'gguf', 'family': m['family'], 'families': [m['family']],
                            'parameter_size': _llm_param_size(m['size'], m['quant'] or 'Q4'), 'quantization_level': m['quant']}
            } for n, m in E.catalog.items()]})
        if path == '/api/ps':
            ms = []
            if E.llm is not None:
                m = E.catalog.get(E.name, {})
                exp = _dt.datetime.fromtimestamp(E.last_used + E.args.keep_alive * 60, _dt.timezone.utc).isoformat()
                ms.append({'name': E.name, 'model': E.name, 'size': m.get('size', 0), 'size_vram': m.get('size', 0) if E.args.gpu else 0,
                           'expires_at': exp, 'context_length': E.args.ctx, 'details': {'format': 'gguf', 'family': m.get('family', '')}})
            return self._json(200, {'models': ms})
        if path == '/v1/models':
            E.refresh()
            return self._json(200, {'object': 'list', 'data': [{'id': n, 'object': 'model', 'owned_by': 'local', 'created': int(m['modified'])} for n, m in E.catalog.items()]})
        return self._err(404, 'not found')

    def do_POST(self):
        if not self._origin_ok():
            return self._err(403, 'origin not allowed')
        path = urlparse(self.path).path.rstrip('/')
        req = self._body()
        if req is None:
            return self._err(400, 'invalid JSON body')
        try:
            if path == '/api/chat':
                return self._chat(req, ollama=True)
            if path == '/v1/chat/completions':
                return self._chat(req, ollama=False)
            if path == '/api/generate':
                return self._generate(req)
            if path in ('/api/embeddings', '/api/embed', '/v1/embeddings'):
                return self._embed(req, path)
            if path == '/api/show':
                real = self.engine.resolve(req.get('model') or req.get('name'))
                if not real:
                    return self._err(404, 'model not found')
                m = self.engine.catalog[real]
                return self._json(200, {'details': {'format': 'gguf', 'family': m['family'], 'quantization_level': m['quant']},
                                        'model_info': {'general.file': m['path']}, 'modelfile': '', 'template': '', 'parameters': ''})
            return self._err(404, 'not found')
        except KeyError as e:
            return self._err(404, str(e).strip('"\''))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            log(f'error on {path}: {e!r}')
            try:
                return self._err(500, str(e))
            except Exception:
                return

    # ── chat (Ollama NDJSON or OpenAI SSE) ──
    def _chat(self, req, ollama=True):
        E = self.engine
        model = req.get('model')
        msgs = llm_clean(req.get('messages'))
        if not msgs:
            return self._err(400, 'messages are required')
        stream = req.get('stream', True if ollama else False)
        params = llm_gen_params(req.get('options') if ollama else None, None if ollama else req)
        if ollama and req.get('format') == 'json':
            params['response_format'] = {'type': 'json_object'}
        t0 = time.time()
        with E.lock:                                   # wait our turn, like Ollama's queue
            llm, real = E.get(model)
            t_load = time.time()

            if stream:
                params['logits_processor'] = self._stopper(llm)

            def run(m):
                return llm.create_chat_completion(messages=m, stream=stream, **params)
            try:
                result = run(msgs)
            except Exception as e:                     # e.g. template says "system role not supported"
                if 'system' in str(e).lower() or 'role' in str(e).lower():
                    result = run(llm_fold(msgs))
                else:
                    raise
            if not stream:
                ch = result['choices'][0]; usage = result.get('usage', {})
                text = ch['message'].get('content') or ''
                if ollama:
                    return self._json(200, {'model': real, 'created_at': llm_now(), 'message': {'role': 'assistant', 'content': text},
                                            'done': True, 'done_reason': ch.get('finish_reason') or 'stop',
                                            'total_duration': int((time.time() - t0) * 1e9), 'load_duration': int((t_load - t0) * 1e9),
                                            'prompt_eval_count': usage.get('prompt_tokens', 0), 'eval_count': usage.get('completion_tokens', 0)})
                result['model'] = real
                return self._json(200, result)

            self._start_stream('application/x-ndjson' if ollama else 'text/event-stream')
            n_tok, reason = 0, 'stop'
            try:
                for chunk in result:
                    if self._gone():
                        raise BrokenPipeError
                    ch = chunk['choices'][0]
                    piece = (ch.get('delta') or {}).get('content') or ''
                    if ch.get('finish_reason'):
                        reason = ch['finish_reason']
                    if ollama:
                        if piece:
                            n_tok += 1
                            self._chunk((json.dumps({'model': real, 'created_at': llm_now(), 'message': {'role': 'assistant', 'content': piece}, 'done': False}) + '\n').encode())
                    else:
                        chunk['model'] = real
                        self._chunk(b'data: ' + json.dumps(chunk).encode() + b'\n\n')
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # the page stopped listening (Stop button / navigated away): stop generating right away
                try:
                    result.close()
                except Exception:
                    pass
                log(f'{real}: client disconnected, generation stopped')
                return
            el = time.time() - t0
            if ollama:
                self._chunk((json.dumps({'model': real, 'created_at': llm_now(), 'message': {'role': 'assistant', 'content': ''}, 'done': True,
                                         'done_reason': reason, 'total_duration': int(el * 1e9), 'load_duration': int((t_load - t0) * 1e9),
                                         'eval_count': n_tok}) + '\n').encode())
            else:
                self._chunk(b'data: [DONE]\n\n')
            self._end_stream()
            log(f'{real}: {n_tok} tokens in {el:.1f}s ({n_tok / max(el - (t_load - t0), 0.01):.1f} tok/s incl. reading the prompt)')

    # ── raw completion (/api/generate) ──
    def _generate(self, req):
        E = self.engine
        prompt = req.get('prompt', '')
        stream = req.get('stream', True)
        params = llm_gen_params(req.get('options'))
        if not prompt:  # Ollama uses an empty prompt to just load the model
            with E.lock:
                E.get(req.get('model'))
            return self._json(200, {'model': req.get('model'), 'created_at': llm_now(), 'response': '', 'done': True})
        # apply the chat template so instruct models answer properly (like Ollama does), unless raw
        if not req.get('raw'):
            msgs = ([{'role': 'system', 'content': req['system']}] if req.get('system') else []) + [{'role': 'user', 'content': prompt}]
            req2 = {'model': req.get('model'), 'messages': msgs, 'stream': stream, 'options': req.get('options')}
            if not stream:
                # reuse chat, then reshape to /api/generate fields
                with E.lock:
                    llm, real = E.get(req.get('model'))
                    try:
                        r = llm.create_chat_completion(messages=msgs, stream=False, **params)
                    except Exception:
                        r = llm.create_chat_completion(messages=llm_fold(msgs), stream=False, **params)
                u = r.get('usage', {})
                return self._json(200, {'model': real, 'created_at': llm_now(), 'response': r['choices'][0]['message'].get('content') or '',
                                        'done': True, 'done_reason': r['choices'][0].get('finish_reason') or 'stop',
                                        'prompt_eval_count': u.get('prompt_tokens', 0), 'eval_count': u.get('completion_tokens', 0)})
            with E.lock:
                llm, real = E.get(req.get('model'))
                params['logits_processor'] = self._stopper(llm)
                try:
                    it = llm.create_chat_completion(messages=msgs, stream=True, **params)
                except Exception:
                    it = llm.create_chat_completion(messages=llm_fold(msgs), stream=True, **params)
                self._start_stream('application/x-ndjson')
                try:
                    for chunk in it:
                        if self._gone():
                            raise BrokenPipeError
                        piece = (chunk['choices'][0].get('delta') or {}).get('content') or ''
                        if piece:
                            self._chunk((json.dumps({'model': real, 'created_at': llm_now(), 'response': piece, 'done': False}) + '\n').encode())
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    try: it.close()
                    except Exception: pass
                    return
                self._chunk((json.dumps({'model': real, 'created_at': llm_now(), 'response': '', 'done': True, 'done_reason': 'stop'}) + '\n').encode())
                self._end_stream()
            return
        with E.lock:
            llm, real = E.get(req.get('model'))
            r = llm.create_completion(prompt=prompt, stream=False, **params)
        return self._json(200, {'model': real, 'created_at': llm_now(), 'response': r['choices'][0]['text'], 'done': True})

    # ── embeddings ──
    def _embed(self, req, path):
        E = self.engine
        inp = req.get('input', req.get('prompt', ''))
        items = inp if isinstance(inp, list) else [inp]
        with E.lock:
            emb = E.get_embedder(req.get('model'))
            vecs = [emb.embed(t) for t in items]
        vecs = [v[0] if v and isinstance(v[0], list) else v for v in vecs]   # some models return per-token vectors
        if path == '/api/embeddings':
            return self._json(200, {'embedding': vecs[0] if vecs else []})
        if path == '/api/embed':
            return self._json(200, {'model': req.get('model'), 'embeddings': vecs})
        return self._json(200, {'object': 'list', 'data': [{'object': 'embedding', 'index': i, 'embedding': v} for i, v in enumerate(vecs)]})


# ───────────────────────────── CLI ─────────────────────────────

def llm_list(args):
    cat = llm_discover(args.models_dir)
    if not cat:
        print('No models found.\n  • Put .gguf files in', args.models_dir, '\n  • or pull one: python3 agentic_server.py --pull <hf-repo> <file.gguf>')
        return
    w = max(len(n) for n in cat)
    for n, m in cat.items():
        print(f'{n.ljust(w)}  {m["size"] / 1e9:5.1f} GB  {m["quant"] or "":10s}  ({m["source"]})')


def llm_pull(args):
    os.makedirs(args.models_dir, exist_ok=True)
    url = f'https://huggingface.co/{args.repo}/resolve/main/{args.file}?download=true'
    dest = os.path.join(args.models_dir, os.path.basename(args.file))
    tmp = dest + '.part'
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    req = urllib.request.Request(url, headers={'User-Agent': 'agentic-llm', **({'Range': f'bytes={have}-'} if have else {})})
    print(f'Downloading {args.repo}/{args.file}\n  → {dest}')
    with urllib.request.urlopen(req) as r:
        total = int(r.headers.get('Content-Length') or 0) + have
        mode = 'ab' if have and r.status == 206 else 'wb'
        done = have if mode == 'ab' else 0
        t0, last = time.time(), 0
        with open(tmp, mode) as f:
            while True:
                buf = r.read(1 << 20)
                if not buf:
                    break
                f.write(buf); done += len(buf)
                if time.time() - last > 0.5:
                    last = time.time(); sp = (done - have) / max(last - t0, 0.01) / 1e6
                    pct = f'{done / total * 100:5.1f}%' if total else ''
                    print(f'\r  {done / 1e9:.2f} / {total / 1e9:.2f} GB {pct}  {sp:.1f} MB/s   ', end='', flush=True)
    os.replace(tmp, dest)
    print(f'\nDone. Model name: {os.path.splitext(os.path.basename(dest))[0]}')




class _LLMArgs:
    models_dir = os.path.expanduser(os.environ.get("AGENTIC_LLM_MODELS", os.path.join(HERE, "models")))
    default_model = os.environ.get("AGENTIC_LLM_MODEL") or None
    ctx = int(os.environ.get("AGENTIC_LLM_CTX", "4096"))
    threads = int(os.environ.get("AGENTIC_LLM_THREADS", "0")) or max(1, (os.cpu_count() or 2) // 2)
    batch = 512
    gpu = os.environ.get("AGENTIC_LLM_GPU", "") in ("1", "true", "yes")
    flash_attn = os.environ.get("AGENTIC_LLM_FLASH", "") in ("1", "true", "yes")
    keep_alive = float(os.environ.get("AGENTIC_LLM_KEEP", "10"))
    verbose = os.environ.get("AGENTIC_LLM_VERBOSE", "") in ("1", "true", "yes")
    host = "127.0.0.1"
    port = int(os.environ.get("AGENTIC_LLM_PORT", "0"))

LLM_MODE = os.environ.get("AGENTIC_LLM", "builtin").strip().lower()   # builtin (default) or ollama


def _import_ok(mod, timeout=90):
    """True if `import mod` works in a throwaway process. A broken native build
    (llama.cpp / onnxruntime on a CPU it wasn't built for) can kill the whole
    interpreter, so it is tested out-of-process and can never take the site down."""
    try:
        r = subprocess.run([sys.executable, "-I", "-c", "import " + mod],
                           capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def start_llm():
    """Start the built-in model server. Returns True if it is serving on :11434 (so Ollama isn't needed)."""
    if "llm" in SKIP or LLM_MODE == "ollama":
        if LLM_MODE == "ollama":
            log("- llm      off (AGENTIC_LLM=ollama)")
        return False
    if not _import_ok("llama_cpp"):
        log("- llm      llama-cpp-python missing or failing to load - using Ollama instead")
        return False
    try:
        os.makedirs(_LLMArgs.models_dir, exist_ok=True)
        engine = LLMEngine(_LLMArgs)
    except Exception as e:
        log("- llm      couldn't start (%s) - using Ollama instead" % str(e)[:160])
        return False
    LLMHandler.engine = engine
    LLMHandler.allow_origins = []
    if not engine.catalog:
        log("- llm      no models found - add .gguf files to %s or: python3 agentic_server.py --pull <repo> <file>" % _LLMArgs.models_dir)
    taken = port_open(11434)
    port = _LLMArgs.port or (11435 if taken else 11434)
    srv = serve("llm", LLMHandler, port)
    if srv and engine.catalog:
        log("  llm      models: %s" % ", ".join(list(engine.catalog)[:8]) + (" ..." if len(engine.catalog) > 8 else ""))
    if srv and port != 11434:
        log(("  llm      something (Ollama?) is already on :11434, so" if taken and not _LLMArgs.port else "  llm     ")+" the built-in server is on :%d - set Settings > Ollama host to http://localhost:%d to use it" % (port, port))
    return bool(srv) and port == 11434


def preload_voice():
    """Load the voice models in the background so the first request isn't slow."""
    if "kokoro" not in SKIP and TTS_DEFAULT_ENGINE == "fast":
        piper_warmup()
    if "whisper" not in SKIP and STT_DEFAULT_ENGINE == "fast":
        try:
            get_moonshine()
        except Exception as e:
            log("- moonshine not ready: %s" % str(e)[:200])
    for name, fn in (("kokoro", get_kokoro), ("whisper", get_whisper)):
        if name in SKIP:
            continue
        mod = "kokoro_onnx" if name == "kokoro" else "faster_whisper"
        if not _import_ok(mod):
            log("- %s not ready: %s missing or failing to load" % (name, mod))
            continue
        try:
            fn()
        except Exception as e:
            log("- %s not ready: %s" % (name, str(e)[:200]))


def main():
    if "--install" in sys.argv or "--uninstall" in sys.argv:
        print(install_autostart(remove="--uninstall" in sys.argv))
        return
    if "--pull" in sys.argv or "--models" in sys.argv:
        rest = [a for a in sys.argv[1:] if not a.startswith("-")]
        class _A: models_dir = _LLMArgs.models_dir
        if "--models" in sys.argv:
            return llm_list(_A)
        if len(rest) < 2:
            print("usage: python3 agentic_server.py --pull <hf-repo> <file.gguf>\n  e.g.  python3 agentic_server.py --pull bartowski/Llama-3.2-3B-Instruct-GGUF Llama-3.2-3B-Instruct-Q4_K_M.gguf")
            return
        _A.repo, _A.file = rest[0], rest[1]
        return llm_pull(_A)
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__)
        return
    log("starting Agentic from %s" % WEB_ROOT)
    if "web" in SKIP:
        log("- web      skipped (AGENTIC_NO)")
    else:
        handler = functools.partial(WebHandler, directory=WEB_ROOT)
        serve("web", handler, WEB_PORT, WEB_HOST)
    try:
        llm_up = start_llm()
    except BaseException as e:
        log("- llm      failed to start (%s)" % str(e)[:160])
        llm_up = False
    if not llm_up:
        try:
            start_ollama()
        except Exception as e:
            log("- ollama   failed to start (%s)" % str(e)[:160])
    else:
        log("- ollama   not needed (built-in model server is on :11434)")
    threading.Thread(target=_load_crescent, daemon=True).start()
    serve("crescent", CrescentHandler, CRESCENT_PORT, CRESCENT_HOST)
    serve("proxy", ProxyHandler, PROXY_PORT)
    serve("kokoro", KokoroHandler, KOKORO_PORT)
    serve("whisper", WhisperHandler, WHISPER_PORT)
    threading.Thread(target=preload_voice, daemon=True).start()
    time.sleep(0.5)  # let the background Crescent search report first if it is quick
    for b in ("yt-dlp", "mpv"):
        if not shutil.which(b):
            log("note: %s not on PATH (needed for Crescent music)" % b)
    print("\nOpen  http://localhost:%d/index.html     (Ctrl+C stops everything)\n" % WEB_PORT, flush=True)
    if "web" not in SKIP and not os.environ.get("AGENTIC_NO_OPEN") and port_open(WEB_PORT):
        def _open_browser():
            try:
                import webbrowser
                time.sleep(1)
                if not webbrowser.open("http://localhost:%d/index.html" % WEB_PORT):
                    log("couldn't open a browser window - open http://localhost:%d/index.html yourself" % WEB_PORT)
            except Exception as e:
                log("couldn't open a browser window (%s)" % str(e)[:100])
        threading.Thread(target=_open_browser, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        SESSION.kill()
        log("stopped")


if __name__ == "__main__":
    main()
