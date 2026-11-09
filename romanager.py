"""
RoManager - Roblox multi-account manager and multi-instance launcher (single file).
Python 3.11+, Windows 10/11 x64.  Dependencies: customtkinter, psutil, Pillow, pywebview.

Status: written without a Windows machine to test on. Expect to paste back errors
from the first runs.

Not implemented (no fake buttons): tray icon, language packs, auto-updater,
whole-file database encryption (secrets are DPAPI-encrypted per field), adopting
Roblox clients that were started before RoManager.
"""
import collections, ctypes, hashlib, hmac, io, json, math, os, queue, random
import re, secrets, shutil, sqlite3, subprocess, sys, threading, time, urllib.error
import urllib.parse, urllib.request
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, messagebox, simpledialog

if sys.platform != "win32":
    print("RoManager only runs on Windows 10/11.")
    sys.exit(1)

import psutil
import customtkinter as ctk

try:
    from PIL import Image
except Exception:  # avatars are optional
    Image = None

APP_NAME = "RoManager"
APPDIR = Path(os.environ.get("APPDATA", ".")) / APP_NAME
PROC_NAME = "RobloxPlayerBeta.exe"
NCPU = psutil.cpu_count() or 1

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
crypt32 = ctypes.windll.crypt32
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
psapi = ctypes.windll.psapi
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
kernel32.CreateJobObjectW.restype = ctypes.c_void_p
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.SetProcessWorkingSetSizeEx.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]

# ----------------------------------------------------------------- DPAPI ---
class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _from_blob(blob: DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


def dpapi_protect(data: bytes) -> bytes:
    inb, _keep = _to_blob(data)
    out = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(inb), APP_NAME, None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError()
    return _from_blob(out)


def dpapi_unprotect(data: bytes) -> bytes:
    inb, _keep = _to_blob(data)
    out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError()
    return _from_blob(out)


def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return salt.hex() + "$" + h.hex()


def verify_pw(pw: str, stored: str) -> bool:
    try:
        s, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(s), 200_000)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


# -------------------------------------------------------------- database ---
DEFAULTS = {
    # appearance
    "theme": "Obsidian", "accent": "", "ui_scale": "100", "radius": "18", "density": "Comfortable",
    "font": "Segoe UI", "opacity": "100", "topmost": "0", "show_avatars": "1", "show_bars": "1",
    "cols": "1", "anim": "1", "sidebar_small": "0",
    # general / launch / recovery
    "start_min": "0", "autostart": "0", "delay": "8", "default_dest": "",
    "auto": "0", "max_attempts": "3", "restart_delay": "15", "multi": "1",
    # performance
    "preset": "smooth", "interval": "3", "monitor": "0",
    "c_prio": "Normal", "c_w": "0", "c_h": "0", "c_automin": "0",
    "p_eco": "0", "p_cores": "0", "p_bg": "0", "p_trim": "0", "p_mem": "0",
    "p_gpu": "default", "p_gfx": "0", "p_fps": "0", "p_gpucap": "0", "p_ramcap": "0",
    "isolate": "0", "app_cap": "15", "ram_guard": "1", "relax_on": "1", "p_cap": "0", "mv_w": "320", "mv_h": "180", "mv_pw": "960", "mv_ph": "540",
    "ui_iv": "1", "ui_pause": "1", "self_eco": "1", "self_eco_always": "0", "prev_on": "0", "prev_iv": "5",
    # discord / security
    "webhook": "", "wh_on": "0", "n_crash": "1", "n_hung": "1", "n_restart": "1", "n_launch": "1",
    "lock_hash": "",
}

MIGRATIONS = [
    """BEGIN;
    CREATE TABLE groups(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
    CREATE TABLE accounts(
        id INTEGER PRIMARY KEY, user_id INTEGER, username TEXT NOT NULL,
        alias TEXT, color TEXT, label TEXT,
        group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
        is_main INTEGER DEFAULT 0, cookie_enc BLOB NOT NULL,
        status TEXT DEFAULT 'Unknown', robux INTEGER, last_used REAL,
        created REAL);
    CREATE TABLE clients(pid INTEGER PRIMARY KEY, account_id INTEGER, dest TEXT, started REAL);
    CREATE TABLE favourites(id INTEGER PRIMARY KEY, place_id INTEGER UNIQUE, name TEXT);
    CREATE TABLE launch_history(id INTEGER PRIMARY KEY, account_id INTEGER, dest TEXT,
        ts REAL, ok INTEGER, error TEXT);
    CREATE TABLE layouts(name TEXT PRIMARY KEY, data TEXT);
    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE recovery_history(id INTEGER PRIMARY KEY, account_id INTEGER, ts REAL,
        attempt INTEGER, result TEXT);
    COMMIT;""",
    """BEGIN; ALTER TABLE accounts ADD COLUMN pw_enc BLOB; COMMIT;""",
    """BEGIN;
    INSERT INTO settings(key,value) VALUES
      ('preset','lowest'),('interval','5'),('anim','0'),('prev_on','0'),('ui_iv','3'),('ui_pause','1'),('self_eco','1'),
      ('c_prio','Below normal'),('c_w','320'),('c_h','180'),('p_eco','1'),('p_cores','1'),('p_bg','1'),('p_trim','1'),
      ('p_mem','1'),('p_gpu','power'),('p_gfx','1'),('p_fps','45'),('p_cap','20')
    ON CONFLICT(key) DO UPDATE SET value=excluded.value;
    COMMIT;""",
    """BEGIN; INSERT INTO settings(key,value) VALUES('p_cap','5'),('p_gpucap','5')
       ON CONFLICT(key) DO UPDATE SET value=excluded.value; COMMIT;""",
    """BEGIN; INSERT INTO settings(key,value) VALUES('self_eco_always','1'),('c_automin','1')
       ON CONFLICT(key) DO UPDATE SET value=excluded.value; COMMIT;""",
    """BEGIN; INSERT INTO settings(key,value) VALUES('p_ramcap','100')
       ON CONFLICT(key) DO UPDATE SET value=excluded.value; COMMIT;""",
    """BEGIN; INSERT INTO settings(key,value) VALUES
      ('theme','Obsidian'),('radius','18'),('anim','1'),('ui_iv','1'),('self_eco_always','0'),('isolate','0'),
      ('p_ramcap','150'),('app_cap','20'),('ram_guard','1'),('relax_on','1'),('prev_on','0')
       ON CONFLICT(key) DO UPDATE SET value=excluded.value; COMMIT;""",
    """BEGIN; INSERT INTO settings(key,value) VALUES('app_cap','15'),('p_ramcap','200')
       ON CONFLICT(key) DO UPDATE SET value=excluded.value; COMMIT;""",
    """BEGIN; INSERT INTO settings(key,value) VALUES('preset','smooth'),('interval','3')
       ON CONFLICT(key) DO UPDATE SET value=excluded.value; COMMIT;""",
]


class DB:
    def __init__(self):
        APPDIR.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._cache = {}
        self.c = self._open(APPDIR / "romanager.db")
        ver = self.c.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[ver:], start=ver + 1):
            with self.lock:
                self.c.executescript(sql)
                self.c.execute(f"PRAGMA user_version={i}")
                self.c.commit()
        self._backup()

    def _open(self, path):
        def connect():
            c = sqlite3.connect(path, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=NORMAL")  # fewer forced disk flushes
            c.execute("PRAGMA temp_store=MEMORY")
            c.execute("PRAGMA cache_size=-4000")
            return c

        try:
            c = connect()
            if c.execute("PRAGMA quick_check").fetchone()[0] == "ok":
                return c
            c.close()
        except sqlite3.DatabaseError:
            pass
        # damaged file: keep it aside and restore the newest automatic backup
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if path.exists():
            path.rename(path.with_name(f"romanager.corrupt-{stamp}.db"))
        for ext in ("-wal", "-shm"):
            Path(str(path) + ext).unlink(missing_ok=True)
        backups = sorted((APPDIR / "backups").glob("romanager-*.db"))
        if backups:
            shutil.copy2(backups[-1], path)
        return connect()

    def _backup(self):
        """Accounts are never lost silently: one backup per day, newest 7 kept."""
        try:
            bdir = APPDIR / "backups"
            bdir.mkdir(exist_ok=True)
            f = bdir / f"romanager-{datetime.now():%Y%m%d}.db"
            if not f.exists():
                dst = sqlite3.connect(f)
                with self.lock:
                    self.c.backup(dst)
                dst.close()
                for old in sorted(bdir.glob("romanager-*.db"))[:-7]:
                    old.unlink(missing_ok=True)
        except Exception:
            pass

    def q(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            self.c.commit()
            return cur

    def all(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchone()

    def get(self, k):
        if k in self._cache:
            return self._cache[k]
        r = self.one("SELECT value FROM settings WHERE key=?", (k,))
        v = r[0] if r else DEFAULTS.get(k, "")
        self._cache[k] = v
        return v

    def set(self, k, v):
        self.q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (k, str(v)))
        self._cache[k] = str(v)

    def flag(self, k):
        return self.get(k) == "1"

    def num(self, k, lo, hi):
        try:
            return max(lo, min(hi, int(float(self.get(k)))))
        except ValueError:
            return int(DEFAULTS[k])


# ------------------------------------------------------------ roblox web ---
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RoManager/1.0"


class AuthExpired(Exception):
    pass


def http(method, url, headers=None, body=None, timeout=15):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def ck(cookie):
    return {"Cookie": f".ROBLOSECURITY={cookie}"}


def whoami(cookie):
    s, _, b = http("GET", "https://users.roblox.com/v1/users/authenticated", ck(cookie))
    if s == 401:
        raise AuthExpired("Session is invalid or expired.")
    if s != 200:
        raise RuntimeError(f"Roblox returned HTTP {s} while verifying the session.")
    return json.loads(b)


def get_robux(cookie, uid):
    s, _, b = http("GET", f"https://economy.roblox.com/v1/users/{uid}/currency", ck(cookie))
    return json.loads(b).get("robux") if s == 200 else None


def get_avatar(uid):
    s, _, b = http("GET", "https://thumbnails.roblox.com/v1/users/avatar-headshot"
                   f"?userIds={uid}&size=48x48&format=Png")
    if s != 200:
        return None
    url = json.loads(b)["data"][0].get("imageUrl")
    if not url:
        return None
    s, _, img = http("GET", url)
    return img if s == 200 else None


def auth_ticket(cookie):
    url = "https://auth.roblox.com/v1/authentication-ticket"
    h = {**ck(cookie), "Referer": "https://www.roblox.com/", "Origin": "https://www.roblox.com",
         "Content-Type": "application/json"}
    s, hd, _ = http("POST", url, h, b"{}")
    if s == 403 and "x-csrf-token" in hd:
        h["X-CSRF-TOKEN"] = hd["x-csrf-token"]
        s, hd, _ = http("POST", url, h, b"{}")
    if s == 401:
        raise AuthExpired("Authentication expired. Re-add this account.")
    if s == 200 and "rbx-authentication-ticket" in hd:
        return hd["rbx-authentication-ticket"]
    raise RuntimeError(f"Could not get a launch ticket (HTTP {s}).")


def parse_dest(text):
    t = (text or "").strip().strip('"').strip()
    if not t:
        raise ValueError("Enter a place ID or a Roblox link.")
    if re.fullmatch(r"\d{1,19}", t):
        return {"place_id": int(t)}
    if re.match(r"^(www\.)?roblox\.com/", t, re.I):
        t = "https://" + t
    u = urllib.parse.urlparse(t)
    host = (u.hostname or "").lower()
    if u.scheme in ("http", "https") and (host == "roblox.com" or host.endswith(".roblox.com")):
        q = urllib.parse.parse_qs(u.query)
        path = u.path.rstrip("/").lower()
        if path in ("/share", "/share-links"):  # new-style share links
            code = q.get("code", [""])[0]
            kind = q.get("type", ["Server"])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", code):
                raise ValueError("The share link code looks invalid.")
            if kind not in ("Server", "ExperienceInvite"):
                raise ValueError(f"Share link type '{kind}' is not supported.")
            return {"share_code": code, "share_type": kind}
        m = re.search(r"/games/(\d{1,19})", u.path)
        pid = int(m.group(1)) if m else None
        if pid is None and path == "/games/start":
            v = q.get("placeId", [""])[0]
            pid = int(v) if v.isdigit() else None
        if pid:
            d = {"place_id": pid}
            code = q.get("privateServerLinkCode", [""])[0]
            if code:
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", code):
                    raise ValueError("The private server link code looks invalid.")
                d["link_code"] = code
            job = q.get("gameInstanceId", [""])[0]
            if job:
                if not re.fullmatch(r"[0-9a-fA-F-]{36}", job):
                    raise ValueError("The server (job) id looks invalid.")
                d["job_id"] = job
            return d
        raise ValueError("Link must contain /games/<place id> or be a roblox.com/share link.")
    raise ValueError("Not a valid place ID or roblox.com link.")


def dest_label(d):
    if not d.get("place_id"):
        return "share link"
    return str(d["place_id"]) + (" (private)" if d.get("link_code") else "") + (" (server)" if d.get("job_id") else "")


def _post_json(cookie, url, payload):
    h = {**ck(cookie), "Referer": "https://www.roblox.com/", "Origin": "https://www.roblox.com",
         "Content-Type": "application/json"}
    body = json.dumps(payload).encode()
    s, hd, b = http("POST", url, h, body)
    if s == 403 and "x-csrf-token" in hd:
        h["X-CSRF-TOKEN"] = hd["x-csrf-token"]
        s, hd, b = http("POST", url, h, body)
    if s == 401:
        raise AuthExpired("Authentication expired. Log this account in again.")
    return s, b


def resolve_share(cookie, code, kind):
    s, b = _post_json(cookie, "https://apis.roblox.com/sharelinks/v1/resolve-link", {"linkId": code, "linkType": kind})
    if s != 200:
        raise RuntimeError(f"Roblox could not resolve the share link (HTTP {s}). It may be expired. "
                           "Tip: open it in your browser and paste the final address-bar URL instead.")
    j = json.loads(b)
    ps = j.get("privateServerInviteData")
    if ps and ps.get("placeId") and ps.get("linkCode"):
        return {"place_id": int(ps["placeId"]), "link_code": str(ps["linkCode"])}
    ex = j.get("experienceInviteData")
    if ex and ex.get("placeId"):
        d = {"place_id": int(ex["placeId"])}
        if ex.get("instanceId"):
            d["job_id"] = str(ex["instanceId"])
        return d
    raise RuntimeError("That share link is invalid, expired or of an unsupported type.")


def resolve_access_code(cookie, place_id, link_code):
    """Private server links need an access code that the game page hands to logged-in users."""
    url = f"https://www.roblox.com/games/{place_id}?privateServerLinkCode={urllib.parse.quote(link_code)}"
    s, _, b = http("GET", url, ck(cookie), timeout=20)
    if s != 200:
        return ""
    page = b.decode("utf-8", "ignore")
    for pat in (r"joinPrivateGame\(\s*\d+\s*,\s*['\"]([\w-]+)['\"]",
                r"data-access-code=['\"]([\w-]+)['\"]",
                r"accessCode['\"]?\s*[:=]\s*['\"]([\w-]+)['\"]"):
        m = re.search(pat, page)
        if m:
            return m.group(1)
    return ""


def build_uri(ticket, d):
    tid = str(random.randint(10**10, 10**11 - 1))
    pid = d["place_id"]
    if d.get("job_id"):
        p = f"request=RequestGameJob&browserTrackerId={tid}&placeId={pid}&gameId={d['job_id']}&isPlayTogetherGame=false"
    elif d.get("link_code"):
        p = (f"request=RequestPrivateGame&browserTrackerId={tid}&placeId={pid}"
             f"&accessCode={d.get('access_code', '')}&linkCode={d['link_code']}")
    else:
        p = f"request=RequestGame&browserTrackerId={tid}&placeId={pid}&isPlayTogetherGame=false"
    pl = urllib.parse.quote("https://assetgame.roblox.com/game/PlaceLauncher.ashx?" + p, safe="")
    return (f"roblox-player:1+launchmode:play+gameinfo:{ticket}+launchtime:{int(time.time() * 1000)}"
            f"+placelauncherurl:{pl}+browsertrackerid:{tid}+robloxLocale:en_us+gameLocale:en_us+channel:+LaunchExp:InApp")


def roblox_installed():
    roots = [Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "Versions",
             Path(os.environ.get("ProgramFiles(x86)", "")) / "Roblox" / "Versions"]
    return any(r.exists() and any(r.glob("*/RobloxPlayerBeta.exe")) for r in roots)


def roblox_pids():
    return {p.pid for p in psutil.process_iter(["name"]) if p.info["name"] == PROC_NAME}


WEBHOOK_RE = re.compile(r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")


def discord_post(url, text):
    body = json.dumps({"content": text[:1900], "username": APP_NAME}).encode()
    s, _, _ = http("POST", url, {"Content-Type": "application/json"}, body, timeout=10)
    return s in (200, 204)


# ----------------------------------------------------------- browser login ---
FILL_JS = """(function(u,p){
  function setv(el,v){var d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
    d.set.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));}
  var tries=0;var t=setInterval(function(){
    var a=document.getElementById('login-username'),b=document.getElementById('login-password');
    if(a&&b){clearInterval(t);setv(a,u);setv(b,p);
      setTimeout(function(){var x=document.getElementById('login-button');if(x)x.click();},500);}
    else if(++tries>40){clearInterval(t);}
  },500);})(__U__,__P__);"""


def _tmpdir():
    d = APPDIR / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _wipe(path):
    try:
        if path.exists():
            path.write_bytes(b"\0" * max(1, path.stat().st_size))
            path.unlink()
    except OSError:
        pass


def login_window():
    """Runs in a separate process (--login <token>). Opens the real Roblox login page in a private
    WebView2 window. Credentials (optional) arrive and the session leaves through short-lived
    DPAPI-encrypted files that are wiped immediately, because a windowed exe has no stdin/stdout."""
    tok = sys.argv[sys.argv.index("--login") + 1] if len(sys.argv) > sys.argv.index("--login") + 1 else "x"
    base = _tmpdir() / tok

    def fail(msg):
        base.with_suffix(".err").write_text(msg, encoding="utf-8")

    try:
        import webview
        creds = None
        fin = base.with_suffix(".in")
        if fin.exists():
            try:
                creds = json.loads(dpapi_unprotect(fin.read_bytes()).decode())
            finally:
                _wipe(fin)
        win = webview.create_window("Log in to Roblox - RoManager", "https://www.roblox.com/login",
                                    width=520, height=780)
        box = {"creds": creds}

        def fill(*_):
            c = box["creds"]
            if not c:
                return
            try:
                if "login" not in (win.get_current_url() or "").lower():
                    return
                box["creds"] = None
                win.evaluate_js(FILL_JS.replace("__U__", json.dumps(c["u"])).replace("__P__", json.dumps(c["p"])))
            except Exception:
                pass

        if creds:
            win.events.loaded += fill

        def watch():
            logged_since, last_err = None, ""
            while True:
                time.sleep(1.5)
                try:
                    for jar in win.get_cookies():
                        if ".ROBLOSECURITY" in jar:
                            val = jar[".ROBLOSECURITY"].value
                            if len(val) > 100:
                                base.with_suffix(".out").write_bytes(dpapi_protect(val.encode()))
                                win.destroy()
                                return
                except Exception as e:
                    last_err = f"cookies: {e}"
                url = ""
                try:
                    url = win.get_current_url() or win.evaluate_js("location.href") or ""
                except Exception as e:
                    last_err = f"url: {e}"
                if any(x in url for x in ("/home", "/games", "/discover", "/charts")):
                    logged_since = logged_since or time.time()
                    if time.time() - logged_since > 15:
                        fail("You are logged in, but the login window did not expose the session cookie"
                             + (f" ({last_err})" if last_err else "") + ". Use Import cookie instead.")
                        win.destroy()
                        return

        webview.start(watch, private_mode=True)
    except ModuleNotFoundError as e:
        fail(f"This build of RoManager is missing the login component ('{e.name}'). "
             "Rebuild it with the updated build.yml (it installs pywebview), or use Import cookie meanwhile.")
    except Exception:
        import traceback
        fail(traceback.format_exc())


def browser_login(user=None, pw=None):
    tok = secrets.token_hex(8)
    base = _tmpdir() / tok
    cmd = [sys.executable, "--login", tok] if getattr(sys, "frozen", False) else \
        [sys.executable, os.path.abspath(sys.argv[0]), "--login", tok]
    try:
        if user and pw:
            base.with_suffix(".in").write_bytes(dpapi_protect(json.dumps({"u": user, "p": pw}).encode()))
        proc = subprocess.Popen(cmd, creationflags=0x08000000, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait()  # no time limit: it ends when you finish logging in or close the window
        out, err = base.with_suffix(".out"), base.with_suffix(".err")
        if out.exists():
            return dpapi_unprotect(out.read_bytes()).decode()
        hint = err.read_text(encoding="utf-8", errors="ignore")[-700:] if err.exists() else ""
        raise RuntimeError("The login window was closed before login finished." + (f"\n\n{hint}" if hint else ""))
    finally:
        for ext in (".in", ".out", ".err"):
            _wipe(base.with_suffix(ext))


# --------------------------------------------------------------- windows ---
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MonEnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def find_windows(pids):
    out = {}

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                area = (r.right - r.left) * (r.bottom - r.top)
                if area > out.get(pid.value, (0, 0))[1]:
                    out[pid.value] = (hwnd, area)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return {p: v[0] for p, v in out.items()}


def monitors():
    res = []

    def cb(hm, hdc, rc, lp):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hm, ctypes.byref(mi)):
            r = mi.rcWork
            res.append((r.left, r.top, r.right - r.left, r.bottom - r.top, bool(mi.dwFlags & 1)))
        return True

    user32.EnumDisplayMonitors(None, None, MonEnumProc(cb), 0)
    res.sort(key=lambda m: not m[4])
    return [m[:4] for m in res] or [(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))]


def win_show(hwnd, cmd):  # 6 minimize, 3 maximize, 9 restore
    user32.ShowWindow(hwnd, cmd)


def win_focus(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)


def win_place(hwnd, x, y, w, h):
    if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, 9)
    user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h), 0x0004 | 0x0010)  # NOZORDER|NOACTIVATE


def win_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return [r.left, r.top, r.right - r.left, r.bottom - r.top]


def win_state(hwnd):
    if not hwnd:
        return "No window"
    if user32.IsHungAppWindow(hwnd):
        return "Not responding"
    if user32.IsIconic(hwnd):
        return "Minimized"
    if user32.IsZoomed(hwnd):
        return "Maximized"
    return "Normal"


