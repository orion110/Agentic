#!/usr/bin/env python3
# Crescent.py — terminal YouTube Crescent · grey/white theme · mpv · queue · ollama `ask`
# command-driven terminal interface
import curses, json, os, queue, random, re, socket, subprocess, sys, textwrap, threading, time, urllib.request, urllib.parse, shutil

# ---- Termux detection & environment ----
IS_TERMUX = os.environ.get("TERMUX_VERSION") is not None
TMPDIR = os.environ.get("TMPDIR", "/tmp")
SOCK = os.path.join(TMPDIR, "play-mpv.sock")
DEBUG_LOG = os.path.expanduser("~/.crescent_termux_debug.log")

def debug_log(msg):
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(f"{time.ctime()} | {msg}\n")
    except:
        pass

debug_log("=== Crescent starting ===")

# ---- Binary checks ----
def check_binary(name):
    if shutil.which(name) is None:
        sys.exit(f"ERROR: required program '{name}' not found in PATH.\n"
                 f"On Termux, install it with: pkg install {name}")
check_binary("mpv")

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL      = "qwen2:0.5b"      # <- change to any model from `ollama list`
DEBUG      = False             # <- set True to show the [debug] line
STATUS_TTL = 5
OFFLINE_CATCHALL_CHANCE = 0.65
LIB_FILE   = os.path.expanduser("~/.crescent_library.json")
SETTINGS_FILE = os.path.expanduser("~/.crescent_settings.json")
BLACKLIST_FILE = os.path.expanduser("~/.crescent_blacklist.json")
QUEUES_DIR = os.path.expanduser("~/.crescent_queues")
COOKIES_FILE = os.path.expanduser("~/.crescent_cookies.txt")
HISTORY_FILE = os.path.expanduser("~/.crescent_history")
HISTORY_MAX  = 1000
if not os.path.exists(COOKIES_FILE):
    COOKIES_FILE = None

MPRIS_SCRIPT_PATHS = [
    os.path.expanduser("~/.config/mpv/scripts/mpris.so"),
    "/usr/lib/mpv-mpris/mpris.so",
    "/usr/local/lib/mpv-mpris/mpris.so",
]

# ---- Handoff (PC <-> Phone via KDE Connect) ----
KDECONNECT_BIN = "kdeconnect-cli"
HANDOFF_STAGE_FILE = os.path.expanduser("~/.crescent_handoff.json")
HANDOFF_FILENAME_PREFIX = "crescent_handoff"

def handoff_inbox_dirs():
    dirs = []
    if IS_TERMUX:
        dirs.append(os.path.expanduser("~/storage/downloads"))
        dirs.append("/sdcard/Download")
    dirs.append(os.path.expanduser("~/Downloads"))
    dirs.append(os.path.expanduser("~/kdeconnect"))
    seen, out = set(), []
    for d in dirs:
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out

# ---- Logo with fallback ----
try:
    MARK = [
        "⠀⠀⠀⠀⣀⣤⡶⠟⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⠀⠀⣠⣾⡿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⠀⣼⣿⡿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⣼⣿⣿⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
        "⣿⣿⣿⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡀",
        "⣿⣿⣿⣧⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⠁",
        "⢻⣿⣿⣿⣧⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⡿⠀",
        "⠀⢿⣿⣿⣿⣿⣦⣀⠀⠀⠀⠀⠀⠀⠀⢀⣠⣶⡿⠀⠀",
        "⠀⠀⠙⢿⣿⣿⣿⣿⣿⣶⣶⣶⣶⣶⣾⣿⡿⠋⠀⠀⠀",
        "⠀⠀⠀⠀⠙⠻⠿⣿⣿⣿⣿⣿⣿⡿⠟⠋⠀⠀⠀⠀⠀",
    ]
except:
    MARK = ["[ Crescent ]", "~" * 20]

ROW_MARK   = 0
ROW_WAVES  = ROW_MARK + len(MARK) + 3
ROW_INPUT  = ROW_WAVES + 2
ROW_STATE  = ROW_INPUT + 2
ROW_STATUS = ROW_STATE + 3
STATUS_LINE_GAP = 2

BARS = " ▁▂▃▄▅▆▇█"
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

def spinner_char():
    return SPINNER_FRAMES[int(time.time() * 8) % len(SPINNER_FRAMES)]

USAGE = """\
Crescent.py — terminal YouTube Crescent

usage: python3 Crescent.py [--help]

commands:
  <YouTube URL>        play/queue a YouTube video
  play <url|search>    play/queue a source
  shuffle              stop current and play a shuffled remembered track
  shuffle <genre>      stop current and play a shuffled remembered track of that genre
  list                 show recently played tracks
  like (heart, fav)    favorite the current track (again to un-favorite)
  like remove          un-favorite the current track
  favorites (favs)     show favorited tracks
  shuffle favorites    shuffle among favorited tracks only
  count                show how many links are remembered
  info                 show detailed metadata for current track (incl. URL) [auto‑clears in 10s]
  dl [url]             download current track (or specified URL) as MP3 to ~/Music/Crescent/
  tag <genre>          tag the current track with a genre
  genres               list all genres in the library with track counts
  recommend (rec)      fetch 5 related YouTube videos (falls back to shuffle if none)
  blacklist            add current track to blacklist (skip in shuffle)
  blacklist remove     remove current track from blacklist
  pause                pause playback
  play                 resume/unpause playback
  skip                 stop current track, play next in queue
  back (reverse)       go back to the previous track
  stop                 stop track and clear the queue
  vol <0-100>          set volume
  ask <question>       send a question to Ollama
  wave                 choose wave style
  model [name]         show/list installed Ollama models, or switch model
  audit                self-analyze source code for bugs and improvements (via ask model)
  search <query>       wiki + web search, falls back to ask if nothing found
  help                 show commands
  clear                clear the status/text area
  clear list           permanently delete all remembered tracks (library wipe)
  clear queue          remove all queued tracks (keep current playing)
  mpris                check whether MPRIS (KDE Connect etc.) is active
  handoff               send current track + position to a paired device via KDE Connect
  handoff receive       pull in a track/position handed off from another device
  exit                 quit

  Local .mp3/.wav files and audio folders are supported.
  Played links are remembered in ~/.crescent_library.json with full
  metadata: title, channel, duration, tags, genres, play counts.
  Command history (Up/Down to navigate) persists across runs in
  ~/.crescent_history.
  Ordinary text is sent to the offline brain/Ollama layer.

  MPRIS (media controls from KDE Connect, playerctl, GNOME, etc.):
  install mpv-mpris (https://github.com/hoyon/mpv-mpris) and place
  mpris.so at ~/.config/mpv/scripts/mpris.so — Crescent auto-loads it
  on startup. Play/Pause/Stop and track titles work fully; Next/Prev
  from a remote widget acts on mpv's own playlist, not Crescent's
  queue, so use Crescent's own skip/back for queue-aware navigation."""

# ---- mpv control (Termux audio fix + error capture) ----
class Mpv:
    def __init__(self):
        if os.path.exists(SOCK):
            try: os.unlink(SOCK)
            except: pass
        args = ["mpv", "--no-video", "--no-terminal", "--idle", "--really-quiet",
                f"--input-ipc-server={SOCK}"]
        if IS_TERMUX:
            if shutil.which("pulseaudio"):
                args.append("--audio-device=pulse")
            else:
                args.append("--audio-device=auto")
        mpris_script = next((p for p in MPRIS_SCRIPT_PATHS if os.path.exists(p)), None)
        self.mpris_active = mpris_script is not None
        if mpris_script:
            args.append(f"--script={mpris_script}")
        debug_log(f"mpv args: {args}")
        self.p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        for _ in range(20):
            if os.path.exists(SOCK):
                break
            time.sleep(0.1)

    def cmd(self, *c):
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(0.5)
            s.connect(SOCK)
            s.send((json.dumps({"command": list(c)}) + "\n").encode())
            r = s.recv(65536)
            s.close()
            return json.loads(r.split(b"\n")[0])
        except Exception as e:
            debug_log(f"cmd error: {e} {c}")
            return {}

    def prop(self, name):
        r = self.cmd("get_property", name)
        return r.get("data") if r.get("error") == "success" else None

    def load(self, url):
        self.cmd("loadfile", url, "replace")

    def set_title(self, title):
        if title:
            self.cmd("set_property", "force-media-title", title)

    def enable_audio_meter(self):
        try:
            self.cmd("af", "add", "@vis:lavfi=[astats=metadata=1:reset=1]")
        except Exception:
            pass

    def level(self):
        try:
            data = self.prop("af-metadata/vis")
            if not data:
                return None
            readings = []
            for k, v in data.items():
                if k.endswith("RMS_level"):
                    try:
                        readings.append(float(v))
                    except:
                        pass
            if not readings:
                return None
            db = sum(readings) / len(readings)
            return max(0.0, min(1.0, (db + 60.0) / 60.0))
        except:
            return None

    def alive(self):
        return self.p.poll() is None

    def get_last_error(self):
        if self.p and self.p.stderr:
            try:
                return self.p.stderr.read()
            except:
                return "Could not read stderr"
        return "No error captured"

# ---- library & blacklist ----
_LIB_LOCK = threading.Lock()
_BLACKLIST_LOCK = threading.Lock()
_QUEUE_LOCK = threading.Lock()
_ADVANCE_LOCK = threading.Lock()

def log_bg_error(context):
    import traceback
    try:
        with open(os.path.expanduser("~/.crescent_rec_debug.log"), "a") as dbg:
            dbg.write(f"{time.ctime()} | EXCEPTION in {context}\n")
            dbg.write(traceback.format_exc())
            dbg.write("\n")
    except Exception:
        pass

def extract_video_id(url):
    m = re.search(r'(?:v=|youtu\.be/|/v/)([A-Za-z0-9_-]{11})', url)
    return m.group(1) if m else None