# ---------------------------------------------------- per-process tuning ---
class POWER_THROTTLING(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


class MEM_PRIO(ctypes.Structure):
    _fields_ = [("MemoryPriority", wintypes.ULONG)]


PRIO = {"Normal": psutil.NORMAL_PRIORITY_CLASS, "Below normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
        "Idle": psutil.IDLE_PRIORITY_CLASS}

PRESET_DEFS = {
    "balanced": dict(prio="Normal", size=None, automin=False, eco=False, cores=0, bgthrottle=False, trim=False,
                     memprio=False, gpu="default", gfx=0, fps=0, cpucap=0, interval=None),
    "low": dict(prio="Below normal", size=(640, 360), automin=False, eco=False, cores=0, bgthrottle=True,
                trim=True, memprio=True, gpu="power", gfx=3, fps=30, cpucap=50, interval=5),
    "ultra": dict(prio="Below normal", size=(480, 270), automin=False, eco=True, cores=max(1, NCPU // 2),
                  bgthrottle=True, trim=True, memprio=True, gpu="power", gfx=1, fps=20, cpucap=25, interval=5),
    "minimal": dict(prio="Below normal", size=(320, 180), automin=False, eco=True, cores=1,
                    bgthrottle=True, trim=True, memprio=True, gpu="power", gfx=1, fps=15, cpucap=10, interval=5),
    "lowest": dict(prio="Below normal", size=(320, 180), automin=True, eco=True, cores=1,
                   bgthrottle=True, trim=True, memprio=True, gpu="power", gfx=1, fps=45, cpucap=5, gpucap=5, ramcap=200, interval=5),
    # Lowest resources that still run smoothly: the CPU ceiling follows each client's real need (never squeezing it),
    # windows shrink only when the GPU is busy, FPS is capped at 45, graphics at the floor, and nothing is forced
    # out of RAM (swapping is what makes games stutter).
    "smooth": dict(prio="Normal", size=(480, 270), automin=False, eco=False, cores=0, bgthrottle=False,
                   trim=False, memprio=False, gpu="power", gfx=1, fps=45, cpucap=25, gpucap=12, gpumin=0.5,
                   ramcap=0, auto=True, capfloor=8, interval=3),
}


MIN_SETTINGS = ('<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                'xsi:noNamespaceSchemaLocation="http://www.roblox.com/roblox.xsd" version="4">\n'
                '\t<External>null</External>\n\t<External>nil</External>\n'
                '\t<Item class="UserGameSettings" referent="RBX0">\n\t\t<Properties>\n\t\t</Properties>\n'
                '\t</Item>\n</roblox>\n')


class JOB_CPU_RATE(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]


def _handle(pid, access):
    return kernel32.OpenProcess(access, False, pid) or None


def os_set_eco(pid, on):
    """Windows 'efficiency mode' (EcoQoS): documented SetProcessInformation / ProcessPowerThrottling."""
    h = _handle(pid, 0x0200 | 0x1000)
    if not h:
        return False
    try:
        st = POWER_THROTTLING(1, 1, 1 if on else 0)
        return bool(kernel32.SetProcessInformation(h, 4, ctypes.byref(st), ctypes.sizeof(st)))
    finally:
        kernel32.CloseHandle(h)


def os_set_memprio(pid, level):
    h = _handle(pid, 0x0200 | 0x1000)
    if not h:
        return False
    try:
        mp = MEM_PRIO(level)
        return bool(kernel32.SetProcessInformation(h, 0, ctypes.byref(mp), ctypes.sizeof(mp)))
    finally:
        kernel32.CloseHandle(h)


def os_trim(pid):
    h = _handle(pid, 0x0100 | 0x0400)
    if not h:
        return False
    try:
        return bool(psapi.EmptyWorkingSet(h))
    finally:
        kernel32.CloseHandle(h)


def os_set_ramcap(pid, mb):
    """Hard working-set ceiling (documented SetProcessWorkingSetSizeEx): Windows keeps at most `mb` MB of the
    process in physical RAM and pages the rest out. 0 lifts the limit."""
    h = _handle(pid, 0x0100 | 0x0400)  # PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION
    if not h:
        return False
    try:
        if mb <= 0:  # HARDWS_MIN_DISABLE | HARDWS_MAX_DISABLE
            return bool(kernel32.SetProcessWorkingSetSizeEx(h, 1 << 20, 64 << 30, 0x2 | 0x8))
        return bool(kernel32.SetProcessWorkingSetSizeEx(h, 1 << 20, int(mb) << 20, 0x2 | 0x4))  # MIN_DISABLE | MAX_ENABLE
    finally:
        kernel32.CloseHandle(h)


def os_set_cap(c, pct):
    """Hard CPU cap through a Windows job object (documented JobObjectCpuRateControlInformation).
    pct is a percent of TOTAL CPU for that one client; 0 removes the cap."""
    if pct <= 0:
        if c.get("job"):
            info = JOB_CPU_RATE(0, 0)
            kernel32.SetInformationJobObject(c["job"], 15, ctypes.byref(info), ctypes.sizeof(info))
        return True
    if not c.get("job"):
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        ph = _handle(c["pid"], 0x0100 | 0x0001)
        if not ph:
            kernel32.CloseHandle(job)
            return False
        ok = kernel32.AssignProcessToJobObject(job, ph)
        kernel32.CloseHandle(ph)
        if not ok:
            kernel32.CloseHandle(job)
            return False
        c["job"] = job
    info = JOB_CPU_RATE(0x1 | 0x4, int(pct * 100))  # ENABLE | HARD_CAP, rate in 1/100 percent
    return bool(kernel32.SetInformationJobObject(c["job"], 15, ctypes.byref(info), ctypes.sizeof(info)))


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                   "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOB_BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class JOB_EXT_LIMIT(ctypes.Structure):
    _fields_ = [("Basic", JOB_BASIC_LIMIT), ("Io", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_SELF_JOB = None


def set_self_cap(pct):
    """Hard CPU ceiling for RoManager itself (percent of total CPU, 0 = none). SILENT_BREAKAWAY_OK keeps the
    Roblox clients and the login window out of this job, so they are not squeezed by it."""
    global _SELF_JOB
    try:
        if _SELF_JOB is None:
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return False
            ext = JOB_EXT_LIMIT()
            ext.Basic.LimitFlags = 0x1000  # JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
            if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(ext), ctypes.sizeof(ext)) or \
                    not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
                kernel32.CloseHandle(job)
                return False
            _SELF_JOB = job
        info = JOB_CPU_RATE((0x1 | 0x4) if pct > 0 else 0, int(pct * 100) if pct > 0 else 0)
        return bool(kernel32.SetInformationJobObject(_SELF_JOB, 15, ctypes.byref(info), ctypes.sizeof(info)))
    except Exception:
        return False


class PDH_VALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class PDH_ITEM(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", PDH_VALUE)]


pdh = ctypes.windll.pdh
pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
pdh.PdhGetFormattedCounterArrayW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
pdh.PdhGetFormattedCounterValue.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]


def gpu_usage_by_pid():
    """GPU % per process (busiest engine, like Task Manager) from the Windows 'GPU Engine' performance
    counters. Returns None when this PC does not provide them."""
    q = ctypes.c_void_p()
    if pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) != 0:
        return None
    try:
        c = ctypes.c_void_p()
        if pdh.PdhAddEnglishCounterW(q, r"\GPU Engine(*)\Utilization Percentage", 0, ctypes.byref(c)) != 0:
            return None
        pdh.PdhCollectQueryData(q)
        time.sleep(0.4)
        pdh.PdhCollectQueryData(q)
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        r = pdh.PdhGetFormattedCounterArrayW(c, 0x200, ctypes.byref(size), ctypes.byref(count), None)
        if (r & 0xFFFFFFFF) != 0x800007D2 or not size.value:  # PDH_MORE_DATA
            return None
        buf = ctypes.create_string_buffer(size.value)
        if pdh.PdhGetFormattedCounterArrayW(c, 0x200, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return None
        items = ctypes.cast(buf, ctypes.POINTER(PDH_ITEM))
        eng = {}
        for i in range(count.value):
            it = items[i]
            m = re.match(r"pid_(\d+)_.*?engtype_(\w+)", it.szName or "")
            if m and it.FmtValue.CStatus in (0, 1):
                key = (int(m.group(1)), m.group(2))
                eng[key] = eng.get(key, 0.0) + it.FmtValue.doubleValue
        out = {}
        for (pid, _t), v in eng.items():
            out[pid] = max(out.get(pid, 0.0), v)
        return out
    finally:
        pdh.PdhCloseQuery(q)


class SysMeter:
    """System-wide disk busy % and memory pages/sec (Windows performance counters, read once per sample)."""

    def __init__(self):
        self.q, self.cd, self.cp, self.ok = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p(), False
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.q)) == 0:
            a = pdh.PdhAddEnglishCounterW(self.q, r"\PhysicalDisk(_Total)\% Disk Time", 0, ctypes.byref(self.cd))
            b = pdh.PdhAddEnglishCounterW(self.q, r"\Memory\Pages/sec", 0, ctypes.byref(self.cp))
            self.ok = a == 0 and b == 0
            if self.ok:
                pdh.PdhCollectQueryData(self.q)

    def read(self):
        if not self.ok:
            return None
        pdh.PdhCollectQueryData(self.q)
        out = []
        for c in (self.cd, self.cp):
            v = PDH_VALUE()
            if pdh.PdhGetFormattedCounterValue(c, 0x200, None, ctypes.byref(v)) != 0 or v.CStatus not in (0, 1):
                return None
            out.append(v.doubleValue)
        return out[0], out[1]


def roblox_exes():
    roots = [Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "Versions",
             Path(os.environ.get("ProgramFiles(x86)", "")) / "Roblox" / "Versions"]
    return [p for r in roots if r.exists() for p in r.glob("*/RobloxPlayerBeta.exe")]


# ------------------------------------------------------------------ core ---
def fmt_dur(s):
    s = int(max(0, s))
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_mb(b):
    return f"{b / 1048576:,.0f} MB"


class Core:
    def __init__(self):
        self.db = DB()
        self.clients = {}
        self.lock = threading.RLock()
        self.logs = []
        self.toastq = []
        self.hist = collections.deque(maxlen=3700)
        self.started = time.time()
        self.attempts = {}
        self.failed = 0
        self.mutex = None
        self.ram_lifted, self.relax, self.ram_extra = False, 0, 0
        self.meter, self.disk_hot, self.disk_cool, self.disk_pct, self.pages_s = None, 0, 0, 0.0, 0.0
        self.holder = None
        self.multi_status = "Multi-instance is off."
        self.last_net = None
        self.qs = {"running": False, "total": 0, "done": 0, "failed": 0}
        self.pause_ev = threading.Event()
        self.pause_ev.set()
        self.cancel_ev = threading.Event()
        self.stop = False

    # -- infra
    def log(self, msg, kind=None):
        self.logs.append(f"[{datetime.now():%H:%M:%S}] {msg}")
        if kind:
            self.toastq.append((msg, kind))

    def start(self):
        psutil.cpu_percent(None)
        threading.Thread(target=self.sample_loop, daemon=True).start()
        threading.Thread(target=self.gpu_loop, daemon=True).start()
        self.log(self.set_multi(True))

    def shutdown(self):
        self.stop = True
        self.cancel_ev.set()

    def notify(self, kind, text):
        if not self.db.flag("wh_on") or not self.db.flag("n_" + kind):
            return
        enc = self.db.get("webhook")
        if not enc:
            return

        def send():
            try:
                url = dpapi_unprotect(bytes.fromhex(enc)).decode()
                if not discord_post(url, text):
                    self.log("Discord webhook rejected the message.")
            except Exception as e:
                self.log(f"Discord notification failed: {e}")

        threading.Thread(target=send, daemon=True).start()

    # -- multi-instance (holds Roblox's documented-by-observation named mutex in OUR process)
    def _hold(self, info):
        """Owns Roblox's two single-instance kernel objects (a named mutex and a named event) in OUR process,
        from one long-lived thread (a mutex belongs to the thread that created it). Nothing in Roblox is touched."""
        res = info["res"]
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = ctypes.c_void_p
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        m = k.CreateMutexW(None, True, "ROBLOX_singletonMutex")
        em = ctypes.get_last_error()
        ev = k.CreateEventW(None, True, False, "ROBLOX_singletonEvent")
        ee = ctypes.get_last_error()
        if not m or not ev:
            res["msg"] = "Windows would not create the single-instance objects."
        elif em == 183 or ee == 183:
            res["msg"] = ("Roblox is already running and owns its single-instance objects. Close every Roblox "
                          "window, then turn multi-instance on (or start RoManager before Roblox).")
        else:
            res["ok"] = True
        info["ready"].set()
        if res.get("ok"):
            info["stop"].wait()
        for h in (m, ev):
            if h:
                k.CloseHandle(h)

    def set_multi(self, on):
        if not on:
            if self.holder:
                self.holder["stop"].set()
                self.holder = None
            self.mutex = None
            self.multi_status = "Multi-instance is off."
            return self.multi_status
        if self.holder:
            return self.multi_status
        info = {"stop": threading.Event(), "ready": threading.Event(), "res": {}}
        threading.Thread(target=self._hold, args=(info,), daemon=True).start()
        info["ready"].wait(5)
        if info["res"].get("ok"):
            self.holder, self.mutex = info, True
            self.multi_status = "Multi-instance ON: RoManager holds Roblox's single-instance mutex and event."
        else:
            info["stop"].set()
            self.mutex = None
            self.multi_status = info["res"].get("msg", "Could not start multi-instance.")
        return self.multi_status

    # -- launching
    def launch_one(self, aid, dest):
        if not roblox_installed():
            raise RuntimeError("Roblox Player is not installed for this Windows user.")
        acc = self.db.one("SELECT * FROM accounts WHERE id=?", (aid,))
        if not acc:
            raise RuntimeError("Account no longer exists.")
        name = acc["alias"] or acc["username"]
        iso = self.db.flag("isolate")
        try:
            cookie = dpapi_unprotect(acc["cookie_enc"]).decode()
            self.resolve_dest(cookie, dest)
            pid = self._start_client(acc, cookie, dest, iso)
            if pid is None and iso:
                self.log(f"{name}: the isolated start produced no client, retrying with the standard launcher.", "warn")
                pid = self._start_client(acc, cookie, dest, False)
        except AuthExpired:
            self.db.q("UPDATE accounts SET status='Expired' WHERE id=?", (aid,))
            raise
        finally:
            cookie = None
        if pid is None:
            raise RuntimeError("Roblox did not start. If another client is open, read the multi-instance status on the Play tab.")
        c = {"pid": pid, "account_id": aid, "name": name, "dest": dest, "start": time.time(),
             "proc": psutil.Process(pid), "cpu": 0.0, "rss": 0, "hwnd": None, "hung": 0, "expected": False,
             "tuned": None, "sized": None, "last_trim": 0.0, "denied": set()}
        c["proc"].cpu_percent(None)
        with self.lock:
            self.clients[pid] = c
        self.db.q("INSERT OR REPLACE INTO clients(pid,account_id,dest,started) VALUES(?,?,?,?)",
                  (pid, aid, dest_label(dest), c["start"]))
        self.db.q("UPDATE accounts SET last_used=?, status='Valid' WHERE id=?", (time.time(), aid))
        threading.Thread(target=self.apply_when_ready, args=(pid,), daemon=True).start()
        return pid

    def _start_client(self, acc, cookie, dest, isolated):
        ticket = auth_ticket(cookie)  # tickets are single-use, so every attempt asks for a fresh one
        before = roblox_pids()
        uri = build_uri(ticket, dest)
        if isolated:
            self.launch_isolated(acc, uri)
        else:
            os.startfile(uri)
        end = time.time() + (30 if isolated else 60)
        while time.time() < end and not self.stop:
            time.sleep(1)
            with self.lock:
                tracked = set(self.clients)
            new = roblox_pids() - before - tracked
            if new:
                return min(new)
        return None

    def _check_kill(self, others, name):
        dead = [p for p in others if not psutil.pid_exists(p)]
        if dead:
            self.log(f"{len(dead)} other client(s) closed when {name} started, so Roblox's single-instance lock is "
                     f"still active. Status: {self.multi_status}", "bad")

    def resolve_dest(self, cookie, dest):
        if dest.get("share_code") and not dest.get("place_id"):
            dest.update(resolve_share(cookie, dest["share_code"], dest.get("share_type", "Server")))
            self.log(f"Share link resolved to place {dest_label(dest)}")
        if dest.get("link_code") and not dest.get("access_code") and not dest.get("job_id"):
            code = resolve_access_code(cookie, dest["place_id"], dest["link_code"])
            dest["access_code"] = code
            if not code:
                self.log("Could not read the private server access code; trying the link code alone.", "warn")

    def prepare_sandbox(self, key):
        """Each account gets its own Roblox data folder so clients stop sharing (and fighting over) local session data."""
        local = APPDIR / "sandbox" / str(key) / "Local"
        rb = local / "Roblox"
        rb.mkdir(parents=True, exist_ok=True)
        dst = rb / "GlobalBasicSettings_13.xml"
        src = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "GlobalBasicSettings_13.xml"
        if not dst.exists() and src.exists():
            shutil.copy2(src, dst)
        pr = self.profile()
        if not dst.exists() and (pr["gfx"] or pr["fps"]):
            dst.write_text(MIN_SETTINGS, encoding="utf-8")
        if (pr["gfx"] or pr["fps"]) and dst.exists():
            try:
                self.edit_roblox_settings(pr["gfx"], pr["fps"], dst)
            except Exception as e:
                self.log(f"Sandbox graphics settings not changed: {e}")
        return local

    def launch_isolated(self, acc, uri):
        exes = roblox_exes()
        if not exes:
            raise RuntimeError("RobloxPlayerBeta.exe was not found. Install Roblox first.")
        exe = max(exes, key=lambda p: p.stat().st_mtime)
        local = self.prepare_sandbox(acc["user_id"] or acc["id"])
        env = os.environ.copy()
        env["LOCALAPPDATA"] = str(local)
        subprocess.Popen([str(exe), uri], env=env, cwd=str(exe.parent), close_fds=True,
                         creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP

    def start_queue(self, ids, dest, delay):
        if self.qs["running"]:
            raise RuntimeError("A launch queue is already running.")
        self.cancel_ev.clear()
        self.pause_ev.set()
        self.qs.update(running=True, total=len(ids), done=0, failed=0)
        threading.Thread(target=self._queue, args=(list(ids), dest, delay), daemon=True).start()

    def _queue(self, ids, dest, delay):
        if not self.mutex:
            self.log(self.set_multi(True))
        self.apply_global_perf()
        for i, aid in enumerate(ids):
            self.pause_ev.wait()
            if self.cancel_ev.is_set():
                self.log("Launch queue cancelled.")
                break
            row = self.db.one("SELECT username, alias FROM accounts WHERE id=?", (aid,))
            name = (row["alias"] or row["username"]) if row else str(aid)
            self.log(f"Launching {name} -> {dest_label(dest)}")
            try:
                pid = self.launch_one(aid, dest)
                self.db.q("INSERT INTO launch_history(account_id,dest,ts,ok) VALUES(?,?,?,1)",
                          (aid, dest_label(dest), time.time()))
                self.log(f"{name}: started (PID {pid})", "ok")
                others = {c["pid"] for c in self.snapshot() if c["pid"] != pid}
                if others:
                    threading.Timer(8, self._check_kill, args=(others, name)).start()
            except Exception as e:
                self.qs["failed"] += 1
                self.failed += 1
                self.db.q("INSERT INTO launch_history(account_id,dest,ts,ok,error) VALUES(?,?,?,0,?)",
                          (aid, dest_label(dest), time.time(), str(e)))
                self.log(f"{name}: FAILED - {e}", "bad")
            self.qs["done"] = i + 1
            if i < len(ids) - 1:
                t_end = time.time() + delay
                while time.time() < t_end and not self.cancel_ev.is_set():
                    self.pause_ev.wait()
                    time.sleep(0.25)
        self.qs["running"] = False
        ok = self.qs["done"] - self.qs["failed"]
        self.log(f"Queue finished: {ok} ok, {self.qs['failed']} failed.")
        self.notify("launch", f"Launch finished: {ok} started, {self.qs['failed']} failed.")

    # -- client control
    def snapshot(self):
        with self.lock:
            return list(self.clients.values())

    def close_client(self, pid, expected=True):
        with self.lock:
            c = self.clients.get(pid)
        if c:
            c["expected"] = expected
            try:
                c["proc"].terminate()
            except psutil.Error as e:
                self.log(f"Close failed for PID {pid}: {e}")

    def restart_client(self, pid):
        with self.lock:
            c = self.clients.get(pid)
        if not c:
            return
        aid, dest = c["account_id"], c["dest"]
        self.close_client(pid)

        def go():
            time.sleep(2)
            try:
                self.launch_one(aid, dest)
                self.log(f"{c['name']}: restarted")
            except Exception as e:
                self.log(f"{c['name']}: restart failed - {e}")

        threading.Thread(target=go, daemon=True).start()

    def close_all(self):
        for c in self.snapshot():
            self.close_client(c["pid"])

    def window_cmd(self, pid, cmd):
        with self.lock:
            c = self.clients.get(pid)
        if not c or not c["hwnd"]:
            self.log("Window not found for that client.")
            return
        if c.get("embedded") or c.get("mv"):
            self.log(f"{c['name']}: Multi-View is managing this window. Use its buttons there, or leave the tab.")
            return
        {"min": lambda h: win_show(h, 6), "max": lambda h: win_show(h, 3),
         "restore": lambda h: win_show(h, 9), "focus": win_focus}[cmd](c["hwnd"])

    def window_all(self, cmd):
        for c in self.snapshot():
            self.window_cmd(c["pid"], cmd)

    def resize(self, pid, w, h):
        with self.lock:
            c = self.clients.get(pid)
        if c and c["hwnd"] and not (c.get("embedded") or c.get("mv")):
            x, y, _, _ = win_rect(c["hwnd"])
            win_place(c["hwnd"], x, y, w, h)

    def arrange(self, mode):
        cl = [c for c in self.snapshot() if c["hwnd"] and not (c.get("embedded") or c.get("mv"))]
        if not cl:
            self.log("No client windows to arrange.")
            return
        mons = monitors()
        x, y, w, h = mons[min(self.db.num("monitor", 0, 16), len(mons) - 1)]
        n = len(cl)
        if mode == "h":
            cols, rows = n, 1
        elif mode == "v":
            cols, rows = 1, n
        else:
            cols = math.ceil(math.sqrt(n))
            rows = math.ceil(n / cols)
        cw, ch = w // cols, h // rows
        for i, c in enumerate(cl):
            r, k = divmod(i, cols)
            win_place(c["hwnd"], x + k * cw, y + r * ch, cw, ch)

    def save_layout(self, name):
        data = {str(c["account_id"]): win_rect(c["hwnd"]) for c in self.snapshot() if c["hwnd"] and not (c.get("embedded") or c.get("mv"))}
        self.db.q("INSERT INTO layouts(name,data) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET data=excluded.data",
                  (name, json.dumps(data)))

    def restore_layout(self, name):
        row = self.db.one("SELECT data FROM layouts WHERE name=?", (name,))
        if not row:
            raise ValueError(f"No layout named '{name}'.")
        data = json.loads(row[0])
        mons = monitors()
        minx = min(m[0] for m in mons)
        maxx = max(m[0] + m[2] for m in mons)
        miny = min(m[1] for m in mons)
        maxy = max(m[1] + m[3] for m in mons)
        for c in self.snapshot():
            r = data.get(str(c["account_id"]))
            if r and c["hwnd"] and not (c.get("embedded") or c.get("mv")):
                x, y, w, h = r  # clamp so a layout saved on another monitor setup stays on-screen
                x = max(minx, min(x, maxx - 100))
                y = max(miny, min(y, maxy - 100))
                win_place(c["hwnd"], x, y, w, h)

    # -- performance
    def profile(self):
        p = self.db.get("preset")
        iv = self.db.num("interval", 1, 30)
        if p in PRESET_DEFS:
            d = dict(PRESET_DEFS[p])
        else:
            w, h = self.db.num("c_w", 0, 8000), self.db.num("c_h", 0, 8000)
            d = dict(prio=self.db.get("c_prio"), size=(w, h) if w > 0 and h > 0 else None,
                     automin=self.db.flag("c_automin"), eco=self.db.flag("p_eco"),
                     cores=self.db.num("p_cores", 0, NCPU), bgthrottle=self.db.flag("p_bg"),
                     trim=self.db.flag("p_trim"), memprio=self.db.flag("p_mem"), gpu=self.db.get("p_gpu"),
                     gfx=self.db.num("p_gfx", 0, 10), fps=self.db.num("p_fps", 0, 240),
                     cpucap=self.db.num("p_cap", 0, 100),
                     gpucap=self.db.num("p_gpucap", 0, 100),
                     ramcap=self.db.num("p_ramcap", 0, 8000), interval=None)
        d.setdefault("gpucap", 0)
        d.setdefault("ramcap", 0)
        d.setdefault("gpumin", 0.03)
        d.setdefault("auto", False)
        d.setdefault("capfloor", 5)
        if d["ramcap"] and self.ram_extra:
            d["ramcap"] += self.ram_extra
        if self.ram_lifted:
            d["ramcap"] = 0
        if self.relax >= 1:  # a client crashed under the limits: ease them step by step
            d["ramcap"] = 0
            d["cpucap"] = 0 if (self.relax >= 2 or not d["cpucap"]) else min(100, d["cpucap"] * 3)
        if self.relax >= 2:
            d["auto"] = False
        if self.relax >= 3:
            d["gpucap"] = 0
            d["automin"] = False
        d["prio_class"] = PRIO.get(d["prio"], psutil.NORMAL_PRIORITY_CLASS)
        d["interval"] = max(d["interval"], iv) if d["interval"] else iv
        return d

    def _deny(self, c, what):
        if what not in c["denied"]:
            c["denied"].add(what)
            self.log(f"{c['name']}: Windows or Roblox did not allow '{what}' for PID {c['pid']} (skipped).", "warn")

    def auto_cap(self, c, pr):
        """Finds the lowest CPU ceiling that does not squeeze the client. Usage close to the ceiling means the client is
        being starved (that is what stutter is), so the ceiling jumps up at once; if usage stays well below it for a few
        checks, the ceiling comes down to just above what the client really needs. New clients load at full speed."""
        cap = c.get("cap_now") or pr["cpucap"]
        use = c["cpu"]
        new = cap
        if time.time() - c["start"] < 45:
            new = 100
        elif use >= cap * 0.85:
            c["calm"] = 0
            new = min(100, max(cap * 3, use * 4))
        elif use < cap * 0.55:
            c["calm"] = c.get("calm", 0) + 1
            if c["calm"] >= 3:
                c["calm"] = 0
                new = max(pr["capfloor"], use * 1.5)
        else:
            c["calm"] = 0
        c["cap_now"] = round(new, 1) if abs(new - cap) >= 2 else round(cap, 1)

    def tune(self, c, pr, fg, idx):
        bg = bool(c["hwnd"]) and c["hwnd"] != fg and not c.get("embedded")  # the client you are playing is never throttled
        prio = pr["prio_class"]
        if bg and pr["bgthrottle"] and prio == psutil.NORMAL_PRIORITY_CLASS:
            prio = psutil.BELOW_NORMAL_PRIORITY_CLASS
        eco = bool(pr["eco"] or (bg and pr["bgthrottle"]))
        now = time.time()
        do_trim = bg and pr["trim"] and not pr["ramcap"] and now - c["last_trim"] > 60
        if pr["auto"]:
            self.auto_cap(c, pr)
            cap = int(c.get("cap_now") or pr["cpucap"])
        else:
            cap = pr["cpucap"]
        state = (prio, eco, bool(pr["memprio"]), pr["cores"], pr["size"], bool(c["hwnd"]), cap, pr["ramcap"])
        if c["tuned"] == state and not do_trim:
            return
        pid = c["pid"]
        if c["tuned"] != state:
            try:
                c["proc"].nice(prio)
            except psutil.Error:
                self._deny(c, "CPU priority")
            if eco or c.get("eco_set"):
                if os_set_eco(pid, eco):
                    c["eco_set"] = eco
                elif eco:
                    self._deny(c, "efficiency mode")
            if pr["memprio"] or c.get("mem_set"):
                if os_set_memprio(pid, 2 if pr["memprio"] else 5):
                    c["mem_set"] = pr["memprio"]
                elif pr["memprio"]:
                    self._deny(c, "low memory priority")
            if pr["ramcap"] or c.get("ram_set"):
                if os_set_ramcap(pid, pr["ramcap"]):
                    c["ram_set"] = pr["ramcap"] > 0
                elif pr["ramcap"]:
                    self._deny(c, "RAM limit")
            if cap or c.get("cap_set"):
                if os_set_cap(c, cap):
                    c["cap_set"] = cap > 0
                elif cap:
                    self._deny(c, "CPU usage cap")
            if pr["cores"] > 0 or c.get("aff_set"):
                n = pr["cores"] or NCPU
                cores = sorted({(idx * n + k) % NCPU for k in range(n)})
                try:
                    c["proc"].cpu_affinity(cores)
                    c["aff_set"] = pr["cores"] > 0
                except (psutil.Error, OSError, ValueError):
                    self._deny(c, "CPU core limit")
            if pr["size"] and c["hwnd"] and not (c.get("embedded") or c.get("mv")) and c["sized"] != pr["size"]:
                x, y, _, _ = win_rect(c["hwnd"])
                win_place(c["hwnd"], x, y, *pr["size"])
                c["sized"] = pr["size"]
            c["tuned"] = state
        if do_trim:
            os_trim(pid)
            c["last_trim"] = now

    def tune_all(self, cl=None):
        cl = self.snapshot() if cl is None else cl
        if not cl:
            return
        pr = self.profile()
        fg = user32.GetForegroundWindow()
        for i, c in enumerate(cl):
            try:
                self.tune(c, pr, fg, i)
            except Exception as e:
                self.log(f"Tuning error for PID {c['pid']}: {e}")
        if pr["automin"]:
            for c in cl:
                if c["hwnd"] and not (c.get("embedded") or c.get("mv")) and c["hwnd"] != fg and not user32.IsIconic(c["hwnd"]):
                    win_show(c["hwnd"], 6)

    def apply_when_ready(self, pid):
        for _ in range(40):
            with self.lock:
                c = self.clients.get(pid)
            if not c:
                return
            w = find_windows({pid})
            if pid in w:
                c["hwnd"] = w[pid]
                break
            time.sleep(1)
        self.tune_all()

    def apply_all(self):
        self.relax, self.ram_lifted, self.disk_hot, self.ram_extra = 0, False, 0, 0
        for c in self.snapshot():
            c["tuned"] = None
            c["sized"] = None
        threading.Thread(target=self.apply_global_perf, daemon=True).start()
        self.tune_all()

    def apply_global_perf(self):
        pr = self.profile()
        notes = []
        try:
            notes.append(self.set_gpu_pref(pr["gpu"]))
        except Exception as e:
            notes.append(f"GPU preference not changed: {e}")
        if (pr["gfx"] or pr["fps"]) and not self.db.flag("isolate"):
            if roblox_pids():
                notes.append("Roblox is running, so graphics quality / FPS cap were not changed "
                             "(Roblox rewrites its settings file on exit). Close all clients and launch again.")
            else:
                try:
                    notes.append(self.edit_roblox_settings(pr["gfx"], pr["fps"]))
                except Exception as e:
                    notes.append(f"Roblox settings not changed: {e}")
        for n in notes:
            if n:
                self.log(n)

    def set_gpu_pref(self, mode):
        import winreg
        exes = roblox_exes()
        if not exes:
            return ""
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\DirectX\UserGpuPreferences")
        with key:
            for e in exes:
                if mode == "default":
                    try:
                        winreg.DeleteValue(key, str(e))
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(key, str(e), 0, winreg.REG_SZ, f"GpuPreference={1 if mode == 'power' else 2};")
        label = {"default": "system default", "power": "power saving", "high": "high performance"}[mode]
        return (f"Windows GPU preference set to {label} for Roblox (applies to clients started afterwards; "
                "only matters on PCs with more than one GPU).")

    def edit_roblox_settings(self, gfx, fps, path=None):
        real = path is None
        path = path or Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "GlobalBasicSettings_13.xml"
        if not path.exists():
            return "Roblox settings file not found yet. Start Roblox once, close it, then apply again."
        txt = path.read_text(encoding="utf-8", errors="ignore")
        if real:
            bak = path.with_name(path.name + ".romanager.bak")
            if not bak.exists():
                bak.write_text(txt, encoding="utf-8")
        done = []

        def sub(pattern, val, label, tag):
            nonlocal txt
            new, n = re.subn(pattern, lambda m: m.group(1) + str(val) + m.group(2), txt)
            if n:
                txt = new
            elif "</Properties>" in txt:
                txt = txt.replace("</Properties>", "\t" + tag.format(val) + "\n\t\t</Properties>", 1)
            else:
                return
            done.append(label)

        if gfx:
            sub(r'(<int name="GraphicsQualityLevel">)-?\d+(</int>)', 2 * gfx - 1, f"graphics quality {gfx}/10",
                '<int name="GraphicsQualityLevel">{}</int>')
            sub(r'(<token name="SavedQualityLevel">)-?\d+(</token>)', gfx, "quality mode",
                '<token name="SavedQualityLevel">{}</token>')
        if fps:
            sub(r'(<int name="FramerateCap">)-?\d+(</int>)', fps, f"FPS cap {fps}",
                '<int name="FramerateCap">{}</int>')
        path.write_text(txt, encoding="utf-8")
        return ("Roblox settings updated: " + ", ".join(done) + ".") if done else \
            "Roblox settings file has no matching keys; nothing changed."

    def restore_roblox_settings(self):
        path = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "GlobalBasicSettings_13.xml"
        bak = path.with_name(path.name + ".romanager.bak")
        if roblox_pids():
            raise RuntimeError("Close all Roblox clients first.")
        if not bak.exists():
            raise RuntimeError("No backup found (nothing was changed yet).")
        path.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
        self.log("Original Roblox graphics settings restored.", "ok")

    # -- GPU target (Windows cannot hard-cap another program's GPU, so measure and steer the render size)
    def gpu_loop(self):
        wait = 4
        while not self.stop:
            time.sleep(wait)
            wait = 10  # relaxed, unless a client needed adjusting
            try:
                pr = self.profile()
                cl = self.snapshot()
                if pr["gpucap"] and cl and self.gpu_control(cl, pr):
                    wait = 4
            except Exception as e:
                self.log(f"GPU control error: {e}")

    def gpu_control(self, cl, pr):
        target = pr["gpucap"]
        usage = gpu_usage_by_pid()
        if usage is None:
            if not getattr(self, "_gpu_warned", False):
                self._gpu_warned = True
                self.log("This PC does not provide per-process GPU counters, so the GPU target cannot be enforced "
                         "(window size, graphics quality and FPS cap still apply).", "warn")
            return
        changed = False
        for c in cl:
            h = c["hwnd"]
            if not h or user32.IsIconic(h):
                continue
            g = usage.get(c["pid"], 0.0)
            c["gpu"] = g
            base = tuple(pr["size"]) if pr["size"] else c.setdefault("gbase", tuple(win_rect(h)[2:]))
            sc = c.get("gscale", 1.0)
            if g > target * 1.15:
                sc = max(pr.get("gpumin", 0.03), sc * 0.8)
            elif g < target * 0.5 and sc < 1.0:
                sc = min(1.0, sc * 1.15)
            else:
                continue
            c["gscale"] = sc
            changed = True
            x, y = (0, 0) if c.get("embedded") else win_rect(h)[:2]
            win_place(h, x, y, max(32, int(base[0] * sc)), max(18, int(base[1] * sc)))
        return changed

    # -- monitoring + recovery
    def sample_loop(self):
        while not self.stop:
            iv = self.profile()["interval"]
            time.sleep(iv if self.clients else max(iv, 10))
            try:
                self.sample()
            except Exception as e:
                self.log(f"Monitor error: {e}")

    def sample(self):
        vm = psutil.virtual_memory()
        net = psutil.net_io_counters()
        now = time.time()
        up = down = 0.0
        if self.last_net:
            dt = max(0.001, now - self.last_net[0])
            up, down = (net.bytes_sent - self.last_net[1]) / dt, (net.bytes_recv - self.last_net[2]) / dt
        self.last_net = (now, net.bytes_sent, net.bytes_recv)
        cl = self.snapshot()
        need = {c["pid"] for c in cl if not c["hwnd"] or not user32.IsWindow(c["hwnd"])}
        wins = find_windows(need) if need else {}
        per, rb_cpu, rb_ram = {}, 0.0, 0
        for c in cl:
            p = c["proc"]
            try:
                with p.oneshot():
                    if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                        raise psutil.NoSuchProcess(c["pid"])
                    c["cpu"] = p.cpu_percent(None) / NCPU
                    c["rss"] = p.memory_info().rss
                c["hwnd"] = wins.get(c["pid"], c["hwnd"])
                per[c["pid"]] = (c["cpu"], c["rss"])
                rb_cpu += c["cpu"]
                rb_ram += c["rss"]
                self._check_hung(c)
            except psutil.NoSuchProcess:
                self.on_exit(c["pid"])
        if self.db.flag("ram_guard") and any(c.get("ram_set") for c in cl):
            if self.meter is None:
                self.meter = SysMeter()
            r = self.meter.read()
            if r:
                self.disk_pct, self.pages_s = r
                if r[0] > 2.0 and r[1] > 15:  # disk busy above 2% because Windows is swapping the clients
                    self.disk_hot, self.disk_cool = self.disk_hot + 1, 0
                elif r[0] < 1.0 and r[1] < 5:
                    self.disk_cool, self.disk_hot = self.disk_cool + 1, 0
                else:
                    self.disk_hot = self.disk_cool = 0
                if self.disk_hot >= 2:
                    self.disk_hot = 0
                    self.ram_extra += 50
                    cap = self.profile()["ramcap"]
                    if not cap or cap >= 1500:
                        self.ram_lifted = True
                        self.log("Swapping kept the disk busy even with a big allowance, so the RAM limit was lifted.", "bad")
                    else:
                        self.log(f"Disk was {r[0]:.0f}% busy because Windows was swapping the clients. "
                                 f"RAM allowance raised to {cap} MB to bring the disk back under 2%.", "warn")
                elif self.disk_cool >= 12 and self.ram_extra > 0:  # about a minute of calm: tighten again
                    self.ram_extra, self.disk_cool = max(0, self.ram_extra - 25), 0
        else:
            self.disk_hot = self.disk_cool = 0
        self.hist.append({"t": now, "cpu": psutil.cpu_percent(None), "ram_pct": vm.percent,
                          "ram_used": vm.used, "ram_avail": vm.available, "total": vm.total,
                          "rb_cpu": rb_cpu, "rb_ram": rb_ram, "up": up, "down": down, "per": per,
                          "count": len(per)})
        self.tune_all(cl)

    def _check_hung(self, c):
        if c["hwnd"] and user32.IsHungAppWindow(c["hwnd"]):
            c["hung"] += 1
        else:
            c["hung"] = 0
            c.pop("hung_notified", None)
        if c["hung"] >= 3 and not c.get("hung_notified"):
            c["hung_notified"] = True
            self.log(f"{c['name']}: client is not responding", "warn")
            if self.limited(c):
                self._relax(f"{c['name']} stopped responding under the limits.")
            self.notify("hung", f"{c['name']}: Roblox client (PID {c['pid']}) is not responding.")
            if self.db.flag("auto"):
                self.log(f"{c['name']}: terminating unresponsive client for recovery")
                self.close_client(c["pid"], expected=False)

    def limited(self, c):
        return bool(c.get("cap_set") or c.get("ram_set") or c.get("aff_set") or c.get("eco_set"))

    def _relax(self, why):
        if not self.db.flag("relax_on") or self.relax >= 3:
            return
        self.relax += 1
        self.log(f"{why} RoManager eased the limits (step {self.relax}/3) so clients are less likely to crash. "
                 "Performance → Apply and save puts your own limits back.", "warn")

    def on_exit(self, pid):
        with self.lock:
            c = self.clients.pop(pid, None)
        self.db.q("DELETE FROM clients WHERE pid=?", (pid,))
        if not c:
            return
        if c["expected"]:
            self.log(f"{c['name']}: client closed")
            return
        self.failed += 1
        self.log(f"{c['name']}: client exited unexpectedly (PID {pid})", "bad")
        if time.time() - c["start"] < 900 and self.limited(c):
            self._relax(f"{c['name']} crashed under the limits.")
        self.notify("crash", f"{c['name']}: Roblox client (PID {pid}) exited unexpectedly.")
        if not self.db.flag("auto"):
            return
        aid = c["account_id"]
        if time.time() - c["start"] > 600:
            self.attempts[aid] = 0
        n = self.attempts.get(aid, 0)
        threading.Thread(target=self._recover, args=(aid, c["name"], c["dest"], n), daemon=True).start()

    def _recover(self, aid, name, dest, n):
        mx = self.db.num("max_attempts", 1, 20)
        while n < mx and not self.stop:
            n += 1
            self.attempts[aid] = n
            time.sleep(self.db.num("restart_delay", 1, 3600))
            try:
                self.launch_one(aid, dest)
                self.db.q("INSERT INTO recovery_history(account_id,ts,attempt,result) VALUES(?,?,?,?)",
                          (aid, time.time(), n, "restarted"))
                self.log(f"{name}: recovered (attempt {n}/{mx})", "ok")
                self.notify("restart", f"{name}: client restarted (attempt {n}/{mx}).")
                return
            except Exception as e:
                self.db.q("INSERT INTO recovery_history(account_id,ts,attempt,result) VALUES(?,?,?,?)",
                          (aid, time.time(), n, f"failed: {e}"))
                self.log(f"{name}: restart attempt {n}/{mx} failed - {e}")
        self.log(f"{name}: giving up after {mx} attempts", "bad")
        self.notify("restart", f"{name}: automatic restart gave up after {mx} attempts.")


# ============================================================================
#                                    UI
# ============================================================================
try:
    from PIL import ImageDraw
except Exception:
    ImageDraw = None

THEMES = {
    "Obsidian": dict(mode="dark", bg="#07080c", side="#0b0d13", panel="#10131b", panel2="#171b26", border="#222838", text="#eef1f8", muted="#8a93a8", accent="#7c8bff"),
    "Frost": dict(mode="light", bg="#e9edf5", side="#f6f8fc", panel="#ffffff", panel2="#eef2f9", border="#d9e0ee", text="#121722", muted="#66708a", accent="#5b6cff"),
    "Midnight": dict(mode="dark", bg="#0a0c11", side="#0e1117", panel="#131720", panel2="#1a202b", border="#222a38", text="#e8ecf4", muted="#8791a7", accent="#5b8cff"),
    "Aurora": dict(mode="dark", bg="#07100f", side="#0a1514", panel="#0f1c1b", panel2="#152826", border="#1d3532", text="#e6f5f2", muted="#7fa39d", accent="#2dd4bf"),
    "Crimson": dict(mode="dark", bg="#0e0a0b", side="#140e10", panel="#1b1214", panel2="#251a1d", border="#3a2529", text="#f5e9eb", muted="#a58a90", accent="#f43f5e"),
    "Violet": dict(mode="dark", bg="#0c0a14", side="#110e1c", panel="#171325", panel2="#1f1a31", border="#2d2648", text="#ece8fa", muted="#9890b8", accent="#a78bfa"),
    "Graphite": dict(mode="dark", bg="#111111", side="#161616", panel="#1c1c1c", panel2="#262626", border="#333333", text="#eeeeee", muted="#9a9a9a", accent="#f59e0b"),
    "Daylight": dict(mode="light", bg="#eef0f4", side="#ffffff", panel="#ffffff", panel2="#f1f3f8", border="#dde1e8", text="#171a21", muted="#6b7385", accent="#3b6cf6"),
}
OK, WARN, BAD = "#34d399", "#fbbf24", "#f43f5e"
T = {}
FONT_FAMILY, R, PAD = "Segoe UI", 14, 11
V = ctypes.c_void_p
gdi32 = ctypes.windll.gdi32
user32.SetWindowPos.argtypes = [V, V, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.GetWindowDC.restype = V
user32.GetWindowDC.argtypes = [V]
user32.ReleaseDC.argtypes = [V, V]
user32.PrintWindow.argtypes = [V, V, wintypes.UINT]
gdi32.CreateCompatibleDC.restype = V
gdi32.CreateCompatibleDC.argtypes = [V]
gdi32.CreateCompatibleBitmap.restype = V
gdi32.CreateCompatibleBitmap.argtypes = [V, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = V
gdi32.SelectObject.argtypes = [V, V]
gdi32.DeleteObject.argtypes = [V]
gdi32.DeleteDC.argtypes = [V]
gdi32.GetDIBits.argtypes = [V, V, wintypes.UINT, wintypes.UINT, V, V, wintypes.UINT]


import tkinter as tk

user32.SetParent.restype = V
user32.SetParent.argtypes = [V, V]
user32.GetParent.restype = V
user32.GetParent.argtypes = [V]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [V, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [V, ctypes.c_int, ctypes.c_ssize_t]
GWL_STYLE, WS_CHILD, WS_POPUP, WS_VISIBLE = -16, 0x40000000, 0x80000000, 0x10000000
WS_STRIP = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000  # caption, frame, sysmenu, min, max


def embed_window(hwnd, parent, w, h):
    """Re-parent a Roblox window into one of our frames (documented SetParent). As a child window
    it is no longer a top-level window, so top-level minimum-size rules do not apply to it."""
    user32.ShowWindow(hwnd, 9)
    old_style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & 0xFFFFFFFF
    old_rect = win_rect(hwnd)
    user32.SetParent(hwnd, parent)
    new = ((old_style & ~(WS_STRIP | WS_POPUP)) | WS_CHILD | WS_VISIBLE) & 0xFFFFFFFF
    user32.SetWindowLongPtrW(hwnd, GWL_STYLE, new)
    user32.SetWindowPos(hwnd, V(0), 0, 0, int(w), int(h), 0x0004 | 0x0020 | 0x0040)
    if (user32.GetParent(hwnd) or 0) != (parent or 0):
        release_window(hwnd, (old_style, old_rect))
        raise RuntimeError("Windows refused to move the Roblox window into the app")
    return old_style, old_rect


def release_window(hwnd, saved):
    if not user32.IsWindow(hwnd):
        return
    style, (x, y, w, h) = saved
    user32.SetParent(hwnd, V(0))
    user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style & 0xFFFFFFFF)
    user32.SetWindowPos(hwnd, V(0), int(x), int(y), int(w), int(h), 0x0004 | 0x0020 | 0x0040)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


user32.WindowFromPoint.restype = V
user32.WindowFromPoint.argtypes = [POINT]
user32.GetCursorPos.argtypes = [V]
user32.SetFocus.restype = V
user32.SetFocus.argtypes = [V]
user32.GetFocus.restype = V
user32.IsChild.argtypes = [V, V]
user32.GetAncestor.restype = V
user32.GetAncestor.argtypes = [V, wintypes.UINT]
user32.GetForegroundWindow.restype = V
user32.ClientToScreen.argtypes = [V, ctypes.POINTER(POINT)]
dwmapi = ctypes.windll.dwmapi
dwmapi.DwmRegisterThumbnail.argtypes = [V, V, ctypes.POINTER(ctypes.c_void_p)]
dwmapi.DwmUpdateThumbnailProperties.argtypes = [ctypes.c_void_p, V]
dwmapi.DwmUnregisterThumbnail.argtypes = [ctypes.c_void_p]
dwmapi.DwmQueryThumbnailSourceSize.argtypes = [ctypes.c_void_p, V]
dwmapi.DwmSetWindowAttribute.argtypes = [V, wintypes.DWORD, V, wintypes.DWORD]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
    _fields_ = [("dwFlags", wintypes.DWORD), ("rcDestination", wintypes.RECT), ("rcSource", wintypes.RECT),
                ("opacity", ctypes.c_ubyte), ("fVisible", wintypes.BOOL), ("fSourceClientAreaOnly", wintypes.BOOL)]


def style_window(win):
    """Windows 11 look: dark title bar tinted like the app, rounded corners (ignored on older Windows)."""
    try:
        win.update_idletasks()
        hwnd = user32.GetAncestor(win.winfo_id(), 2) or win.winfo_id()

        def put(attr, val):
            v = ctypes.c_int(val)
            dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4)

        put(20, 1 if T["mode"] == "dark" else 0)
        r, g, b = hexrgb(T["bg"])
        put(35, r | (g << 8) | (b << 16))
        r, g, b = hexrgb(T["text"])
        put(36, r | (g << 8) | (b << 16))
        put(33, 2)
    except Exception:
        pass


def hexrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def blend(a, b, t):
    ra, rb = hexrgb(a), hexrgb(b)
    return "#%02x%02x%02x" % tuple(int(ra[i] * t + rb[i] * (1 - t)) for i in range(3))


_NA = object()
_label_cfg = ctk.CTkLabel.configure
_bar_set = ctk.CTkProgressBar.set


def _lazy_label_cfg(self, *args, **kw):
    """Skip redraws when a label is set to what it already shows (most refresh work is exactly that)."""
    if args or "require_redraw" in kw:
        return _label_cfg(self, *args, **kw)
    cache = self.__dict__.setdefault("_cc", {})
    ch = {k: v for k, v in kw.items() if cache.get(k, _NA) != v}
    if not ch:
        return None
    cache.update(ch)
    return _label_cfg(self, **ch)


def _lazy_bar_set(self, value, *args, **kw):
    if abs(getattr(self, "_lv", -1.0) - value) < 0.004:
        return None
    self._lv = value
    return _bar_set(self, value, *args, **kw)


ctk.CTkLabel.configure = _lazy_label_cfg
ctk.CTkProgressBar.set = _lazy_bar_set


_FONTS = {}


def _has_font(prefix):
    if prefix not in _FONTS:
        ok = False
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as k:
                i = 0
                while not ok:
                    ok = winreg.EnumValue(k, i)[0].startswith(prefix)
                    i += 1
        except OSError:
            pass
        _FONTS[prefix] = ok
    return _FONTS[prefix]


def apply_theme(db):
    global FONT_FAMILY, R, PAD
    T.clear()
    T.update(THEMES.get(db.get("theme"), THEMES["Midnight"]))
    acc = db.get("accent")
    if re.fullmatch(r"#[0-9a-fA-F]{6}", acc or ""):
        T["accent"] = acc
    R = db.num("radius", 0, 24)
    PAD = 6 if db.get("density") == "Compact" else 11
    fam = db.get("font") or "Segoe UI"
    if fam == "Segoe UI" and _has_font("Segoe UI Variable"):
        fam = "Segoe UI Variable Text"  # the Windows 11 / 2026 system font
    FONT_FAMILY = fam
    ctk.set_appearance_mode(T["mode"])


def F(size, bold=False):
    return ctk.CTkFont(family=FONT_FAMILY, size=int(size), weight="bold" if bold else "normal")


def L(parent, text="", size=13, bold=False, muted=False, color=None, **kw):
    return ctk.CTkLabel(parent, text=text, font=F(size, bold), text_color=color or (T["muted"] if muted else T["text"]), **kw)


def card(parent, **kw):
    return ctk.CTkFrame(parent, fg_color=T["panel"], border_color=T["border"], border_width=1, corner_radius=R, **kw)


def btn(parent, text, cmd, kind="ghost", w=None, h=32):
    acc = T["accent"]
    st = {"accent": dict(fg_color=acc, hover_color=blend("#ffffff", acc, .18), text_color="#ffffff"),
          "ghost": dict(fg_color=T["panel2"], hover_color=blend(T["text"], T["panel2"], .10), text_color=T["text"]),
          "flat": dict(fg_color="transparent", hover_color=T["panel2"], text_color=T["text"]),
          "danger": dict(fg_color=blend(BAD, T["panel"], .16), hover_color=blend(BAD, T["panel"], .35), text_color=BAD)}[kind]
    kw = dict(text=text, command=cmd, height=h, corner_radius=max(R - 4, 4), font=F(12, True), **st)
    if w:
        kw["width"] = w
    return ctk.CTkButton(parent, **kw)


def pill(parent, text, color, bg=None):
    return ctk.CTkLabel(parent, text=f" {text} ", font=F(11, True), text_color=color,
                        fg_color=blend(color, bg or T["panel"], .16), corner_radius=10, height=22)


def E(parent, **kw):
    o = dict(fg_color=T["panel2"], border_color=T["border"], text_color=T["text"],
             placeholder_text_color=T["muted"], corner_radius=max(R - 4, 4), height=34, font=F(13))
    o.update(kw)
    return ctk.CTkEntry(parent, **o)


def SW(parent, text, **kw):
    return ctk.CTkSwitch(parent, text=text, text_color=T["text"], progress_color=T["accent"], fg_color=T["border"],
                         button_color="#ffffff", button_hover_color="#e5e7eb", font=F(13), **kw)


def SL(parent, **kw):
    return ctk.CTkSlider(parent, progress_color=T["accent"], button_color=T["accent"],
                         button_hover_color=blend("#ffffff", T["accent"], .2), fg_color=T["border"], **kw)


def OM(parent, values, **kw):
    o = dict(fg_color=T["panel2"], button_color=T["border"], button_hover_color=blend(T["text"], T["border"], .15),
             text_color=T["text"], dropdown_fg_color=T["panel2"], dropdown_text_color=T["text"],
             dropdown_hover_color=T["border"], corner_radius=max(R - 4, 4), height=34, font=F(13))
    o.update(kw)
    return ctk.CTkOptionMenu(parent, values=values, **o)


def SEG(parent, values, **kw):
    return ctk.CTkSegmentedButton(parent, values=values, fg_color=T["panel2"], selected_color=T["accent"],
                                  selected_hover_color=T["accent"], unselected_color=T["panel2"],
                                  unselected_hover_color=T["border"], text_color=T["text"],
                                  corner_radius=max(R - 4, 4), font=F(12, True), **kw)


def TB(parent, **kw):
    return ctk.CTkTextbox(parent, fg_color=T["panel2"], text_color=T["text"], border_color=T["border"],
                          border_width=1, corner_radius=max(R - 4, 4), font=F(12), **kw)


def slider_row(parent, text, frm, to, steps, value, fmt, save=None):
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=4)
    top = ctk.CTkFrame(row, fg_color="transparent")
    top.pack(fill="x")
    L(top, text, 12).pack(side="left")
    val = L(top, fmt(value), 12, True, color=T["accent"])
    val.pack(side="right")
    s = SL(row, from_=frm, to=to, number_of_steps=steps)
    s.set(value)
    s.pack(fill="x", pady=(2, 0))
    s.configure(command=lambda v: (val.configure(text=fmt(v)), save and save(v)))
    return s


def dialog(app, title, w, h):
    top = ctk.CTkToplevel(app)
    top.title(title)
    top.geometry(f"{w}x{h}")
    top.configure(fg_color=T["bg"])
    top.transient(app)

    def grab():
        try:
            top.grab_set()
            top.focus_set()
        except Exception:
            pass

    top.after(250, grab)
    top.after(80, lambda: style_window(top))
    return top


def ask_secret(parent, title, prompt):
    res = {"v": None}
    top = dialog(parent, title, 360, 190)
    L(top, prompt, 13).pack(pady=(24, 8))
    e = E(top, show="•", width=260)
    e.pack()

    def ok(_=None):
        res["v"] = e.get()
        top.destroy()

    e.bind("<Return>", ok)
    btn(top, "OK", ok, "accent", w=100).pack(pady=16)
    top.after(300, e.focus_set)
    parent.wait_window(top)
    return res["v"]


class BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def capture(hwnd, size=(300, 190)):
    """Best-effort window snapshot via PrintWindow(PW_RENDERFULLCONTENT). DirectX windows may come back black."""
    if Image is None or user32.IsIconic(hwnd):
        return None
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w < 8 or h < 8 or w > 8000 or h > 8000:
        return None
    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        return None
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        if not user32.PrintWindow(hwnd, mdc, 2):
            return None
        bi = BMIH()
        bi.biSize = ctypes.sizeof(BMIH)
        bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = w, -h, 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0):
            return None
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
    img.thumbnail(size)
    if img.getextrema() == ((0, 0), (0, 0), (0, 0)):
        return None
    return img


class Spark(ctk.CTkCanvas):
    def __init__(self, parent, w=150, h=28, color=None):
        super().__init__(parent, width=w, height=h, bg=T["panel"], highlightthickness=0)
        self.w, self.h, self.color = w, h, color or T["accent"]

    def draw(self, vals, ymax=None):
        if vals == getattr(self, "_last", None):
            return
        self._last = list(vals)
        self.delete("all")
        if len(vals) < 2:
            return
        m = ymax or max(max(vals), 1e-9)
        n, pts = len(vals), []
        for i, v in enumerate(vals):
            pts += [i * (self.w - 1) / (n - 1), self.h - 2 - (self.h - 4) * min(v / m, 1)]
        self.create_polygon([0, self.h] + pts + [self.w - 1, self.h], fill=blend(self.color, T["panel"], .18), outline="")
        self.create_line(*pts, fill=self.color, width=2)


def draw_area(cv, series, ymax, title):
    cv.delete("all")
    w, h = cv.winfo_width(), cv.winfo_height()
    if w < 40:
        return
    top, bot = 30, h - 10
    cv.create_text(12, 9, anchor="nw", fill=T["muted"], font=(FONT_FAMILY, 9), text=title)
    cv.create_text(w - 10, 9, anchor="ne", fill=T["muted"], font=(FONT_FAMILY, 9), text=f"max {ymax:,.0f}")
    for f in (0.0, 0.33, 0.66, 1.0):
        y = top + (bot - top) * f
        cv.create_line(0, y, w, y, fill=T["border"])
    for pts, color in series:
        pts = pts[::max(1, len(pts) // 320)]
        if len(pts) < 2:
            continue
        co = []
        for i, v in enumerate(pts):
            co += [i * (w - 1) / (len(pts) - 1), bot - (bot - top) * min(v, ymax) / ymax]
        cv.create_polygon([0, bot] + co + [w - 1, bot], fill=blend(color, T["panel"], .15), outline="")
        cv.create_line(*co, fill=color, width=2)


def circle(img, size=48):
    img = img.convert("RGBA").resize((size * 2, size * 2))
    if ImageDraw:
        m = Image.new("L", (size * 2, size * 2), 0)
        ImageDraw.Draw(m).ellipse((0, 0, size * 2 - 1, size * 2 - 1), fill=255)
        img.putalpha(m)
    return img


def make_avatar(parent, app, aid, name, size=40, color=None):
    img = app.avatar(aid) if app.core.db.flag("show_avatars") else None
    if img:
        return ctk.CTkLabel(parent, text="", image=img, width=size, height=size)
    col = color or T["accent"]
    return ctk.CTkLabel(parent, text=(name[:1] or "?").upper(), width=size, height=size, corner_radius=size // 2,
                        fg_color=blend(col, T["panel"], .2), text_color=col, font=F(size * 0.42, True))


class Page(ctk.CTkFrame):
    def __init__(self, parent, app, title, sub):
        super().__init__(parent, fg_color="transparent")
        self.app, self.core = app, app.core
        self.grid(row=0, column=0, sticky="nsew")
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", pady=(0, PAD + 4))
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left")
        L(left, title, 26, True).pack(anchor="w")
        L(left, sub, 13, muted=True).pack(anchor="w")
        ctk.CTkFrame(left, height=3, width=38, fg_color=T["accent"], corner_radius=2).pack(anchor="w", pady=(7, 0))
        self.actions = ctk.CTkFrame(head, fg_color="transparent")
        self.actions.pack(side="right")

    def refresh(self):
        pass


def parse_wh(s):
    m = re.fullmatch(r"\s*(\d{1,5})\s*[xX×,]\s*(\d{1,5})\s*", s or "")
    return (int(m.group(1)), int(m.group(2))) if m and int(m.group(1)) > 0 and int(m.group(2)) > 0 else None


# ------------------------------------------------------------------ Home ---
class HomePage(Page):
    def __init__(self, parent, app):
        super().__init__(parent, app, "Dashboard", "Every running Roblox client at a glance")
        self.cards, self.order = {}, None
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x")
        row.grid_columnconfigure(tuple(range(5)), weight=1, uniform="s")
        self.stat = {}
        for i, (k, t, ic) in enumerate([("clients", "Clients", "▣"), ("ram", "Roblox RAM", "▤"), ("cpu", "Roblox CPU", "◔"),
                                        ("running", "Accounts running", "☺"), ("failed", "Failed", "⚠")]):
            f = card(row)
            f.grid(row=0, column=i, sticky="nsew", padx=4)
            top = ctk.CTkFrame(f, fg_color="transparent")
            top.pack(fill="x", padx=14, pady=(12, 0))
            ctk.CTkLabel(top, text=ic, width=30, height=30, corner_radius=9, fg_color=blend(T["accent"], T["panel"], .16),
                         text_color=T["accent"], font=F(14, True)).pack(side="left")
            L(top, "  " + t, 12, muted=True).pack(side="left")
            val = L(f, "-", 26, True)
            val.pack(anchor="w", padx=14)
            sub = L(f, "", 11, muted=True)
            sub.pack(anchor="w", padx=14)
            sp = Spark(f, 150, 28) if k in ("clients", "ram", "cpu") else None
            if sp:
                sp.pack(anchor="w", padx=14, pady=(2, 12))
            else:
                ctk.CTkFrame(f, height=14, fg_color="transparent").pack()
            self.stat[k] = (val, sub, sp)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", pady=(PAD + 2, PAD))

        def group(items):
            g = ctk.CTkFrame(bar, fg_color=T["panel"], border_color=T["border"], border_width=1, corner_radius=R)
            g.pack(side="left", padx=(0, 8))
            for t, f, k in items:
                btn(g, t, f, k, h=30).pack(side="left", padx=3, pady=4)

        btn(bar, "▶  Launch", lambda: app.show("Play"), "accent", w=110, h=40).pack(side="left", padx=(0, 8))
        group([("▦ Grid", lambda: self.core.arrange("grid"), "flat"), ("▥ Row", lambda: self.core.arrange("h"), "flat"),
               ("▤ Column", lambda: self.core.arrange("v"), "flat")])
        group([("Minimize all", lambda: self.core.window_all("min"), "flat"), ("Restore all", lambda: self.core.window_all("restore"), "flat")])
        group([("Save layout", self.save_layout, "flat"), ("Load layout", self.restore_layout, "flat")])
        group([("Close all", self.close_all, "danger")])
        btn(bar, "⟳", lambda: app.bg(self.core.sample, lambda r, e: self.refresh()), "ghost", w=40, h=40).pack(side="right")
        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list.pack(fill="both", expand=True)
        self.empty = card(self.list)
        L(self.empty, "▣", 38, color=T["accent"]).pack(pady=(30, 0))
        L(self.empty, "No Roblox clients running", 18, True).pack()
        L(self.empty, "Select accounts, pick a destination on the Play tab, and launch.", 13, muted=True).pack(pady=(2, 12))
        btn(self.empty, "Go to Play", lambda: app.show("Play"), "accent", w=120).pack(pady=(0, 30))

    def close_all(self):
        if self.core.snapshot() and messagebox.askyesno(APP_NAME, "Close all Roblox clients?"):
            self.core.close_all()

    def save_layout(self):
        n = simpledialog.askstring(APP_NAME, "Layout name:", parent=self.app)
        if n and n.strip():
            self.core.save_layout(n.strip())
            self.app.toast(f"Layout '{n.strip()}' saved", "ok")

    def restore_layout(self):
        names = [r[0] for r in self.core.db.all("SELECT name FROM layouts ORDER BY name")]
        if not names:
            messagebox.showinfo(APP_NAME, "No saved layouts yet.")
            return
        n = simpledialog.askstring(APP_NAME, "Layout to load:\n" + ", ".join(names), parent=self.app)
        if n:
            try:
                self.core.restore_layout(n.strip())
            except ValueError as e:
                messagebox.showerror(APP_NAME, str(e))

    def resize(self, pid):
        s = simpledialog.askstring(APP_NAME, "New size, WIDTHxHEIGHT (any size, e.g. 200x120):", parent=self.app)
        wh = parse_wh(s)
        if wh:
            self.core.resize(pid, *wh)
        elif s:
            messagebox.showerror(APP_NAME, "Use the format WIDTHxHEIGHT, for example 320x180.")

    def make_card(self, c):
        row = self.core.db.one("SELECT color FROM accounts WHERE id=?", (c["account_id"],))
        col = (row[0] if row and row[0] else None) or T["accent"]
        f = card(self.list)
        ctk.CTkFrame(f, width=4, fg_color=col, corner_radius=0).place(x=0, y=14, relheight=0.84)
        body = ctk.CTkFrame(f, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=(18, 14), pady=PAD)
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x")
        av = make_avatar(head, self.app, c["account_id"], c["name"], 42, col)
        av.pack(side="left")
        mid = ctk.CTkFrame(head, fg_color="transparent")
        mid.pack(side="left", padx=10)
        L(mid, c["name"], 15, True).pack(anchor="w")
        game = L(mid, "", 12, muted=True)
        game.pack(anchor="w")
        pl = pill(head, "Running", OK)
        pl.pack(side="right")
        bars = {}
        if self.core.db.flag("show_bars"):
            for k, name in (("cpu", "CPU"), ("ram", "RAM"), ("gpu", "GPU")):
                r = ctk.CTkFrame(body, fg_color="transparent")
                r.pack(fill="x", pady=(8 if k == "cpu" else 2, 0))
                L(r, name, 11, True, muted=True, width=34, anchor="w").pack(side="left")
                pb = ctk.CTkProgressBar(r, height=7, progress_color=T["accent"], fg_color=T["panel2"])
                pb.set(0)
                pb.pack(side="left", fill="x", expand=True, padx=8)
                vl = L(r, "", 11, width=90, anchor="e")
                vl.pack(side="left")
                bars[k] = (pb, vl)
        meta = L(body, "", 11, muted=True, anchor="w")
        meta.pack(fill="x", pady=(8, 6))
        row2 = ctk.CTkFrame(body, fg_color="transparent")
        row2.pack(fill="x")
        pid = c["pid"]
        for t, cmd, k in [("Focus", lambda: self.core.window_cmd(pid, "focus"), "ghost"),
                          ("Min", lambda: self.core.window_cmd(pid, "min"), "ghost"),
                          ("Max", lambda: self.core.window_cmd(pid, "max"), "ghost"),
                          ("Restore", lambda: self.core.window_cmd(pid, "restore"), "ghost"),
                          ("Size", lambda: self.resize(pid), "ghost"),
                          ("Restart", lambda: self.core.restart_client(pid), "ghost"),
                          ("Close", lambda: self.core.close_client(pid), "danger")]:
            btn(row2, t, cmd, k, h=28).pack(side="left", padx=(0, 4))
        return {"frame": f, "game": game, "pill": pl, "bars": bars, "meta": meta, "av": av, "avimg": None}

    def regrid(self, order):
        cols = 2 if self.core.db.get("cols") == "2" else 1
        for i in range(2):
            self.list.grid_columnconfigure(i, weight=1 if i < cols else 0)
        for i, pid in enumerate(order):
            self.cards[pid]["frame"].grid(row=i // cols, column=i % cols, sticky="ew", padx=5, pady=5)

    def refresh(self):
        cl = sorted(self.core.snapshot(), key=lambda c: c["start"])
        alive = {c["pid"] for c in cl}
        for pid in list(self.cards):
            if pid not in alive:
                self.cards.pop(pid)["frame"].destroy()
        for c in cl:
            if c["pid"] not in self.cards:
                self.cards[c["pid"]] = self.make_card(c)
        order = [c["pid"] for c in cl]
        if order != self.order:
            self.order = order
            self.regrid(order)
        if cl:
            self.empty.grid_remove()
        else:
            self.empty.grid(row=0, column=0, columnspan=2, sticky="ew", padx=5, pady=20)
        for c in cl:
            k = self.cards[c["pid"]]
            state = win_state(c["hwnd"])
            if c["hung"] >= 1 or state == "Not responding":
                txt, col = "Not responding", BAD
            elif state == "Minimized":
                txt, col = "Minimized", WARN
            elif state == "No window":
                txt, col = "Starting", T["muted"]
            else:
                txt, col = "Running", OK
            k["pill"].configure(text=f" {txt} ", text_color=col, fg_color=blend(col, T["panel"], .16))
            k["game"].configure(text=f"Place {dest_label(c['dest'])}")
            capn = f"   ·   CPU cap {int(c['cap_now'])}%" if c.get("cap_now") else ""
            k["meta"].configure(text=f"PID {c['pid']}   ·   up {fmt_dur(time.time() - c['start'])}   ·   window: {state.lower()}{capn}")
            if "cpu" in k["bars"]:
                k["bars"]["cpu"][0].set(min(1, c["cpu"] * 4 / 100))
                k["bars"]["cpu"][1].configure(text=f"{c['cpu']:.1f}%")
                k["bars"]["ram"][0].set(min(1, c["rss"] / ((self.core.profile()["ramcap"] << 20) or 3e9)))
                k["bars"]["ram"][1].configure(text=fmt_mb(c["rss"]))
                g = c.get("gpu")
                k["bars"]["gpu"][0].set(min(1, (g or 0) * 4 / 100))
                k["bars"]["gpu"][1].configure(text="n/a" if g is None else f"{g:.1f}%")
            img = self.app.avatar(c["account_id"]) if self.core.db.flag("show_avatars") else None
            if img and k["avimg"] is not img:
                k["avimg"] = img
                k["av"].configure(image=img, text="", fg_color="transparent")
        h = list(self.core.hist)[-90:]
        tot_ram = sum(c["rss"] for c in cl)
        vals = {"clients": (str(len(cl)), "", [x["count"] for x in h]),
                "ram": (fmt_mb(tot_ram), "", [x["rb_ram"] for x in h]),
                "cpu": (f"{sum(c['cpu'] for c in cl):.1f}%", "", [x["rb_cpu"] for x in h]),
                "running": (str(len({c['account_id'] for c in cl})), "", None),
                "failed": (str(self.core.failed), "", None)}
        for key, (v, sub, data) in vals.items():
            self.stat[key][0].configure(text=v)
            if self.stat[key][2] is not None and data:
                self.stat[key][2].draw(data)


# -------------------------------------------------------------- Accounts ---
class AccountsPage(Page):
    def __init__(self, parent, app):
        super().__init__(parent, app, "Accounts", "Log in, organise, and select the accounts you want to run")
        self.vars, self.frames = {}, {}
        btn(self.actions, "＋  Log in to add", self.add_login, "accent", w=140, h=38).pack(side="left", padx=4)
        btn(self.actions, "Import cookie", self.add_cookie, "ghost", w=120, h=38).pack(side="left", padx=4)
        btn(self.actions, "Bulk import", self.add_bulk, "ghost", w=110, h=38).pack(side="left", padx=4)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x")
        self.search = E(bar, width=240, placeholder_text="🔍  Search accounts")
        self.search.pack(side="left", padx=(0, 6))
        self.search.bind("<KeyRelease>", lambda e: self.rebuild())
        self.sort = OM(bar, ["Name", "Last used", "Group", "Robux", "Main first"], width=120, command=lambda v: self.rebuild())
        self.sort.pack(side="left", padx=3)
        self.gfilter = OM(bar, ["All groups"], width=140, command=lambda v: self.rebuild())
        self.gfilter.pack(side="left", padx=3)
        for t, f, k in [("Remove", self.remove, "danger"), ("Re-login", self.relogin, "ghost"), ("Check status", self.check, "ghost"), ("Clear", self.clear_sel, "ghost"),
                        ("Select all", self.select_all, "ghost"), ("New group", self.new_group, "ghost"),
                        ("Set MAIN", self.set_main, "ghost"), ("Edit", self.edit, "ghost")]:
            btn(bar, t, f, k, h=34).pack(side="right", padx=3)
        self.count = L(self, "", 12, muted=True)
        self.count.pack(anchor="w", pady=(PAD, 4))
        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list.pack(fill="both", expand=True)
        self.rebuild()

    def groups(self):
        return [r[0] for r in self.core.db.all("SELECT name FROM groups ORDER BY name")]

    def rebuild(self):
        for w in self.list.winfo_children():
            w.destroy()
        self.gfilter.configure(values=["All groups", "Ungrouped"] + self.groups())
        rows = self.core.db.all("SELECT a.*, g.name AS gname FROM accounts a LEFT JOIN groups g ON g.id=a.group_id")
        total = len(rows)
        s, g = self.search.get().strip().lower(), self.gfilter.get()
        rows = [r for r in rows if (not s or s in (r["alias"] or r["username"]).lower() or s in (r["label"] or "").lower())
                and (g == "All groups" or (r["gname"] or "Ungrouped") == g)]
        keyf = {"Name": lambda r: (r["alias"] or r["username"]).lower(), "Last used": lambda r: -(r["last_used"] or 0),
                "Group": lambda r: (r["gname"] or "~").lower(), "Robux": lambda r: -(r["robux"] or 0),
                "Main first": lambda r: (-r["is_main"], (r["alias"] or r["username"]).lower())}[self.sort.get()]
        rows.sort(key=keyf)
        self.vars, self.frames = {}, {}
        if not rows:
            e = card(self.list)
            e.pack(fill="x", pady=20)
            L(e, "☺", 36, color=T["accent"]).pack(pady=(24, 0))
            L(e, "No accounts yet" if not total else "No accounts match", 17, True).pack()
            L(e, "Use “Log in to add” to sign in like in a browser.", 13, muted=True).pack(pady=(2, 24))
        for r in rows:
            aid = r["id"]
            sel = aid in self.app.selected
            f = card(self.list)
            f.pack(fill="x", pady=3, padx=2)
            if sel:
                f.configure(border_color=T["accent"])
            v = ctk.BooleanVar(value=sel)
            self.vars[aid], self.frames[aid] = v, f
            ctk.CTkCheckBox(f, text="", variable=v, width=22, checkbox_width=20, checkbox_height=20, corner_radius=6,
                            fg_color=T["accent"], hover_color=T["accent"], border_color=T["border"],
                            command=lambda i=aid: self.toggle(i)).pack(side="left", padx=(14, 6), pady=PAD)
            name = r["alias"] or r["username"]
            av = make_avatar(f, self.app, aid, name, 40, r["color"])
            av.pack(side="left", padx=6)
            mid = ctk.CTkFrame(f, fg_color="transparent")
            mid.pack(side="left", padx=10)
            L(mid, name, 14, True, color=r["color"] or T["text"]).pack(anchor="w")
            L(mid, f"{r['label'] or 'No label'}  ·  {r['gname'] or 'Ungrouped'}", 12, muted=True).pack(anchor="w")
            right = ctk.CTkFrame(f, fg_color="transparent")
            right.pack(side="right", padx=14)
            lu = datetime.fromtimestamp(r["last_used"]).strftime("%b %d, %H:%M") if r["last_used"] else "never used"
            L(right, f"last used {lu}", 11, muted=True).pack(side="right", padx=(12, 0))
            L(right, f"R$ {r['robux']:,}" if r["robux"] is not None else "R$ n/a", 12, True).pack(side="right", padx=8)
            st = r["status"] or "Unknown"
            pill(right, st, OK if st == "Valid" else BAD if st == "Expired" else T["muted"]).pack(side="right", padx=4)
            if r["is_main"]:
                pill(right, "★ MAIN", WARN).pack(side="right", padx=4)
            for w in (f, av, mid) + tuple(mid.winfo_children()):
                w.bind("<Button-1>", lambda e, i=aid: self.click(i))
        self.update_count()

    def update_count(self):
        n = self.core.db.one("SELECT COUNT(*) FROM accounts")[0]
        self.count.configure(text=f"{n} account(s)   ·   {len(self.app.selected)} selected")

    def click(self, i):
        self.vars[i].set(not self.vars[i].get())
        self.toggle(i)

    def toggle(self, i):
        on = self.vars[i].get()
        (self.app.selected.add if on else self.app.selected.discard)(i)
        self.frames[i].configure(border_color=T["accent"] if on else T["border"])
        self.update_count()

    def select_all(self):
        for i, v in self.vars.items():
            v.set(True)
            self.toggle(i)

    def clear_sel(self):
        for i, v in self.vars.items():
            v.set(False)
            self.toggle(i)

    def sel_ids(self):
        return [i for i in self.vars if self.vars[i].get()]

    def new_group(self):
        n = simpledialog.askstring(APP_NAME, "Group name:", parent=self.app)
        if n and n.strip():
            self.core.db.q("INSERT OR IGNORE INTO groups(name) VALUES(?)", (n.strip(),))
            self.rebuild()

    def set_main(self):
        ids = self.sel_ids()
        if len(ids) != 1:
            messagebox.showinfo(APP_NAME, "Select exactly one account.")
            return
        self.core.db.q("UPDATE accounts SET is_main=0")
        self.core.db.q("UPDATE accounts SET is_main=1 WHERE id=?", (ids[0],))
        self.rebuild()

    def store(self, cookie, me, rb):
        enc = dpapi_protect(cookie.encode())
        row = self.core.db.one("SELECT id FROM accounts WHERE user_id=?", (me["id"],))
        if row:
            self.core.db.q("UPDATE accounts SET cookie_enc=?, status='Valid', robux=?, username=? WHERE id=?",
                           (enc, rb, me["name"], row[0]))
            return "updated"
        self.core.db.q("INSERT INTO accounts(user_id,username,cookie_enc,status,robux,created) VALUES(?,?,?,?,?,?)",
                       (me["id"], me["name"], enc, "Valid", rb, time.time()))
        return "added"

    # --- add: username + password (typed into the real Roblox login page) / browser login
    def add_login(self):
        top = dialog(self.app, "Log in to add", 480, 500)
        L(top, "Log in to Roblox", 20, True).pack(anchor="w", padx=24, pady=(22, 2))
        L(top, "Works like Chrome: a real roblox.com login window opens (captcha and 2FA included).\n"
               "Your password is typed into that page once and is never saved by RoManager.",
          12, muted=True, justify="left").pack(anchor="w", padx=24)
        u = E(top, width=420, placeholder_text="Roblox username")
        u.pack(padx=24, pady=(16, 6))
        p = E(top, width=420, placeholder_text="Password", show="•")
        p.pack(padx=24, pady=6)
        keep = ctk.CTkCheckBox(top, text="Remember the password (DPAPI-encrypted) for one-click re-login",
                               text_color=T["text"], fg_color=T["accent"], hover_color=T["accent"],
                               border_color=T["border"], font=F(12))
        keep.pack(anchor="w", padx=24, pady=4)
        status = L(top, "", 12, color=BAD)
        status.pack(padx=24)

        def go_creds():
            if not u.get().strip() or not p.get():
                status.configure(text="Enter the username and password, or use the other button.")
                return
            user, pw, remember = u.get().strip(), p.get(), bool(keep.get())
            p.delete(0, "end")
            top.destroy()
            self.app.toast("Login window opening… finish any captcha / 2FA there", "info")
            self.app.bg(lambda: browser_login(user, pw), lambda r, e: self._after_login(r, e, pw if remember else None))

        def go_browser():
            top.destroy()
            self.app.toast("Login window opening…", "info")
            self.app.bg(browser_login, self._after_login)

        btn(top, "Log in with username and password", go_creds, "accent", w=420, h=40).pack(padx=24, pady=(12, 6))
        btn(top, "Open the Roblox login page and type it there", go_browser, "ghost", w=420, h=36).pack(padx=24)
        L(top, "Or use “Import cookie” / “Bulk import” for .ROBLOSECURITY cookies.", 11, muted=True).pack(pady=12)

    def _after_login(self, cookie, err, pw=None):
        if err:
            messagebox.showerror(APP_NAME, str(err))
            return

        def work():
            me = whoami(cookie)
            return me, get_robux(cookie, me["id"])

        def done(res, e):
            if e:
                messagebox.showerror(APP_NAME, f"Login worked but verification failed: {e}")
                return
            me, rb = res
            r = self.store(cookie, me, rb)
            if pw:
                self.core.db.q("UPDATE accounts SET pw_enc=? WHERE user_id=?", (dpapi_protect(pw.encode()), me["id"]))
            self.rebuild()
            self.app.toast(f"{r.capitalize()} {me['name']}", "ok")

        self.app.bg(work, done)

    def relogin(self):
        ids = self.sel_ids()
        if not ids:
            messagebox.showinfo(APP_NAME, "Select the accounts to sign in again (their stored sessions stay until replaced).")
            return

        def work():
            out = []
            for i in ids:
                a = self.core.db.one("SELECT user_id, username, pw_enc FROM accounts WHERE id=?", (i,))
                if not a:
                    continue
                try:
                    pw = dpapi_unprotect(a["pw_enc"]).decode() if a["pw_enc"] else None
                    cookie = browser_login(a["username"], pw) if pw else browser_login()
                    me = whoami(cookie)
                    if a["user_id"] and me["id"] != a["user_id"]:
                        out.append(f"{a['username']}: you logged into a different account, skipped")
                        continue
                    self.store(cookie, me, get_robux(cookie, me["id"]))
                    out.append(f"{a['username']}: signed in again")
                except Exception as e:
                    out.append(f"{a['username']}: {str(e).splitlines()[0]}")
            return out

        def done(res, err):
            self.rebuild()
            self.app.toast("\n".join(res) if res else str(err), "ok" if res else "bad")

        self.app.bg(work, done)

    # --- add: single cookie
    def add_cookie(self):
        top = dialog(self.app, "Import cookie", 600, 360)
        L(top, "Import one .ROBLOSECURITY cookie", 18, True).pack(anchor="w", padx=22, pady=(20, 4))
        L(top, "That cookie is a full login for the account. RoManager encrypts it with Windows DPAPI\n"
               "(only this Windows user on this PC can decrypt it) and never shows, logs or exports it.\n"
               "Only import accounts you own, and never share the cookie with anyone.", 12, muted=True,
          justify="left").pack(anchor="w", padx=22)
        e = E(top, show="•", width=550, placeholder_text="Paste .ROBLOSECURITY value")
        e.pack(pady=14)
        status = L(top, "", 12)
        status.pack()

        def go():
            raw = e.get().strip().strip('"')
            if len(raw) < 100:
                status.configure(text="That does not look like a .ROBLOSECURITY cookie.", text_color=BAD)
                return
            status.configure(text="Verifying with Roblox…", text_color=T["muted"])

            def work():
                me = whoami(raw)
                return me, get_robux(raw, me["id"])

            def done(res, err):
                if err:
                    status.configure(text=str(err), text_color=BAD)
                    return
                r = self.store(raw, res[0], res[1])
                top.destroy()
                self.rebuild()
                self.app.toast(f"{r.capitalize()} {res[0]['name']}", "ok")

            self.app.bg(work, done)

        btn(top, "Verify and add", go, "accent", w=160, h=38).pack(pady=10)

    # --- add: many cookies at once
    def add_bulk(self):
        top = dialog(self.app, "Bulk import", 720, 560)
        L(top, "Import many accounts at once", 18, True).pack(anchor="w", padx=22, pady=(20, 4))
        L(top, "Paste one .ROBLOSECURITY cookie per line. Lines in the form  user:password:cookie  also work\n"
               "(only the cookie is used; the rest is ignored). Cookies are verified with Roblox, then stored\n"
               "DPAPI-encrypted. Existing accounts get their session refreshed.", 12, muted=True,
          justify="left").pack(anchor="w", padx=22)
        tb = TB(top, height=250)
        tb.pack(fill="both", expand=True, padx=22, pady=12)
        status = L(top, "", 12, justify="left")
        status.pack(anchor="w", padx=22)

        def go():
            seen, cookies = set(), []
            for ln in tb.get("1.0", "end").splitlines():
                ln = ln.strip()
                m = re.search(r"_\|WARNING:[A-Za-z0-9_|.\-+/=]+", ln)
                c = m.group(0) if m else (ln if len(ln) > 100 and " " not in ln and ":" not in ln else "")
                if c and c not in seen:
                    seen.add(c)
                    cookies.append(c)
            tb.delete("1.0", "end")
            if not cookies:
                status.configure(text="No cookies found in the text.", text_color=BAD)
                return
            go_btn.configure(state="disabled")

            def run():
                added = updated = 0
                failed = []
                for i, ck_ in enumerate(cookies, 1):
                    self.app.ui(lambda i=i: status.configure(text=f"Checking {i}/{len(cookies)}…", text_color=T["muted"]))
                    try:
                        me = whoami(ck_)
                        r = self.store(ck_, me, get_robux(ck_, me["id"]))
                        added += r == "added"
                        updated += r == "updated"
                    except Exception as e:
                        failed.append(f"#{i}: {e}")
                    time.sleep(0.4)
                return added, updated, failed

            def done(res, err):
                go_btn.configure(state="normal")
                if err:
                    status.configure(text=str(err), text_color=BAD)
                    return
                a, u_, f_ = res
                msg = f"Added {a}, refreshed {u_}, failed {len(f_)}."
                if f_:
                    msg += "\n" + "\n".join(f_[:4])
                status.configure(text=msg, text_color=OK if not f_ else WARN)
                self.rebuild()

            self.app.bg(run, done)

        go_btn = btn(top, "Verify and import all", go, "accent", w=190, h=38)
        go_btn.pack(pady=12)

    def edit(self):
        ids = self.sel_ids()
        if len(ids) != 1:
            messagebox.showinfo(APP_NAME, "Select exactly one account.")
            return
        r = self.core.db.one("SELECT a.*, g.name AS gname FROM accounts a LEFT JOIN groups g ON g.id=a.group_id WHERE a.id=?", (ids[0],))
        top = dialog(self.app, "Edit account", 400, 430)
        L(top, "Edit account", 18, True).pack(anchor="w", padx=24, pady=(20, 8))
        alias = E(top, width=350, placeholder_text="Display name")
        alias.pack(pady=5)
        alias.insert(0, r["alias"] or "")
        label = E(top, width=350, placeholder_text="Label")
        label.pack(pady=5)
        label.insert(0, r["label"] or "")
        grp = OM(top, ["Ungrouped"] + self.groups(), width=350)
        grp.set(r["gname"] or "Ungrouped")
        grp.pack(pady=5)
        color = {"v": r["color"]}

        def pick():
            c = colorchooser.askcolor(parent=top)[1]
            if c:
                color["v"] = c

        btn(top, "Choose color", pick, "ghost", w=350).pack(pady=5)
        btn(top, "Clear color", lambda: color.update(v=None), "ghost", w=350).pack(pady=5)

        def save():
            gid = None
            if grp.get() != "Ungrouped":
                gid = self.core.db.one("SELECT id FROM groups WHERE name=?", (grp.get(),))[0]
            self.core.db.q("UPDATE accounts SET alias=?, label=?, color=?, group_id=? WHERE id=?",
                           (alias.get().strip() or None, label.get().strip() or None, color["v"], gid, ids[0]))
            top.destroy()
            self.rebuild()

        btn(top, "Save", save, "accent", w=160, h=38).pack(pady=14)

    def remove(self):
        ids = self.sel_ids()
        if not ids or not messagebox.askyesno(APP_NAME, f"Securely remove {len(ids)} account(s)? The stored session is overwritten and deleted."):
            return
        for i in ids:
            self.core.db.q("UPDATE accounts SET cookie_enc=? WHERE id=?", (os.urandom(256), i))
            self.core.db.q("DELETE FROM accounts WHERE id=?", (i,))
            self.app.selected.discard(i)
        self.rebuild()

    def check(self):
        ids = self.sel_ids() or [r[0] for r in self.core.db.all("SELECT id FROM accounts")]

        def work():
            for i in ids:
                a = self.core.db.one("SELECT cookie_enc FROM accounts WHERE id=?", (i,))
                try:
                    cookie = dpapi_unprotect(a["cookie_enc"]).decode()
                    me = whoami(cookie)
                    self.core.db.q("UPDATE accounts SET status='Valid', robux=?, username=? WHERE id=?",
                                   (get_robux(cookie, me["id"]), me["name"], i))
                except AuthExpired:
                    self.core.db.q("UPDATE accounts SET status='Expired' WHERE id=?", (i,))
                except Exception as e:
                    self.core.db.q("UPDATE accounts SET status=? WHERE id=?", (f"Error: {e}"[:30], i))

        self.app.bg(work, lambda r, e: (self.rebuild(), self.app.toast("Status refreshed", "ok")))


# ------------------------------------------------------------------ Play ---
class PlayPage(Page):
    def __init__(self, parent, app):
        super().__init__(parent, app, "Play", "Pick a destination, pick accounts, launch")
        self.seen, self.lists_key = 0, None
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=3, uniform="p")
        body.grid_columnconfigure(1, weight=2, uniform="p")
        body.grid_rowconfigure(0, weight=1)
        left = card(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        pad = ctk.CTkFrame(left, fg_color="transparent")
        pad.pack(fill="both", expand=True, padx=18, pady=16)
        L(pad, "Destination", 15, True).pack(anchor="w")
        L(pad, "Place ID, game link, private server link or share link", 12, muted=True).pack(anchor="w", pady=(0, 8))
        self.dest = E(pad, height=40, placeholder_text="https://www.roblox.com/share?code=…&type=Server")
        self.dest.pack(fill="x")
        self.dest.insert(0, self.core.db.get("default_dest"))
        self.dest.bind("<KeyRelease>", self.validate)
        self.prev = L(pad, "", 12)
        self.prev.pack(anchor="w", pady=(6, 10))
        r = ctk.CTkFrame(pad, fg_color="transparent")
        r.pack(fill="x")
        btn(r, "★ Save favourite", self.fav, "ghost", w=130).pack(side="left")
        r2 = ctk.CTkFrame(pad, fg_color="transparent")
        r2.pack(fill="x", pady=(6, 0))
        self.iso = SW(r2, "Isolated sessions (recommended when running several accounts)",
                      command=lambda: self.core.db.set("isolate", "1" if self.iso.get() else "0"))
        if self.core.db.flag("isolate"):
            self.iso.select()
        self.iso.pack(side="left")
        self.multi_lbl = L(pad, "", 11, muted=True, justify="left", wraplength=520)
        self.multi_lbl.pack(anchor="w", pady=(4, 0))
        self.fix = btn(pad, "Fix: close Roblox and retry", self.fix_multi, "ghost", w=230, h=30)
        d0 = max(0, min(120, int(float(self.core.db.get("delay") or 8))))
        self.delay = slider_row(pad, "Delay between launches", 0, 120, 120, d0, lambda v: f"{int(v)} s")
        self.sel = L(pad, "", 12, muted=True, justify="left", wraplength=520)
        self.sel.pack(anchor="w", pady=(10, 8))
        row = ctk.CTkFrame(pad, fg_color="transparent")
        row.pack(fill="x", pady=4)
        btn(row, "▶  Launch selected", self.launch, "accent", w=190, h=42).pack(side="left")
        btn(row, "Pause", lambda: self.core.pause_ev.clear(), "ghost", w=80, h=42).pack(side="left", padx=6)
        btn(row, "Resume", lambda: self.core.pause_ev.set(), "ghost", w=80, h=42).pack(side="left")
        btn(row, "Cancel", self.cancel, "danger", w=80, h=42).pack(side="left", padx=6)
        self.err = L(pad, "", 12, color=BAD, justify="left", wraplength=520)
        self.err.pack(anchor="w", pady=(8, 0))
        self.prog = ctk.CTkProgressBar(pad, height=8, progress_color=T["accent"], fg_color=T["panel2"])
        self.prog.set(0)
        self.prog.pack(fill="x", pady=(14, 4))
        self.progl = L(pad, "Queue idle", 12, muted=True)
        self.progl.pack(anchor="w")
        right = card(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.tabs = ctk.CTkTabview(right, fg_color="transparent", segmented_button_fg_color=T["panel2"],
                                   segmented_button_selected_color=T["accent"],
                                   segmented_button_selected_hover_color=T["accent"],
                                   segmented_button_unselected_color=T["panel2"],
                                   segmented_button_unselected_hover_color=T["border"], text_color=T["text"])
        self.tabs.pack(fill="both", expand=True, padx=8, pady=8)
        for n in ("Favourites", "Recent", "Log"):
            self.tabs.add(n)
        self.favs = ctk.CTkScrollableFrame(self.tabs.tab("Favourites"), fg_color="transparent")
        self.favs.pack(fill="both", expand=True)
        self.hist = ctk.CTkScrollableFrame(self.tabs.tab("Recent"), fg_color="transparent")
        self.hist.pack(fill="both", expand=True)
        self.log = TB(self.tabs.tab("Log"), state="disabled")
        self.log.pack(fill="both", expand=True)
        self.validate()

    def validate(self, _=None):
        t = self.dest.get().strip()
        if not t:
            self.prev.configure(text="Waiting for a destination…", text_color=T["muted"])
            return
        try:
            d = parse_dest(t)
            if d.get("share_code"):
                msg = f"✓ Share link ({d['share_type']}), resolved when launching"
            else:
                msg = f"✓ Place {d['place_id']}" + ("  ·  private server" if d.get("link_code") else "") + ("  ·  specific server" if d.get("job_id") else "")
            self.prev.configure(text=msg, text_color=OK)
        except ValueError as e:
            self.prev.configure(text="✗ " + str(e), text_color=BAD)

    def fix_multi(self):
        if roblox_pids() and messagebox.askyesno(APP_NAME, self.core.multi_status + "\n\nClose all running Roblox windows now and try again?"):
            for p in roblox_pids():
                try:
                    psutil.Process(p).terminate()
                except psutil.Error:
                    pass
            time.sleep(2)
        self.core.log(self.core.set_multi(True))

    def fav(self):
        try:
            d = parse_dest(self.dest.get())
        except ValueError as e:
            self.err.configure(text=str(e))
            return
        if not d.get("place_id"):
            self.err.configure(text="Launch it once first so the share link resolves to a place, then save it.")
            return
        n = simpledialog.askstring(APP_NAME, "Name for this favourite:", parent=self.app) or str(d["place_id"])
        self.core.db.q("INSERT INTO favourites(place_id,name) VALUES(?,?) ON CONFLICT(place_id) DO UPDATE SET name=excluded.name",
                       (d["place_id"], n.strip()))
        self.lists_key = None

    def launch(self):
        self.err.configure(text="")
        try:
            d = parse_dest(self.dest.get())
        except ValueError as e:
            self.err.configure(text=str(e))
            return
        ids = [i for i in self.app.selected if self.core.db.one("SELECT 1 FROM accounts WHERE id=?", (i,))]
        if not ids:
            self.err.configure(text="Select at least one account on the Accounts tab.")
            return
        try:
            self.core.start_queue(ids, d, float(self.delay.get()))
        except RuntimeError as e:
            self.err.configure(text=str(e))

    def cancel(self):
        self.core.cancel_ev.set()
        self.core.pause_ev.set()

    def fill(self, p):
        self.dest.delete(0, "end")
        self.dest.insert(0, str(p))
        self.validate()

    def refresh(self):
        self.multi_lbl.configure(text=self.core.multi_status, text_color=OK if self.core.mutex else WARN)
        if self.core.mutex:
            self.fix.pack_forget()
        elif not self.fix.winfo_ismapped():
            self.fix.pack(anchor="w", pady=(4, 0))
        names = [(r["alias"] or r["username"]) for r in self.core.db.all("SELECT username, alias, id FROM accounts") if r["id"] in self.app.selected]
        self.sel.configure(text=f"Launching with {len(names)} account(s): " + (", ".join(names[:8]) + ("…" if len(names) > 8 else "") if names else "none selected yet"))
        q = self.core.qs
        self.prog.set(q["done"] / q["total"] if q["total"] else 0)
        self.progl.configure(text=(f"Launching {q['done']}/{q['total']}  ·  {q['failed']} failed" if q["running"]
                                   else (f"Finished: {q['done'] - q['failed']} started, {q['failed']} failed" if q["total"] else "Queue idle")))
        fk = (self.core.db.one("SELECT COUNT(*) FROM favourites")[0], self.core.db.one("SELECT COUNT(*) FROM launch_history")[0])
        if fk != self.lists_key:
            self.lists_key = fk
            for w in self.favs.winfo_children() + self.hist.winfo_children():
                w.destroy()
            for r in self.core.db.all("SELECT * FROM favourites ORDER BY name"):
                row = ctk.CTkFrame(self.favs, fg_color="transparent")
                row.pack(fill="x", pady=2)
                btn(row, f"{r['name']}  ({r['place_id']})", lambda p=r["place_id"]: self.fill(p), "ghost").pack(side="left", fill="x", expand=True)
                btn(row, "✕", lambda i=r["id"]: (self.core.db.q("DELETE FROM favourites WHERE id=?", (i,)), setattr(self, "lists_key", None)), "danger", w=32).pack(side="left", padx=(4, 0))
            seen = set()
            for r in self.core.db.all("SELECT dest FROM launch_history ORDER BY ts DESC LIMIT 60"):
                if r["dest"] not in seen and len(seen) < 12 and r["dest"].split()[0].isdigit():
                    seen.add(r["dest"])
                    btn(self.hist, r["dest"], lambda p=r["dest"].split()[0]: self.fill(p), "ghost").pack(fill="x", pady=2)
        if self.seen < len(self.core.logs):
            self.log.configure(state="normal")
            for line in self.core.logs[self.seen:]:
                self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
            self.seen = len(self.core.logs)


# --------------------------------------------------------------- Windows ---
class WindowsPage(Page):
    def __init__(self, parent, app):
        super().__init__(parent, app, "Windows", "See and control every Roblox window, at any size you want")
        self.tiles, self.cols, self.order = {}, 0, None
        self.last_cap, self.capturing = {}, set()
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", pady=(0, PAD))
        g = card(bar)
        g.pack(side="left", padx=(0, 8))
        i1 = ctk.CTkFrame(g, fg_color="transparent")
        i1.pack(padx=10, pady=8)
        L(i1, "Size all", 12, True).pack(side="left", padx=(0, 8))
        self.aw, self.ah = E(i1, width=70, placeholder_text="W"), E(i1, width=70, placeholder_text="H")
        self.aw.pack(side="left", padx=2)
        L(i1, "×", 13, muted=True).pack(side="left")
        self.ah.pack(side="left", padx=2)
        btn(i1, "Apply", self.size_all, "accent", w=64, h=34).pack(side="left", padx=(8, 0))
        g2 = card(bar)
        g2.pack(side="left", padx=(0, 8))
        i2 = ctk.CTkFrame(g2, fg_color="transparent")
        i2.pack(padx=6, pady=6)
        for t, f in [("▦ Grid", lambda: self.core.arrange("grid")), ("▥ Row", lambda: self.core.arrange("h")),
                     ("▤ Column", lambda: self.core.arrange("v")), ("❐ Cascade", self.cascade)]:
            btn(i2, t, f, "flat", h=32).pack(side="left", padx=2)
        mons = monitors()
        self.mon_names = [f"Monitor {i + 1}{' (primary)' if i == 0 else ''}  {m[2]}×{m[3]}" for i, m in enumerate(mons)]
        self.mon = OM(bar, self.mon_names, width=210, command=self.pick_mon)
        self.mon.set(self.mon_names[min(self.core.db.num("monitor", 0, 16), len(mons) - 1)])
        self.mon.pack(side="left")
        self.count = L(self, "", 12, muted=True)
        self.count.pack(anchor="w", pady=(0, 4))
        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list.pack(fill="both", expand=True)
        self.empty = L(self.list, "No Roblox windows yet. Launch an account and its window appears here.", 14, muted=True)

    def pick_mon(self, v):
        self.core.db.set("monitor", self.mon_names.index(v))

    def size_all(self):
        wh = parse_wh(f"{self.aw.get()}x{self.ah.get()}")
        if not wh:
            messagebox.showerror(APP_NAME, "Enter a width and a height (any positive numbers).")
            return
        for c in self.core.snapshot():
            if c["hwnd"] and not (c.get("embedded") or c.get("mv")):
                x, y, _, _ = win_rect(c["hwnd"])
                win_place(c["hwnd"], x, y, *wh)

    def cascade(self):
        mons = monitors()
        x, y, w, h = mons[min(self.core.db.num("monitor", 0, 16), len(mons) - 1)]
        for i, c in enumerate([c for c in self.core.snapshot() if c["hwnd"] and not (c.get("embedded") or c.get("mv"))]):
            win_place(c["hwnd"], x + 32 * i, y + 32 * i, int(w * 0.55), int(h * 0.55))

    def make_tile(self, c):
        f = card(self.list)
        head = ctk.CTkFrame(f, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 0))
        L(head, c["name"], 14, True).pack(side="left")
        pl = pill(head, "Running", OK)
        pl.pack(side="right")
        info = L(f, "", 11, muted=True, anchor="w")
        info.pack(fill="x", padx=14, pady=(2, 6))
        r1 = ctk.CTkFrame(f, fg_color="transparent")
        r1.pack(fill="x", padx=12)
        pid = c["pid"]
        t = {"frame": f, "prev": None, "pill": pl, "info": info, "img": None, "pinned": False}
        for txt, cmd in [("Focus", lambda: self.core.window_cmd(pid, "focus")), ("Min", lambda: self.core.window_cmd(pid, "min")),
                         ("Max", lambda: self.core.window_cmd(pid, "max")), ("Restore", lambda: self.core.window_cmd(pid, "restore"))]:
            btn(r1, txt, cmd, "ghost", h=28).pack(side="left", padx=(0, 4))
        t["pin"] = btn(r1, "Pin", lambda: self.pin(pid), "ghost", h=28, w=46)
        t["pin"].pack(side="left")
        r2 = ctk.CTkFrame(f, fg_color="transparent")
        r2.pack(fill="x", padx=12, pady=(8, 12))
        ents = []
        for lab in ("X", "Y", "W", "H"):
            L(r2, lab, 11, True, muted=True).pack(side="left", padx=(2, 2))
            e = E(r2, width=58, height=30)
            e.pack(side="left", padx=(0, 4))
            ents.append(e)
        t["ents"] = ents
        btn(r2, "Set", lambda: self.apply_geom(pid), "accent", h=30, w=44).pack(side="left", padx=2)
        btn(r2, "↻", lambda: self.read_geom(pid), "ghost", h=30, w=32).pack(side="left")
        self.tiles[pid] = t
        self.read_geom(pid)
        return t

    def read_geom(self, pid):
        c = next((c for c in self.core.snapshot() if c["pid"] == pid), None)
        t = self.tiles.get(pid)
        if c and c["hwnd"] and t:
            for e, v in zip(t["ents"], win_rect(c["hwnd"])):
                e.delete(0, "end")
                e.insert(0, str(v))

    def apply_geom(self, pid):
        c = next((c for c in self.core.snapshot() if c["pid"] == pid), None)
        t = self.tiles.get(pid)
        if not (c and c["hwnd"] and t) or c.get("embedded"):
            return
        v = [e.get().strip() for e in t["ents"]]
        if not (re.fullmatch(r"-?\d{1,6}", v[0]) and re.fullmatch(r"-?\d{1,6}", v[1]) and re.fullmatch(r"\d{1,5}", v[2]) and re.fullmatch(r"\d{1,5}", v[3])
                and int(v[2]) > 0 and int(v[3]) > 0):
            messagebox.showerror(APP_NAME, "X and Y can be any whole numbers. W and H must be positive whole numbers.")
            return
        win_place(c["hwnd"], int(v[0]), int(v[1]), int(v[2]), int(v[3]))
        hw, want = c["hwnd"], (int(v[2]), int(v[3]))

        def check():
            _, _, w_, h_ = win_rect(hw)
            if w_ > want[0] + 2 or h_ > want[1] + 2:
                self.app.toast(f"Windows kept it at {w_}×{h_} (the minimum for a normal window). "
                               "Use the Multi-View tab to go smaller.", "warn")

        self.after(250, check)

    def pin(self, pid):
        c = next((c for c in self.core.snapshot() if c["pid"] == pid), None)
        t = self.tiles.get(pid)
        if c and c["hwnd"] and t and not (c.get("embedded") or c.get("mv")):
            t["pinned"] = not t["pinned"]
            user32.SetWindowPos(c["hwnd"], V(-1 if t["pinned"] else -2), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            t["pin"].configure(text="Unpin" if t["pinned"] else "Pin", width=56 if t["pinned"] else 46)

    def _got(self, pid, img):
        self.capturing.discard(pid)
        self.last_cap[pid] = time.time()
        t = self.tiles.get(pid)
        if not t:
            return
        if img:
            t["img"] = ctk.CTkImage(img, img, size=img.size)
            t["prev"].configure(image=t["img"], text="", width=img.size[0], height=img.size[1])
        elif t["img"] is None:
            t["prev"].configure(text="Preview unavailable\n(Roblox draws with DirectX)", image=None)

    def refresh(self):
        cl = sorted(self.core.snapshot(), key=lambda c: c["start"])
        alive = {c["pid"] for c in cl}
        for pid in list(self.tiles):
            if pid not in alive:
                self.tiles.pop(pid)["frame"].destroy()
        for c in cl:
            if c["pid"] not in self.tiles:
                self.make_tile(c)
        cols = max(1, min(4, self.list.winfo_width() // 340))
        order = [c["pid"] for c in cl]
        if cols != self.cols or order != self.order:
            self.cols, self.order = cols, order
            for i in range(4):
                self.list.grid_columnconfigure(i, weight=1 if i < cols else 0)
            for i, pid in enumerate(order):
                self.tiles[pid]["frame"].grid(row=i // cols, column=i % cols, sticky="n", padx=6, pady=6)
        if cl:
            self.empty.grid_remove()
        else:
            self.empty.grid(row=0, column=0, pady=40)
        self.count.configure(text=f"{len(cl)} window(s)")
        for c in cl:
            t = self.tiles[c["pid"]]
            state = win_state(c["hwnd"])
            col = BAD if state == "Not responding" else WARN if state == "Minimized" else T["muted"] if state == "No window" else OK
            t["pill"].configure(text=f" {state} ", text_color=col, fg_color=blend(col, T["panel"], .16))
            if c.get("embedded"):
                t["info"].configure(text=f"PID {c['pid']}   ·   shown inside Multi-View")
            elif c["hwnd"]:
                x, y, w, h = win_rect(c["hwnd"])
                t["info"].configure(text=f"PID {c['pid']}   ·   {w}×{h} at ({x}, {y})")
            else:
                t["info"].configure(text=f"PID {c['pid']}   ·   waiting for window…")


# ------------------------------------------------------------ Multi-View ---
class MultiViewPage(Page):
    """Grid of live previews drawn by Windows' own compositor (DWM): the whole window, scaled, GPU-composed,
    no screen grabbing and no polling. Click one to open it on the stage, where you can play it normally."""
    GAP, CAP = 12, 34

    def __init__(self, parent, app):
        super().__init__(parent, app, "Multi-View", "Live previews of every client. Click one to play it.")
        self.tiles, self.thumbs, self.orig = {}, {}, {}
        self.stage_pid = self.stage_saved = None
        self.active, self._job, self._order = False, None, None
        self.back = btn(self.actions, "▦  Back to grid", self.unstage, "ghost", w=140, h=36)
        btn(self.actions, "Show all on desktop", self.release_btn, "ghost", w=170, h=36).pack(side="left", padx=4)
        self.area = ctk.CTkFrame(self, fg_color="transparent")
        self.area.pack(fill="both", expand=True)
        self.area.bind("<Configure>", lambda e: self.schedule())
        self.stage = tk.Frame(self.area, bg="#05070a")
        self.empty = L(self.area, "No Roblox windows yet. Launch an account and its live preview appears here.", 14, muted=True)
        self.empty.place(relx=0.5, rely=0.45, anchor="center")
        self.after(500, self.follow)
        self.after(1000, self.pulse)

    def root(self):
        return user32.GetAncestor(self.winfo_id(), 2)

    def client(self, pid):
        return next((c for c in self.core.snapshot() if c["pid"] == pid), None)

    # ---- real windows: parked off-screen while previewed, put back afterwards
    def park(self, c):
        h = c["hwnd"]
        if not h or c.get("embedded") or c.get("mv"):
            return
        if c["pid"] not in self.orig:
            user32.ShowWindow(h, 9)
            self.orig[c["pid"]] = win_rect(h)
        c["mv"] = True
        x, y, w, hh = win_rect(h)
        if x > -10000:
            win_place(h, -20000, -20000, max(w, 16), max(hh, 16))

    def unpark(self, c):
        c["mv"] = False
        r = self.orig.pop(c["pid"], None)
        if r and c["hwnd"] and user32.IsWindow(c["hwnd"]):
            x, y, w, hh = r
            win_place(c["hwnd"], x if x > -10000 else 100, y if y > -10000 else 100, w, hh)

    # ---- DWM thumbnails
    def thumb_for(self, c):
        pid = c["pid"]
        if pid in self.thumbs:
            return self.thumbs[pid]
        th = ctypes.c_void_p()
        if dwmapi.DwmRegisterThumbnail(self.root(), c["hwnd"], ctypes.byref(th)) != 0 or not th.value:
            return None
        self.thumbs[pid] = th.value
        return th.value

    def drop_thumb(self, pid):
        th = self.thumbs.pop(pid, None)
        if th:
            dwmapi.DwmUnregisterThumbnail(th)

    def sync(self):
        if not self.active:
            return
        origin = POINT(0, 0)
        user32.ClientToScreen(self.root(), ctypes.byref(origin))
        for pid, t in list(self.tiles.items()):
            c = self.client(pid)
            if not (c and c["hwnd"] and pid != self.stage_pid and t["frame"].winfo_ismapped() and user32.IsWindow(c["hwnd"])):
                self.drop_thumb(pid)
                continue
            th = self.thumb_for(c)
            if not th:
                t["lab"].configure(text="Preview unavailable")
                continue
            sz = SIZE()
            if dwmapi.DwmQueryThumbnailSourceSize(th, ctypes.byref(sz)) != 0 or sz.cx < 1 or sz.cy < 1:
                self.drop_thumb(pid)
                continue
            v = t["view"]
            vx, vy = v.winfo_rootx() - origin.x, v.winfo_rooty() - origin.y
            vw, vh = max(1, v.winfo_width()), max(1, v.winfo_height())
            sc = min(vw / sz.cx, vh / sz.cy)  # fit the WHOLE window inside the tile, keep its shape
            dw, dh = max(1, int(sz.cx * sc)), max(1, int(sz.cy * sc))
            x0, y0 = vx + (vw - dw) // 2, vy + (vh - dh) // 2
            pr = DWM_THUMBNAIL_PROPERTIES()
            pr.dwFlags = 0x1 | 0x8 | 0x10  # destination | visible | client area only
            pr.rcDestination = wintypes.RECT(x0, y0, x0 + dw, y0 + dh)
            pr.fVisible = True
            pr.fSourceClientAreaOnly = True
            if dwmapi.DwmUpdateThumbnailProperties(th, ctypes.byref(pr)) != 0:
                self.drop_thumb(pid)
            else:
                t["lab"].configure(text="")

    # ---- layout
    def schedule(self):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(50, self.layout)

    def layout(self):
        self._job = None
        W, H = self.area.winfo_width(), self.area.winfo_height()
        if W < 60 or H < 60:
            return
        G, CAP = self.GAP, self.CAP
        order = [p for p in (self._order or []) if p in self.tiles]
        stage = self.stage_pid if self.stage_pid in self.tiles else None
        rects = {}
        if stage:
            others = [p for p in order if p != stage]
            sw = W if not others else int(W * 0.76)
            self.stage.place(x=0, y=0, width=sw, height=H)
            c = self.client(stage)
            if c and c.get("embedded") and c["hwnd"]:
                win_place(c["hwnd"], 0, 0, sw, H)
            if others:
                x0, w = sw + G, W - sw - G
                h = int(w * 9 / 16)
                avail = H - G * (len(others) - 1)
                if len(others) * (h + CAP) > avail:
                    h = max(24, avail // len(others) - CAP)
                    w = int(h * 16 / 9)
                for i, p in enumerate(others):
                    rects[p] = (x0, i * (h + CAP + G), w, h)
        elif order:
            self.stage.place_forget()
            n, best = len(order), None
            for cols in range(1, n + 1):
                rows = math.ceil(n / cols)
                w = (W - G * (cols - 1)) / cols
                h = w * 9 / 16
                if rows * (h + CAP) + G * (rows - 1) > H:
                    h = (H - G * (rows - 1)) / rows - CAP
                    w = h * 16 / 9
                if best is None or w > best[0]:
                    best = (w, h, cols)
            w, h, cols = max(40, int(best[0])), max(24, int(best[1])), best[2]
            ox = max(0, (W - (cols * w + (cols - 1) * G)) // 2)
            for i, p in enumerate(order):
                r, k = divmod(i, cols)
                rects[p] = (ox + k * (w + G), r * (h + CAP + G), w, h)
        else:
            self.stage.place_forget()
        for pid, t in self.tiles.items():
            if pid in rects:
                x, y, w, h = rects[pid]
                t["frame"].place(x=x, y=y, width=w, height=h + CAP)
                t["view"].place(x=3, y=3, width=max(1, w - 6), height=max(1, h - 3))
                t["cap"].place(x=8, y=h + 2, width=max(1, w - 16), height=CAP - 6)
            else:
                t["frame"].place_forget()
        self.update_idletasks()
        self.sync()

    # ---- stage (the one window you can play)
    def play(self, pid):
        c, t = self.client(pid), self.tiles.get(pid)
        if not (c and t and c["hwnd"]) or self.stage_pid == pid:
            return
        self.unstage(redraw=False)
        self.drop_thumb(pid)
        self.park(c)
        self.stage_pid = pid
        self.back.pack(side="left", padx=4)
        self.layout()
        try:
            self.stage.update_idletasks()
            w, h = max(2, self.stage.winfo_width()), max(2, self.stage.winfo_height())
            self.stage_saved = embed_window(c["hwnd"], self.stage.winfo_id(), w, h)
            c["embedded"] = True
        except Exception as e:
            self.stage_pid = self.stage_saved = None
            self.back.pack_forget()
            self.app.toast(f"Could not open it on the stage: {e}", "bad")
        self.schedule()

    def unstage(self, redraw=True):
        pid = self.stage_pid
        if pid is None:
            return
        c = self.client(pid)
        if c and c.get("embedded") and self.stage_saved:
            release_window(c["hwnd"], self.stage_saved)
        if c:
            c["embedded"] = False
        self.stage_pid = self.stage_saved = None
        self.back.pack_forget()
        self.stage.place_forget()
        if redraw:
            self.schedule()

    def to_desktop(self, pid):
        c, t = self.client(pid), self.tiles.get(pid)
        if not (c and t):
            return
        if self.stage_pid == pid:
            self.unstage()
        self.drop_thumb(pid)
        self.unpark(c)
        t["detached"] = True
        if c["hwnd"]:
            win_focus(c["hwnd"])

    def restore_all(self):
        self.unstage(redraw=False)
        for pid in list(self.thumbs):
            self.drop_thumb(pid)
        for c in self.core.snapshot():
            if c.get("mv"):
                self.unpark(c)
        for t in self.tiles.values():
            t["detached"] = False

    def release_all(self):
        self.restore_all()
        self.active = False

    def release_btn(self):
        self.restore_all()
        for t in self.tiles.values():
            t["detached"] = True
        self.app.toast("Windows are back on your desktop. Leave this tab and come back to preview them again.", "info")

    def on_enter(self):
        self.active = True
        self.after(60, self.schedule)

    def on_leave(self):
        self.restore_all()
        self.active = False

    def pulse(self):
        if self.active:
            try:
                self.sync()
            except Exception:
                pass
        self.after(1000, self.pulse)

    def follow(self):
        """Keyboard focus goes to the stage window while the mouse is over it."""
        busy = self.active and self.stage_pid is not None
        try:
            if busy and (user32.GetForegroundWindow() or 0) == (self.root() or 0):
                c = self.client(self.stage_pid)
                if c and c.get("embedded") and c["hwnd"]:
                    pt = POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    under = user32.WindowFromPoint(pt)
                    if under and (under == c["hwnd"] or user32.IsChild(c["hwnd"], under)):
                        if (user32.GetFocus() or 0) != c["hwnd"]:
                            user32.SetFocus(c["hwnd"])
        except Exception:
            pass
        self.after(120 if busy else 1000, self.follow)

    # ---- tiles
    def make_tile(self, c):
        f = card(self.area)
        view = tk.Frame(f, bg="#05070a", cursor="hand2")
        lab = tk.Label(view, text="starting…", fg="#8791a7", bg="#05070a", font=(FONT_FAMILY, 10))
        lab.place(relx=0.5, rely=0.5, anchor="center")
        cap = ctk.CTkFrame(f, fg_color="transparent")
        pid = c["pid"]
        L(cap, c["name"], 12, True).pack(side="left")
        pl = pill(cap, "…", T["muted"])
        pl.pack(side="left", padx=6)
        btn(cap, "✕", lambda: self.core.close_client(pid), "danger", w=30, h=26).pack(side="right")
        btn(cap, "⧉", lambda: self.to_desktop(pid), "ghost", w=30, h=26).pack(side="right", padx=4)
        btn(cap, "▶ Play", lambda: self.play(pid), "accent", w=70, h=26).pack(side="right")
        for w in (view, lab):
            w.bind("<Button-1>", lambda e, p=pid: self.play(p))
        self.tiles[pid] = {"frame": f, "view": view, "lab": lab, "cap": cap, "pill": pl, "detached": False}

    def refresh(self):
        cl = sorted(self.core.snapshot(), key=lambda c: c["start"])
        alive = {c["pid"] for c in cl}
        changed = False
        for pid in list(self.tiles):
            if pid not in alive:
                self.drop_thumb(pid)
                if self.stage_pid == pid:
                    self.stage_pid = self.stage_saved = None
                    self.back.pack_forget()
                    self.stage.place_forget()
                self.tiles.pop(pid)["frame"].destroy()
                self.orig.pop(pid, None)
                changed = True
        for c in cl:
            if c["pid"] not in self.tiles:
                self.make_tile(c)
                changed = True
        order = [c["pid"] for c in cl]
        if order != self._order:
            self._order, changed = order, True
        if cl:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.45, anchor="center")
        for c in cl:
            t = self.tiles[c["pid"]]
            if self.active and c["hwnd"] and not t["detached"] and not c.get("embedded") and not c.get("mv"):
                self.park(c)
                changed = True
            state = win_state(c["hwnd"])
            if c.get("embedded"):
                txt, col = "Playing", OK
            elif state == "Not responding":
                txt, col = "Not responding", BAD
            elif t["detached"]:
                txt, col = "On desktop", WARN
            elif state == "No window":
                txt, col = "Starting", T["muted"]
            else:
                txt, col = "Live", OK
            t["pill"].configure(text=f" {txt} ", text_color=col, fg_color=blend(col, T["panel"], .16))
        if changed and self.active:
            self.schedule()


# --------------------------------------------------------------- Monitor ---
class MonitorPage(Page):
    RANGES = {"1 min": 60, "5 min": 300, "15 min": 900, "1 hour": 3600}

    def __init__(self, parent, app):
        super().__init__(parent, app, "Monitor", "Live system and per-client resource usage")
        self.range = SEG(self.actions, list(self.RANGES))
        self.range.set("5 min")
        self.range.pack(side="left", padx=4)
        self.client = OM(self.actions, ["All clients"], width=240)
        self.client.pack(side="left", padx=4)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x")
        row.grid_columnconfigure(tuple(range(5)), weight=1, uniform="m")
        self.tiles = {}
        for i, (k, t) in enumerate([("cpu", "System CPU"), ("ram", "System RAM"), ("rb", "Roblox (selected)"), ("net", "Network (system)"), ("up", "Uptime")]):
            f = card(row)
            f.grid(row=0, column=i, sticky="nsew", padx=4)
            L(f, t, 12, muted=True).pack(anchor="w", padx=14, pady=(12, 0))
            v = L(f, "-", 20, True)
            v.pack(anchor="w", padx=14)
            s = L(f, "", 11, muted=True)
            s.pack(anchor="w", padx=14, pady=(0, 12))
            self.tiles[k] = (v, s)
        c1 = card(self)
        c1.pack(fill="both", expand=True, pady=(PAD, 4))
        self.cpu = ctk.CTkCanvas(c1, height=200, bg=T["panel"], highlightthickness=0)
        self.cpu.pack(fill="both", expand=True, padx=2, pady=2)
        c2 = card(self)
        c2.pack(fill="both", expand=True, pady=(4, 0))
        self.ram = ctk.CTkCanvas(c2, height=200, bg=T["panel"], highlightthickness=0)
        self.ram.pack(fill="both", expand=True, padx=2, pady=2)

    def refresh(self):
        cl = self.core.snapshot()
        opts = ["All clients"] + [f"{c['pid']} - {c['name']}" for c in cl]
        self.client.configure(values=opts)
        if self.client.get() not in opts:
            self.client.set("All clients")
        now = time.time()
        s = [x for x in self.core.hist if x["t"] >= now - self.RANGES[self.range.get()]]
        if not s:
            return
        last, sel = s[-1], self.client.get()
        key = (last["t"], self.range.get(), sel, self.cpu.winfo_width())
        if key == getattr(self, "_key", None):
            return
        self._key = key
        if sel == "All clients":
            rb_cpu, rb_ram = [x["rb_cpu"] for x in s], [x["rb_ram"] / 1048576 for x in s]
            rb_v, rb_s = f"{last['rb_cpu']:.1f}%  ·  {fmt_mb(last['rb_ram'])}", f"{last['count']} client(s)"
        else:
            pid = int(sel.split(" ")[0])
            rb_cpu = [x["per"].get(pid, (0, 0))[0] for x in s]
            rb_ram = [x["per"].get(pid, (0, 0))[1] / 1048576 for x in s]
            c = next((c for c in cl if c["pid"] == pid), None)
            rb_v, rb_s = f"{rb_cpu[-1]:.1f}%  ·  {rb_ram[-1]:,.0f} MB", f"up {fmt_dur(now - c['start']) if c else '-'}"
        self.tiles["cpu"][0].configure(text=f"{last['cpu']:.0f}%")
        self.tiles["ram"][0].configure(text=f"{last['ram_used'] / 2**30:.1f} GB")
        self.tiles["ram"][1].configure(text=f"{last['ram_avail'] / 2**30:.1f} GB free of {last['total'] / 2**30:.0f}")
        self.tiles["rb"][0].configure(text=rb_v)
        self.tiles["rb"][1].configure(text=rb_s)
        self.tiles["net"][0].configure(text=f"↓ {last['down'] / 1024:.0f} KB/s")
        self.tiles["net"][1].configure(text=f"↑ {last['up'] / 1024:.0f} KB/s")
        self.tiles["up"][0].configure(text=fmt_dur(now - self.core.started))
        self.tiles["up"][1].configure(text="RoManager")
        draw_area(self.cpu, [([x["cpu"] for x in s], T["muted"]), (rb_cpu, T["accent"])], 100, "CPU %   (grey: whole system · accent: Roblox)")
        draw_area(self.ram, [(rb_ram, OK)], max(512, max(rb_ram) * 1.2), "Roblox RAM (MB)")


# ----------------------------------------------------------- Performance ---
class PerfPage(Page):
    def __init__(self, parent, app):
        super().__init__(parent, app, "Performance", "Make every Roblox client use less CPU and GPU")
        db = self.core.db
        self.sel = db.get("preset") if db.get("preset") in ("smooth", "balanced", "low", "ultra", "minimal", "lowest") else "custom"
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x")
        row.grid_columnconfigure(tuple(range(4)), weight=1, uniform="pp")
        self.pcards = {}
        info = {"balanced": ("Balanced", "Windows defaults. Nothing is throttled."),
                "low": ("Low resource", "50% CPU cap per client, 640×360 windows, 30 FPS, graphics 3/10, background clients in efficiency mode."),
                "ultra": ("Ultra low", f"25% CPU cap, 480×270, 20 FPS, graphics 1/10, efficiency mode, {max(1, NCPU // 2)} core(s) each, power-saving GPU."),
                "minimal": ("Minimal", "10% CPU cap, 320×180, 15 FPS, graphics 1/10, 1 core each, efficiency mode, memory trim. As light as it gets."),
                "lowest": ("Lowest · 5% / 5%", "5% CPU, 5% GPU, 200 MB RAM max per client, 45 FPS cap, 320×180, graphics 1/10, 1 core, efficiency mode."),
                "smooth": ("Smooth · auto-lowest", "Adapts live: each client gets just enough CPU not to stutter, windows shrink only when the GPU is busy, 45 FPS cap, graphics 1/10. No RAM cap, so no swapping."),
                "custom": ("Custom", "Every option below, your values.")}
        for i, k in enumerate(("smooth", "balanced", "low", "ultra", "minimal", "lowest", "custom")):
            f = card(row)
            f.grid(row=i // 4, column=i % 4, sticky="nsew", padx=4, pady=4)
            L(f, info[k][0], 15, True).pack(anchor="w", padx=14, pady=(12, 2))
            L(f, info[k][1], 11, muted=True, wraplength=230, justify="left").pack(anchor="w", padx=14, pady=(0, 14))
            for w in [f] + list(f.winfo_children()):
                w.bind("<Button-1>", lambda e, k=k: self.choose(k))
            self.pcards[k] = f
        panel = card(self)
        panel.pack(fill="both", expand=True, pady=PAD)
        sc = ctk.CTkScrollableFrame(panel, fg_color="transparent")
        sc.pack(fill="both", expand=True, padx=6, pady=6)
        sc.grid_columnconfigure((0, 1), weight=1, uniform="cc")
        a = ctk.CTkFrame(sc, fg_color="transparent")
        a.grid(row=0, column=0, sticky="nsew", padx=10)
        b = ctk.CTkFrame(sc, fg_color="transparent")
        b.grid(row=0, column=1, sticky="nsew", padx=10)
        self.cw = []
        L(a, "CPU and memory", 14, True, color=T["accent"]).pack(anchor="w", pady=(4, 6))
        r = ctk.CTkFrame(a, fg_color="transparent")
        r.pack(fill="x", pady=4)
        L(r, "Process priority", 12).pack(side="left")
        self.prio = OM(r, ["Normal", "Below normal", "Idle"], width=150)
        self.prio.set(db.get("c_prio"))
        self.prio.pack(side="right")
        self.cw.append(self.prio)
        self.eco = SW(a, "Efficiency mode (Windows EcoQoS)")
        self.bg_ = SW(a, "Throttle background clients (not the focused one)")
        self.trim = SW(a, "Trim memory of background clients every minute")
        self.mem = SW(a, "Low memory priority")
        for sw, key in ((self.eco, "p_eco"), (self.bg_, "p_bg"), (self.trim, "p_trim"), (self.mem, "p_mem")):
            sw.pack(anchor="w", pady=5)
            if db.flag(key):
                sw.select()
            self.cw.append(sw)
        self.cores = slider_row(a, "Limit each client to N CPU cores (0 = all)", 0, NCPU, NCPU, db.num("p_cores", 0, NCPU),
                                lambda v: "all cores" if int(v) == 0 else f"{int(v)} of {NCPU}")
        self.cw.append(self.cores)
        self.cap = slider_row(a, "Hard CPU cap per client (0 = off)", 0, 100, 100, db.num("p_cap", 0, 100),
                              lambda v: "off" if int(v) == 0 else f"{int(v)}% of total CPU")
        self.cw.append(self.cap)
        self.ramcap = slider_row(a, "RAM max per client, MB (0 = off)", 0, 1000, 100, db.num("p_ramcap", 0, 8000),
                                 lambda v: "off" if int(v) == 0 else f"up to {int(v)} MB")
        self.cw.append(self.ramcap)
        self.iv = slider_row(a, "Monitoring interval", 1, 15, 14, db.num("interval", 1, 30), lambda v: f"{int(v)} s")
        L(b, "GPU and display", 14, True, color=T["accent"]).pack(anchor="w", pady=(4, 6))
        r = ctk.CTkFrame(b, fg_color="transparent")
        r.pack(fill="x", pady=4)
        L(r, "Windows GPU preference", 12).pack(side="left")
        self.gpu = OM(r, ["System default", "Power saving", "High performance"], width=170)
        self.gpu.set({"default": "System default", "power": "Power saving", "high": "High performance"}.get(db.get("p_gpu"), "System default"))
        self.gpu.pack(side="right")
        self.cw.append(self.gpu)
        self.gfx = slider_row(b, "Graphics quality (0 = leave as is)", 0, 10, 10, db.num("p_gfx", 0, 10), lambda v: "unchanged" if int(v) == 0 else f"{int(v)} / 10")
        self.fps = slider_row(b, "FPS cap (0 = leave as is)", 0, 240, 24, db.num("p_fps", 0, 240), lambda v: "unchanged" if int(v) == 0 else f"{int(v)} FPS")
        self.gpucap = slider_row(b, "GPU target per client (0 = off)", 0, 100, 100, db.num("p_gpucap", 0, 100),
                                 lambda v: "off" if int(v) == 0 else f"{int(v)}% max")
        self.cw += [self.gfx, self.fps, self.gpucap]
        r = ctk.CTkFrame(b, fg_color="transparent")
        r.pack(fill="x", pady=6)
        L(r, "Window size (any size)", 12).pack(side="left")
        self.h_ = E(r, width=66, height=30, placeholder_text="H")
        self.h_.pack(side="right")
        L(r, "×", 12, muted=True).pack(side="right", padx=3)
        self.w_ = E(r, width=66, height=30, placeholder_text="W")
        self.w_.pack(side="right")
        for e, k in ((self.w_, "c_w"), (self.h_, "c_h")):
            if db.get(k) not in ("", "0"):
                e.insert(0, db.get(k))
            self.cw.append(e)
        self.automin = SW(b, "Minimize clients that are not in the foreground")
        if db.flag("c_automin"):
            self.automin.select()
        self.automin.pack(anchor="w", pady=6)
        self.cw.append(self.automin)
        L(sc, "How it works: documented Windows settings only (priority class, EcoQoS efficiency mode, memory priority, CPU core limit, a Windows job-object hard CPU cap, working-set trim, per-app GPU preference) plus Roblox's own graphics-quality and FPS-cap settings, written to each account's isolated profile (or, without isolation, while no client is running; a backup is kept).\n"
              "If Windows or Roblox refuses a setting for a client, it is skipped and shown in the Play log. Windows has no hard GPU cap for another program, so the GPU target is enforced by measuring each client's GPU use (Windows performance counters) every few seconds and shrinking or growing its window to stay under the target. The RAM limit is a hard working-set cap: Windows keeps at most that much of a client in physical memory and moves the rest to the page file, so very low limits make Roblox slow and disk-heavy.",
          11, muted=True, wraplength=900, justify="left").grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 4))
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x")
        btn(bar, "Apply and save", self.save, "accent", w=150, h=38).pack(side="left")
        btn(bar, "Undo Roblox graphics changes", self.undo, "ghost", w=210, h=38).pack(side="left", padx=8)
        self.sw_ = {}
        for key, text in (("ram_guard", "Keep disk under 2%"), ("relax_on", "Ease limits after a crash")):
            sw = SW(bar, text, command=lambda k=key: db.set(k, "1" if self.sw_[k].get() else "0"))
            if db.flag(key):
                sw.select()
            sw.pack(side="left", padx=10)
            self.sw_[key] = sw
        self.live = L(bar, "", 12, muted=True)
        self.live.pack(side="right")
        self.choose(self.sel, init=True)

    def choose(self, k, init=False):
        self.sel = k
        for n, f in self.pcards.items():
            f.configure(border_color=T["accent"] if n == k else T["border"], border_width=2 if n == k else 1)
        for w in self.cw:
            try:
                w.configure(state="normal" if k == "custom" else "disabled")
            except Exception:
                pass

    def save(self):
        db = self.core.db
        db.set("preset", self.sel)
        db.set("c_prio", self.prio.get())
        for sw, key in ((self.eco, "p_eco"), (self.bg_, "p_bg"), (self.trim, "p_trim"), (self.mem, "p_mem")):
            db.set(key, "1" if sw.get() else "0")
        db.set("p_cores", int(self.cores.get()))
        db.set("p_cap", int(self.cap.get()))
        db.set("p_gpucap", int(self.gpucap.get()))
        db.set("p_ramcap", int(self.ramcap.get()))
        db.set("interval", int(self.iv.get()))
        db.set("p_gpu", {"System default": "default", "Power saving": "power", "High performance": "high"}[self.gpu.get()])
        db.set("p_gfx", int(self.gfx.get()))
        db.set("p_fps", int(self.fps.get()))
        db.set("c_automin", "1" if self.automin.get() else "0")
        for e, k in ((self.w_, "c_w"), (self.h_, "c_h")):
            v = e.get().strip()
            db.set(k, v if re.fullmatch(r"\d{1,5}", v) else "0")
        self.core.apply_all()
        self.app.toast("Performance settings applied", "ok")

    def undo(self):
        try:
            self.core.restore_roblox_settings()
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def refresh(self):
        cl = self.core.snapshot()
        self.live.configure(text=f"{len(cl)} client(s)  ·  {sum(c['cpu'] for c in cl):.1f}% CPU  ·  {fmt_mb(sum(c['rss'] for c in cl))} RAM  ·  disk {self.core.disk_pct:.1f}%")


# -------------------------------------------------------------- Settings ---
class SettingsPage(Page):
    def __init__(self, parent, app):
        super().__init__(parent, app, "Settings", "Appearance, behaviour, recovery, notifications and security")
        db = self.core.db
        sc = ctk.CTkScrollableFrame(self, fg_color="transparent")
        sc.pack(fill="both", expand=True)

        def section(t, sub=""):
            p = card(sc)
            p.pack(fill="x", pady=6, padx=2)
            L(p, t, 16, True).pack(anchor="w", padx=18, pady=(14, 0))
            if sub:
                L(p, sub, 12, muted=True, justify="left").pack(anchor="w", padx=18)
            inner = ctk.CTkFrame(p, fg_color="transparent")
            inner.pack(fill="x", padx=18, pady=(8, 14))
            return inner

        def switch(p, text, key, cb=None):
            s = SW(p, text, command=lambda: (db.set(key, "1" if s.get() else "0"), cb and cb()))
            if db.flag(key):
                s.select()
            s.pack(anchor="w", pady=4)
            return s

        def entry(p, text, key, w=90):
            f = ctk.CTkFrame(p, fg_color="transparent")
            f.pack(anchor="w", pady=3)
            L(f, text, 13, width=270, anchor="w").pack(side="left")
            e = E(f, width=w)
            e.insert(0, db.get(key))
            e.pack(side="left")
            e.bind("<FocusOut>", lambda ev: db.set(key, e.get().strip()) if e.get().strip() else None)

        def row(p, text, widget):
            f = ctk.CTkFrame(p, fg_color="transparent")
            f.pack(anchor="w", fill="x", pady=4)
            L(f, text, 13, width=270, anchor="w").pack(side="left")
            widget(f).pack(side="left")

        # appearance
        p = section("Appearance", "Everything here applies instantly when you press Apply appearance.")
        self.v = {}

        def lrow(parent, text):
            f = ctk.CTkFrame(parent, fg_color="transparent")
            f.pack(anchor="w", pady=4)
            L(f, text, 13, width=270, anchor="w").pack(side="left")
            return f

        f = lrow(p, "Theme")
        self.v["theme"] = OM(f, list(THEMES), width=160)
        self.v["theme"].set(db.get("theme") if db.get("theme") in THEMES else "Midnight")
        self.v["theme"].pack(side="left")
        self.accent = {"v": db.get("accent")}
        f = lrow(p, "Accent color")
        btn(f, "Pick color", self.pick_accent, "ghost", w=100).pack(side="left")
        btn(f, "Theme default", lambda: self.accent.update(v=""), "ghost", w=120).pack(side="left", padx=6)
        self.v["scale"] = slider_row(p, "UI scale", 80, 140, 12, db.num("ui_scale", 80, 140), lambda v: f"{int(v)}%")
        self.v["radius"] = slider_row(p, "Corner roundness", 0, 24, 24, db.num("radius", 0, 24), lambda v: f"{int(v)} px")
        self.v["opacity"] = slider_row(p, "Window opacity", 50, 100, 50, db.num("opacity", 50, 100), lambda v: f"{int(v)}%")
        f = lrow(p, "Density")
        self.v["density"] = SEG(f, ["Comfortable", "Compact"])
        self.v["density"].set(db.get("density") if db.get("density") in ("Comfortable", "Compact") else "Comfortable")
        self.v["density"].pack(side="left")
        f = lrow(p, "Client cards per row")
        self.v["cols"] = SEG(f, ["1", "2"])
        self.v["cols"].set(db.get("cols") if db.get("cols") in ("1", "2") else "1")
        self.v["cols"].pack(side="left")
        f = lrow(p, "Font")
        self.v["font"] = OM(f, ["Segoe UI", "Bahnschrift", "Calibri", "Consolas", "Cascadia Code", "Arial"], width=160)
        self.v["font"].set(db.get("font"))
        self.v["font"].pack(side="left")
        self.sw = {}
        for key, text in (("topmost", "Keep RoManager on top"), ("show_avatars", "Show avatars"), ("show_bars", "Show CPU/RAM bars on client cards"),
                          ("anim", "Fade-in animation"), ("sidebar_small", "Compact sidebar (icons only)")):
            s = SW(p, text)
            if db.flag(key):
                s.select()
            s.pack(anchor="w", pady=4)
            self.sw[key] = s
        btn(p, "Apply appearance", self.apply_appearance, "accent", w=180, h=38).pack(anchor="w", pady=(10, 0))

        p = section("RoManager's own CPU / GPU use", "RoManager may use up to this much CPU to stay smooth. The Roblox clients keep their own, much lower limits.")
        slider_row(p, "UI refresh interval", 1, 10, 9, db.num("ui_iv", 1, 10), lambda v: f"{int(v)} s",
                   save=lambda v: db.set("ui_iv", int(v)))
        switch(p, "Slow down while in the background, pause while minimized", "ui_pause")
        switch(p, "Run RoManager in Windows efficiency mode while idle", "self_eco")
        switch(p, "Efficiency mode all the time, not only while idle", "self_eco_always")
        slider_row(p, "RoManager CPU ceiling (hard limit)", 5, 100, 19, db.num("app_cap", 5, 100), lambda v: f"{int(v)}%",
                   save=lambda v: (db.set("app_cap", int(v)), set_self_cap(int(v))))
        p = section("General")
        switch(p, "Start with Windows", "autostart", self.autostart)
        switch(p, "Start minimized", "start_min")
        p = section("Launch")
        entry(p, "Default launch delay (seconds)", "delay")
        entry(p, "Default destination", "default_dest", 320)
        L(p, "Applied the next time RoManager starts.", 11, muted=True).pack(anchor="w")
        p = section("Recovery", "Relaunches only clients you launched. It never bypasses kicks or bans.")
        switch(p, "Automatic recovery (restart crashed or unresponsive clients)", "auto")
        entry(p, "Maximum restart attempts", "max_attempts")
        entry(p, "Restart delay (seconds)", "restart_delay")
        p = section("Discord notifications")
        self.wh = E(p, width=460, show="•", placeholder_text="Webhook URL" + (" (saved, hidden)" if db.get("webhook") else ""))
        self.wh.pack(anchor="w", pady=4)
        r = ctk.CTkFrame(p, fg_color="transparent")
        r.pack(anchor="w")
        btn(r, "Save and test", self.save_wh, "accent", w=130).pack(side="left", padx=(0, 6))
        btn(r, "Remove webhook", self.rm_wh, "ghost", w=140).pack(side="left")
        self.whmsg = L(p, "", 12, muted=True)
        self.whmsg.pack(anchor="w", pady=2)
        switch(p, "Enable notifications", "wh_on")
        switch(p, "Client crash", "n_crash")
        switch(p, "Unresponsive client", "n_hung")
        switch(p, "Restart", "n_restart")
        switch(p, "Launch completion", "n_launch")
        p = section("Anti-AFK", "RoManager does not simulate input. Simulated input may not count as player activity and Roblox can disconnect idle sessions at any time, so no AFK guarantee is offered.")
        p = section("Security", "Session cookies and the webhook are DPAPI-encrypted per field. The master password locks the app at startup; it does not encrypt the database file. Accounts are removed by overwriting the stored secret.")
        r = ctk.CTkFrame(p, fg_color="transparent")
        r.pack(anchor="w")
        btn(r, "Set master password", self.set_pw, "accent", w=170).pack(side="left", padx=(0, 6))
        btn(r, "Remove master password", self.rm_pw, "ghost", w=190).pack(side="left")

    def pick_accent(self):
        c = colorchooser.askcolor(parent=self.app)[1]
        if c:
            self.accent["v"] = c

    def apply_appearance(self):
        db = self.core.db
        db.set("theme", self.v["theme"].get())
        db.set("accent", self.accent["v"])
        db.set("ui_scale", int(self.v["scale"].get()))
        db.set("radius", int(self.v["radius"].get()))
        db.set("opacity", int(self.v["opacity"].get()))
        db.set("density", self.v["density"].get())
        db.set("cols", self.v["cols"].get())
        db.set("font", self.v["font"].get())
        for k, s in self.sw.items():
            db.set(k, "1" if s.get() else "0")
        self.app.after(30, lambda: self.app.rebuild("Settings"))

    def autostart(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
                if self.core.db.flag("autostart"):
                    cmd = f'"{sys.executable}"' if getattr(sys, "frozen", False) else f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'
                    winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
                else:
                    try:
                        winreg.DeleteValue(k, APP_NAME)
                    except FileNotFoundError:
                        pass
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Could not change startup setting: {e}")

    def save_wh(self):
        url = self.wh.get().strip()
        if not WEBHOOK_RE.match(url):
            self.whmsg.configure(text="Not a valid Discord webhook URL.", text_color=BAD)
            return
        self.whmsg.configure(text="Sending test message…", text_color=T["muted"])

        def done(ok, err):
            if ok:
                self.core.db.set("webhook", dpapi_protect(url.encode()).hex())
                self.wh.delete(0, "end")
                self.whmsg.configure(text="Webhook saved (test message sent).", text_color=OK)
            else:
                self.whmsg.configure(text="Discord rejected the test message. Not saved.", text_color=BAD)

        self.app.bg(lambda: discord_post(url, "RoManager webhook test."), done)

    def rm_wh(self):
        self.core.db.set("webhook", "")
        self.whmsg.configure(text="Webhook removed.", text_color=T["muted"])

    def set_pw(self):
        a = ask_secret(self.app, "Master password", "New master password:")
        if not a:
            return
        if len(a) < 6:
            messagebox.showerror(APP_NAME, "Use at least 6 characters.")
            return
        if ask_secret(self.app, "Master password", "Repeat it:") != a:
            messagebox.showerror(APP_NAME, "Passwords did not match.")
            return
        self.core.db.set("lock_hash", hash_pw(a))
        messagebox.showinfo(APP_NAME, "Master password set. It is asked at startup.")

    def rm_pw(self):
        h = self.core.db.get("lock_hash")
        if h and verify_pw(ask_secret(self.app, "Master password", "Current password:") or "", h):
            self.core.db.set("lock_hash", "")
        elif h:
            messagebox.showerror(APP_NAME, "Wrong password.")


# ------------------------------------------------------------------- App ---
class Splash(ctk.CTkToplevel):
    """Loading screen shown while the interface is built."""

    def __init__(self, app):
        super().__init__(app)
        self.overrideredirect(True)
        w, h = 460, 290
        self.geometry(f"{w}x{h}+{(self.winfo_screenwidth() - w) // 2}+{(self.winfo_screenheight() - h) // 2}")
        self.configure(fg_color=T["bg"])
        self.attributes("-topmost", True)
        box = ctk.CTkFrame(self, fg_color=T["panel"], border_color=T["border"], border_width=1, corner_radius=R + 6)
        box.pack(fill="both", expand=True, padx=1, pady=1)
        ctk.CTkLabel(box, text="◈", font=F(54, True), text_color=T["accent"]).pack(pady=(30, 0))
        ctk.CTkLabel(box, text="RoManager", font=F(28, True), text_color=T["text"]).pack()
        self.msg = ctk.CTkLabel(box, text="Starting…", font=F(12), text_color=T["muted"])
        self.msg.pack(pady=(6, 16))
        self.bar = ctk.CTkProgressBar(box, height=6, width=320, progress_color=T["accent"], fg_color=T["panel2"])
        self.bar.set(0)
        self.bar.pack()
        self.update()

    def step(self, text, value):
        self.msg.configure(text=text)
        self.bar.set(value)
        self.update()

    def close(self):
        for i in range(8):
            self.attributes("-alpha", 1 - i / 8)
            self.update()
            time.sleep(0.01)
        self.destroy()


class LazyPages(dict):
    """Builds a tab the first time it is opened, so start-up and idle memory stay small."""

    def __init__(self, app, classes):
        super().__init__()
        self.app, self.classes = app, classes

    def __missing__(self, name):
        page = self.classes[name](self.app.content, self.app)
        self[name] = page
        return page


class App(ctk.CTk):
    NAV = [("Home", "Dashboard", "⌂"), ("Accounts", "Accounts", "☺"), ("Play", "Play", "▶"), ("Windows", "Windows", "▣"),
           ("MultiView", "Multi-View", "▦"), ("Monitor", "Monitor", "◔"), ("Performance", "Performance", "⚡"), ("Settings", "Settings", "⚙")]

    def __init__(self, core):
        super().__init__()
        self.withdraw()
        self.core = core
        self.selected, self.avatars, self.uiq, self.toasts = set(), {}, queue.Queue(), []
        self.current = "Home"
        self.title(APP_NAME)
        self.geometry("1320x820")
        self.minsize(1080, 680)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        apply_theme(core.db)
        splash = Splash(self)
        splash.step("Preparing the interface…", 0.25)
        self.build()
        splash.step("Loading your accounts…", 0.6)
        self.show("Home")
        style_window(self)
        splash.step("Almost ready…", 0.9)
        self.after(100, self.pump)
        self.after(1000, self.tick)
        if not core.mutex:
            self.after(1200, lambda: self.toast(core.multi_status, "warn"))
        if not roblox_installed():
            self.after(900, lambda: self.toast("Roblox Player not found for this Windows user. Install it from roblox.com.", "warn"))
        splash.step("Ready", 1.0)
        splash.close()
        if core.db.flag("start_min"):
            self.iconify()
        else:
            if core.db.flag("anim"):
                self.attributes("-alpha", 0.0)
            self.deiconify()
            if core.db.flag("anim"):
                self.fade(0)

    def report_callback_exception(self, exc, val, tb):
        import traceback
        _log_crash("".join(traceback.format_exception(exc, val, tb)))
        self.core.log(f"UI error: {val}")

    def fade(self, n):
        target = self.core.db.num("opacity", 50, 100) / 100
        self.attributes("-alpha", min(target, n / 10 * target))
        if n < 10:
            self.after(25, lambda: self.fade(n + 1))

    def build(self):
        db = self.core.db
        apply_theme(db)
        sc = db.num("ui_scale", 80, 140) / 100
        ctk.set_widget_scaling(sc)
        ctk.set_window_scaling(sc)
        for w in self.winfo_children():
            w.destroy()
        self.toasts = []
        self.configure(fg_color=T["bg"])
        self.attributes("-topmost", db.flag("topmost"))
        if not db.flag("anim") and db.num("opacity", 50, 100) < 100:
            self.attributes("-alpha", db.num("opacity", 50, 100) / 100)
        small = db.flag("sidebar_small")
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        side = ctk.CTkFrame(self, width=76 if small else 224, fg_color=T["panel"], corner_radius=R, border_width=1,
                            border_color=T["border"])
        side.grid(row=0, column=0, sticky="nsw", padx=(14, 0), pady=14)
        side.grid_propagate(False)
        logo = ctk.CTkFrame(side, fg_color="transparent")
        logo.pack(fill="x", padx=16, pady=(24, 20))
        L(logo, "◈", 24, True, color=T["accent"]).pack(side="left")
        if not small:
            L(logo, " RoManager", 19, True).pack(side="left")
        self.navb = {}
        for key, name, icon in self.NAV:
            rw = ctk.CTkFrame(side, fg_color="transparent", height=42)
            rw.pack(fill="x", padx=10, pady=2)
            ind = ctk.CTkFrame(rw, width=3, fg_color="transparent", corner_radius=2)
            ind.pack(side="left", fill="y", pady=7)
            b = ctk.CTkButton(rw, text=icon if small else f"  {icon}    {name}", anchor="center" if small else "w", height=40,
                              corner_radius=max(R - 4, 4), fg_color="transparent", text_color=T["muted"],
                              hover_color=T["panel2"], font=F(14), command=lambda k=key: self.show(k))
            b.pack(side="left", fill="x", expand=True, padx=(4, 0))
            self.navb[key] = (b, ind)
        foot = ctk.CTkFrame(side, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=16, pady=16)
        self.dot = L(foot, "●", 14, color=T["muted"])
        self.dot.pack(side="left")
        self.status = L(foot, "" if small else " 0 running", 12, muted=True)
        self.status.pack(side="left")
        self.small = small
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew", padx=22, pady=18)
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)
        classes = {"Home": HomePage, "Accounts": AccountsPage, "Play": PlayPage, "Windows": WindowsPage, "MultiView": MultiViewPage,
                   "Monitor": MonitorPage, "Performance": PerfPage, "Settings": SettingsPage}
        self.pages = LazyPages(self, classes)

    def rebuild(self, page):
        mv = self.pages.get("MultiView")
        if mv:
            try:
                mv.release_all()  # destroying the frames would close embedded clients
            except Exception:
                pass
        self.avatars = {k: v for k, v in self.avatars.items() if v is not None}
        self.build()
        style_window(self)
        self.show(page)

    def show(self, name):
        prev = self.pages.get(self.current)
        if prev is not None and self.pages[name] is not prev and hasattr(prev, "on_leave"):
            prev.on_leave()
        self.current = name
        self.pages[name].tkraise()
        if hasattr(self.pages[name], "on_enter"):
            self.pages[name].on_enter()
        for n, (b, ind) in self.navb.items():
            on = n == name
            b.configure(fg_color=blend(T["accent"], T["panel"], .16) if on else "transparent",
                        text_color=T["text"] if on else T["muted"])
            ind.configure(fg_color=T["accent"] if on else "transparent")
        try:
            self.pages[name].refresh()
        except Exception as e:
            self.core.log(f"UI error: {e}")

    def toast(self, msg, kind="info"):
        col = {"ok": OK, "warn": WARN, "bad": BAD, "info": T["accent"]}.get(kind, T["accent"])
        f = ctk.CTkFrame(self, fg_color=T["panel2"], border_color=col, border_width=1, corner_radius=R)
        L(f, {"ok": "✓  ", "warn": "!  ", "bad": "✕  ", "info": "i  "}.get(kind, "") + msg, 12, wraplength=330, justify="left").pack(padx=14, pady=10)
        self.toasts.append(f)
        self.layout_toasts()
        self.after(4500, lambda: self.drop_toast(f))

    def drop_toast(self, f):
        if f in self.toasts:
            self.toasts.remove(f)
        try:
            f.destroy()
        except Exception:
            pass
        self.layout_toasts()

    def layout_toasts(self):
        self.update_idletasks()
        y = 18
        for f in self.toasts[-5:]:
            f.place(relx=1.0, x=-18, y=y, anchor="ne")
            f.lift()
            y += f.winfo_reqheight() + 8

    def pump(self):
        try:
            while True:
                fn = self.uiq.get_nowait()
                try:
                    fn()
                except Exception as e:
                    self.core.log(f"UI error: {e}")
        except queue.Empty:
            pass
        self.after(1000 if self.state() == "iconic" else 100, self.pump)

    def ui(self, fn):
        self.uiq.put(fn)

    def bg(self, work, done=None):
        def run():
            try:
                r, err = work(), None
            except Exception as e:
                r, err = None, e
            if done:
                self.ui(lambda: done(r, err))

        threading.Thread(target=run, daemon=True).start()

    def avatar(self, aid, user_id=None):
        if aid in self.avatars:
            return self.avatars[aid]
        if Image is None:
            return None
        self.avatars[aid] = None
        if user_id is None:
            r = self.core.db.one("SELECT user_id FROM accounts WHERE id=?", (aid,))
            user_id = r[0] if r else None
        if not user_id:
            return None

        def work():
            b = get_avatar(user_id)
            return Image.open(io.BytesIO(b)) if b else None

        def done(img, err):
            if img:
                self.avatars[aid] = ctk.CTkImage(circle(img), circle(img), size=(40, 40))
                if self.current == "Accounts":
                    self.pages["Accounts"].rebuild()

        self.bg(work, done)
        return None

    def root_hwnd(self):
        return user32.GetAncestor(self.winfo_id(), 2)

    def eco_self(self, on):
        if self.core.db.flag("self_eco") and getattr(self, "_eco", None) != on:
            self._eco = on
            os_set_eco(os.getpid(), on)

    def tick(self):
        db = self.core.db
        delay = db.num("ui_iv", 1, 10) * 1000
        try:
            iconic = self.state() == "iconic"
            idle = iconic or (user32.GetForegroundWindow() or 0) != (self.root_hwnd() or 0)
            self.eco_self(idle or db.flag("self_eco_always"))
            if idle and time.time() - getattr(self, "_trim_t", 0) > 120:
                self._trim_t = time.time()
                os_trim(os.getpid())  # hand back unused memory while nobody is looking
            while self.core.toastq:
                m, k = self.core.toastq.pop(0)
                self.toast(m, k)
            if not (iconic and db.flag("ui_pause")):
                self.pages[self.current].refresh()
                n = len(self.core.snapshot())
                self.dot.configure(text_color=OK if n else T["muted"])
                if not self.small:
                    self.status.configure(text=f" {n} running")
            if idle and db.flag("ui_pause"):
                delay = max(delay, 4000)
            if iconic:
                delay = max(delay, 8000)
        except Exception as e:
            self.core.log(f"UI error: {e}")
        self.after(delay, self.tick)

    def on_close(self):
        mv = self.pages.get("MultiView")
        if mv:
            try:
                mv.release_all()  # give embedded Roblox windows back before we exit
            except Exception:
                pass
        self.core.shutdown()
        self.destroy()


def _log_crash(text):
    try:
        f = APPDIR / "crash.log"
        if f.exists() and f.stat().st_size > 200_000:
            f.unlink()
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}]\n{text}\n")
    except Exception:
        pass


def install_crash_logging():
    import traceback
    sys.excepthook = lambda et, ev, tb: _log_crash("".join(traceback.format_exception(et, ev, tb)))
    threading.excepthook = lambda a: _log_crash("".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))


def unlock(core):
    ok, tries = [False], [0]
    w = ctk.CTk()
    w.title("RoManager locked")
    w.geometry("360x230")
    w.configure(fg_color=T["bg"])
    L(w, "◈", 30, True, color=T["accent"]).pack(pady=(22, 0))
    L(w, "RoManager is locked", 16, True).pack(pady=(0, 8))
    e = E(w, show="•", width=250, placeholder_text="Master password")
    e.pack()
    msg = L(w, "", 12, color=BAD)
    msg.pack(pady=4)

    def go(_=None):
        if verify_pw(e.get(), core.db.get("lock_hash")):
            ok[0] = True
            w.destroy()
        else:
            tries[0] += 1
            msg.configure(text=f"Wrong password ({tries[0]}/5)")
            e.delete(0, "end")
            if tries[0] >= 5:
                w.destroy()

    e.bind("<Return>", go)
    btn(w, "Unlock", go, "accent", w=120).pack(pady=6)
    e.focus_set()
    w.mainloop()
    return ok[0]


def main():
    if "--login" in sys.argv:
        login_window()
        return
    install_crash_logging()
    try:
        core = Core()
    except Exception as e:
        user32.MessageBoxW(0, f"Database unavailable:\n{e}", APP_NAME, 0x10)
        return
    apply_theme(core.db)
    if core.db.get("lock_hash") and not unlock(core):
        return
    sys.setswitchinterval(0.005)
    core.start()
    set_self_cap(core.db.num("app_cap", 5, 100))
    App(core).mainloop()


if __name__ == "__main__":
    main()