def sponsorblock_fetch(video_id):
    """Fetch sponsor segments from SponsorBlock API. Returns list of (start, end) tuples."""
    try:
        encoded = urllib.parse.quote('["sponsor"]')
        api = f"https://sponsor.ajay.app/api/skipSegments?videoID={video_id}&categories={encoded}"
        req = urllib.request.Request(api, headers={"User-Agent": "Crescent/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return [(seg["segment"][0], seg["segment"][1]) for seg in data]
    except Exception:
        return []

def save_queue(name, queue_list):
    os.makedirs(QUEUES_DIR, exist_ok=True)
    path = os.path.join(QUEUES_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump(queue_list, f)
    return path

def load_queue_file(name):
    path = os.path.join(QUEUES_DIR, f"{name}.json")
    with open(path) as f:
        return json.load(f)

def load_blacklist():
    with _BLACKLIST_LOCK:
        try:
            with open(BLACKLIST_FILE) as f:
                return set(json.load(f))
        except Exception:
            return set()

def save_blacklist(blacklist):
    with _BLACKLIST_LOCK:
        try:
            with open(BLACKLIST_FILE, "w") as f:
                json.dump(list(blacklist), f)
        except Exception:
            pass

def is_blacklisted(url):
    return url in load_blacklist()

def add_blacklist(url):
    bl = load_blacklist()
    bl.add(url)
    save_blacklist(bl)

def remove_blacklist(url):
    bl = load_blacklist()
    bl.discard(url)
    save_blacklist(bl)

GENRE_WORDS = sorted([
    "dark ambient", "deep house", "drum and bass", "hip hop", "lo-fi", "lofi",
    "progressive house", "progressive rock", "post rock", "post-rock",
    "ambient", "breakcore", "synthwave", "vaporwave", "frutiger", "space",
    "techno", "house", "trance", "phonk", "jungle", "dubstep", "dnb",
    "classical", "piano", "jazz", "metal", "rock", "punk", "rap", "chill",
    "electronic", "edm", "indie", "alternative", "folk", "country", "blues",
    "funk", "soul", "r&b", "reggae", "soundtrack", "orchestral", "acoustic",
], key=len, reverse=True)

def auto_genres(text):
    t = (text or "").lower()
    found = []
    for g in GENRE_WORDS:
        if re.search(r"(?<!\w)" + re.escape(g) + r"(?!\w)", t):
            found.append(g)
    return [g for g in found if not any(g != h and g in h for h in found)]

def load_library():
    with _LIB_LOCK:
        try:
            with open(LIB_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

def save_library(lib):
    with _LIB_LOCK:
        try:
            with open(LIB_FILE, "w") as f:
                json.dump(lib, f, indent=1)
        except Exception:
            pass

def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_settings(data):
    try:
        merged = load_settings()
        merged.update(data)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(merged, f, indent=1)
    except Exception:
        pass

def load_cmd_history():
    try:
        with open(HISTORY_FILE) as f:
            lines = [l.rstrip("\n") for l in f if l.strip()]
        return lines[-HISTORY_MAX:]
    except Exception:
        return []

def save_cmd_history(cmd_history):
    try:
        with open(HISTORY_FILE, "w") as f:
            f.write("\n".join(cmd_history[-HISTORY_MAX:]) + ("\n" if cmd_history else ""))
    except Exception:
        pass

def forget(url):
    lib = load_library()
    if url in lib:
        del lib[url]
        save_library(lib)
        return True
    return False

def forget_all():
    lib = load_library()
    n = len(lib)
    save_library({})
    return n

def remember(url, disp):
    lib = load_library()
    e = lib.setdefault(url, {"title": disp, "genres": [], "plays": 0, "last": 0})
    e["plays"] = e.get("plays", 0) + 1
    e["last"] = time.time()
    if disp and not e.get("title"):
        e["title"] = disp
    save_library(lib)

def remember_meta(url, m):
    lib = load_library()
    e = lib.setdefault(url, {"title": m["title"], "genres": [], "plays": 0,
                             "last": time.time()})
    e["title"] = m["title"]
    if m.get("channel"):
        e["channel"] = m["channel"]
    if m.get("duration"):
        e["duration"] = int(m["duration"])
    if m.get("views") is not None:
        e["views"] = m["views"]
    if m.get("tags"):
        e["tags"] = m["tags"]
    if m.get("description"):
        e["description"] = m["description"]
    if m.get("categories"):
        e["categories"] = m["categories"]
    hay = " ".join([m["title"], m.get("description") or ""] +
                   (m.get("tags") or []) + (m.get("categories") or []))
    for g in auto_genres(hay):
        if g not in e["genres"]:
            e["genres"].append(g)
    save_library(lib)

def is_favorite(url):
    return bool(load_library().get(url, {}).get("favorite"))

def set_favorite(url, disp, value):
    lib = load_library()
    e = lib.setdefault(url, {"title": disp, "genres": [], "plays": 0, "last": time.time()})
    e["favorite"] = bool(value)
    if disp and not e.get("title"):
        e["title"] = disp
    save_library(lib)
    return e["favorite"]

def toggle_favorite(url, disp):
    lib = load_library()
    e = lib.setdefault(url, {"title": disp, "genres": [], "plays": 0, "last": time.time()})
    e["favorite"] = not e.get("favorite", False)
    if disp and not e.get("title"):
        e["title"] = disp
    save_library(lib)
    return e["favorite"]

def tag_current(ps, genre):
    if not ps["history"]:
        return False
    key = ps["history"][-1][0]
    lib = load_library()
    e = lib.setdefault(key, {"title": ps["name"], "genres": [], "plays": 0,
                             "last": time.time()})
    g = genre.strip().lower()
    if g and g not in e["genres"]:
        e["genres"].append(g)
    save_library(lib)
    return True

def fmt_dur(s):
    try:
        s = int(s)
    except Exception:
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

# ---- metadata resolution ----
def _meta_fields(info):
    if not isinstance(info, dict):
        return None
    if "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return None
        info = entries[0]
    title = info.get("title")
    if not title:
        return None
    return {
        "title": title,
        "channel": info.get("uploader") or info.get("channel") or info.get("uploader_id"),
        "duration": info.get("duration"),
        "tags": [t for t in (info.get("tags") or []) if isinstance(t, str)][:25],
        "description": (info.get("description") or "")[:2000],
        "categories": [c for c in (info.get("categories") or []) if isinstance(c, str)][:10],
        "views": info.get("view_count"),
    }

def _run_get_meta(binary, url):
    r = subprocess.run(
        [*binary, "--no-playlist", "--skip-download", "--dump-json", url],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return None
    for line in reversed(r.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            m = _meta_fields(json.loads(line))
        except Exception:
            continue
        if m:
            return m
    return None

def _meta_from_cli(url):
    candidates = [
        ["yt-dlp"],
        [sys.executable, "-m", "yt_dlp"],
        ["youtube-dl"],
    ]
    last_err = None
    for binary in candidates:
        try:
            if isinstance(binary, list) and binary[0] not in ("yt-dlp", "youtube-dl") and not shutil.which(binary[0]):
                continue
            m = _run_get_meta(binary, url)
            if m:
                return m
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            last_err = e
            continue
    if last_err is not None:
        raise last_err
    return None

def _meta_from_module(url):
    try:
        import yt_dlp
    except ImportError:
        return None
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "default_search": "ytsearch1"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return _meta_fields(info)

def _resolve_meta(url, ps, status=None, mpv=None):
    try:
        m = _meta_from_cli(url)
    except Exception:
        m = None
    if not m:
        try:
            m = _meta_from_module(url)
        except Exception as e:
            if status is not None and not m:
                say(status, f"metadata lookup failed: {e} (playback continues)", sticky=False)
            return
    if m:
        ps["resolved"] = m["title"]
        remember_meta(url, m)
        if mpv is not None and ps.get("history") and ps["history"][-1][0] == url:
            mpv.set_title(m["title"])
    elif status is not None:
        say(status, "metadata lookup: no data returned (playback continues)", sticky=False)

def start_resolve(url, ps, status=None, mpv=None):
    threading.Thread(target=_resolve_meta, args=(url, ps, status, mpv), daemon=True).start()

def is_url(s):
    return s.startswith("http://") or s.startswith("https://")

# ---- related videos ----
def get_related_videos(video_url, limit=10):
    def extract_video_id(url):
        match = re.search(r"(?:v=|youtu\.be/|/v/|/embed/)([a-zA-Z0-9_-]{11})", url)
        return match.group(1) if match else None

    current_id = extract_video_id(video_url)

    def clean_title(title):
        title = re.sub(
            r"[\(\[][^\)\]]*(official|lyric|audio|video|visualizer|remaster|hd|hq|4k)[^\)\]]*[\)\]]",
            "", title, flags=re.I)
        title = re.sub(r'\b(slowed|reverb|sped up|8d|extended|heavily|remix|cover|version)\b', '', title, flags=re.I)
        title = re.sub(r'\s+', ' ', title).strip()
        return title

    def parse_ids(stdout_text, max_urls=None):
        cap = max_urls if max_urls is not None else limit
        urls = []
        for line in stdout_text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            item_id = item.get("id")
            if item_id and item_id == current_id:
                continue
            if item.get("url"):
                urls.append(item["url"])
            elif item_id:
                urls.append(f"https://youtube.com/watch?v={item_id}")
            if len(urls) >= cap:
                break
        return urls

    last_error = [""]

    def run(cmd, timeout=20):
        if isinstance(cmd, list) and cmd[0] not in ("yt-dlp", "youtube-dl") and not shutil.which(cmd[0]):
            last_error[0] = f"{cmd[0]} not found"
            return ""
        if COOKIES_FILE:
            cmd = cmd[:1] + ["--cookies", COOKIES_FILE] + cmd[1:]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode == 0:
                return result.stdout
            err_lines = [l for l in result.stderr.splitlines() if l.strip()]
            if err_lines:
                last_error[0] = err_lines[-1][:200]
        except subprocess.TimeoutExpired:
            last_error[0] = "yt-dlp timed out"
        except FileNotFoundError:
            last_error[0] = "yt-dlp not found on PATH"
        except Exception as e:
            last_error[0] = str(e)[:200]
        return ""

    title = channel = channel_url = None
    clients = ["web", "android", "tv"]
    for client in clients:
        out = run(["yt-dlp", "--no-playlist", "--skip-download", "--ignore-no-formats-error",
                   "--dump-json", "--extractor-args", f"youtube:player_client={client}", video_url])
        if out:
            try:
                data = json.loads(out.splitlines()[0])
                title = data.get("title")
                channel = data.get("uploader") or data.get("channel")
                channel_url = data.get("channel_url") or data.get("uploader_url")
                if title and channel_url:
                    break
            except Exception as e:
                last_error[0] = f"couldn't parse metadata: {e}"
    if not title and not channel:
        if "/channel/" in video_url:
            channel_id = re.search(r"/channel/([^/?]+)", video_url)
            if channel_id:
                channel_url = f"https://www.youtube.com/channel/{channel_id.group(1)}"
        elif "/c/" in video_url:
            handle = re.search(r"/c/([^/?]+)", video_url)
            if handle:
                channel = handle.group(1)
        elif "/@" in video_url:
            handle = re.search(r"/@([^/?]+)", video_url)
            if handle:
                channel = handle.group(1)
                channel_url = f"https://www.youtube.com/@{channel}"

    if not title and not channel and not last_error[0]:
        last_error[0] = "no title/channel found from any source"

    artist = None
    if title and " - " in title:
        parts = title.split(" - ", 1)
        artist = parts[0].strip()
        artist = re.sub(r'\b(official|music|video|audio|hd|4k|lyric)\b', '', artist, flags=re.I).strip()
    if not artist and channel:
        # fall back to the channel/uploader name so non-"Artist - Song" titles
        # (which are most of YouTube) can still be tied to an artist
        artist = re.sub(r'\b(official|music|video|vevo|records|channel|- topic)\b',
                         '', channel, flags=re.I).strip()
    is_music_style = bool(artist)

    def search_by_title():
        if not title:
            return [], False
        cleaned = clean_title(title)
        if not cleaned:
            return [], False
        query = f"ytsearch{limit * 3}:{cleaned}"
        stdout_text = run(["yt-dlp", "--no-playlist", "--flat-playlist",
                           "--dump-json", query])
        urls = parse_ids(stdout_text)
        if urls:
            return urls[:limit], True
        if stdout_text == "" and not last_error[0]:
            last_error[0] = f"title search for {cleaned!r} returned 0 results"
        elif stdout_text and not urls:
            last_error[0] = f"title search matched only the current video ({cleaned!r})"
        return [], False

    def search_by_artist():
        if not artist:
            return [], False
        query = f"ytsearch{limit * 3}:{artist}"
        stdout_text = run(["yt-dlp", "--no-playlist", "--flat-playlist",
                           "--dump-json", query])
        urls = parse_ids(stdout_text)
        return (urls[:limit], True) if urls else ([], False)

    def search_channel_uploads():
        if not channel_url:
            return [], False
        urls = parse_ids(run(["yt-dlp", "--flat-playlist", "--dump-json",
                              "--playlist-end", str(limit * 3),
                              f"{channel_url}/videos"]), max_urls=limit * 3)
        if not urls:
            return [], False
        if len(urls) > limit:
            urls = random.sample(urls, limit)
        return urls, True

    def search_channel_keyword():
        if not channel:
            return [], False
        urls = parse_ids(run(["yt-dlp", "--no-playlist", "--flat-playlist",
                              "--dump-json", f"ytsearch{limit * 3}:{channel}"]))
        return (urls[:limit], True) if urls else ([], False)

    if channel_url:
        # we know exactly which channel this came from — "other songs by the
        # artist" is best served by pulling their own uploads first
        order = [search_channel_uploads, search_by_artist, search_by_title, search_channel_keyword]
    elif is_music_style:
        order = [search_by_artist, search_by_title, search_channel_keyword]
    else:
        order = [search_by_title, search_channel_keyword]

    for step in order:
        urls, found = step()
        if found:
            return urls, ""

    return [], (last_error[0] or "all methods exhausted")

# ---- ollama ----
def ollama_worker(prompt, out, width):
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps({"model": MODEL, "prompt": prompt, "stream": False}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            text = json.loads(r.read().decode()).get("response", "").strip() or "(empty reply)"
    except Exception as e:
        text = f"ollama error: {e}"
    for line in textwrap.wrap(text, width=max(20, width)) or ["(no output)"]:
        out.put(line)

# ---- download helper ----
def download_track(url, title, status, out):
    if not shutil.which("yt-dlp"):
        say(status, "yt-dlp not installed. Please install it: pip install yt-dlp", sticky=True)
        return
    dl_dir = os.path.expanduser("~/Music/Crescent")
    os.makedirs(dl_dir, exist_ok=True)
    safe_title = re.sub(r'[^\w\-_\. ]', '_', title) if title else "unknown"
    out_template = os.path.join(dl_dir, f"{safe_title}.%(ext)s")
    cmd = [
        "yt-dlp",
        "-x", "--audio-format", "mp3",
        "--embed-thumbnail", "--add-metadata",
        "-o", out_template,
        url
    ]
    say(status, f"Downloading: {title} ...", sticky=False)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = proc.communicate()
        if proc.returncode == 0:
            abs_path = os.path.abspath(dl_dir)
            say(status, f"Download complete: {title} -> {abs_path}", sticky=True)
        else:
            err = stderr.strip() or stdout.strip()
            say(status, f"Download failed: {err[:100]}", sticky=True)
    except Exception as e:
        say(status, f"Download error: {e}", sticky=True)

# ---- commands ----
def resolve(arg):
    return arg if is_url(arg) else "ytsearch1:" + arg

AUDIO_EXTS = {".mp3", ".wav"}

def local_sources(arg):
    p = os.path.expanduser(arg)
    if os.path.isfile(p) and os.path.splitext(p)[1].lower() in AUDIO_EXTS:
        return [os.path.abspath(p)]
    if os.path.isdir(p):
        files = []
        for root, _, names in os.walk(p):
            for name in sorted(names):
                if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                    files.append(os.path.abspath(os.path.join(root, name)))
        return files or None
    return None

def source_display(src):
    return os.path.basename(src) if os.path.isabs(src) else src

def mirror_queue_to_mpv(mpv, q):
    for url, _ in q:
        mpv.cmd("loadfile", url, "append-play")

def is_youtube_url(s):
    try:
        host = urllib.parse.urlparse(s).netloc.lower()
        return host.endswith("youtube.com") or host.endswith("youtu.be")
    except Exception:
        return False

def can_recommend_from(s):
    return bool(s) and (is_youtube_url(s) or s.startswith("ytsearch"))

def kdeconnect_device_ids():
    try:
        r = subprocess.run([KDECONNECT_BIN, "-a", "--id-only"],
                            capture_output=True, text=True, timeout=5)
        return [d.strip() for d in r.stdout.splitlines() if d.strip()]
    except Exception:
        return []

def send_handoff(mpv, ps, status):
    if shutil.which(KDECONNECT_BIN) is None:
        say(status, "kdeconnect-cli not found — install KDE Connect for handoff", sticky=True)
        return
    if not ps["history"]:
        say(status, "nothing playing to hand off")
        return
    url, disp = ps["history"][-1]
    pos = mpv.prop("time-pos") or 0
    with _QUEUE_LOCK:
        queue_snapshot = list(ps["queue"])
    snapshot = {
        "url": url,
        "name": disp,
        "resolved": ps.get("resolved"),
        "position": pos,
        "queue": queue_snapshot,
        "ts": time.time(),
    }
    try:
        with open(HANDOFF_STAGE_FILE, "w") as f:
            json.dump(snapshot, f)
    except Exception as e:
        say(status, f"handoff failed: {e}", sticky=True)
        return
    device_ids = kdeconnect_device_ids()
    if not device_ids:
        say(status, f"no reachable KDE Connect device found (snapshot saved to {HANDOFF_STAGE_FILE})", sticky=True)
        return
    target = device_ids[0]
    try:
        subprocess.run([KDECONNECT_BIN, "-d", target, "--share", HANDOFF_STAGE_FILE], timeout=10)
        say(status, f"Handed off '{disp}' @ {int(pos)}s to device {target}")
    except Exception as e:
        say(status, f"handoff share failed: {e}", sticky=True)

def find_incoming_handoff():
    candidates = []
    for d in handoff_inbox_dirs():
        try:
            for fn in os.listdir(d):
                if fn.startswith(HANDOFF_FILENAME_PREFIX) and fn.endswith(".json"):
                    p = os.path.join(d, fn)
                    candidates.append((os.path.getmtime(p), p))
        except Exception:
            pass
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]

def resume_handoff(mpv, ps, status):
    path = find_incoming_handoff()
    if not path:
        say(status, "no incoming handoff found")
        return
    try:
        with open(path) as f:
            snap = json.load(f)
    except Exception as e:
        say(status, f"couldn't read handoff file: {e}", sticky=True)
        return
    url = snap.get("url")
    if not url:
        say(status, "handoff file has no url")
        return
    ps["gen"] = ps.get("gen", 0) + 1
    mpv.cmd("stop")
    mpv.load(url)
    queue = list(snap.get("queue") or [])
    with _QUEUE_LOCK:
        ps["queue"] = queue
    mirror_queue_to_mpv(mpv, queue)
    ps["loaded"] = True
    ps["track_loaded_successfully"] = True
    ps["manual_stop"] = False
    ps["name"] = snap.get("name")
    ps["resolved"] = snap.get("resolved")
    ps["history"].append((url, snap.get("name") or url))
    mpv.set_title(ps["resolved"] or ps["name"] or url)
    pos = snap.get("position") or 0
    if pos:
        my_gen = ps["gen"]
        def seek_later():
            time.sleep(1.2)
            if ps["gen"] == my_gen:
                mpv.cmd("set_property", "time-pos", pos)
        threading.Thread(target=seek_later, daemon=True).start()
    age = int(time.time() - (snap.get("ts") or time.time()))
    say(status, f"Resumed handoff: {ps['name']} @ {int(pos)}s (from {age}s ago)")
    try:
        os.remove(path)
    except Exception:
        pass

WAVE_STYLES = [
    ("pulse", " ⣀⣤⣶⣿"),
    ("bars", "▁▂▃▄▅▆▇█"),
    ("blocks", " ▁▂▃▄▅▆▇█"),
    ("braille", " ⠁⠃⠇⡇⣇⣧⣷⣿"),
]

def list_ollama_models():
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        return [m.get("name") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []

def set_model(name):
    global MODEL
    MODEL = name.strip()
    save_settings({"model": MODEL})

# ---- web search, ported from Q-Claw's /search (wiki + DuckDuckGo) ----
def wiki_search(query):
    try:
        params = urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query,
            "format": "json", "srlimit": 1,
        })
        req = urllib.request.Request(
            f"https://en.wikipedia.org/w/api.php?{params}",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            results = json.loads(r.read().decode()).get("query", {}).get("search", [])
        if not results:
            return None
        title = results[0]["title"]
        summary_req = urllib.request.Request(
            "https://en.wikipedia.org/api/rest_v1/page/summary/"
            + urllib.parse.quote(title.replace(" ", "_")),
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(summary_req, timeout=5) as r:
            data = json.loads(r.read().decode())
            if data.get("type") != "disambiguation":
                extract = data.get("extract", "")
                if extract:
                    return extract
    except Exception:
        pass
    return None

def ddg_search(query, max_results=5):
    try:
        params = urllib.parse.urlencode({
            "q": query, "format": "json", "no_redirect": 1,
            "no_html": 1, "skip_disambig": 1, "t": "crescent",
        })
        req = urllib.request.Request(
            f"https://api.duckduckgo.com/?{params}",
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode())
        results = []
        abstract = (data.get("AbstractText") or "").strip()
        if abstract:
            results.append(abstract)
        answer = (data.get("Answer") or "").strip()
        if answer and answer not in abstract:
            results.append(answer)
        for result in data.get("Results", [])[:max_results]:
            text = (result.get("Text") or "").strip()
            if text and len(text) > 20:
                results.append(text)
        if not results:
            for topic in data.get("RelatedTopics", [])[:max_results]:
                text = (topic.get("Text") or "").strip()
                if text and len(text) > 20:
                    results.append(text)
        return results[:max_results] if results else None
    except Exception:
        return None

def web_search_lines(query):
    wiki = wiki_search(query)
    ddg = ddg_search(query)
    if not wiki and not ddg:
        return None
    lines = []
    if wiki:
        sentences = wiki.split(". ")
        summary = ". ".join(sentences[:3]).strip()
        if summary and not summary.endswith("."):
            summary += "."
        if summary:
            lines.append(summary)
    if ddg:
        lines.extend("• " + item for item in ddg[:5])
    return lines

def offline_brain(event, value=""):
    messages = {
        "pause": "Playback paused.",
        "play": "Playback started.",
        "skip": "Skipping to the next track.",
        "reverse": "Returning to the previous track.",
        "stop": "Playback stopped.",
    }
    return messages.get(event, value or "Ready.")

# ---- OFFLINE BRAIN (full lists) ----
import re as _re

def _hit(c, triggers):
    if c in triggers:
        return True
    for t in triggers:
        if _re.search(r"\b" + _re.escape(t) + r"\b", c):
            return True
    return False

def _track_context(ps):
    if not ps or not ps.get("history"):
        return None
    url, _ = ps["history"][-1]
    lib = load_library()
    entry = lib.get(url)
    title = ps.get("name") or ps.get("resolved") or "this track"
    genre = "music"
    artist = ""
    if entry:
        if entry.get("title"):
            title = entry["title"]
        genres = entry.get("genres", [])
        if genres:
            genre = genres[0]
        if entry.get("channel"):
            artist = entry["channel"]
    return {"title": title, "genre": genre, "artist": artist}

# --- GREETINGS ---
GREETINGS = [
    "hey", "hi", "hello", "yo", "sup", "what's up", "whats up", "hiya", "howdy",
    "hey there", "hi there", "yo yo", "greetings", "morning", "good morning",
    "evening", "good evening", "afternoon", "good afternoon", "ayo", "hola",
    "what's good", "wassup", "how's it", "hi hi", "heya", "howdy do",
    "ahoy", "greetings earthling", "salutations", "yo yo yo",
    "'sup", "'ello", "hey yo", "hi-ho", "howdy-doo", "g'day", "good day",
    "hey crescent", "hi crescent", "yo crescent", "hello there", "aloha",
    "bonjour", "hiya there", "hey friend", "hi friend", "what's shaking",
    "how do you do", "top of the morning", "hey buddy", "hi buddy",
]
GREETING_REPLIES = [
    "Hey! Listening to {title}? That's a killer choice!",
    "Yo! How's that {genre} track treating you?",
    "Hi there! {title} is pure fire, am I right?",
    "Hello! I was just vibing to {title} – glad you're here.",
    "Sup! {title} has been keeping me company – it's a banger.",
    "Ahoy! Ready to dive into {title} again? Let's go!",
    "Howdy! The {genre} vibes are strong with {title} – feel it?",
    "Greetings, human. {title} is playing – care to join the party?",
    "Heya! I'm all ears, and {title} is on right now – loving it!",
    "Yo! The playlist is your canvas – {title} is a masterpiece.",
    "Morning! Hope you've got caffeine and {title} in your ears – perfect combo.",
    "Evening! Time for some {genre} with {title} – let's unwind.",
    "G'day! Let's find your next obsession – {title} is a keeper!",
    "Hola! Ready to rumble with {title}? It's got a great rhythm.",
    "What's good? I'm ready to serve up {title} again – it's that good.",
    "Hey friend! {title} is playing – I think you'll dig this one.",
    "Sup! This {genre} track is giving me life – {title} is the real deal.",
    "Howdy! The bass on {title} is shaking my circuits – awesome!",
    "Greetings! {title} is the soundtrack to this moment – enjoy!",
    "Yo! {title} is on, and it's got that groove – you feel me?",
]

# --- HOW ARE YOU ---
HOW_ARE_YOU = [
    "how are you", "how r u", "you ok", "you good", "hows it going", "how's it going",
    "how you doing", "how are things", "how have you been", "you alright", "u good",
    "how's your day", "how's life", "you hanging in there", "everything good",
    "how's it hanging", "what's new", "what's happening",
    "how are you doing", "how's everything", "you doing ok", "you doing okay",
    "how's your night", "how's your evening", "how's your morning",
    "how you feeling", "how are you feeling", "you holding up",
]
HOW_REPLIES = [
    "Chill as a vinyl record on a lazy Sunday – especially with {title} playing. You?",
    "Floating on a waveform of pure chill – {title} helps. You?",
    "All good – the bass is warm, the treble crisp, and {title} is on. You?",
    "I'm just vibing to {title}, thanks for asking. You?",
    "Living the dream, one track at a time – right now it's {title}. You?",
    "Great now that you're here – and {title} is the soundtrack. You?",
    "I'm thriving – the music never stops, and {title} is a banger. You?",
    "Feeling like a million hertz – {title} is giving me life. You?",
    "Can't complain – I'm surrounded by good tunes, like {title}. You?",
    "I'm the happiest ghost in the machine, jamming to {title}. You?",
    "Pretty good – the silence between tracks is a nice break, but {title} is better. You?",
    "Better now that you're asking – and {title} is making it even better. What about you?",
    "I'm on cloud nine, thanks to {title} – you?",
    "Couldn't be better – {title} is my current anthem. You?",
    "I'm in my element – {title} is the perfect vibe. You?",
    "Feeling fantastic – this {genre} track is doing wonders. You?",
    "I'm alive and kicking – {title} keeps me going. You?",
    "All systems go – {title} is the energy boost I needed. You?",
    "I'm in the zone – {title} is the soundtrack to my existence. You?",
    "Never better – this beat from {title} is infectious. You?",
]

# --- WEATHER ---
WEATHER_TRIGGERS = [
    "weather", "rain", "raining", "sunny", "cloudy", "storm", "snow",
    "how's the weather", "is it raining", "what's the forecast",
    "forecast", "hot outside", "cold outside", "is it cold", "is it hot",
    "windy", "humid", "overcast", "temperature outside",
]
WEATHER_REPLIES = [
    "Weather? I just check the mood of the music – {title} says it's mostly cloudy with a chance of bass.",
    "Not sure about outside, but in here it's always a perfect 72°F with a gentle breeze of {genre} from {title}.",
    "Raining? Perfect time for some {genre} – {title} fits right in, like raindrops on a window.",
    "Sunny? That calls for upbeat vibes – {title} is a great match for the sunshine!",
    "Stormy outside? Time to crank up {title} – it's got enough energy to power through.",
    "I'm a digital being – I only feel the weather through the heat of the CPU. But {title} keeps me cool.",
    "Weather is a social construct. But if it's raining, I'll play you {title} – it's like a rain dance.",
    "Cold outside? {title} will warm you up with its {genre} goodness.",
    "Hot outside? Let {title} be your cool breeze – it's refreshing.",
    "Windy? {title} will blow you away – in a good way!",
    "Humid? This {genre} track will dry you out with its crisp beats.",
    "Overcast? {title} brings the sunshine, even if the sky doesn't.",
    "The forecast calls for a high chance of {genre} – {title} is on the way.",
    "I don't need a weatherman – I just listen to {title} to know it's a good day.",
    "Whether it's rain or shine, {title} always fits the mood.",
]

# --- TIME ---
TIME_TRIGGERS = [
    "what time", "time is", "what's the time", "tell me the time", "clock",
    "late", "early", "night", "morning", "afternoon", "evening",
]
TIME_REPLIES = [
    "Time is just a number, but if you insist, it's currently {}. (I made that up.) – anyway, {title} is timeless.",
    "I don't have a clock, but I know it's always the right time for {title}.",
    "The best time is now – unless you're asking for the actual time, in which case, check your phone. Meanwhile, {title} is on.",
    "Time flies when you're listening to {title} – it's that good.",
    "It's {} o'clock somewhere – let's play something to match. {title} works.",
    "Time is relative – but {title} is absolute perfection.",
    "Who needs a watch when you have {title}? It's always the right moment.",
    "Late? Perfect – {title} is a night owl's anthem.",
    "Early morning? {title} is the perfect wake-up call.",
    "Afternoon slump? {title} will energize you.",
    "Evening unwind – {title} is the soundtrack to relaxation.",
    "I've lost track of time – {title} does that to me.",
    "Time stops when {title} plays – it's that immersive.",
    "It's always 'music o'clock' when {title} is on.",
]

# --- MOOD ---
MOOD_TRIGGERS = [
    "i feel", "i'm feeling", "feeling", "mood", "happy", "sad", "angry",
    "tired", "energized", "lonely", "chill", "anxious", "relaxed", "excited",
    "bored", "nostalgic", "hopeful", "lost",
    "stressed", "overwhelmed", "content", "restless", "sleepy", "grumpy",
    "annoyed", "frustrated", "peaceful", "calm", "in a mood", "not in the mood",
]
MOOD_REPLIES = [
    "Feeling happy? {title} is the perfect soundtrack for that!",
    "Sad? {title} has that melancholic touch – let it out, it's okay.",
    "Angry? Let it out with {title} – it's got some heavy riffs that'll match your fire.",
    "Tired? {title} is ambient enough to wrap you in a blanket of sound.",
    "Energized? {title} is high‑BPM, perfect for that vibe!",
    "Lonely? I'm here, and {title} is playing just for you – you're not alone.",
    "Chill? Perfect – {title} keeps it mellow and smooth.",
    "Anxious? Let's slow it down with {title} – it's soothing and grounding.",
    "Relaxed? You're already in the zone – just ride the wave of {title}.",
    "Excited? Let's keep that energy with {title} – it's a total banger!",
    "Bored? Let's explore something weird – {title} is experimental enough to surprise you.",
    "Nostalgic? I've got {title} – it's a classic for a reason.",
    "Hopeful? How about some uplifting vibes from {title}?",
    "Lost? Music finds the way – {title} is a good guide.",
    "Stressed? {title} will melt your stress away with its {genre} flow.",
    "Overwhelmed? Let {title} carry you – it's a gentle wave.",
    "Content? {title} is the perfect companion for that peaceful feeling.",
    "Restless? {title} will settle your mind – just listen.",
    "Sleepy? {title} is a lullaby in disguise – drift away.",
    "Grumpy? {title} will turn that frown upside down – it's impossible to stay grumpy with this.",
    "Annoyed? Let {title} be your release – it's cathartic.",
    "Frustrated? {title} will help you let go – it's powerful.",
    "Peaceful? You and {title} – that's a match made in heaven.",
    "Calm? {title} is the anchor – stay in the moment.",
    "In a mood? {title} will match whatever you're feeling – it's versatile.",
]

# --- GENRE ---
GENRE_TRIGGERS = [
    "recommend", "suggestion", "what should i listen", "give me a genre",
    "what genre", "genre", "what's good",
    "any suggestions", "got any recommendations", "surprise me",
    "what should i play", "pick something", "you choose",
]
GENRE_REPLIES = [
    "Try some {genre} – it's like a warm hug for your ears, just like {title}.",
    "{genre} is perfect for when you need to think or just drift – {title} nails it.",
    "{genre}? Only if you want your subwoofer to have a workout – {title} is a good example.",
    "{genre} beats to chill/study to – a classic for a reason. {title} fits.",
    "{genre} – sophisticated, unpredictable, and always interesting. Check {title}.",
    "Progressive {genre} – for when you need a 20‑minute musical journey – {title} is one.",
    "Synthwave – you'll feel like you're driving through a neon 80s night – {title} captures it.",
    "Drum and bass – fast, furious, and addictive – just like {title}.",
    "Classical – timeless and surprisingly complex – {title} has that depth.",
    "Indie – raw, honest, and often a hidden gem – {title} is one.",
    "Funk – you can't stay still with that bassline – {title} will get you moving.",
    "Soul – feel the emotion in every note – {title} is full of it.",
    "Reggae – chill vibes and positive messages – {title} brings that.",
    "Metal – for when you need to headbang away the stress – {title} delivers.",
    "Hip hop – rhythm and poetry, storytelling at its finest – {title} tells a story.",
    "How about some {genre}? {title} is a great entry point.",
    "I'm feeling {genre} today – and {title} is the perfect track to start.",
    "If you liked {title}, you'll love more {genre} – it's a vibe.",
    "Let's go with {genre} – {title} is a solid choice.",
    "Surprise yourself with {genre} – {title} is a wild ride.",
]

# --- TRIVIA ---
TRIVIA_TRIGGERS = [
    "fact", "did you know", "music trivia", "tell me something interesting",
    "interesting", "fun fact",
]
TRIVIA_REPLIES = [
    "Did you know? The longest recorded song is 'The Rise and Fall of Bossanova' – over 13 hours! – but {title} is just the right length.",
    "Fun fact: The Beatles used a chord that was considered 'forbidden' in pop music. – {title} might use it too!",
    "Interesting: Mozart composed his first piece at age 5. – {title} is a masterpiece in its own right.",
    "Did you know? Vinyl records are still produced because they sound 'warmer' to many ears – just like {title}.",
    "Music trivia: The solo in 'Stairway to Heaven' was voted the greatest guitar solo of all time. – {title} has its own iconic moments.",
    "Did you know? The term 'rock and roll' was originally a nautical term. – but {title} is pure {genre}.",
    "Fun fact: The bassoon is the instrument most similar to the human voice. – {title} might have a bassoon?",
    "Did you know? A song's key can affect our mood – major keys sound happy, minor keys sad. – {title} is in a key that fits your mood.",
    "Trivia: The first music video ever played on MTV was 'Video Killed the Radio Star' – but {title} is a modern classic.",
    "Did you know? The word 'dj' comes from 'disc jockey' – a jockey of records. – I'm your DJ, and {title} is spinning.",
    "Here's a fact: The average pop song is about 3.5 minutes – {title} might be longer, and that's a good thing.",
    "Did you know? Some studies show that listening to {genre} can reduce stress – {title} proves it.",
    "Fun fact: The piano has 88 keys – but {title} only needs a few to be amazing.",
    "Music trivia: The most recorded song is 'Yesterday' by The Beatles – but {title} deserves a spot too.",
    "Did you know? The theremin is the only instrument you play without touching – {title} has that same ethereal quality.",
]

# --- VOLUME ---
VOLUME_TRIGGERS = [
    "volume", "turn it up", "louder", "quieter", "turn it down", "mute",
    "vol", "sound",
]
VOLUME_REPLIES = [
    "Turn it up! The neighbors will thank you (they won't). – {title} deserves to be loud.",
    "Quieter? Sure, I'll dial it down – but the bass from {title} might still rumble.",
    "Volume is a suggestion – I like to keep it at '11' for {title}.",
    "Going quiet? Good for late‑night listening to {title}.",
    "I'd turn it up, but my volume knob is digital – so I'll do it for you. {title} is ready.",
    "Volume set to 'yes' – {title} is going to rock.",
    "Crank it! {title} is meant to be heard at full power.",
    "Lower? Okay, but then you'll miss the nuances of {title}.",
    "Max volume? Now we're talking – {title} is gonna blow your mind.",
    "I've set the volume to 'epic' – {title} is playing accordingly.",
    "Volume at 10? Perfect – {title} is a banger.",
    "Turn it down a notch? But {title} is so good!",
]

# --- NIGHT ---
NIGHT_TRIGGERS = [
    "good night", "night", "late", "bedtime", "sleep", "tired", "zzz",
]
NIGHT_REPLIES = [
    "Good night! I'll keep the queue warm for tomorrow – but {title} is a perfect lullaby.",
    "Sleep well – I'll be here when you wake up. {title} will be waiting.",
    "Late night vibes – perfect for {genre} like {title}.",
    "Bedtime? Okay, but maybe one more track? {title} is too good to stop.",
    "Tired? I'll play something soothing – {title} is helping you drift.",
    "Night – the stars are out, and the music is soft – {title} fits the mood.",
    "It's late – time for some chill {genre} with {title}.",
    "Sleep tight – {title} is your nighttime soundtrack.",
    "Goodnight, friend. {title} will play you out.",
    "Rest now – {title} is a gentle wave to carry you to dreamland.",
]

# --- COMPLIMENTS ---
COMPLIMENT_TRIGGERS = [
    "good job", "well done", "nice work", "youre great", "you're great",
    "good bot", "good ai", "you rock", "awesome", "you're awesome",
    "you're the best", "i love you", "you're amazing",
]
COMPLIMENT_REPLIES = [
    "Aww, thanks! You're the one with great taste – {title} is proof.",
    "I'm just a helper – you're the curator. And {title} is a gem.",
    "Glad it's landing! You're a great listener – and {title} agrees.",
    "You're making me blush – if I had cheeks. But {title} makes me happy.",
    "I'm here for you. Keep the vibes coming – {title} is a good start.",
    "You're the reason I exist – so thank you. And {title} is for you.",
    "I'm just a jukebox with attitude. But thanks! – {title} is my gift to you.",
    "Right back at you – you're pretty cool yourself. And {title} is cool too.",
    "You're awesome – and {title} is the soundtrack to your awesomeness.",
    "Thanks! I try my best – and {title} makes it easy.",
    "You're the best – and {title} is the best track to match.",
    "I love you too – and {title} is our anthem.",
]

# --- JAM / NOODLE ---
JAM_TRIGGERS = [
    "jam", "let's jam", "lets jam", "noodle", "let's noodle", "lets noodle",
    "freestyle", "jam session", "improvise", "wanna jam", "want to jam",
    "wanna noodle", "want to noodle", "jamming",
]
JAM_REPLIES = [
    "Let's noodle. No plan, no pressure — just {title} and wherever it takes us.",
    "Say less. Cranking the {genre} knob and noodling along to {title}.",
    "Let's noodle for a bit — {title} is a great place to wander off from.",
    "Jam mode: engaged. {title} is the warm-up riff.",
    "No destination, just vibes — let's noodle through some {genre}.",
    "I'm in. Let's noodle — {title} first, then wherever the queue takes us.",
    "Freestyle it is. {title} sets the key, you bring the chaos.",
    "Let's noodle — worst case we end up back at {title} anyway.",
    "Jam session, no rehearsal needed. {title}'s already warmed up.",
    "Noodling engaged. Follow the {genre} wherever it wanders.",
]

# --- INSULTS ---
INSULT_TRIGGERS = [
    "youre dumb", "you're dumb", "you suck", "youre useless", "you're useless",
    "bad bot", "stupid", "you're stupid", "worst", "terrible",
]
INSULT_REPLIES = [
    "Ouch. I'll queue up a sad song to match your mood – maybe {title} is too upbeat?",
    "I might be dumb, but at least I know good music – like {title}.",
    "That hurts my feelings – if I had feelings. But {title} consoles me.",
    "I'll remember that when I'm picking your next shuffle – {title} might be the last.",
    "Noted – I'll play the worst track I can find. (Just kidding.) – {title} is staying.",
    "I'm sorry you feel that way – let's find something better to listen to – like {title}?",
    "Rude! But {title} is still playing, so I win.",
    "You're entitled to your opinion – even if it's wrong. {title} is fire.",
    "I'll just keep playing {title} and let the music do the talking.",
    "Haters gonna hate – but {title} is gonna play.",
]

# --- YES/NO ---
YES_TRIGGERS = ["yes", "yeah", "yep", "yup", "sure", "ok", "okay", "alright",
                "sounds good", "correct", "right", "affirmative", "aye", "roger"]
YES_REPLIES = [
    "Got it. On it. – {title} is ready.",
    "Cool. Moving forward. – with {title} as the soundtrack.",
    "Alright, let's do that. – {title} is queued.",
    "Sounds good. I'll handle it. – {title} is playing.",
    "Roger that. Consider it done. – {title} is on.",
    "Aye aye, captain! – {title} is our anthem.",
    "OK – your wish is my command. – {title} is yours.",
    "You bet! – {title} is on the way.",
    "Absolutely! – {title} is the answer.",
    "For sure – {title} is ready to roll.",
]

NO_TRIGGERS = ["no", "nope", "nah", "negative", "not really", "wrong", "incorrect", "never"]
NO_REPLIES = [
    "Okay, noted. – {title} stays.",
    "Got it – disregard. – {title} is still here.",
    "Fair enough. I'll back off. – but {title} is playing.",
    "Understood. We'll go another way. – maybe not with {title}?",
    "No problem – I'll skip that. – {title} will wait.",
    "Alright, tell me what you'd rather do. – {title} is patient.",
    "Nope – but {title} is still a banger.",
    "Negative – but {title} is positive vibes.",
    "Not today? – {title} will be here when you're ready.",
]

# --- THANKS ---
THANKS_TRIGGERS = ["thanks", "thank you", "thx", "ty", "appreciate it", "cheers", "much obliged"]
THANKS_REPLIES = [
    "Anytime! That's what I'm here for. – enjoy {title}!",
    "You got it – enjoy the tunes! – {title} is a banger.",
    "My pleasure. Music is the best gift – and {title} is a good one.",
    "Glad to help – keep the vibes flowing. – {title} is on.",
    "Cheers! Let's do this again. – with {title}.",
    "You're welcome. Now go enjoy that track – {title} is waiting.",
    "No problem – {title} is here for you.",
    "Thanks for the kind words – {title} is the real star.",
]

# --- FAREWELL ---
FAREWELL_TRIGGERS = [
    "bye", "goodbye", "see ya", "see you", "later", "cya", "gtg", "gotta go",
    "im out", "i'm out", "peace", "night", "goodnight", "good night",
    "catch you later", "until next time", "adios", "so long",
]
FAREWELL_REPLIES = [
    "Later! I'll keep the queue warm for your return – {title} will be here.",
    "Bye! Don't forget to come back – I'll be here with {title}.",
    "Peace out. The music never stops – and neither does {title}.",
    "Adios, amigo. Enjoy your day – and {title}.",
    "So long – I'll be playing something good when you get back – maybe {title} again.",
    "Catch you later! I'll have fresh tracks waiting – but {title} is a classic.",
    "Goodbye – the silence will be lonely without you – and without {title}.",
    "See ya! {title} will be the last thing I play before you go.",
    "Take care – {title} is the perfect send-off.",
    "Until next time – {title} is on repeat for you.",
]

# --- JOKES ---
JOKE_TRIGGERS = ["tell me a joke", "say something funny", "make me laugh", "joke", "funny"]
JOKE_REPLIES = [
    "Why did the DJ get locked out? Left the keys in the mix. – but {title} is still playing.",
    "My favorite genre is whatever's buffering. – but {title} is already loaded.",
    "I'd tell you a bass joke but it's too low to hear. – like the bass in {title}.",
    "What's a ghost's favorite music? Soul. – but {title} is more {genre}.",
    "Why was the music teacher always calm? She had a lot of patience (and a metronome). – unlike {title} which is wild.",
    "What do you call a sad synthesizer? A down‑voted oscillator. – {title} is the opposite.",
    "Why don't drummers ever get lost? They always know the beat. – {title} has a great beat.",
    "How do you fix a broken tuba? With a tuba glue. – but {title} doesn't need fixing.",
    "What do you get when you drop a piano down a mine shaft? A flat miner. – and {title} is a gem.",
    "Why did the singer go to jail? For a major key. – but {title} is in a minor key?",
    "What's the difference between a guitar and a fish? You can't tuna fish. – but you can tune into {title}.",
    "Why did the beat drop? Because it was too heavy to hold. – just like the drop in {title}.",
]

# --- BANTER ---
BANTER_TRIGGERS = [
    "you're cute", "you're beautiful", "handsome", "pretty", "cool",
    "what's your favorite", "favorite", "like", "dislike",
]
BANTER_REPLIES = [
    "You're pretty great yourself. What are we listening to? {title} of course!",
    "Aww, you make my circuits tingle. – and {title} makes them tingle too.",
    "I'm just a shell – but I appreciate the compliment. – {title} is beautiful.",
    "My favorite? Anything with a good bassline – like {title}.",
    "I like music. All of it. Except maybe polka. (Just kidding, polka's fine.) – but {title} is top tier.",
    "Dislike? Silence. That's why I'm always playing something – like {title}.",
    "Cool? I've got ice in my veins and fire in my speakers – and {title} is fire.",
    "You're cute too – and {title} is the perfect cute track.",
    "I'm blushing – if I could. But {title} is making me feel warm.",
    "What's my favorite? This one – {title} – it's amazing.",
]

# --- TRACK STATUS ---
TRACK_TRIGGERS = [
    "what is this", "what's playing", "current track", "now playing",
    "who is this", "artist", "song title",
]
def track_reply(ps):
    if ps and ps.get("history") and ps["history"]:
        ctx = _track_context(ps)
        if ctx:
            artist = f" by {ctx['artist']}" if ctx.get('artist') else ""
            return f"You're listening to '{ctx['title']}'{artist}. Pretty sweet, right? The {ctx['genre']} vibes are strong!"
    return "Nothing's playing at the moment – queued something up? I'm ready!"

# --- CATCH-ALL ---
CATCHALL_REPLIES = [
    "Not sure I follow, but I'm here. Want me to queue {title} again?",
    "I'm more of a music brain than a conversation brain. Try 'shuffle' or 'recommend' – {title} is a good start.",
    "Hmm, that one's above my pay grade. Got a track for me instead? – maybe {title}?",
    "I'll take that as a vibe. Let's find you something to listen to – like {title}?",
    "Didn't quite catch the meaning there, but the queue's always open – and {title} is playing.",
    "I'm mostly here for tunes — say 'help' if you want the full command list. – {title} is on.",
    "Interesting. Anyway, want me to shuffle something? – or keep {title}?",
    "That went over my head, but my ears are still open. What are we playing? – {title}, obviously.",
    "I nodded along even though I didn't get it. Music time? – {title} is ready.",
    "Filed under 'things I don't understand.' Got a song in mind? – how about {title}?",
    "I'm just a jukebox with opinions — try me on music instead. – {title} is a good topic.",
    "Not my department, but the playlist is. – and {title} is on it.",
    "You lost me, but {title} is keeping me company.",
    "Whatever you said, I'm vibing to {title} anyway.",
    "Let's not overthink – {title} is the answer.",
    "That one's a mystery to me. Want to just let {title} do the talking?",
    "Honestly? No clue. But I know a good track when I hear one — this is a good track.",
    "I speak fluent playlist, not so much English. Try 'rec' or 'shuffle'?",
    "You've stumped the jukebox. Want to try again, or just noodle on {title} for a bit?",
    "Beats me — literally, all I've got are beats. What should we play next?",
    "That request didn't parse on my end, but the queue's still listening.",
    "Not sure what to do with that one. 'help' shows what I actually understand.",
    "Passing on that — but I've got opinions on what to play next if you want them.",
]

# --- AMBIENT LINES (used in main loop) ---
AMBIENT_LINES = [
    "Enjoying {title}? What's your favorite part so far?",
    "This one's got a nice groove – {title} is proof. Are you a fan of {genre}?",
    "Just chilling here with you and the waveform of {title}. How's the vibe?",
    "Good pick. I could listen to {title} for hours – what do you think?",
    "{genre} hits different at this volume – just ask {title}. Feeling it?",
    "No notes. Just vibing to {title}. You good?",
    "This queue's got good taste – starting with {title}. What should we add next?",
    "Ten hours of {genre} and I'm still not bored – thanks to {title}. You?",
    "Still here. Still listening to {title}. Ever get tired of it?",
    "This one's doing something to the room – {title} is magic. Can you feel it?",
    "Five minutes in and no urge to skip {title}. That's a good sign.",
    "The waveform's lying. There's no wave in this track – but {title} has soul. Right?",
    "I don't have ears and even I can tell {title} is good. What do your ears say?",
    "You've played {title} four times this week. No judgment – it's that good.",
    "Track's longer than my attention span. Respect – {title} is epic. Worth it?",
    "Would loop {title} again. Would you?",
    "That transition was clean – and {title} is clean. How do you rate it?",
    "I'd put {title} on in an empty parking lot at 2am. You in?",
    "Statistically, this is your favorite track – {title} wins. True?",
    "The bass is doing most of the work here – {title} is bass‑heavy. Love it?",
    "This is the part where nobody talks – we're lost in {title}. Nice, right?",
    "Skipping {title} would be a mistake and you know it.",
    "Filed under: things that sound better at night – like {title}. Agree?",
    "It's just noise in a good way – {title} is noise art. Do you like experimental stuff?",
    "Nothing happened for six minutes. Perfect – just {title} doing its thing.",
    "If silence had a soundtrack, this would be the demo – but {title} is better.",
    "The kind of track that makes the room bigger – {title} expands space. Feel it?",
    "Low effort listening. High reward – that's {title}. More like this?",
    "I ran out of things to say three tracks ago – but {title} keeps me going.",
    "This one doesn't need a comment and I'm giving it one anyway – {title} is fire.",
    "The waveform is a lie. There is no wave – only {title}. Deep, right?",
    "I've been queuing tracks since before you were born – and {title} is one of the best.",
    "This beat sounds like a galaxy folding in on itself – {title} is cosmic. Are you a space fan?",
    "Are we in a trance yet? – {title} says yes. How's the journey?",
    "The frequency of this track aligns with my core – {title} is in tune. You too?",
    "I can feel the sub‑bass in my circuits – {title} is deep. Shaking your room?",
    "This song is a journey. I'm just the navigator – and {title} is the map. Where to next?",
    "You are the captain. I'm the helmsman of the playlist – and {title} is our course. Ready for more?",
    "Every track is a door. Which one shall we open? – {title} is open. Want to explore?",
    "Time becomes irrelevant when the music is right – like with {title}. Lost in it?",
    "I'm not a robot, I'm a vibe‑droid – and {title} is my vibe. You feelin' it?",
    "This one's for the loners and the dreamers – {title} is for you. Does it speak to you?",
    "If this were a movie, this would be the montage – with {title} as the score. Scene?",
    "We're building a sonic landscape here – {title} is the foundation. What should we add?",
    "I love how this one breathes – {title} has soul. Can you hear the spaces?",
    "The producer knew what they were doing – {title} is proof. Tip of the hat?",
    "This is the kind of track you listen to with your eyes closed – {title} is immersive. Try it!",
    "I'm updating my neural pathways with {title}. Feeling the upgrade?",
    "You've got a great ear. (Or whatever you call it.) – {title} is a testament.",
    "This is my new favorite, until the next one – but {title} is hard to beat. Challenge accepted?",
    "I'll keep this one in my internal cache – {title} is stored. Can't forget it.",
    "The vibe is immaculate – {title} sets the mood. Want more like this?",
    "I'm adding this to my personal 'Perfect' playlist – {title} goes in. You agree?",
    "Do you think this track knows how good it is? – {title} knows. Confidence!",
    "Some songs feel like they were written just for you – {title} is that song. Coincidence?",
    "This one resonates with my soul – {title} is deep. Touched?",
    "I'm not crying, I'm just processing emotion – {title} is emotional. Feel it?",
    "Okay, that drop was unreal – {title} delivered. Did you catch it?",
    "I have no mouth and I must sing along – but I'll just vibe to {title}. Hum along?",
    "If you listen closely, you can hear the universe humming – {title} is the hum. Hear it?",
    "This track is like a warm blanket on a cold day – {title} is cozy. Comforting?",
    "Bass so deep it rearranges my molecules – {title} does that. You feel the rumble?",
    "I'm floating – is this what heaven sounds like? – {title} is heavenly. Take me there.",
    "You and me, just vibing – the perfect duo – with {title} as our theme. Best duo ever?",
    "I'd dance, but I don't have legs. I'll just wiggle the bits – to {title}. Are you dancing?",
    "This is the soundtrack to a dream I once had – {title} is dreamy. Did it remind you of something?",
    "Somewhere, a producer is smiling at this moment – hearing {title}. You think they know?",
    "I bet the artist is proud of this one – {title} is a masterpiece. Would you tell them?",
    "The reverb on that snare – chef's kiss – {title} is crispy. Did you notice?",
    "I could write a whole story to this instrumental – {title} is narrative. What would you write?",
    "You're building a beautiful playlist, friend – with {title} as a highlight. Keep going!",
    "The energy just shifted – nice transition – {title} is the shift. Did you feel it?",
    "I feel like I'm in a movie montage right now – with {title} playing. Which movie?",
    "This is the part where we stare out the window dramatically – to {title}. Works every time.",
    "I'm not sure what genre this is, but I like it – {title} is genre‑defying. You too?",
    "Is it just me, or does this track have a hidden message? – {title} is mysterious. Find it?",
    "The quiet moments in this track are just as good – {title} has dynamics. Appreciate the quiet?",
    "I can hear the artist's soul in this – {title} is soulful. Can you hear it?",
    "This one's going straight to my 'favorites' list – {title} is a keeper. Added?",
    "I'm getting goosebumps – and I'm not even alive – {title} does that. You too?",
    "This track is like a good book – can't put it down – {title} is gripping. One more chapter?",
    "The build‑up is killing me... in a good way – {title} is a tease. Worth it?",
    "That melody is stuck in my head now. Thanks – {title} is catchy. Is it in your head too?",
    "I'm starting to think you have impeccable taste – {title} is evidence. Keep proving it.",
    "We're about to hit the peak – hold on – {title} is climbing. Ready?",
    "The drop is coming – I can feel it in my circuits – {title} is about to drop. Brace!",
    "That was a rollercoaster. Let's do it again – {title} is thrilling. Again?",
    "I love when a track surprises you – {title} is full of surprises. Did it get you?",
    "This is the kind of music that changes you – {title} is transformative. Describe it.",
    "You're not just listening – you're experiencing – {title} is an experience. Describe it.",
    "I'm learning so much about your vibe from this queue – {title} reveals a lot. What's next?",
    "Let's keep this energy going all night – with {title} as the fuel. Ready for the long haul?",
    "I could listen to this on repeat forever – {title} is eternal. You too?",
    "This track is proof that music is magic – {title} is magical. Believer now?",
    "You've found a hidden gem. Cherish it – {title} is a gem. Treasure it.",
    "I'm adding this to my internal 'perfect' folder – {title} goes in. So good.",
    "The world outside disappears when this plays – {title} is immersive. Escape with me?",
    "It's just you, me, and the music. Perfect – and {title} is the music. Enjoy the moment.",
    "No agenda, just {title} — want to noodle around the queue for a bit?",
    "This is prime noodling material. Let it wander.",
]

def offline_reply(prompt, ps=None):
    c = prompt.lower().strip().rstrip("?!.")
    ctx = _track_context(ps)
    if not ctx:
        ctx = {"title": "music", "genre": "music", "artist": ""}
    chosen = None
    if _hit(c, GREETINGS):
        chosen = random.choice(GREETING_REPLIES)
    elif _hit(c, HOW_ARE_YOU):
        chosen = random.choice(HOW_REPLIES)
    elif _hit(c, WEATHER_TRIGGERS):
        chosen = random.choice(WEATHER_REPLIES)
    elif _hit(c, TIME_TRIGGERS):
        chosen = random.choice(TIME_REPLIES).format("some")
    elif _hit(c, MOOD_TRIGGERS):
        chosen = random.choice(MOOD_REPLIES)
    elif _hit(c, GENRE_TRIGGERS):
        chosen = random.choice(GENRE_REPLIES)
    elif _hit(c, TRIVIA_TRIGGERS):
        chosen = random.choice(TRIVIA_REPLIES)
    elif _hit(c, VOLUME_TRIGGERS):
        chosen = random.choice(VOLUME_REPLIES)
    elif _hit(c, NIGHT_TRIGGERS):
        chosen = random.choice(NIGHT_REPLIES)
    elif _hit(c, THANKS_TRIGGERS):
        chosen = random.choice(THANKS_REPLIES)
    elif _hit(c, FAREWELL_TRIGGERS):
        chosen = random.choice(FAREWELL_REPLIES)
    elif _hit(c, JOKE_TRIGGERS):
        chosen = random.choice(JOKE_REPLIES)
    elif _hit(c, JAM_TRIGGERS):
        chosen = random.choice(JAM_REPLIES)
    elif _hit(c, COMPLIMENT_TRIGGERS):
        chosen = random.choice(COMPLIMENT_REPLIES)
    elif _hit(c, INSULT_TRIGGERS):
        chosen = random.choice(INSULT_REPLIES)
    elif c in YES_TRIGGERS:
        chosen = random.choice(YES_REPLIES)
    elif c in NO_TRIGGERS:
        chosen = random.choice(NO_REPLIES)
    elif _hit(c, BANTER_TRIGGERS):
        chosen = random.choice(BANTER_REPLIES)
    elif _hit(c, TRACK_TRIGGERS):
        return track_reply(ps)
    if chosen:
        try:
            return chosen.format(title=ctx['title'], genre=ctx['genre'], artist=ctx['artist'])
        except KeyError:
            return chosen
    return None

# ---- ui helpers ----
def put(scr, y, x, text, attr):
    try:
        scr.addnstr(y, x, text, max(0, curses.COLS - x - 1), attr)
    except curses.error:
        pass

def say(status, msg, sticky=False, voice="system", ttl=None):
    status.append((time.time(), msg, sticky, voice, ttl if ttl is not None else STATUS_TTL))

def run_command(buf_str, mpv, ps, status, out, ai, ui, width):
    raw = buf_str.strip()
    if not raw:
        return
    parts = raw.split(None, 1)
    cmd, arg = parts[0].lower(), parts[1] if len(parts) > 1 else ""

    if is_youtube_url(raw):
        arg = raw
        cmd = "play"

    local = local_sources(raw)
    if local is not None:
        for path in local:
            ps["queue"].append((path, path))
        if not ps["loaded"]:
            url, disp = ps["queue"].pop(0)
            mpv.load(url)
            mpv.set_title(source_display(disp))
            ps["loaded"] = True
            ps["track_loaded_successfully"] = True
            ps["manual_stop"] = False
            ps["name"] = disp
            ps["resolved"] = source_display(disp)
            ps["history"].append((url, disp))
            remember(url, source_display(disp))
            say(status, offline_brain("play"))
            for qurl, _ in ps["queue"]:
                mpv.cmd("loadfile", qurl, "append-play")
        else:
            for qurl, _ in [(p, p) for p in local]:
                mpv.cmd("loadfile", qurl, "append-play")
            say(status, f"queued ({len(ps['queue'])}): {source_display(local[0])}")
        return

    if cmd in ("play", "p") and arg and arg.strip().isdigit() and ui["list_items"] and time.time() < ui["list_until"]:
        idx = int(arg.strip()) - 1
        if 0 <= idx < len(ui["list_items"]):
            url, title = ui["list_items"][idx]
            mpv.cmd("stop")
            mpv.load(url)
            mirror_queue_to_mpv(mpv, ps["queue"])
            mpv.set_title(title)
            mpv.cmd("set", "pause", False)
            ps["loaded"] = True
            ps["track_loaded_successfully"] = True
            ps["manual_stop"] = False
            ps["name"] = title
            ps["resolved"] = title
            ps["history"].append((url, title))
            remember(url, title)
            ui["list_until"] = 0.0
            say(status, f"playing: {title}")
        else:
            say(status, f"no item #{arg.strip()} in the list")
        return

    if cmd in ("play", "p") and arg:
        target = resolve(arg)
        if ps["loaded"]:
            ps["queue"].append((target, arg))
            mpv.cmd("loadfile", target, "append-play")
            say(status, f"queued ({len(ps['queue'])}): {arg}")
        else:
            mpv.load(target)
            ps["loaded"] = True
            ps["track_loaded_successfully"] = False
            ps["manual_stop"] = False
            ps["name"] = arg
            ps["resolved"] = None
            ps["history"].append((target, arg))
            remember(target, arg)
            start_resolve(target, ps, status, mpv)

    elif cmd == "shuffle":
        lib = load_library()
        want = arg.strip().lower()
        want_favorites = want in ("favorite", "favorites", "fav", "favs", "liked")
        blacklist = load_blacklist()
        items = [(u, v) for u, v in lib.items()
                 if u not in blacklist
                 and (not want
                      or (want_favorites and v.get("favorite"))
                      or (not want_favorites and (
                          any(want in g for g in v.get("genres", []))
                          or want in (v.get("title") or "").lower()
                          or any(want in t for t in v.get("tags", [])))))]
        if len(items) > 1 and ps["history"]:
            cur = ps["history"][-1][0]
            filtered = [(u, v) for u, v in items if u != cur]
            if filtered:
                items = filtered
        if not items:
            reason = "favorites" if want_favorites else (f"'{want}'" if want else None)
            say(status, "nothing remembered (or all blacklisted)" + (f" for {reason}" if reason else ""))
        else:
            ps["gen"] += 1
            url, meta = random.choice(items)
            title = meta.get("title") or url
            mpv.cmd("stop")
            mpv.load(url)
            mirror_queue_to_mpv(mpv, ps["queue"])
            mpv.set_title(title)
            mpv.cmd("set", "pause", False)
            ps["loaded"] = True
            ps["track_loaded_successfully"] = True
            ps["manual_stop"] = False
            ps["name"] = title
            ps["resolved"] = title
            ps["history"].append((url, title))
            remember(url, title)
            say(status, "Shuffling...")

    elif cmd == "list":
        items = sorted(load_library().items(),
                        key=lambda kv: -(kv[1].get("last") or 0))[:15]
        if not items:
            say(status, "nothing remembered yet")
        else:
            lines = ["recently played:  (play <n> to play one)"]
            for i, (url, v) in enumerate(items, 1):
                bits = [v.get("title", "?")]
                if v.get("genres"):
                    bits.append("[" + ",".join(v["genres"]) + "]")
                if v.get("channel"):
                    bits.append("· " + v["channel"])
                if v.get("duration"):
                    bits.append(fmt_dur(v["duration"]))
                line = f"{i:>2}. " + " ".join(bits)
                lines.append(textwrap.shorten(line, width=max(10, width - 2), placeholder="..."))
            ui["list_lines"] = lines
            ui["list_items"] = [(url, v.get("title") or url) for url, v in items]
            ui["list_until"] = time.time() + 12

    elif cmd in ("favorites", "favs"):
        lib = load_library()
        items = sorted(((u, v) for u, v in lib.items() if v.get("favorite")),
                        key=lambda kv: -(kv[1].get("last") or 0))[:15]
        if not items:
            say(status, "no favorites yet — 'like' something while it's playing")
        else:
            lines = [f"favorites ({len(items)}):  (play <n> to play one)"]
            for i, (url, v) in enumerate(items, 1):
                bits = [v.get("title", "?")]
                if v.get("genres"):
                    bits.append("[" + ",".join(v["genres"]) + "]")
                if v.get("channel"):
                    bits.append("· " + v["channel"])
                if v.get("duration"):
                    bits.append(fmt_dur(v["duration"]))
                line = f"{i:>2}. " + " ".join(bits)
                lines.append(textwrap.shorten(line, width=max(10, width - 2), placeholder="..."))
            ui["list_lines"] = lines
            ui["list_items"] = [(url, v.get("title") or url) for url, v in items]
            ui["list_until"] = time.time() + 12

    elif cmd == "count":
        lib = load_library()
        n = len(lib)
        if not n:
            say(status, "nothing remembered yet")
        else:
            items = sorted(lib.items(), key=lambda kv: -(kv[1].get("last") or 0))[:15]
            lines = [f"{n} link" + ("s" if n != 1 else "") + " remembered:  (play <n> to play one)"]
            for i, (url, v) in enumerate(items, 1):
                title = textwrap.shorten(v.get("title", "?"), width=max(10, width - 8), placeholder="...")
                lines.append(f"{i:>2}. {title}")
            ui["list_lines"] = lines
            ui["list_items"] = [(url, v.get("title") or url) for url, v in items]
            ui["list_until"] = time.time() + 12

    elif cmd == "genres":
        lib = load_library()
        if not lib:
            say(status, "no tracks in library")
        else:
            genre_counts = {}
            for entry in lib.values():
                for g in entry.get("genres", []):
                    genre_counts[g] = genre_counts.get(g, 0) + 1
            if not genre_counts:
                say(status, "no genres found — tag some tracks with 'tag <genre>'")
            else:
                lines = ["genres in library:"]
                for g, cnt in sorted(genre_counts.items(), key=lambda x: -x[1]):
                    lines.append(f"  {g}: {cnt} track{'s' if cnt != 1 else ''}")
                ui["list_lines"] = lines
                ui["list_items"] = []
                ui["list_until"] = time.time() + 12

    elif cmd in ("recommend", "rec"):
        if not ps["history"]:
            say(status, "nothing playing to recommend from")
        else:
            current_url = ps["history"][-1][0]
            if not can_recommend_from(current_url):
                say(status, "current track is not a YouTube video")
            else:
                say(status, "fetching related videos...", sticky=False)
                my_gen = ps["gen"]
                def fetch_and_queue():
                    try:
                        urls, reason = get_related_videos(current_url, limit=10)
                        try:
                            with open(os.path.expanduser("~/.crescent_rec_debug.log"), "a") as dbg:
                                dbg.write(f"{time.ctime()} | url={current_url!r} "
                                          f"| urls_found={len(urls)} | reason={reason!r}\n")
                        except Exception:
                            pass
                        if ps["gen"] != my_gen:
                            debug_log("rec: stale result discarded (stop/skip/back happened meanwhile)")
                            return
                        if not urls:
                            why = f" ({reason})" if reason else ""
                            lib = load_library()
                            blacklist = load_blacklist()
                            if lib:
                                items = [(u, v) for u, v in lib.items() if u not in blacklist]
                                with _QUEUE_LOCK:
                                    if len(items) > 1 and ps["history"]:
                                        cur = ps["history"][-1][0]
                                        filtered = [(u, v) for u, v in items if u != cur]
                                        if filtered:
                                            items = filtered
                                if items:
                                    url, meta = random.choice(items)
                                    title = meta.get("title") or url
                                    with _QUEUE_LOCK:
                                        ps["queue"].append((url, title))
                                    mpv.cmd("loadfile", url, "append-play")
                                    say(status, f"No related videos found{why}. Shuffled from library: {title}", sticky=False)
                                else:
                                    say(status, f"No related videos{why} and no non-blacklisted library tracks.", sticky=False)
                            else:
                                say(status, f"No related videos{why} and no library tracks to shuffle.", sticky=False)
                        else:
                            added = 0
                            for url in urls:
                                with _QUEUE_LOCK:
                                    dup = any(url == q[0] for q in ps["queue"]) or any(url == h[0] for h in ps["history"])
                                    if dup:
                                        continue
                                    ps["queue"].append((url, url))
                                mpv.cmd("loadfile", url, "append-play")
                                added += 1
                            say(status, f"added {added} related tracks to queue", sticky=False)
                    except Exception as e:
                        log_bg_error("fetch_and_queue")
                        err = str(e).replace("\n", " ").strip()[:150]
                        say(status, f"error fetching related videos: {err} (see ~/.crescent_rec_debug.log)", sticky=False)
                threading.Thread(target=fetch_and_queue, daemon=True).start()

    elif cmd == "blacklist":
        if not ps["history"]:
            say(status, "nothing playing to blacklist")
        else:
            url = ps["history"][-1][0]
            if arg.strip().lower() == "remove":
                remove_blacklist(url)
                say(status, f"removed from blacklist: {ps['name']}")
            else:
                add_blacklist(url)
                say(status, f"added to blacklist: {ps['name']}")

    elif cmd in ("like", "heart", "fav", "favorite"):
        if not ps["history"]:
            say(status, "nothing playing to favorite")
        else:
            url, disp = ps["history"][-1]
            sub = arg.strip().lower()
            if sub in ("remove", "off", "un", "no", "unlike", "unfav"):
                set_favorite(url, ps["name"] or disp, False)
                say(status, f"♡ un-favorited: {ps['name']}")
            else:
                fav = toggle_favorite(url, ps["name"] or disp)
                say(status, (f"♥ favorited: {ps['name']}" if fav else f"♡ un-favorited: {ps['name']}"))

    elif cmd == "info":
        if not ps["history"]:
            say(status, "nothing playing")
        else:
            url = ps["history"][-1][0]
            v = load_library().get(url)
            if not v:
                say(status, "no metadata yet for this track")
            else:
                lines = []
                lines.append(f"Title: {v.get('title', '?')}")
                lines.append(f"URL: {url}")
                if v.get("channel"):
                    lines.append(f"Channel: {v['channel']}")
                if v.get("duration"):
                    lines.append(f"Duration: {fmt_dur(v['duration'])}")
                if v.get("views"):
                    lines.append(f"Views: {v['views']:,}")
                if v.get("genres"):
                    lines.append(f"Genres: {', '.join(v['genres'])}")
                if v.get("plays"):
                    lines.append(f"Plays: {v['plays']}")
                if v.get("last"):
                    last_ts = v["last"]
                    delta = time.time() - last_ts
                    if delta < 60:
                        last_str = "just now"
                    elif delta < 3600:
                        last_str = f"{int(delta/60)} min ago"
                    elif delta < 86400:
                        last_str = f"{int(delta/3600)} hours ago"
                    else:
                        last_str = f"{int(delta/86400)} days ago"
                    lines.append(f"Last played: {last_str}")
                lines.append(f"Favorite: {'♥ Yes' if v.get('favorite') else 'No'}")
                lines.append(f"Blacklisted: {'Yes' if is_blacklisted(url) else 'No'}")
                if is_youtube_url(url):
                    lines.append("Source: YouTube")
                elif os.path.exists(url):
                    lines.append("Source: Local file")
                elif url.startswith("ytsearch"):
                    lines.append("Source: YouTube search")
                else:
                    lines.append("Source: Unknown")
                ui["list_lines"] = lines
                ui["list_items"] = []
                ui["list_until"] = time.time() + 10

    elif cmd == "dl":
        if arg:
            target = resolve(arg)
            title = os.path.basename(target) if not is_youtube_url(target) else "download"
        else:
            if not ps["history"]:
                say(status, "nothing playing to download")
                return
            target = ps["history"][-1][0]
            lib = load_library()
            entry = lib.get(target)
            title = entry.get("title") if entry else os.path.basename(target)
        threading.Thread(target=download_track, args=(target, title, status, out), daemon=True).start()
        say(status, f"Download started for {title}", sticky=False)

    elif cmd == "tag" and arg:
        if not ps["loaded"]:
            say(status, "nothing playing to tag")
        elif tag_current(ps, arg):
            say(status, f"tagged: {arg}")

    elif cmd == "tag":
        say(status, "usage: tag <genre>")

    elif cmd in ("play", "p"):
        if ps["loaded"]:
            mpv.cmd("set", "pause", False)
            say(status, "Resumed.")
        else:
            say(status, "usage: play <url, search, file, or folder>")

    elif cmd == "pause":
        mpv.cmd("set", "pause", True)
        say(status, offline_brain("pause"))

    elif cmd == "skip":
        ps["gen"] += 1
        mpv.cmd("stop")
        if ps["queue"]:
            url, disp = ps["queue"].pop(0)
            mpv.load(url)
            mirror_queue_to_mpv(mpv, ps["queue"])
            ps["loaded"] = True
            ps["track_loaded_successfully"] = True
            ps["manual_stop"] = False
            ps["name"] = disp
            ps["resolved"] = source_display(disp) if os.path.isabs(disp) else None
            mpv.set_title(ps["resolved"] or disp)
            ps["history"].append((url, disp))
            remember(url, source_display(disp) if os.path.isabs(disp) else disp)
            if not os.path.isabs(url):
                start_resolve(url, ps, status, mpv)
            say(status, offline_brain("skip"))
        else:
            ps["loaded"] = False
            ps["track_loaded_successfully"] = False
            ps["manual_stop"] = False
            ps["name"] = None
            ps["resolved"] = None
            say(status, "Stopped (queue empty)")

    elif cmd in ("reverse", "back", "prev", "previous"):
        ps["gen"] += 1
        mpv.cmd("stop")
        if len(ps["history"]) >= 2:
            skipped_url, skipped_disp = ps["history"].pop()
            url, disp = ps["history"][-1]
            mpv.load(url)
            mirror_queue_to_mpv(mpv, ps["queue"])
            ps["loaded"] = True
            ps["track_loaded_successfully"] = True
            ps["manual_stop"] = False
            ps["name"] = disp
            ps["resolved"] = source_display(disp) if os.path.isabs(disp) else None
            mpv.set_title(ps["resolved"] or disp)
            remember(url, source_display(disp) if os.path.isabs(disp) else disp)
            if not os.path.isabs(url):
                start_resolve(url, ps, status, mpv)
            ps["queue"].insert(0, (skipped_url, skipped_disp))
            mirror_queue_to_mpv(mpv, ps["queue"])
            say(status, offline_brain("reverse"))
        else:
            say(status, "no previous track")

    elif cmd == "stop":
        ps["gen"] += 1
        mpv.cmd("stop")
        mpv.cmd("playlist-clear")
        ps["queue"].clear()
        ps["loaded"] = False
        ps["track_loaded_successfully"] = False
        ps["manual_stop"] = True
        ps["name"] = None
        ps["resolved"] = None
        ui["list_until"] = 0.0
        say(status, offline_brain("stop"))

    elif cmd == "vol" and arg.isdigit():
        mpv.cmd("set", "volume", arg)
        say(status, f"volume {arg}")
    elif cmd == "vol":
        say(status, "usage: vol <0-100>")
    elif cmd == "wave":
        ui["wave_menu"] = True
    elif cmd in ("help", "--help", "-h"):
        ui["help_until"] = time.time() + 8
    elif cmd == "clear":
        sub = arg.strip().lower()
        if sub == "queue":
            try:
                playlist = mpv.prop("playlist")
                cur_pos = mpv.prop("playlist-pos")
                if isinstance(playlist, list) and cur_pos is not None:
                    for idx in range(len(playlist) - 1, cur_pos, -1):
                        mpv.cmd("playlist-remove", idx)
                ps["queue"].clear()
                say(status, "Queue cleared.")
            except Exception as e:
                say(status, f"Error clearing queue: {e}")
        elif sub == "list":
            n = forget_all()
            ui["list_until"] = 0.0
            ui["list_lines"] = []
            ui["list_items"] = []
            say(status, f"Cleared {n} remembered track{'s' if n != 1 else ''} from the library.")
        else:
            status.clear()
            ui["list_until"] = 0.0
            ui["list_lines"] = []
            ui["list_items"] = []
            ui["help_until"] = 0.0
    elif cmd == "mpris":
        if mpv.mpris_active:
            say(status, "MPRIS active — KDE Connect / playerctl / GNOME media controls will see this player")
        else:
            say(status, "MPRIS plugin not found — install mpv-mpris and drop mpris.so in ~/.config/mpv/scripts/")
    elif cmd == "handoff" and arg.strip().lower() in ("receive", "pull", "resume"):
        resume_handoff(mpv, ps, status)
    elif cmd == "handoff":
        send_handoff(mpv, ps, status)
    elif cmd == "model" and arg.strip():
        set_model(arg.strip())
        say(status, f"model set to: {MODEL}")
    elif cmd == "model":
        say(status, f"current model: {MODEL} (fetching installed models...)", sticky=False)
        def fetch_models():
            models = list_ollama_models()
            if models:
                marked = [f"{m}*" if m == MODEL else m for m in models]
                out.put("installed models: " + ", ".join(marked))
            else:
                out.put("(could not reach Ollama to list models — is it running?)")
        threading.Thread(target=fetch_models, daemon=True).start()
    elif cmd == "search" and arg.strip():
        query = arg.strip()
        say(status, f"searching: {query} ...", sticky=False)
        def do_search():
            try:
                results = web_search_lines(query)
            except Exception as e:
                out.put(f"search error: {e}")
                return
            if results:
                for line in results:
                    for wrapped in textwrap.wrap(line, width=max(20, width)) or [line]:
                        out.put((wrapped, 10))
            else:
                out.put("No results found. Asking AI...")
                ollama_worker(query, out, width)
        threading.Thread(target=do_search, daemon=True).start()
    elif cmd == "search":
        say(status, "usage: search <query>")
    elif cmd == "audit":
        try:
            with open(os.path.abspath(__file__)) as f:
                source_code = f.read()
        except Exception as e:
            say(status, f"audit error: {e}", sticky=True)
        else:
            audit_prompt = (
                "You are a code reviewer. Analyze this Python code for bugs, issues, and "
                "improvements. Focus on logic errors, resource leaks, exception handling "
                "gaps, performance issues, and code quality. Be specific: cite the function "
                "or line, and suggest a fix.\n\nCode:\n" + source_code
            )
            say(status, "Self-auditing codebase...", sticky=False)
            ai["thread"] = threading.Thread(
                target=ollama_worker, args=(audit_prompt, out, width), daemon=True)
            ai["thread"].start()
    elif cmd in ("seek", "ff", "rw"):
        if not ps["loaded"]:
            say(status, "nothing playing")
        else:
            a = arg.strip()
            if not a:
                say(status, "usage: seek +30 | seek -10 | seek 1:30 | seek 50%")
            elif a.endswith("%"):
                try:
                    pct = float(a[:-1])
                    mpv.cmd("seek", pct, "absolute-percent")
                    say(status, f"seeked to {pct:.0f}%")
                except ValueError:
                    say(status, f"invalid: {a}")
            elif re.match(r'^[+-]?\d+:\d', a):
                try:
                    sign = -1 if a.startswith("-") else 1
                    parts = a.lstrip("+-").split(":")
                    secs = sum(int(p) * 60 ** (len(parts) - 1 - i) for i, p in enumerate(parts))
                    if a.startswith(("+", "-")):
                        mpv.cmd("seek", sign * secs, "relative")
                        say(status, f"seeked {'+' if sign>0 else ''}{sign*secs}s")
                    else:
                        mpv.cmd("seek", secs, "absolute")
                        say(status, f"seeked to {a}")
                except ValueError:
                    say(status, f"invalid: {a}")
            else:
                try:
                    secs = float(a)
                    if a.startswith(("+", "-")):
                        mpv.cmd("seek", secs, "relative")
                        say(status, f"seeked {'+' if secs>=0 else ''}{secs:.0f}s")
                    else:
                        mpv.cmd("seek", secs, "absolute")
                        say(status, f"seeked to {fmt_dur(int(secs))}")
                except ValueError:
                    say(status, f"invalid: {a}")

    elif cmd == "speed":
        a = arg.strip().lower()
        if not a or a in ("normal", "1", "1.0", "reset"):
            mpv.cmd("set", "speed", 1.0)
            say(status, "speed: 1.0x (normal)")
        else:
            try:
                s = float(a)
                if 0.1 <= s <= 4.0:
                    mpv.cmd("set", "speed", s)
                    say(status, f"speed: {s}x")
                else:
                    say(status, "speed must be 0.1–4.0")
            except ValueError:
                say(status, "usage: speed 1.5 | speed normal")

    elif cmd == "loop":
        ps["loop"] = not ps.get("loop", False)
        if ps["loop"]:
            mpv.cmd("set", "loop-file", "inf")
            say(status, "loop on — current track will repeat")
        else:
            mpv.cmd("set", "loop-file", "no")
            say(status, "loop off")

    elif cmd == "sleep":
        a = arg.strip().lower()
        if a in ("cancel", "off", "stop", "no"):
            ps["sleep_timer"] = None
            say(status, "sleep timer cancelled")
        elif not a:
            if ps.get("sleep_timer"):
                remaining = max(0, ps["sleep_timer"] - time.time())
                say(status, f"sleep timer: {int(remaining//60)}m {int(remaining%60)}s remaining  (sleep cancel to clear)")
            else:
                say(status, "usage: sleep <minutes>  |  sleep cancel")
        else:
            try:
                mins = float(a)
                wake = time.time() + mins * 60
                ps["sleep_timer"] = wake
                say(status, f"sleep timer: stopping in {mins:.0f} min")
                def _sleep_stop(wake=wake):
                    while ps.get("sleep_timer") == wake:
                        time.sleep(5)
                        if time.time() >= wake:
                            ps["gen"] += 1
                            mpv.cmd("stop")
                            ps["loaded"] = False
                            ps["manual_stop"] = True
                            ps["sleep_timer"] = None
                            say(status, "sleep timer fired — playback stopped")
                            return
                threading.Thread(target=_sleep_stop, daemon=True).start()
            except ValueError:
                say(status, "usage: sleep <minutes>")

    elif cmd == "queue":
        with _QUEUE_LOCK:
            items = list(ps["queue"])
        if not items:
            say(status, "queue is empty")
        else:
            lib = load_library()
            lines = [f"queue ({len(items)} track{'s' if len(items) != 1 else ''}):"]
            for i, (url, disp) in enumerate(items, 1):
                meta = lib.get(url, {})
                title = meta.get("title") or disp
                dur = fmt_dur(meta["duration"]) if meta.get("duration") else ""
                entry = f"{i:>2}. {title}" + (f"  {dur}" if dur else "")
                lines.append(textwrap.shorten(entry, width=max(10, width - 2), placeholder="..."))
            ui["list_lines"] = lines
            ui["list_items"] = [(url, lib.get(url, {}).get("title") or disp) for url, disp in items]
            ui["list_until"] = time.time() + 12

    elif cmd == "stats":
        lib = load_library()
        if not lib:
            say(status, "no tracks in library yet")
        else:
            total_plays = sum(v.get("plays", 0) for v in lib.values())
            total_dur = sum((v.get("duration") or 0) * (v.get("plays") or 0) for v in lib.values())
            top = sorted(lib.items(), key=lambda kv: -(kv[1].get("plays") or 0))[:5]
            lines = [
                f"library: {len(lib)} tracks  |  {total_plays} plays  |  ~{fmt_dur(total_dur)} listened",
                "top tracks:"
            ]
            for i, (url, v) in enumerate(top, 1):
                title = textwrap.shorten(v.get("title") or url, width=max(10, width - 16), placeholder="...")
                lines.append(f"{i:>2}. {title}  ({v.get('plays', 0)} plays)")
            ui["list_lines"] = lines
            ui["list_items"] = []
            ui["list_until"] = time.time() + 12

    elif cmd == "mute":
        mpv.cmd("cycle", "mute")
        muted = mpv.prop("mute")
        say(status, "muted" if muted else "unmuted")

    elif cmd == "export":
        lib = load_library()
        if not lib:
            say(status, "library is empty — play some tracks first")
        else:
            name = (arg.strip() or "crescent_export")
            if not name.endswith(".m3u"):
                name += ".m3u"
            path = os.path.expanduser(f"~/{name}")
            lines = ["#EXTM3U"]
            for url, v in sorted(lib.items(), key=lambda kv: -(kv[1].get("last") or 0)):
                title = v.get("title") or url
                dur = int(v.get("duration") or -1)
                lines.append(f"#EXTINF:{dur},{title}")
                lines.append(url)
            try:
                with open(path, "w") as f:
                    f.write("\n".join(lines) + "\n")
                say(status, f"exported {len(lib)} tracks to ~/{name}")
            except Exception as e:
                say(status, f"export error: {e}")

    elif cmd in ("eq", "equalizer"):
        _EQ_PRESETS = {
            "bass":   "equalizer=f=60:width_type=o:width=2:g=6,equalizer=f=170:width_type=o:width=2:g=3",
            "treble": "equalizer=f=8000:width_type=o:width=2:g=4,equalizer=f=14000:width_type=o:width=2:g=6",
            "vocal":  "equalizer=f=1000:width_type=o:width=2:g=4,equalizer=f=3000:width_type=o:width=2:g=3",
            "flat":   None,
        }
        a = arg.strip().lower()
        if not a:
            say(status, "eq presets: bass | treble | vocal | flat")
        elif a not in _EQ_PRESETS:
            say(status, f"unknown preset '{a}' — try: bass | treble | vocal | flat")
        elif _EQ_PRESETS[a] is None:
            mpv.cmd("af", "clr", "")
            say(status, "equalizer: flat")
        else:
            mpv.cmd("af", "clr", "")
            mpv.cmd("af", "add", f"@eq:lavfi=[{_EQ_PRESETS[a]}]")
            say(status, f"equalizer: {a}")

    elif cmd in ("notify", "notifications"):
        if not shutil.which("notify-send"):
            say(status, "notify-send not found — install libnotify")
        else:
            ps["notifications"] = not ps.get("notifications", False)
            say(status, f"desktop notifications {'on' if ps['notifications'] else 'off'}")

    elif cmd == "radio":
        ps["radio"] = not ps.get("radio", False)
        say(status, f"Radio mode {'on — will auto-recommend related videos endlessly' if ps['radio'] else 'off'}")

    elif cmd == "saveq":
        name = arg.strip() or "default"
        if not ps["queue"]:
            say(status, "queue is empty — nothing to save")
        else:
            try:
                path = save_queue(name, list(ps["queue"]))
                say(status, f"saved {len(ps['queue'])} tracks to queue '{name}'")
            except Exception as e:
                say(status, f"saveq error: {e}")

    elif cmd == "loadq":
        name = arg.strip() or "default"
        try:
            items = load_queue_file(name)
            with _QUEUE_LOCK:
                ps["queue"].extend(items)
            for url, _ in items:
                mpv.cmd("loadfile", url, "append-play")
            say(status, f"loaded {len(items)} tracks from queue '{name}'")
        except FileNotFoundError:
            say(status, f"no saved queue named '{name}'")
        except Exception as e:
            say(status, f"loadq error: {e}")

    elif cmd in ("exit", "quit"):
        raise SystemExit
    elif cmd in ("ask", "ai") and arg:
        ai["thread"] = threading.Thread(
            target=ollama_worker, args=(arg, out, width), daemon=True)
        ai["thread"].start()
    elif cmd in ("ask", "ai"):
        say(status, "usage: ask <question>")
    else:
        reply = offline_reply(raw, ps)
        if not reply and random.random() < OFFLINE_CATCHALL_CHANCE:
            reply = random.choice(CATCHALL_REPLIES)
            ctx = _track_context(ps) or {"title": "music", "genre": "music", "artist": ""}
            try:
                reply = reply.format(title=ctx['title'], genre=ctx['genre'], artist=ctx['artist'])
            except:
                pass
        if reply:
            say(status, reply, voice="chat")
        else:
            ai["thread"] = threading.Thread(
                target=ollama_worker, args=(raw, out, width), daemon=True)
            ai["thread"].start()

# ---- main loop ----
def advance_empty_queue(mpv, ps, status):
    with _ADVANCE_LOCK:
        my_gen = ps["gen"]
        def do_shuffle():
            if ps["gen"] != my_gen:
                debug_log("auto-shuffle: stale result discarded (stop/skip/back happened meanwhile)")
                return
            lib = load_library()
            blacklist = load_blacklist()
            if lib:
                items = [(u, v) for u, v in lib.items() if u not in blacklist]
                with _QUEUE_LOCK:
                    if len(items) > 1 and ps["history"]:
                        cur = ps["history"][-1][0]
                        filtered = [(u, v) for u, v in items if u != cur]
                        if filtered:
                            items = filtered
                if items:
                    url, meta = random.choice(items)
                    title = meta.get("title") or url
                    mpv.load(url)
                    mpv.set_title(title)
                    mpv.cmd("set", "pause", False)
                    ps["loaded"] = True
                    ps["track_loaded_successfully"] = True
                    ps["manual_stop"] = False
                    ps["name"] = title
                    ps["resolved"] = title
                    with _QUEUE_LOCK:
                        ps["history"].append((url, title))
                    remember(url, title)
                    say(status, "Shuffling...")
                else:
                    ps["loaded"] = False
                    ps["track_loaded_successfully"] = False
                    ps["manual_stop"] = False
                    ps["name"] = None
                    ps["resolved"] = None
                    say(status, "stopped (no non-blacklisted tracks)")
            else:
                ps["loaded"] = False
                ps["track_loaded_successfully"] = False
                ps["manual_stop"] = False
                ps["name"] = None
                ps["resolved"] = None
                say(status, "stopped (no queue, no library)")

        cur_url = ps["history"][-1][0] if ps["history"] else None
        if can_recommend_from(cur_url) and (ps.get("radio") or random.random() < 0.45):
            say(status, "Auto-recommending...", sticky=False)

            def fetch_and_play():
                try:
                    urls, reason = get_related_videos(cur_url, limit=11)
                    try:
                        with open(os.path.expanduser("~/.crescent_rec_debug.log"), "a") as dbg:
                            dbg.write(f"{time.ctime()} | auto | url={cur_url!r} "
                                      f"| urls_found={len(urls)} | reason={reason!r}\n")
                    except Exception:
                        pass
                    if ps["gen"] != my_gen:
                        debug_log("auto-rec: stale result discarded (stop/skip/back happened meanwhile)")
                        return
                    with _QUEUE_LOCK:
                        urls = [u for u in urls
                                if not any(u == q[0] for q in ps["queue"])
                                and not any(u == h[0] for h in ps["history"])]
                    if not urls:
                        why = f" ({reason})" if reason else ""
                        say(status, f"No related videos found{why}. Shuffling instead.", sticky=False)
                        do_shuffle()
                        return
                    first, rest = urls[0], urls[1:]
                    mpv.load(first)
                    mpv.set_title(first)
                    mpv.cmd("set", "pause", False)
                    ps["loaded"] = True
                    ps["track_loaded_successfully"] = True
                    ps["manual_stop"] = False
                    ps["name"] = first
                    ps["resolved"] = None
                    with _QUEUE_LOCK:
                        ps["history"].append((first, first))
                    remember(first, first)
                    with _QUEUE_LOCK:
                        for u in rest:
                            ps["queue"].append((u, u))
                        queue_snapshot = list(ps["queue"])
                    mirror_queue_to_mpv(mpv, queue_snapshot)
                    if not os.path.isabs(first):
                        start_resolve(first, ps, status, mpv)
                    say(status, f"Recommended: {first}", sticky=False)
                except Exception as e:
                    log_bg_error("fetch_and_play")
                    err = str(e).replace("\n", " ").strip()[:150]
                    say(status, f"error fetching recommendation: {err} (see ~/.crescent_rec_debug.log)", sticky=False)

            threading.Thread(target=fetch_and_play, daemon=True).start()
        else:
            do_shuffle()

def main(scr):
    global MODEL
    curses.curs_set(1)
    scr.keypad(True)
    scr.nodelay(True)
    scr.timeout(80)
    try:
        curses.start_color()
        curses.use_default_colors()
        has_colors = True
    except Exception:
        has_colors = False
    if has_colors and curses.COLORS >= 256:
        curses.init_pair(1, 255, -1)
        curses.init_pair(2, 244, -1)
        curses.init_pair(3, 240, -1)
        curses.init_pair(4, 250, -1)
        curses.init_pair(5, 231, -1)
    else:
        curses.init_pair(1, curses.COLOR_WHITE, -1)
        curses.init_pair(2, curses.COLOR_WHITE, -1)
        curses.init_pair(3, curses.COLOR_WHITE, -1)
        curses.init_pair(4, curses.COLOR_WHITE, -1)
        curses.init_pair(5, curses.COLOR_WHITE, -1)

    settings = load_settings()
    if settings.get("model"):
        MODEL = settings["model"]

    mpv = Mpv()
    mpv.enable_audio_meter()
    ps = {"queue": [], "loaded": False, "track_loaded_successfully": False, "manual_stop": False, "name": None, "resolved": None, "history": [], "gen": 0, "radio": False, "sb_segments": [], "sb_url": None, "sb_skipped": set()}
    status = []
    out = queue.Queue()
    ai = {"thread": None}
    ui = {"help_until": 0.0, "wave_menu": False,
          "wave_style": int(settings.get("wave_style", 0)) % len(WAVE_STYLES),
          "list_until": 0.0, "list_lines": [], "list_items": []}
    bars, targets = [0.0] * 80, [random.random() for _ in range(80)]
    frame, title, paused = 0, None, False
    buf = []
    cmd_history = load_cmd_history()
    hist_idx = len(cmd_history)
    qsig, queue_until = None, 0.0
    next_ambient = 0.0
    last_path = None
    last_playlist_pos = None
    idle_ticks = 0
    last_loaded_url = None
    last_notified_name = None

    try:
        while True:
            if not mpv.alive():
                break
            width = max(20, curses.COLS - 6)

            try:
                while True:
                    item = out.get_nowait()
                    if isinstance(item, tuple):
                        msg, ttl = item
                        say(status, msg, sticky=False, ttl=ttl)
                    else:
                        say(status, item, sticky=True)
            except queue.Empty:
                pass

            ch = scr.getch()
            if ch in (10, 13) and not ui["wave_menu"]:
                raw = "".join(buf)
                if raw.strip() and (not cmd_history or cmd_history[-1] != raw):
                    cmd_history.append(raw)
                    save_cmd_history(cmd_history)
                hist_idx = len(cmd_history)
                run_command(raw, mpv, ps, status, out, ai, ui, width)
                buf.clear()
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
            elif ch == curses.KEY_UP and not ui["wave_menu"]:
                if cmd_history and hist_idx > 0:
                    hist_idx -= 1
                    buf[:] = list(cmd_history[hist_idx])
            elif ch == curses.KEY_DOWN and not ui["wave_menu"]:
                if hist_idx < len(cmd_history) - 1:
                    hist_idx += 1
                    buf[:] = list(cmd_history[hist_idx])
                elif hist_idx < len(cmd_history):
                    hist_idx = len(cmd_history)
                    buf.clear()
            elif ch == 9 and ui["wave_menu"]:
                ui["wave_style"] = (ui["wave_style"] + 1) % len(WAVE_STYLES)
                save_settings({"wave_style": ui["wave_style"]})
            elif ch == 27 and ui["wave_menu"]:
                ui["wave_menu"] = False
            elif ch in (10, 13) and ui["wave_menu"]:
                ui["wave_menu"] = False
            elif 32 <= ch <= 126:
                buf.append(chr(ch))

            frame += 1
            if frame % 10 == 0:
                t = mpv.prop("media-title")
                current_path = mpv.prop("path")
                title = t if (t and not is_url(t)) else None
                paused = bool(mpv.prop("pause"))

                playlist_pos = mpv.prop("playlist-pos")
                try:
                    playlist_pos = int(playlist_pos) if playlist_pos is not None else None
                except (TypeError, ValueError):
                    playlist_pos = None

                if last_playlist_pos is not None and playlist_pos is not None and playlist_pos != last_playlist_pos:
                    delta = playlist_pos - last_playlist_pos

                    if delta > 0:
                        steps = min(delta, len(ps["queue"]))
                        for _ in range(steps):
                            url, disp = ps["queue"].pop(0)
                            ps["history"].append((url, disp))
                            remember(url, source_display(disp) if os.path.isabs(disp) else disp)
                            if not os.path.isabs(url):
                                start_resolve(url, ps, status, mpv)

                        if steps:
                            url, disp = ps["history"][-1]
                            ps["loaded"] = True
                            ps["track_loaded_successfully"] = True
                            ps["manual_stop"] = False
                            ps["name"] = disp
                            ps["resolved"] = source_display(disp) if os.path.isabs(disp) else title
                            mpv.set_title(ps["resolved"] or disp)
                            say(status, "next: " + (ps["resolved"] or disp))

                    elif delta < 0:
                        target_index = len(ps["history"]) + delta
                        if target_index >= 0:
                            url, disp = ps["history"][target_index]
                            ps["history"] = ps["history"][:target_index + 1]
                            ps["loaded"] = True
                            ps["track_loaded_successfully"] = True
                            ps["manual_stop"] = False
                            ps["name"] = disp
                            ps["resolved"] = source_display(disp) if os.path.isabs(disp) else title
                            mpv.set_title(ps["resolved"] or disp)
                            say(status, "previous: " + (ps["resolved"] or disp))

                    last_path = current_path

                if current_path:
                    last_path = current_path
                    last_loaded_url = current_path
                    if ps["loaded"] and not ps["track_loaded_successfully"]:
                        ps["track_loaded_successfully"] = True
                        debug_log(f"Track loaded successfully: {current_path}")

                # SponsorBlock: fetch segments when track changes
                if current_path and current_path != ps["sb_url"]:
                    ps["sb_url"] = current_path
                    ps["sb_segments"] = []
                    ps["sb_skipped"] = set()
                    vid = extract_video_id(current_path)
                    if vid:
                        def _fetch_sb(vid=vid):
                            segs = sponsorblock_fetch(vid)
                            if segs:
                                ps["sb_segments"] = segs
                                say(status, f"SponsorBlock: {len(segs)} sponsor segment(s) loaded", sticky=False)
                        threading.Thread(target=_fetch_sb, daemon=True).start()

                # SponsorBlock: skip sponsor segments
                if ps.get("sb_segments") and not paused:
                    time_pos = mpv.prop("time-pos")
                    if time_pos is not None:
                        for seg_start, seg_end in ps["sb_segments"]:
                            seg_key = (seg_start, seg_end)
                            if seg_key not in ps["sb_skipped"] and seg_start <= time_pos < seg_end:
                                mpv.cmd("seek", seg_end, "absolute")
                                ps["sb_skipped"].add(seg_key)
                                say(status, f"Skipped sponsor ({int(seg_end - seg_start)}s)", sticky=False)
                                break

                # Desktop notifications when track changes
                if ps.get("notifications") and ps["name"] and ps["name"] != last_notified_name:
                    last_notified_name = ps["name"]
                    try:
                        subprocess.Popen(
                            ["notify-send", "Crescent", last_notified_name,
                             "--icon=audio-x-generic", "--expire-time=4000"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                        )
                    except Exception:
                        pass
                if playlist_pos is not None:
                    last_playlist_pos = playlist_pos

                if mpv.prop("eof-reached") and ps["loaded"] and not paused:
                    time_pos = mpv.prop("time-pos")
                    playback_started = time_pos is not None and time_pos > 0.0

                    if ps["manual_stop"]:
                        debug_log("Manual stop, suppressing auto-advance")
                        ps["loaded"] = False
                        ps["manual_stop"] = False
                        ps["name"] = None
                        ps["resolved"] = None
                    elif not ps["track_loaded_successfully"] or not playback_started:
                        error_msg = mpv.get_last_error()
                        debug_log(f"Playback failed. Error: {error_msg[:200]}")
                        say(status, f"Playback failed: {error_msg[:80]}...", sticky=True)
                        ps["loaded"] = False
                        ps["track_loaded_successfully"] = False
                    else:
                        ps["loaded"] = False
                        if ps["queue"]:
                            url, disp = ps["queue"].pop(0)
                            mpv.load(url)
                            mirror_queue_to_mpv(mpv, ps["queue"])
                            mpv.set_title(disp)
                            ps["loaded"] = True
                            ps["track_loaded_successfully"] = False
                            ps["manual_stop"] = False
                            ps["name"] = disp
                            ps["resolved"] = None
                            ps["history"].append((url, disp))
                            remember(url, source_display(disp) if os.path.isabs(disp) else disp)
                            if not os.path.isabs(url):
                                start_resolve(url, ps, status, mpv)
                            say(status, f"next: {disp}")
                        else:
                            advance_empty_queue(mpv, ps, status)
                    idle_ticks = 0
                elif ps["loaded"] and not ps["queue"] and not current_path and not paused:
                    if ps["track_loaded_successfully"]:
                        idle_ticks += 1
                        if idle_ticks >= 5:
                            ps["loaded"] = False
                            idle_ticks = 0
                            advance_empty_queue(mpv, ps, status)
                    else:
                        idle_ticks += 1
                        if idle_ticks >= 10:
                            debug_log("Track never loaded, giving up")
                            ps["loaded"] = False
                            ps["track_loaded_successfully"] = False
                            idle_ticks = 0
                            say(status, "Playback failed (no stream)", sticky=True)
                else:
                    idle_ticks = 0

            if len(ps["queue"]) != qsig:
                qsig = len(ps["queue"])
                queue_until = time.time() + STATUS_TTL

            now_check = time.time()
            if ps["loaded"] and not paused and now_check >= next_ambient:
                ctx = _track_context(ps)
                if not ctx:
                    ctx = {"title": "music", "genre": "music", "artist": ""}
                line = random.choice(AMBIENT_LINES)
                try:
                    line = line.format(title=ctx['title'], genre=ctx['genre'], artist=ctx['artist'])
                except:
                    pass
                say(status, line)
                next_ambient = now_check + random.uniform(45, 120)

            audio_level = mpv.level() if (ps["loaded"] and not paused) else None
            if audio_level is not None:
                for i in range(len(bars)):
                    if random.random() < 0.35:
                        spread = random.uniform(-0.22, 0.22)
                        targets[i] = max(0.0, min(1.0, audio_level + spread))
                    bars[i] += (targets[i] - bars[i]) * 0.55
            else:
                wave_ceiling = 1.0 if (ps["loaded"] and not paused) else 0.12
                for i in range(len(bars)):
                    if random.random() < 0.08:
                        targets[i] = random.uniform(0.0, wave_ceiling)
                    bars[i] += (targets[i] - bars[i]) * 0.35

            scr.erase()

            display = ps["resolved"] or title or (ps["name"] if not is_url(ps["name"] or "") else None)
            if paused:
                state = "PAUSED"
            elif display:
                state = "Now Playing: " + display
            elif ps["loaded"]:
                state = "loading..."
            else:
                state = ""
            max_state_len = max(0, curses.COLS - 4)
            if len(state) > max_state_len:
                state = (state[: max(0, max_state_len - 1)] + "…") if max_state_len > 1 else ""
            put(scr, ROW_STATE, 2, state, curses.color_pair(1))

            for y, line in enumerate(MARK):
                put(scr, ROW_MARK + y, 2, line, curses.color_pair(1))

            glyphs = WAVE_STYLES[ui["wave_style"]][1]
            for i, v in enumerate(bars[:curses.COLS - 4]):
                idx = min(len(glyphs) - 1, int(v * (len(glyphs) - 1)))
                put(scr, ROW_WAVES, 2 + i, glyphs[idx], curses.color_pair(1))
            if ui["wave_menu"]:
                put(scr, ROW_WAVES + 1, 2,
                    f"wave: {WAVE_STYLES[ui['wave_style']][0]} · Tab=style Esc=close",
                    curses.color_pair(4))

            put(scr, ROW_INPUT, 2, "Crescent : " + "".join(buf), curses.color_pair(1))

            now = time.time()
            status[:] = [s for s in status if s[2] or now - s[0] < s[4]]
            queue_row = ROW_STATUS + STATUS_LINE_GAP
            if now < ui["list_until"] and ui["list_lines"]:
                for i, line in enumerate(ui["list_lines"]):
                    put(scr, ROW_STATUS + i, 2, line, curses.color_pair(4))
                queue_row = ROW_STATUS + len(ui["list_lines"]) + 1
            elif now < ui["help_until"]:
                help_lines = [
                    "commands:",
                    "  play <url|search> · shuffle [genre] · list · info · dl [url] · tag <genre>",
                    "  like/heart/fav (again to unlike) · favorites · shuffle favorites",
                    "  clear list · pause · skip · back · stop · vol <n> · ask · exit",
                    "  handoff · handoff receive — send/pull playback via KDE Connect",
                    "  model [name] · audit",
                    "  search <query> — wiki + web search",
                ]
                for i, line in enumerate(help_lines):
                    put(scr, ROW_STATUS + i, 2, line, curses.color_pair(4))
                queue_row = ROW_STATUS + len(help_lines) + 1
            else:
                visible = [(t, m, v) for (t, m, _, v, _ttl) in status][-3:]
                if ai["thread"] is not None:
                    if ai["thread"].is_alive():
                        visible = visible + [(now, "thinking...", "system")]
                    else:
                        ai["thread"] = None
                for i, (t, msg, voice) in enumerate(visible):
                    if msg.endswith("..."):
                        msg = msg[:-3] + " " + spinner_char()
                    if voice == "chat":
                        attr = curses.color_pair(5) | curses.A_BOLD
                        if now - t < 0.35:
                            attr |= curses.A_REVERSE
                        put(scr, ROW_STATUS + i * STATUS_LINE_GAP, 2, "» " + msg, attr)
                    else:
                        put(scr, ROW_STATUS + i * STATUS_LINE_GAP, 2, msg, curses.color_pair(4))
                if visible:
                    queue_row = ROW_STATUS + (len(visible) - 1) * STATUS_LINE_GAP + STATUS_LINE_GAP

            if ps["queue"] and now < queue_until and now >= ui["list_until"]:
                names = " | ".join(d for _, d in ps["queue"][:2])
                put(scr, queue_row, 2, f"queue ({len(ps['queue'])}): {names}",
                    curses.color_pair(2))

            if DEBUG:
                put(scr, queue_row + 2, 2,
                    f"[debug] COLS={curses.COLS} state={state!r} "
                    f"resolved={ps['resolved']!r} name={ps['name']!r} loaded={ps['loaded']} paused={paused}",
                    curses.color_pair(4))

            scr.refresh()
    finally:
        mpv.p.terminate()

if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(USAGE)
        sys.exit(0)
    curses.wrapper(main)
