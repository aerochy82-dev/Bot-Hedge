"""
OKX Hedge / Net Bot
===================
.env baru (opsional):
  OKX_DASH_TOKEN=rahasia123     -> wajib diisi kalau mau buka dashboard dari HP
  OKX_EMERGENCY_SL_PCT=0.03     -> SL darurat di exchange (3%), 0 = mati
  OKX_TG_TOKEN=123:ABC          -> token bot Telegram (dari @BotFather), kosong = Telegram mati
  OKX_TG_CHAT_ID=123456789      -> chat id Anda; hanya chat ini yang boleh memberi perintah
  OKX_LOCK_PORT=48787           -> port localhost pengunci satu instance (0 = tanpa pengunci)

Jalan di latar belakang (Windows): pythonw.exe okx_hedge_bot.fixed.py (lihat folder win).
Berhenti dengan rapi: buat file okx_stop.flag di folder bot (stop_bot.ps1 melakukannya).

Membaca position mode akun. Kalau masih net_mode (ganti hedge gagal
karena ada posisi/order), tutup pakai posSide=net. Kalau sudah
long_short_mode, tutup pakai posSide long/short.
"""
import json
import os
import queue
import re
import socket
import sys
import time
import logging
import urllib.error
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler
import threading
from datetime import datetime
from pathlib import Path

# pythonw.exe (tanpa jendela): sys.stdout/sys.stderr = None. Arahkan ke devnull supaya print/library tidak crash.
HAS_CONSOLE = sys.stderr is not None
if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _devnull
    if sys.stderr is None:
        sys.stderr = _devnull

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

try:
    from flask import Flask, jsonify, Response
    from okx import Account, Trade, MarketData, PublicData
except ImportError as e:
    raise SystemExit("Install dulu: pip install python-okx python-dotenv") from e

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
FLAG = os.getenv("OKX_FLAG", "1")
# Key terpisah untuk demo dan live (opsional). Kalau kosong, OKX_API_KEY dipakai untuk akun sesuai OKX_FLAG.
DEMO_KEYS = (os.getenv("OKX_DEMO_API_KEY", ""), os.getenv("OKX_DEMO_SECRET_KEY", ""), os.getenv("OKX_DEMO_PASSPHRASE", ""))
LIVE_KEYS = (os.getenv("OKX_LIVE_API_KEY", ""), os.getenv("OKX_LIVE_SECRET_KEY", ""), os.getenv("OKX_LIVE_PASSPHRASE", ""))


def keys_for(flag):
    """Kembalikan (key, secret, pass) untuk flag '1' (demo) atau '0' (live), atau None kalau tidak ada."""
    own = DEMO_KEYS if flag == "1" else LIVE_KEYS
    if all(own):
        return own
    if FLAG == flag and API_KEY and SECRET_KEY and PASSPHRASE:
        return (API_KEY, SECRET_KEY, PASSPHRASE)
    return None

INST_ID = os.getenv("OKX_INST_ID", "BTC-USDT-SWAP")
MARKETS = (
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP", "BNB-USDT-SWAP", "ADA-USDT-SWAP", "AVAX-USDT-SWAP",
    "LINK-USDT-SWAP", "SUI-USDT-SWAP", "TON-USDT-SWAP", "LTC-USDT-SWAP",
    "BCH-USDT-SWAP", "DOT-USDT-SWAP", "NEAR-USDT-SWAP", "APT-USDT-SWAP",
    "ARB-USDT-SWAP", "OP-USDT-SWAP", "PEPE-USDT-SWAP", "WIF-USDT-SWAP",
)
TD_MODE = os.getenv("OKX_TD_MODE", "cross")
LEVERAGE = os.getenv("OKX_LEVERAGE", "5")
ORDER_SIZE = os.getenv("OKX_ORDER_SIZE", "1")
TP_PCT = float(os.getenv("OKX_TP_PCT", "0.008"))
SL_PCT = float(os.getenv("OKX_SL_PCT", "0.005"))
LOOP_SEC = int(os.getenv("OKX_LOOP_SEC", "30"))
MAX_LONG = float(os.getenv("OKX_MAX_LONG", "3"))
MAX_SHORT = float(os.getenv("OKX_MAX_SHORT", "3"))
AUTO_CLOSE_ON_TPSL = os.getenv("OKX_AUTO_CLOSE", "1") == "1"
CLOSE_ON_WIN = os.getenv("OKX_CLOSE_ON_WIN", "1") == "1"
WIN_USDT = float(os.getenv("OKX_WIN_USDT", "0.3"))
COOLDOWN_SEC = int(os.getenv("OKX_COOLDOWN_SEC", "90"))
MAX_LOSS_USDT = float(os.getenv("OKX_MAX_LOSS", "2"))
ENTRY = os.getenv("OKX_ENTRY", "both")
# Candle untuk rentang grid "auto" (_swing). Pilihan timeframe di dashboard sudah dihapus.
SWING_BAR = "15m"
# Kunci lama "adaptive", "adaptiveEntry", "bar" di okx_settings.json diabaikan saat dimuat dan tidak ditulis lagi
# (nilai lama dibiarkan apa adanya di file, supaya kembali ke versi lama tetap aman).
GRID_MODES = ("off", "auto", "arith", "up", "down", "dca")
GRID = os.getenv("OKX_GRID", "auto")
if GRID not in GRID_MODES:
    GRID = "off"
GRID_STEP = float(os.getenv("OKX_GRID_STEP", "0"))
GRID_LEVELS = int(os.getenv("OKX_GRID_LEVELS", "4"))
# Mode DCA (OKX_GRID=dca): order awal dalam USDT, lalu safety order makin besar dan makin jauh.
DCA_BASE_USDT = float(os.getenv("OKX_DCA_BASE_USDT", "10"))
DCA_MULT = float(os.getenv("OKX_DCA_MULT", "1.5"))
DCA_STEP = float(os.getenv("OKX_DCA_STEP", "0.008"))
DCA_SCALE = float(os.getenv("OKX_DCA_SCALE", "1.3"))
DCA_MAX = int(os.getenv("OKX_DCA_MAX", "3"))
PORT = int(os.getenv("OKX_DASH_PORT", "8787"))
DASH_TOKEN = os.getenv("OKX_DASH_TOKEN", "").strip()
# Telegram (opsional). Token TIDAK pernah ditulis ke log.
TG_TOKEN = os.getenv("OKX_TG_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("OKX_TG_CHAT_ID", "").strip()
TG_STATUS_HOURS = float(os.getenv("OKX_TG_STATUS_HOURS", "6") or 0)
# Pengunci satu instance: port localhost ini di-bind selama bot hidup. 0 = tanpa pengunci.
LOCK_PORT = int(os.getenv("OKX_LOCK_PORT", "48787") or 0)
# SL darurat di sisi exchange (persen dari harga entry). 0 = mati.
EMERGENCY_SL_PCT = float(os.getenv("OKX_EMERGENCY_SL_PCT", "0.03"))
# Semua file data relatif ke folder skrip (bukan folder tempat perintah dijalankan),
# supaya setting/entry/log tidak "hilang" kalau bot dijalankan dari folder lain.
BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "okx_hedge_bot.log"
EQ_PATH = BASE_DIR / "okx_equity.json"
PRESET_PATH = BASE_DIR / "okx_preset.json"
SETTINGS_PATH = BASE_DIR / "okx_settings.json"
LOTS_PATH = BASE_DIR / "okx_lots.json"
STOP_FLAG = BASE_DIR / "okx_stop.flag"       # dibuat stop_bot.ps1: bot berhenti dengan rapi
COOL_PATH = BASE_DIR / "okx_cooldown.json"   # waktu tutup terakhir per sisi, supaya cooldown tetap jalan setelah restart
# Filter tren (mode DCA): sisi yang melawan tren kuat tidak menambah safety order (dan, opsional, tidak buka L0 baru).
# Tren = (EMA cepat - EMA lambat) / EMA lambat dari candle TREND_TF yang sudah selesai. Hasil backtest 180 hari:
# EMA 50/200 di 15m, ambang 0,4%, blokir L0 = hidup.
TREND_TFS = {"15m": 900, "1H": 3600}
TREND_ON = os.getenv("OKX_TREND", "1") == "1"
TREND_TF = os.getenv("OKX_TREND_TF", "15m")
TREND_FAST = int(os.getenv("OKX_TREND_FAST", "50"))
TREND_SLOW = int(os.getenv("OKX_TREND_SLOW", "200"))
TREND_TH = float(os.getenv("OKX_TREND_TH", "0.004"))
TREND_BLOCK_L0 = os.getenv("OKX_TREND_BLOCK_L0", "1") == "1"
# Filter SnR (mode DCA): safety order (L1 ke atas) tidak diisi kalau harga levelnya terlalu dekat support/resistance.
#   long : diblokir bila harga level ada di [R x (1 - zona), R]   (tepat di bawah resistance)
#   short: diblokir bila harga level ada di [S, S x (1 + zona)]   (tepat di atas support)
# Level: high/low hari sebelumnya (hari WIB, UTC+7) + high/low candle 4H terakhir yang sudah selesai (sumber "pd4"),
# atau hanya hari sebelumnya (sumber "pd"). Dihitung dari candle 1H publik OKX (tanpa API key), dicache 15 menit.
# Data gagal/basi: filter TIDAK memblokir. Entry awal (L0) tidak pernah diblokir filter ini.
# Backtest 180 hari (pd4, zona 0,6%, safety saja, + filter tren): IS +12,86% DD 3,73 | OOS +5,15% DD 1,91
# (tanpa SnR: IS +12,56% DD 4,54 | OOS +4,43% DD 2,11). Zona 0,4-0,5% lebih lemah di OOS: hasilnya belum kokoh.
SNR_SRCS = ("pd4", "pd")
SNR_ON = os.getenv("OKX_SNR", "1") == "1"
SNR_ZONE = float(os.getenv("OKX_SNR_ZONE", "0.006"))
SNR_SRC = os.getenv("OKX_SNR_SRC", "pd4")
SNR_REFRESH_SEC = 900
SNR_TZ_MS = 7 * 3_600_000          # hari dihitung dalam WIB (UTC+7), sama seperti backtest
SNR_DAY_MS = 86_400_000
SNR_H4_MS = 14_400_000             # candle 4H OKX: batas 00/04/08/12/16/20 UTC
# Filter channel regresi linier (mode DCA): safety order (L1 ke atas) tidak diisi kalau harga candle 1H
# terakhir yang sudah tutup terlalu jauh dari garis regresi (terentang).
#   long : diblokir bila z < -k   (di / di bawah band bawah)
#   short: diblokir bila z > +k   (di / di atas band atas)
# z = (close 1H terakhir - ujung garis) / std(residual), dari N candle 1H selesai (publik OKX).
# Entry awal (L0) tidak pernah diblokir. Data gagal/kurang: filter TIDAK memblokir.
# Default mati (lrOn=false) supaya setting lama aman; aktifkan lewat dashboard / okx_settings.json.
# Semantik = varian backtest "chan ext_so" (bt_linreg.py): N 100-200, k 2-2.5.
LR_ON = os.getenv("OKX_LR", "0") == "1"
LR_N = int(os.getenv("OKX_LR_N", "150"))
LR_K = float(os.getenv("OKX_LR_K", "2.0"))
LR_BAR = "1H"
LR_BAR_MS = 3_600_000
DCA_SPLIT = os.getenv("OKX_DCA_SPLIT", "0") == "1"
# Entry terpisah: TP/SL per level, masing-masing bisa arithmetic (tambah tetap) atau geometric (kali rasio)
#   arithmetic: nilai level k = dasar + k x tambahan     geometric: nilai level k = dasar x rasio^k
TP_MODE = os.getenv("OKX_TP_MODE", "geo")              # "arith" atau "geo"
SL_MODE = os.getenv("OKX_SL_MODE", "arith")
TP_ADD = float(os.getenv("OKX_TP_ADD", "0.002"))       # +0,2% per level (arith)
TP_RATIO = float(os.getenv("OKX_TP_RATIO", "1.25"))    # x1,25 per level (geo)
SL_ADD = float(os.getenv("OKX_SL_ADD", "0.005"))       # +0,5% per level (arith)
SL_RATIO = float(os.getenv("OKX_SL_RATIO", "1.3"))     # x1,3 per level (geo)


def load_settings():
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_setting(key, value):
    data = load_settings()
    data[key] = value
    try:
        SETTINGS_PATH.write_text(json.dumps(data), encoding="utf-8")
    except OSError as e:
        logging.getLogger().warning("Simpan setting gagal: %s", e)
LEV_CHOICES = (2, 3, 5, 10, 20)

# Preset siap pakai (mode DCA). Ukuran dalam USDT, jarak/tp/sl/emSl dalam fraksi (0.006 = 0,6%),
# jadi otomatis menyesuaikan di market mana pun.
PRESETS = {
    # baseUsdt = order awal (USDT notional). mult = pengali tiap safety. step = jarak safety pertama.
    # scale = pengali jarak tiap safety berikutnya. so = jumlah safety order. tp/sl dari harga rata-rata.
    "konservatif": {"label": "Konservatif", "baseUsdt": 10, "mult": 1.5, "step": 0.008, "scale": 1.3, "so": 3,
                    "tp": 0.008, "sl": 0.025, "maxLoss": 2.5, "cooldown": 300, "emSl": 0.06},
    "seimbang":    {"label": "Seimbang", "baseUsdt": 15, "mult": 1.4, "step": 0.006, "scale": 1.25, "so": 4,
                    "tp": 0.006, "sl": 0.025, "maxLoss": 4.5, "cooldown": 180, "emSl": 0.06},
    "agresif":     {"label": "Agresif", "baseUsdt": 20, "mult": 1.5, "step": 0.005, "scale": 1.2, "so": 5,
                    "tp": 0.005, "sl": 0.02, "maxLoss": 9.0, "cooldown": 120, "emSl": 0.07},
}

# Preset timeframe (hasil optimasi backtest per jalur harga 1m/5m/15m/1H). Hanya parameter strategi; ukuran (dcaBase),
# rugi maks, cooldown, maker dan ukuran-equity tidak disentuh. Fraksi: 0.005 = 0,5%.
TF_PRESETS = {'live': {'label': 'Live (asli)', 'step': 0.008, 'scale': 1.3, 'tp': 0.008, 'tpRatio': 1.25, 'sl': 0.04, 'trailOn': True, 'trailPct': 0.002, 'trendTh': 0.004, 'snrOn': True, 'lrOn': True}, '1m': {'label': '1M', 'step': 0.005, 'scale': 1.5, 'tp': 0.006, 'tpRatio': 1.4, 'sl': 0.06, 'trailOn': True, 'trailPct': 0.004, 'trendTh': 0.005, 'snrOn': False, 'lrOn': False}, '5m': {'label': '5M', 'step': 0.009, 'scale': 1.3, 'tp': 0.008, 'tpRatio': 1.25, 'sl': 0.06, 'trailOn': True, 'trailPct': 0.001, 'trendTh': 0.003, 'snrOn': False, 'lrOn': False}, '15m': {'label': '15M', 'step': 0.008, 'scale': 1.3, 'tp': 0.008, 'tpRatio': 1.25, 'sl': 0.04, 'trailOn': True, 'trailPct': 0.002, 'trendTh': 0.004, 'snrOn': True, 'lrOn': True}, '1h': {'label': '1JAM', 'step': 0.008, 'scale': 1.3, 'tp': 0.008, 'tpRatio': 1.25, 'sl': 0.04, 'trailOn': True, 'trailPct': 0.002, 'trendTh': 0.004, 'snrOn': True, 'lrOn': True}}
TFP_KEYS = (("step", "dca_step"), ("scale", "dca_scale"), ("tp", "base_tp"), ("tpRatio", "tp_ratio"), ("sl", "base_sl"),
            ("trailOn", "trail_on"), ("trailPct", "trail_pct"), ("trendTh", "trend_th"), ("snrOn", "snr_on"), ("lrOn", "lr_on"))
TFP_SET = {"step": "dcaStep", "scale": "dcaScale", "tp": "tpBase", "tpRatio": "tpRatio", "sl": "slBase", "trailOn": "trailOn",
           "trailPct": "trailPct", "trendTh": "trendTh", "snrOn": "snrOn", "lrOn": "lrOn"}

_log_handlers = [RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
if HAS_CONSOLE:  # di pythonw.exe tidak ada konsol: log hanya ke file
    _log_handlers.insert(0, logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger("okx-hedge")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
_CTX = threading.local()   # _CTX.eth = True di thread ETH live -> baris log diberi awalan [ETH]


class _EthLogTag(logging.Filter):
    def filter(self, record):
        if getattr(_CTX, "mute", False):
            return False
        if getattr(_CTX, "eth", False) and not str(record.msg).startswith("[ETH]"):
            record.msg = "[ETH] " + str(record.msg)
        return True


log.addFilter(_EthLogTag())


def _log_uncaught(exc_type, exc, tb):
    """Tanpa konsol, error yang tidak tertangkap harus masuk log (kalau tidak, hilang)."""
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        return sys.__excepthook__(exc_type, exc, tb)
    log.critical("Error tidak tertangani", exc_info=(exc_type, exc, tb))


def _log_thread_uncaught(args):
    if issubclass(args.exc_type, SystemExit):
        return
    log.critical("Error tidak tertangani di thread %s", getattr(args.thread, "name", "?"),
                 exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


sys.excepthook = _log_uncaught
threading.excepthook = _log_thread_uncaught

_INSTANCE_SOCK = None


def acquire_single_instance(port=None):
    """Bind port localhost. Gagal = bot lain (salinan kedua) sudah jalan. Dipanggil SEBELUM thread trading."""
    global _INSTANCE_SOCK
    port = LOCK_PORT if port is None else port
    if not port:
        return True
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: proses lain tidak bisa ikut bind port yang sama
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            pass
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        return False
    _INSTANCE_SOCK = s
    return True


def _mask(chat_id):
    c = str(chat_id)
    return ("..." + c[-4:]) if len(c) > 4 else c


class Telegram:
    """Notifikasi + perintah Telegram di thread sendiri. Tidak pernah memblokir atau menjatuhkan loop trading."""
    API = "https://api.telegram.org/bot{}/{}"

    def __init__(self, token="", chat_id="", sender=None, min_gap=1.0, maxsize=100):
        self.token = token or ""
        self.chat_id = str(chat_id or "").strip()
        self.enabled = bool(self.token and self.chat_id)
        self.q = queue.Queue(maxsize=maxsize)
        self.sender = sender or self._send_http   # bisa diganti (tes)
        self.min_gap = min_gap                    # jeda minimal antar pesan (detik)
        self.gate = lambda: True                  # notifikasi otomatis hanya kalau gate() True (setting tgOn)
        self.handler = None                       # fungsi(perintah) -> teks balasan
        self.sent = self.failed = self.dropped = 0
        self.last_err = ""
        self._seen = {}
        self._logged = {}
        self._mx = threading.Lock()
        self._stop = threading.Event()
        self._started = False

    # ---- util
    def _clean(self, e):
        txt = str(e)
        return txt.replace(self.token, "***") if self.token else txt

    def _log_every(self, key, every, level, fmt, *args):
        now = time.time()
        if now - self._logged.get(key, -1e18) >= every:
            self._logged[key] = now
            log.log(level, fmt, *args)

    def _gate_ok(self):
        try:
            return bool(self.gate())
        except Exception:
            return True

    def _api(self, method, params, timeout):
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(self.API.format(self.token, method), data=data)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _send_http(self, text):
        res = self._api("sendMessage", {"chat_id": self.chat_id, "text": text[:4000],
                                        "disable_web_page_preview": "true"}, 10)
        if not res.get("ok"):
            raise RuntimeError(res.get("description") or "sendMessage gagal")

    @staticmethod
    def _retry_info(e):
        """(boleh_ulang, tunggu_detik) dari error kirim."""
        if isinstance(e, urllib.error.HTTPError):
            if e.code == 429:
                try:
                    body = json.loads(e.read().decode("utf-8", "replace"))
                    return True, float((body.get("parameters") or {}).get("retry_after") or 5)
                except Exception:
                    return True, 5.0
            return e.code >= 500, 3.0   # 400/401/403/404: token/chat salah, percuma diulang
        return True, 3.0

    # ---- kirim
    def send(self, text, key=None, every=0.0, force=False):
        """Masukkan pesan ke antrean (tidak menunggu). key+every: pesan berkunci sama paling sering 1x per `every` detik.
        force=True: tetap dikirim walau notifikasi dimatikan (balasan perintah, tes)."""
        if not self.enabled or (not force and not self._gate_ok()):
            return False
        if key and every > 0:
            now = time.time()
            with self._mx:
                if now - self._seen.get(key, -1e18) < every:
                    return False
                self._seen[key] = now
                if len(self._seen) > 500:
                    for k in sorted(self._seen, key=self._seen.get)[:250]:
                        del self._seen[k]
        try:
            self.q.put_nowait(str(text))
            return True
        except queue.Full:
            self.dropped += 1
            self._log_every("tgfull", 600, logging.WARNING, "Antrean Telegram penuh, pesan dibuang")
            return False

    def _deliver(self, text):
        for attempt in range(3):
            try:
                self.sender(text)
                self.sent += 1
                return True
            except Exception as e:
                self.last_err = self._clean(e)[:300]
                again, wait = self._retry_info(e)
                if not again or attempt == 2:
                    break
                self._stop.wait(min(30.0, wait * (attempt + 1)))
        self.failed += 1
        self._log_every("tgsend", 600, logging.WARNING, "Telegram gagal kirim: %s", self.last_err)
        return False

    def _worker(self):
        last = 0.0
        while True:
            text = self.q.get()
            try:
                wait = self.min_gap - (time.time() - last)
                if wait > 0:
                    time.sleep(wait)
                self._deliver(text)
                last = time.time()
            except Exception as e:  # jangan pernah mati
                self._log_every("tgworker", 600, logging.WARNING, "Telegram worker error: %s", self._clean(e))
            finally:
                self.q.task_done()

    def flush(self, timeout=8.0):
        """Tunggu antrean kosong (dipakai saat berhenti)."""
        end = time.time() + timeout
        while self.q.unfinished_tasks and time.time() < end:
            time.sleep(0.1)
        return not self.q.unfinished_tasks

    # ---- perintah
    def handle_update(self, up):
        """Proses satu update getUpdates. Hanya chat yang dikonfigurasi yang dilayani. Kembalikan teks balasan."""
        msg = up.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = str(msg.get("text") or "").strip()
        if not text.startswith("/"):
            return None
        if not chat or chat != self.chat_id:
            self._log_every("tgchat:" + chat, 3600, logging.WARNING,
                            "Telegram: perintah dari chat lain (%s) diabaikan", chat)
            return None
        cmd = text.split()[0].split("@")[0].lower()
        try:
            reply = self.handler(cmd) if self.handler else None
        except Exception as e:
            log.exception("Perintah Telegram %s gagal", cmd)
            reply = f"Perintah {cmd} gagal: {e}"
        if reply:
            self.send(reply, force=True)
        return reply

    def _poll(self):
        offset = None
        try:  # buang perintah lama yang menumpuk saat bot mati (mis. /pause kemarin)
            res = self._api("getUpdates", {"timeout": 0, "offset": -1}, 15)
            ups = res.get("result") or []
            if ups:
                offset = int(ups[-1].get("update_id", 0)) + 1
        except Exception as e:
            self._log_every("tgpoll", 600, logging.WARNING, "Telegram getUpdates gagal: %s", self._clean(e))
        errs = 0
        while not self._stop.is_set():
            try:
                params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
                if offset is not None:
                    params["offset"] = offset
                res = self._api("getUpdates", params, 65)
                errs = 0
                for up in res.get("result") or []:
                    offset = int(up.get("update_id", 0)) + 1
                    self.handle_update(up)
            except Exception as e:
                errs += 1
                self._log_every("tgpoll", 600, logging.WARNING, "Telegram getUpdates gagal: %s", self._clean(e))
                self._stop.wait(min(60, 5 * errs))

    def start(self, poll=True):
        if not self.enabled:
            log.info("Telegram mati (OKX_TG_TOKEN / OKX_TG_CHAT_ID belum diisi di .env)")
            return False
        if self._started:
            return True
        self._started = True
        threading.Thread(target=self._worker, name="tg-send", daemon=True).start()
        if poll:
            threading.Thread(target=self._poll, name="tg-poll", daemon=True).start()
        log.info("Telegram aktif (chat %s), perintah: /status /pause /resume /help", _mask(self.chat_id))
        return True


TG = Telegram(TG_TOKEN, TG_CHAT_ID)


def tg_side(side):
    return "LONG" if side == "long" else "SHORT"


def tg_open_text(inst, side, sz, px, reason):
    icon = "\U0001F7E2" if side == "long" else "\U0001F534"
    pxt = f" @ ~{px}" if px else ""
    return f"{icon} BUKA {tg_side(side)} {sz}{pxt} | {reason} | {inst}"


def tg_close_text(inst, side, sz, px, reason, pnl=None):
    icon = "\u2705" if (pnl is None or pnl >= 0) else "\U0001F53B"
    pxt = f" @ ~{px}" if px else ""
    pnl_t = f" | PnL ~{pnl:+.4f} USDT" if pnl is not None else ""
    return f"{icon} TUTUP {tg_side(side)} {sz}{pxt} | {reason}{pnl_t} | {inst}"


class OKXHedgeBot:
    def __init__(self):
        self.flag = FLAG
        saved_acc = load_settings().get("account")
        if saved_acc in ("demo", "live"):
            want = "1" if saved_acc == "demo" else "0"
            if want != self.flag:
                if keys_for(want):
                    self.flag = want
                else:
                    logging.getLogger().warning("Akun tersimpan %s, tapi key-nya tidak ada di .env. Memakai %s.",
                                                saved_acc.upper(), "DEMO" if FLAG == "1" else "LIVE")
        if not keys_for(self.flag):
            raise SystemExit("Isi API key di .env")
        self.build_clients(self.flag)
        self.ct_val = 0.01
        self.lot_sz = 0.01
        self.min_sz = 0.01
        self.tick_sz = 0.1
        self.inst = INST_ID
        self.pos_mode = "net_mode"
        self.paused = False
        self.entry = "both" if ENTRY in ("oneway", "arah") else ENTRY
        self.grid = GRID
        self.grid_step_cfg = GRID_STEP
        self.grid_levels = max(2, GRID_LEVELS)
        self.step = 0.0
        self.grid_low = 0.0
        self.grid_high = 0.0
        self.auto_close = AUTO_CLOSE_ON_TPSL
        self.close_on_win = CLOSE_ON_WIN
        self.win_usdt = WIN_USDT
        self.order_size = float(ORDER_SIZE)
        self.max_long = MAX_LONG
        self.max_short = MAX_SHORT
        self.cooldown_sec = COOLDOWN_SEC
        self.max_loss = MAX_LOSS_USDT
        self.closed_at = {"long": 0.0, "short": 0.0}
        self.load_equity()
        self.base_tp = TP_PCT
        self.base_sl = SL_PCT
        self.emergency_sl = EMERGENCY_SL_PCT
        self.preset = ""
        self.leverage = str(LEVERAGE)
        self.dca_base = DCA_BASE_USDT
        self.dca_mult = DCA_MULT
        self.dca_step = DCA_STEP
        self.dca_scale = DCA_SCALE
        self.dca_max = DCA_MAX
        self.dca_state = {"long": {"n": 0, "last": 0.0}, "short": {"n": 0, "last": 0.0}}
        self.split = DCA_SPLIT
        self.tp_mode = TP_MODE if TP_MODE in ("arith", "geo") else "geo"
        self.sl_mode = SL_MODE if SL_MODE in ("arith", "geo") else "arith"
        self.tp_add = TP_ADD
        self.tp_ratio = TP_RATIO
        self.trail_on = False      # TP trailing (bot-side): trailOn / trailPct di okx_settings.json
        self.trail_pct = 0.002
        # maker (post-only) safety order + ukuran proporsional equity: default MATI (kunci hilang = perilaku lama)
        self.maker_on = False
        self.maker_fb = False
        self.mk_stats = {"placed": 0, "filled": 0, "partial": 0, "canceled": 0, "rejected": 0, "fallback": 0, "fill_sz": 0.0}
        self.eq_size_on = False
        self.eq_ref = 58.0
        self.eq_min = 0.5
        self.eq_max = 2.0
        self.sl_add = SL_ADD
        self.sl_ratio = SL_RATIO
        self.lots = {"long": {"anchor": 0.0, "lots": []}, "short": {"anchor": 0.0, "lots": []}}
        self.side_mgn = {"long": TD_MODE, "short": TD_MODE}  # mgnMode asli per sisi, dari get_positions
        self.trend_on = TREND_ON
        self.trend_tf = TREND_TF if TREND_TF in TREND_TFS else "15m"
        self.trend_fast = TREND_FAST
        self.trend_slow = TREND_SLOW
        self.trend_th = TREND_TH
        self.trend_block_l0 = TREND_BLOCK_L0
        self.trend_val = None        # (EMA cepat - EMA lambat) / EMA lambat, None = belum ada data
        self._trend_bars = {}        # ts candle selesai -> close
        self._trend_key = None
        self._trend_next = 0.0
        self._trend_ok_at = 0.0
        self._trend_backfilled = False
        self._trend_logged = {}
        self._trend_warned = False
        self.snr_on = SNR_ON
        self.snr_zone = SNR_ZONE
        self.snr_src = SNR_SRC if SNR_SRC in SNR_SRCS else "pd4"
        self.snr_lv = {}             # level SnR terakhir: pdH/pdL (+pdDay), h4H/h4L (+h4B), inst
        self._snr_next = 0.0
        self._snr_warned = False
        self._snr_partial_warned = False
        self._snr_logged = {}
        self._snr_shown = None
        self.snr_last_block = None   # (waktu, sisi, level, harga level, level SnR)
        self.lr_on = LR_ON
        self.lr_n = LR_N
        self.lr_k = LR_K
        self.lr = {}                 # mid/upper/lower/z/sd/n/ts/inst dari channel terakhir
        self._lr_next = 0.0
        self._lr_warned = False
        self._lr_logged = {}
        self._lr_shown = None
        self.lr_last_block = None    # (waktu, sisi, level, z, band)
        # Telegram
        self.tg_on = True                  # notifikasi otomatis
        self.tg_status_hours = TG_STATUS_HOURS  # status berkala tiap N jam, 0 = mati
        self.tg_trades = True              # notifikasi buka/tutup order
        self.last_px = 0.0
        self.last_fail = ""
        self._tick_ok = True
        self._fail_streak = 0
        self._fail_alerted = False
        self._tg_prev = {}                 # sisi -> (size, avg, upl) putaran sebelumnya
        self._tg_bot_close = {"long": 0.0, "short": 0.0}
        self._tg_trend = None
        self._tg_status_at = time.time()
        self.lock = threading.Lock()
        log.info(
            "Bot siap | %s | %s | td=%s | lev=%sx",
            self.inst, "DEMO" if self.flag == "1" else "LIVE", TD_MODE, self.leverage,
        )

    # TP/SL dipakai apa adanya (fitur TP/SL adaptive dihapus): selalu sama dengan tpBase/slBase.
    @property
    def tp_pct(self):
        return self.base_tp

    @property
    def sl_pct(self):
        return self.base_sl

    def refresh_config(self):
        result = self.account.get_account_config()
        if result.get("code") != "0":
            log.warning("config gagal: %s", result)
            return
        row = (result.get("data") or [{}])[0]
        self.pos_mode = row.get("posMode") or self.pos_mode
        log.info("posMode akun = %s | acctLv = %s", self.pos_mode, row.get("acctLv"))

    def set_hedge_mode(self):
        self.refresh_config()
        if self.pos_mode == "long_short_mode":
            log.info("Sudah hedge mode, skip set-position-mode")
            return
        result = self.account.set_position_mode(posMode="long_short_mode")
        if result.get("code") != "0":
            log.warning(
                "Tidak bisa ganti hedge (%s). Tutup semua posisi dan order dulu, atau biarkan bot jalan di mode saat ini.",
                result.get("msg") or result,
            )
        self.refresh_config()

    def set_leverage(self, pos_side=None):
        params = {"instId": self.inst, "lever": str(self.leverage), "mgnMode": TD_MODE}
        if pos_side and self.pos_mode == "long_short_mode":
            params["posSide"] = pos_side
        result = self.account.set_leverage(**params)
        if result.get("code") == "0":
            log.info("Leverage %sx diset %s", self.leverage, pos_side or "")
        else:
            log.warning("Set leverage: %s", result.get("msg") or result)
        return result

    def net_upl(self, side, pos, last):
        """uPnL dikurangi perkiraan fee buka + tutup (taker ~0,05% x 2)."""
        size = pos[side]
        if size <= 0:
            return 0.0
        return pos[side + "_upl"] - last * self.ct_val * size * 0.001

    def close_winners(self, sides=("long", "short")):
        """Tutup sisi yang untung bersih (setelah fee). Kembalikan daftar teks hasil."""
        pos = self.get_positions()
        t = self.get_ticker()
        if not t:
            return False, "Harga gagal dibaca, tidak ada yang ditutup"
        done, skipped = [], []
        for side in sides:
            if pos[side] <= 0:
                continue
            net = self.net_upl(side, pos, t["last"])
            if net > 0:
                log.info("CLOSE WIN %s uPnL %.4f bersih ~%.4f", side, pos[side + "_upl"], net)
                (self.close_long if side == "long" else self.close_short)(reason="manual win (dashboard)")
                done.append(f"{side} +{net:.4f}")
            else:
                skipped.append(f"{side} {net:+.4f}")
        if not done:
            why = (" (" + ", ".join(skipped) + ")") if skipped else ""
            return False, "Tidak ada posisi yang untung bersih" + why
        msg = "Ditutup: " + ", ".join(done)
        if skipped:
            msg += " | dibiarkan: " + ", ".join(skipped)
        return True, msg

    def apply_leverage(self):
        """Pasang self.leverage ke OKX. Kembalikan (ok, pesan error)."""
        sides = ["long", "short"] if (self.pos_mode == "long_short_mode" and TD_MODE == "isolated") else [None]
        for side in sides:
            r = self.set_leverage(side)
            if r.get("code") != "0":
                return False, (r.get("msg") or str(r))
        return True, ""

    def change_leverage(self, lev):
        pos = self.get_positions()
        if not pos["ok"]:
            return False, "Posisi gagal dibaca dari OKX, coba lagi."
        if pos["long"] > 0 or pos["short"] > 0:
            return False, "Tutup semua posisi dulu sebelum ganti leverage."
        old = self.leverage
        self.leverage = str(lev)
        ok, err = self.apply_leverage()
        if not ok:
            self.leverage = old
            self.apply_leverage()
            return False, f"OKX menolak leverage {lev}x: {err}"
        try:
            save_setting("leverage", self.leverage)
        except OSError as e:
            log.warning("Simpan leverage gagal: %s", e)
        log.info("Leverage diganti %sx -> %sx", old, self.leverage)
        return True, f"Leverage {self.leverage}x"

    def apply_preset(self, name, save=True):
        p = PRESETS.get(name)
        if not p:
            return False, "preset tidak dikenal"
        self.dca_base = p["baseUsdt"]
        self.dca_mult = p["mult"]
        self.dca_step = p["step"]
        self.dca_scale = p["scale"]
        self.dca_max = p["so"]
        self.max_loss = p["maxLoss"]
        self.cooldown_sec = p["cooldown"]
        self.base_tp = p["tp"]
        self.base_sl = p["sl"]
        self.emergency_sl = p["emSl"]
        self.close_on_win = False
        self.grid = "dca"
        self.grid_low = 0.0
        self.grid_high = 0.0
        self.sync_dca_sizes()
        self.preset = name
        if save:
            try:
                PRESET_PATH.write_text(json.dumps({"preset": name}), encoding="utf-8")
            except OSError as e:
                log.warning("Simpan preset gagal: %s", e)
        msg = (f"Preset {p['label']} (DCA): awal {p['baseUsdt']:g} USDT, {p['so']} safety x{p['mult']:g}, "
               f"jarak {p['step']*100:.2f}% x{p['scale']:g}, TP {p['tp']*100:.2f}% SL {p['sl']*100:.2f}%, "
               f"rugi maks {p['maxLoss']:g}, cooldown {p['cooldown']}s, SL darurat {p['emSl']*100:.1f}%")
        log.info(msg)
        return True, msg

    # ---------- DCA ----------
    def dca_contracts(self, k, price):
        """Ukuran order ke-k (0 = order awal) dalam kontrak, dibulatkan ke lot terdekat."""
        if price <= 0 or self.ct_val <= 0:
            return self.min_sz
        usdt = self.dca_base * (self.dca_mult ** k)
        raw = usdt / (self.ct_val * price)
        steps = max(1, round(raw / self.lot_sz))
        return max(self.min_sz, steps * self.lot_sz)

    # ---------- ukuran proporsional equity (eqSizeOn): dcaBase x equity/eqRef, dijepit eqSizeMin..eqSizeMax, dihitung di L0
    def eq_base(self):
        try:
            eq = float(self.equity[-1].get("eq") or 0) if self.equity else 0.0
        except (TypeError, ValueError, AttributeError):
            eq = 0.0
        if eq <= 0 or self.eq_ref <= 0:
            return float(self.dca_base)
        f = min(float(self.eq_max), max(float(self.eq_min), eq / float(self.eq_ref)))
        return float(self.dca_base) * f

    def contracts_for(self, base, k, price):
        if price <= 0 or self.ct_val <= 0:
            return self.min_sz
        raw = base * (self.dca_mult ** k) / (self.ct_val * price)
        steps = max(1, round(raw / self.lot_sz))
        return max(self.min_sz, steps * self.lot_sz)

    def side_contracts(self, side, k, price):
        if not self.eq_size_on:
            return self.dca_contracts(k, price)
        base = (self.lots.get(side) or {}).get("baseEff") or self.eq_base()
        return self.contracts_for(float(base), k, price)

    # ---------- maker (post-only) untuk safety order L1+ (makerOn). L0 + exit trailing tetap market.
    def mk_tag(self):
        return "hbmk" + ("e" if str(self.inst).startswith("ETH") else "b")

    def maker_ok(self):
        return True

    def maker_active(self):
        return (bool(self.maker_on) and not self.paused and self.entry == "both" and self.pos_mode == "long_short_mode"
                and self.split_active() and self.maker_ok())

    def _mk_pending(self):
        try:
            r = self.trade.get_order_list(instType="SWAP", instId=self.inst)
        except Exception as e:
            log.warning("Maker: daftar order gagal: %s", e)
            return None
        if r.get("code") != "0":
            log.warning("Maker: daftar order gagal: %s", self._resp_txt(r))
            return None
        tag = self.mk_tag()
        return {o.get("clOrdId"): o for o in (r.get("data") or [])
                if str(o.get("clOrdId") or "").startswith(tag) and o.get("instId", self.inst) == self.inst}

    def _mk_get(self, cl):
        try:
            r = self.trade.get_order(instId=self.inst, clOrdId=cl)
        except Exception as e:
            log.warning("Maker: status order %s gagal: %s", cl, e)
            return None
        if r.get("code") == "0" and r.get("data"):
            return r["data"][0]
        if str(r.get("code")) in ("51603",):   # order tidak ada di OKX
            return {"state": "canceled", "accFillSz": None}
        return None

    def _mk_adopt(self, side, rec, o):
        """Isi maker diambil dari status order (accFillSz/avgPx), bukan dari ticker."""
        try:
            acc = float(o.get("accFillSz")) if o.get("accFillSz") not in (None, "") else rec["fill"]
        except (TypeError, ValueError):
            acc = rec["fill"]
        delta = round(acc - rec["fill"], 8)
        if delta <= self.lot_sz / 2:
            return False
        try:
            avg = float(o.get("avgPx") or 0) or rec["px"]
        except (TypeError, ValueError):
            avg = rec["px"]
        book = self.lots[rec["side"] if "side" in rec else side]
        lot = next((l for l in book["lots"] if l.get("mk") == rec["cl"]), None)
        if lot is None:
            lot = {"lvl": rec["lvl"], "entry": avg, "sz": round(acc, 8), "mk": rec["cl"]}
            book["lots"].append(lot)
        else:
            lot["entry"], lot["sz"] = avg, round(acc, 8)   # avgPx OKX = rata-rata semua isi order ini
        rec["fill"] = acc
        self.mk_stats["fill_sz"] = round(self.mk_stats["fill_sz"] + delta, 8)
        full = acc >= rec["sz"] - self.lot_sz / 2
        self.mk_stats["filled" if full else "partial"] += 1
        log.info("ENTRY %s L%d sz=%s level=%.4f px=%.4f (maker %s%s)", side, rec["lvl"], self.fmt_size(delta), rec["px"], avg,
                 rec["cl"], "" if full else f", sebagian {self.fmt_size(acc)}/{self.fmt_size(rec['sz'])}")
        self.notify(tg_open_text(self.inst, side, self.fmt_size(delta), self.fmt_px(avg),
                                 f"L{rec['lvl']} safety (maker{'' if full else ', sebagian'})"), trade=True)
        return True

    def _mk_desired(self, side, last):
        book = self.lots[side]
        if not book.get("anchor") or not book.get("lots"):
            return None
        rec = book.get("mk")
        filled = {l["lvl"] for l in book["lots"] if not (rec and l.get("mk") == rec.get("cl"))}
        locked = set(book.get("locked") or [])
        k = next((k for k in range(1, self.dca_max + 1) if k not in filled and k not in locked), None)
        if k is None:
            return None
        lp = self.level_px(side, k)
        if lp <= 0:
            return None
        if (side == "long" and last <= lp) or (side == "short" and last >= lp):
            return None          # harga sudah tembus level: post-only akan ditolak (fallback taker diurus split_entries)
        sz = self.side_contracts(side, k, lp)
        if self.trend_hit(side, k) or self.snr_near(side, k, lp) is not None or self.lr_hit(side, k) or self.extra_block(side, sz):
            return None
        return (k, float(self.fmt_px(lp)), float(self.fmt_size(sz)))

    def _mk_place(self, side, want):
        k, px, sz = want
        n = self.mk_stats["placed"] + self.mk_stats["rejected"] + 1
        cl = f"{self.mk_tag()}{side[0]}{k}{int(time.time()) % 100000000}{n % 1000}"
        params = {"instId": self.inst, "tdMode": self.side_mgn.get(side, TD_MODE), "side": "buy" if side == "long" else "sell",
                  "posSide": side, "ordType": "post_only", "px": self.fmt_px(px), "sz": self.fmt_size(sz), "clOrdId": cl}
        try:
            r = self.trade.place_order(**params)
        except Exception as e:
            r = {"code": "-1", "msg": str(e)}
        d = (r.get("data") or [{}])[0] if isinstance(r.get("data"), list) else {}
        if r.get("code") == "0" and str(d.get("sCode", "0")) == "0":
            self.lots[side]["mk"] = {"cl": cl, "ordId": d.get("ordId"), "lvl": k, "px": px, "sz": sz, "fill": 0.0, "t": time.time()}
            self.mk_stats["placed"] += 1
            log.info("MAKER %s L%d post-only %s @ %s dipasang (%s)", side, k, params["sz"], params["px"], cl)
            return True
        self.mk_stats["rejected"] += 1
        log.warning("MAKER %s L%d ditolak (coba lagi putaran berikutnya): %s", side, k,
                    d.get("sMsg") or r.get("msg") or self._resp_txt(r))
        return False

    def _mk_cancel(self, cls):
        if not cls:
            return
        try:
            r = self.trade.cancel_multiple_orders([{"instId": self.inst, "clOrdId": c} for c in cls])
            if r.get("code") not in ("0", "2"):
                log.warning("Maker: batal order gagal: %s", self._resp_txt(r))
        except Exception as e:
            log.warning("Maker: batal order gagal: %s", e)

    def maker_tick(self, last):
        """Satu kali per putaran: 1 panggilan daftar order (+ status order yang hilang, pasang/batal seperlunya).
        Return True kalau ada isi baru yang diadopsi (posisi perlu dibaca ulang)."""
        active = self.maker_active()
        recs = [sd for sd in ("long", "short") if self.lots[sd].get("mk")]
        if not active and not recs and not getattr(self, "_mk_scan", False):
            return False
        pend = self._mk_pending()
        if pend is None:
            return False
        self._mk_scan = False
        adopted, changed = False, False
        for sd in ("long", "short"):
            rec = self.lots[sd].get("mk")
            if not rec:
                continue
            o = pend.get(rec["cl"])
            if o is not None:
                if self._mk_adopt(sd, rec, o):
                    adopted = changed = True
                continue
            o = self._mk_get(rec["cl"])          # tidak lagi pending: ambil status akhir
            if o is None:
                continue
            if self._mk_adopt(sd, rec, o):
                adopted = True
            st = str(o.get("state") or "")
            if st in ("filled", "canceled", "mmp_canceled") or not st:
                if st != "filled":
                    self.mk_stats["canceled"] += 1
                self.lots[sd].pop("mk", None)
            changed = True
        known = {self.lots[sd]["mk"]["cl"] for sd in ("long", "short") if self.lots[sd].get("mk")}
        cancel = [c for c in pend if c not in known]       # order bertanda bot tanpa catatan (yatim) -> batalkan
        if cancel:
            log.warning("Maker: %d order yatim bertanda bot dibatalkan: %s", len(cancel), cancel)
        for sd in ("long", "short"):
            rec = self.lots[sd].get("mk")
            want = self._mk_desired(sd, last) if active else None
            if rec:
                if rec.get("cxl"):
                    continue
                if want is None or (want[0], want[1], want[2]) != (rec["lvl"], rec["px"], rec["sz"]):
                    rec["cxl"] = True
                    cancel.append(rec["cl"])
                    changed = True
            elif want is not None:
                if self._mk_place(sd, want):
                    changed = True
        self._mk_cancel(cancel)
        if changed:
            self.save_lots()
        if adopted:
            for sd in ("long", "short"):
                if any(l.get("mk") for l in self.lots[sd]["lots"]):
                    pass
            self._mk_sl_dirty = True
        return adopted

    def maker_cancel_all(self, reason, sides=("long", "short")):
        """Batalkan order maker milik bot (berdasarkan clOrdId bertanda + instId; tidak pernah cancel global)."""
        recs = [sd for sd in sides if self.lots.get(sd, {}).get("mk")]
        if not recs and not (self.maker_on and len(sides) == 2):
            return
        pend = self._mk_pending()
        if pend is None:
            return
        cls = []
        for sd in recs:
            rec = self.lots[sd]["mk"]
            rec["cxl"] = True
            cls.append(rec["cl"])
        if len(sides) == 2:
            cls += [c for c in pend if c not in cls]
        cls = [c for c in cls if c in pend]
        if cls:
            log.info("Maker: %d order dibatalkan (%s)", len(cls), reason)
            self._mk_cancel(cls)
        self.save_lots()

    def maker_info(self):
        st = dict(self.mk_stats)
        st["fillRate"] = round(st["filled"] / st["placed"] * 100, 1) if st["placed"] else None
        return {"on": bool(self.maker_on), "fallback": bool(self.maker_fb), "active": self.maker_active() if self.split_active() else False,
                "stats": st, "orders": {sd: {k: v for k, v in (self.lots[sd].get("mk") or {}).items() if k != "t"} or None
                                        for sd in ("long", "short")}}

    def eq_info(self):
        return {"on": bool(self.eq_size_on), "ref": self.eq_ref, "min": self.eq_min, "max": self.eq_max,
                "baseNow": round(self.eq_base(), 3) if self.eq_size_on else float(self.dca_base), "dcaBase": float(self.dca_base),
                "baseEff": {sd: (self.lots[sd].get("baseEff") if self.eq_size_on else None) for sd in ("long", "short")}}

    def dca_gap(self, k):
        """Jarak (fraksi) dari harga isi terakhir untuk safety ke-k (k >= 1)."""
        return self.dca_step * (self.dca_scale ** max(0, k - 1))

    def sync_dca_sizes(self):
        """Isi order_size / max_long / max_short dari rencana DCA, untuk tombol manual & tampilan."""
        t = self.get_ticker()
        px = t["last"] if t else 0
        if px <= 0:
            return
        sizes = [self.dca_contracts(k, px) for k in range(self.dca_max + 1)]
        self.order_size = sizes[0]
        self.max_long = self.max_short = round(sum(sizes), 6)

    def dca_restore(self, side, size, avg):
        """Bot restart dengan posisi terbuka: tebak berapa order yang sudah terisi."""
        st = self.dca_state[side]
        if st["n"] > 0 or size <= 0:
            return
        total, n = 0.0, 0
        while n <= self.dca_max and total + 1e-9 < size:
            total += self.dca_contracts(n, avg)
            n += 1
        st["n"], st["last"] = max(1, n), avg
        log.info("DCA %s dipulihkan: %d order terisi (perkiraan), acuan %.4f", side, st["n"], avg)

    def dca_next(self, side, pos, last):
        """Kembalikan (boleh, ukuran, harga_pemicu) untuk order DCA berikutnya."""
        size = pos[side]
        avg = pos[side + "_avg"]
        st = self.dca_state[side]
        if size <= 0:
            st["n"], st["last"] = 0, 0.0
            return True, self.dca_contracts(0, last), None
        self.dca_restore(side, size, avg)
        n = st["n"]
        if n > self.dca_max:
            return False, 0, None
        gap = self.dca_gap(n)
        ref = st["last"] or avg
        trigger = ref * (1 - gap) if side == "long" else ref * (1 + gap)
        ok = last <= trigger if side == "long" else last >= trigger
        return ok, self.dca_contracts(n, ref), trigger

    def dca_tick(self, pos, last):
        for side in ("long", "short"):
            size = pos[side]
            if size <= 0:
                waited = time.time() - self.closed_at.get(side, 0)
                if waited < self.cooldown_sec:
                    log.info("Skip %s: cooldown %.0fs", side, self.cooldown_sec - waited)
                    continue
            if self.trend_blocks(side, self.dca_state[side]["n"] if size > 0 else 0):
                continue
            ok, sz, trigger = self.dca_next(side, pos, last)
            st = self.dca_state[side]
            if not ok:
                if trigger is None:
                    log.info("Skip %s: safety order habis (%d/%d)", side, st["n"] - 1, self.dca_max)
                else:
                    log.info("Skip %s: tunggu harga %.4f untuk safety %d", side, trigger, st["n"])
                continue
            if st["n"] >= 1 and self.snr_blocks(side, st["n"], trigger or last):
                continue
            if st["n"] >= 1 and self.lr_blocks(side, st["n"]):
                continue
            label = "order awal" if st["n"] == 0 else f"safety {st['n']}/{self.dca_max}"
            log.info("DCA %s %s sz=%s px=%.4f", side, label, self.fmt_size(sz), last)
            why = "DCA " + label
            res = self.open_long(sz, why, last) if side == "long" else self.open_short(sz, why, last)
            if isinstance(res, dict) and res.get("code") == "0":
                st["n"] += 1
                st["last"] = last

    # ---------- DCA entry terpisah (TP per entry, level diisi ulang) ----------
    # ---------- simpan semua setting dashboard supaya tidak hilang saat restart ----------
    STATE = (
        ("grid", "grid", str), ("gridStep", "grid_step_cfg", float),
        ("orderSize", "order_size", float), ("maxLong", "max_long", float), ("maxShort", "max_short", float),
        ("winUsdt", "win_usdt", float), ("maxLoss", "max_loss", float), ("cooldown", "cooldown_sec", int),
        ("tpBase", "base_tp", float), ("slBase", "base_sl", float), ("emSl", "emergency_sl", float),
        ("autoClose", "auto_close", bool), ("closeOnWin", "close_on_win", bool), ("entry", "entry", str), ("paused", "paused", bool),
        ("dcaBase", "dca_base", float), ("dcaMult", "dca_mult", float), ("dcaStep", "dca_step", float),
        ("dcaScale", "dca_scale", float), ("dcaMax", "dca_max", int), ("split", "split", bool),
        ("tpMode", "tp_mode", str), ("slMode", "sl_mode", str), ("tpAdd", "tp_add", float),
        ("tpRatio", "tp_ratio", float), ("slAdd", "sl_add", float), ("slRatio", "sl_ratio", float),
        ("trendOn", "trend_on", bool), ("trendTf", "trend_tf", str), ("trendFast", "trend_fast", int),
        ("trendSlow", "trend_slow", int), ("trendTh", "trend_th", float), ("trendBlockL0", "trend_block_l0", bool),
        ("tgOn", "tg_on", bool), ("tgStatusHours", "tg_status_hours", float), ("tgTrades", "tg_trades", bool),
        ("snrOn", "snr_on", bool), ("snrZone", "snr_zone", float), ("snrSrc", "snr_src", str),
        ("lrOn", "lr_on", bool), ("lrN", "lr_n", int), ("lrK", "lr_k", float),
        ("trailOn", "trail_on", bool), ("trailPct", "trail_pct", float),
        ("makerOn", "maker_on", bool), ("makerFallbackTaker", "maker_fb", bool),
        ("eqSizeOn", "eq_size_on", bool), ("eqRef", "eq_ref", float), ("eqSizeMin", "eq_min", float), ("eqSizeMax", "eq_max", float),
    )

    def save_state(self):
        data = load_settings()
        for key, attr, _ in self.STATE:
            if hasattr(self, attr):
                data[key] = getattr(self, attr)
        data["inst"] = self.inst
        try:
            SETTINGS_PATH.write_text(json.dumps(data), encoding="utf-8")
        except OSError as e:
            log.warning("Simpan setting gagal: %s", e)

    def load_state(self):
        data = load_settings()
        n = 0
        for key, attr, cast in self.STATE:
            if key in data and hasattr(self, attr):
                try:
                    setattr(self, attr, cast(data[key]))
                    n += 1
                except (TypeError, ValueError):
                    pass
        if self.grid not in GRID_MODES:
            self.grid = "off"
        self.sanitize_trend()
        self.sanitize_snr()
        self.sanitize_lr()
        self.sanitize_trail()
        self.tg_status_hours = max(0.0, min(168.0, float(self.tg_status_hours or 0)))
        if n:
            log.info("Setting dashboard dimuat (%d nilai, menimpa .env): grid=%s TP %.2f%% SL %.2f%% "
                     "entry terpisah=%s | TP trailing %s", n, self.grid, self.base_tp * 100, self.base_sl * 100,
                     "hidup" if self.split else "mati",
                     f"HIDUP {self.trail_pct * 100:.2f}%" if self.trail_on else "mati")
        return data.get("inst")

    # ---------- akun demo / live ----------
    def build_clients(self, flag):
        k, sec, pw = keys_for(flag)
        self.account = Account.AccountAPI(k, sec, pw, False, flag)
        self.trade = Trade.TradeAPI(k, sec, pw, False, flag)
        self.market = MarketData.MarketAPI(flag=flag)
        self.public = PublicData.PublicAPI(flag=flag)

    def eq_path(self):
        return BASE_DIR / ("okx_equity_demo.json" if self.flag == "1" else "okx_equity_live.json")

    def load_equity(self):
        self.equity = []
        path = self.eq_path()
        if not path.exists() and self.flag == "1" and EQ_PATH.exists():
            path = EQ_PATH  # file lama
        try:
            if path.exists():
                self.equity = json.loads(path.read_text(encoding="utf-8"))[-400:]
        except (OSError, json.JSONDecodeError):
            self.equity = []

    def switch_account(self, target):
        """target: 'demo' atau 'live'. Hanya boleh saat tidak ada posisi di akun sekarang."""
        new_flag = "1" if target == "demo" else "0"
        if new_flag == self.flag:
            return True, f"Sudah di akun {target.upper()}"
        if not keys_for(new_flag):
            return False, (f"API key {target.upper()} belum ada di .env. Isi OKX_{target.upper()}_API_KEY, "
                           f"OKX_{target.upper()}_SECRET_KEY, OKX_{target.upper()}_PASSPHRASE lalu restart bot.")
        pos = self.get_positions()
        if not pos["ok"]:
            return False, "Posisi gagal dibaca dari OKX, tidak pindah akun."
        if pos["long"] > 0 or pos["short"] > 0:
            return False, (f"Masih ada posisi di akun {'DEMO' if self.flag == '1' else 'LIVE'} "
                           f"(L {pos['long']:g} / S {pos['short']:g}). Tutup dulu sebelum pindah akun.")
        old_flag = self.flag
        try:
            self.build_clients(new_flag)
            r = self.account.get_account_config()
            if r.get("code") != "0":
                raise RuntimeError(r.get("msg") or str(r))
        except Exception as e:
            self.build_clients(old_flag)
            return False, f"Gagal masuk akun {target.upper()}: {e}. Tetap di akun lama."
        self.flag = new_flag
        self.paused = True  # aman: tidak buka posisi baru sampai Anda lanjutkan sendiri
        self.refresh_config()
        self.set_hedge_mode()
        self.load_spec()
        self.apply_leverage()
        self.dca_state = {"long": {"n": 0, "last": 0.0}, "short": {"n": 0, "last": 0.0}}
        self.load_lots()
        self.load_cooldown()
        self.load_equity()
        if self.grid == "dca":
            self.sync_dca_sizes()
        save_setting("account", target)
        bal = self.get_balance_usdt()
        npos = self.get_positions()
        extra = ""
        if npos["long"] > 0 or npos["short"] > 0:
            extra = f" Ada posisi terbuka di akun ini (L {npos['long']:g} / S {npos['short']:g}), akan dikelola bot."
        log.warning("=== PINDAH AKUN: %s -> %s | saldo USDT %s | bot DIJEDA ===",
                    "DEMO" if old_flag == "1" else "LIVE", target.upper(), bal)
        return True, f"Sekarang di akun {target.upper()}, saldo {bal} USDT. Bot dijeda, cek setting lalu tekan lanjut.{extra}"

    # ---------- filter tren ----------
    def sanitize_trend(self):
        if self.trend_tf not in TREND_TFS:
            self.trend_tf = "15m"
        self.trend_fast = max(2, min(400, int(self.trend_fast)))
        self.trend_slow = max(3, min(500, int(self.trend_slow)))
        if self.trend_fast >= self.trend_slow:
            self.trend_fast, self.trend_slow = TREND_FAST, TREND_SLOW
        self.trend_th = max(0.0, min(0.05, float(self.trend_th)))

    def _trend_fetch(self, fn, **kw):
        try:
            r = fn(instId=self.inst, bar=self.trend_tf, **kw)
        except Exception as e:
            log.warning("Candle tren gagal: %s", e)
            return None
        if r.get("code") != "0":
            log.warning("Candle tren gagal: %s", r.get("msg") or r)
            return None
        n = 0
        for row in r.get("data") or []:
            try:
                if len(row) > 8 and str(row[8]) != "1":
                    continue  # candle belum selesai: tidak dipakai (tanpa melihat ke depan)
                self._trend_bars[int(row[0])] = float(row[4])
                n += 1
            except (TypeError, ValueError, IndexError):
                continue
        return r.get("data") or []

    def refresh_trend(self, force=False):
        """Perbarui nilai tren dari candle yang sudah selesai. Kalau data gagal/basi: filter tidak memblokir."""
        if not self.trend_on:
            self.trend_val = None
            return
        tf_s = TREND_TFS.get(self.trend_tf, 900)
        key = (self.inst, self.trend_tf)
        if key != self._trend_key:
            self._trend_key, self._trend_bars, self._trend_next, self._trend_backfilled = key, {}, 0.0, False
        now = time.time()
        if force or now >= self._trend_next:
            data = self._trend_fetch(self.market.get_candlesticks, limit="300")
            if data is not None:
                need = 4 * self.trend_slow
                pages = 0
                while not self._trend_backfilled and len(self._trend_bars) < need and pages < 15:
                    oldest = min(self._trend_bars) if self._trend_bars else ""
                    got = self._trend_fetch(self.market.get_history_candlesticks, after=str(oldest), limit="100")
                    pages += 1
                    if not got:
                        break
                    time.sleep(0.12)
                self._trend_backfilled = True
                if len(self._trend_bars) > 2000:
                    for t in sorted(self._trend_bars)[:-2000]:
                        del self._trend_bars[t]
                if self._trend_bars:
                    last_ts = max(self._trend_bars) / 1000
                    self._trend_next = last_ts + 2 * tf_s + 5  # candle berikutnya selesai
                    self._trend_ok_at = now
            else:
                self._trend_next = now + 60
        closes = [self._trend_bars[t] for t in sorted(self._trend_bars)]
        stale = now - self._trend_ok_at > 3 * tf_s
        if len(closes) <= self.trend_slow or stale:
            if not self._trend_warned:
                log.warning("Filter tren tidak aktif sementara (data %d candle%s): tidak memblokir entry",
                            len(closes), ", basi" if stale else "")
                self._trend_warned = True
            self.trend_val = None
            return
        self._trend_warned = False
        af, as_ = 2 / (self.trend_fast + 1), 2 / (self.trend_slow + 1)
        ef = es = closes[0]
        for c in closes[1:]:
            ef += af * (c - ef)
            es += as_ * (c - es)
        self.trend_val = (ef - es) / es if es else None
        self._tg_trend_check()

    def trend_hit(self, side, k):
        """Predikat murni (tanpa log/state): entry level k melawan tren kuat. Dipakai engine dan dashboard."""
        if not self.trend_on or self.trend_val is None:
            return False
        if k == 0 and not self.trend_block_l0:
            return False
        v = self.trend_val
        return (side == "long" and v < -self.trend_th) or (side == "short" and v > self.trend_th)

    def trend_blocks(self, side, k):
        """True kalau entry level k di sisi ini melawan tren kuat (k=0 = entry awal/L0)."""
        hit = self.trend_hit(side, k)
        if hit:
            v = self.trend_val
            tag = (side, k == 0)
            if time.time() - self._trend_logged.get(tag, 0) > 300:
                self._trend_logged[tag] = time.time()
                log.info("Skip %s %s: melawan tren (EMA%d/%d %s %+.2f%%, ambang %.2f%%)", side,
                         "entry awal" if k == 0 else "safety", self.trend_fast, self.trend_slow, self.trend_tf,
                         v * 100, self.trend_th * 100)
        return hit

    def trend_text(self):
        if not self.trend_on:
            return "filter tren mati"
        if self.trend_val is None:
            return f"menunggu data candle {self.trend_tf} (tidak memblokir)"
        v = self.trend_val
        st = ("TURUN: long diblokir" if v < -self.trend_th else "NAIK: short diblokir" if v > self.trend_th
              else "netral: long & short boleh")
        return f"EMA{self.trend_fast}/{self.trend_slow} {self.trend_tf} {v * 100:+.2f}% (ambang {self.trend_th * 100:.2f}%) -> {st}"

    def _tg_trend_check(self):
        """Kirim Telegram hanya saat status tren berganti (turun/netral/naik), bukan tiap skip."""
        v = self.trend_val
        if v is None:
            return
        st = "down" if v < -self.trend_th else "up" if v > self.trend_th else "flat"
        prev, self._tg_trend = self._tg_trend, st
        if prev is not None and prev != st:
            self.notify("\U0001F9ED Tren berubah: " + self.trend_text())

    def trend_info(self):
        return {"on": self.trend_on, "tf": self.trend_tf, "fast": self.trend_fast, "slow": self.trend_slow,
                "th": self.trend_th, "blockL0": self.trend_block_l0, "val": self.trend_val,
                "bars": len(self._trend_bars),
                "blockLong": bool(self.trend_on and self.trend_val is not None and self.trend_val < -self.trend_th),
                "blockShort": bool(self.trend_on and self.trend_val is not None and self.trend_val > self.trend_th)}

    # ---------- filter SnR (support/resistance) ----------
    def sanitize_snr(self):
        if self.snr_src not in SNR_SRCS:
            self.snr_src = "pd4"
        try:
            z = float(self.snr_zone)
        except (TypeError, ValueError):
            z = SNR_ZONE
        self.snr_zone = max(0.0005, min(0.03, z))

    @staticmethod
    def _snr_buckets(now):
        """(hari WIB kemarin, nomor candle 4H sebelumnya) untuk waktu now (detik)."""
        ms = int(now * 1000)
        return (ms + SNR_TZ_MS) // SNR_DAY_MS - 1, ms // SNR_H4_MS - 1

    def refresh_snr(self, force=False):
        """Ambil candle 1H publik (maks. 1x per 15 menit, atau sesaat setelah candle 4H / hari baru)."""
        if not self.snr_on:
            return
        now = time.time()
        if self.snr_lv.get("inst") not in (None, self.inst):
            self.snr_lv, self._snr_next = {}, 0.0
        if not force and now < self._snr_next:
            return
        pd_day, h4_b = self._snr_buckets(now)
        why = ""
        try:
            r = self.market.get_candlesticks(instId=self.inst, bar="1H", limit="60")
            if r.get("code") != "0" or not r.get("data"):
                why = str(r.get("msg") or r)[:200]
        except Exception as e:
            r, why = {}, f"{type(e).__name__}: {e}"[:200]
        if why:
            self._snr_next = now + 60
            if not self._snr_warned:
                log.warning("Candle SnR gagal (%s): filter SnR tidak memblokir sampai data kembali", why)
                self._snr_warned = True
            return
        if self._snr_warned:
            log.info("Candle SnR kembali normal")
            self._snr_warned = False
        days, h4s = {}, {}
        for row in r.get("data") or []:
            try:
                if len(row) > 8 and str(row[8]) != "1":
                    continue  # candle belum selesai
                ts, hi, lo = int(row[0]), float(row[2]), float(row[3])
            except (TypeError, ValueError, IndexError):
                continue
            for book, b in ((days, (ts + SNR_TZ_MS) // SNR_DAY_MS), (h4s, ts // SNR_H4_MS)):
                cur = book.get(b)
                book[b] = (max(cur[0], hi), min(cur[1], lo), cur[2] + 1) if cur else (hi, lo, 1)
        lv = {"inst": self.inst}
        d, q = days.get(pd_day), h4s.get(h4_b)
        if d and d[2] >= 24:
            lv.update(pdH=d[0], pdL=d[1], pdDay=pd_day)
        if q and q[2] >= 4:
            lv.update(h4H=q[0], h4L=q[1], h4B=h4_b)
        self.snr_lv = lv
        complete = "pdDay" in lv and ("h4B" in lv or self.snr_src == "pd")
        ms = int(now * 1000)
        nxt_h4 = (ms // SNR_H4_MS + 1) * SNR_H4_MS / 1000
        nxt_day = ((ms + SNR_TZ_MS) // SNR_DAY_MS + 1) * SNR_DAY_MS / 1000 - SNR_TZ_MS / 1000
        self._snr_next = now + 60 if not complete else min(now + SNR_REFRESH_SEC, nxt_h4 + 20, nxt_day + 20)
        if not complete:
            if not self._snr_partial_warned:
                log.warning("Data SnR belum lengkap (hari kemarin %s, 4H %s): level yang kosong tidak dipakai",
                            "ada" if "pdDay" in lv else "kosong", "ada" if "h4B" in lv else "kosong")
                self._snr_partial_warned = True
        else:
            self._snr_partial_warned = False
        shown = tuple(lv.get(k) for k in ("pdH", "pdL", "h4H", "h4L"))
        if shown != self._snr_shown:
            self._snr_shown = shown
            log.info("Level SnR dimuat (%d candle 1H): %s", len(r.get("data") or []), self.snr_text())

    def snr_levels(self):
        """(support, resistance) yang masih berlaku sekarang. Kosong kalau data belum ada / basi."""
        lv = self.snr_lv
        if not self.snr_on or lv.get("inst") != self.inst:
            return [], []
        pd_day, h4_b = self._snr_buckets(time.time())
        sup, res = [], []
        if lv.get("pdDay") == pd_day:
            sup.append(lv["pdL"])
            res.append(lv["pdH"])
        if self.snr_src == "pd4" and lv.get("h4B") == h4_b:
            sup.append(lv["h4L"])
            res.append(lv["h4H"])
        return sup, res

    def snr_near(self, side, k, px):
        """Predikat murni: level SnR yang memblokir safety L k di harga px, atau None. Dipakai engine dan dashboard."""
        if not self.snr_on or k == 0 or not px or px <= 0:
            return None
        sup, res = self.snr_levels()
        z = self.snr_zone
        if side == "long":
            near = [x for x in res if x * (1 - z) <= px <= x]
        else:
            near = [x for x in sup if x <= px <= x * (1 + z)]
        if not near:
            return None
        return min(near) if side == "long" else max(near)

    def snr_blocks(self, side, k, px):
        """True kalau safety order level k (k >= 1) di harga px terlalu dekat resistance (long) / support (short)."""
        lvl = self.snr_near(side, k, px)
        if lvl is None:
            return False
        z = self.snr_zone
        now = time.time()
        self.snr_last_block = (now, side, k, px, lvl)
        tag = (side, k)
        if now - self._snr_logged.get(tag, 0) > 300:
            self._snr_logged[tag] = now
            log.info("Skip %s safety L%d @ %.4f: dekat %s %.4f (zona %.2f%%, filter SnR)", side, k, px,
                     "resistance" if side == "long" else "support", lvl, z * 100)
        return True

    def snr_text(self):
        if not self.snr_on:
            return "filter SnR mati"
        sup, res = self.snr_levels()
        if not sup and not res:
            return "menunggu data candle (tidak memblokir)"
        src = "hari kemarin + 4H" if self.snr_src == "pd4" else "hari kemarin"
        txt = (f"zona {self.snr_zone * 100:.2f}% ({src}) | R {' / '.join(self.fmt_px(x) for x in res)} | "
               f"S {' / '.join(self.fmt_px(x) for x in sup)}")
        lb = self.snr_last_block
        if lb and time.time() - lb[0] < 6 * 3600:
            txt += (f" | blokir terakhir {datetime.fromtimestamp(lb[0]).strftime('%H:%M')} "
                    f"{lb[1]} L{lb[2]} @ {self.fmt_px(lb[3])}")
        return txt

    def snr_info(self):
        sup, res = self.snr_levels()
        lb = self.snr_last_block
        return {"on": self.snr_on, "zone": self.snr_zone, "src": self.snr_src, "sup": sup, "res": res,
                "lastBlock": ({"t": lb[0], "side": lb[1], "lvl": lb[2], "px": lb[3], "level": lb[4]} if lb else None)}

    # ---------- filter channel regresi linier (LR) ----------
    def sanitize_lr(self):
        try:
            n = int(self.lr_n)
        except (TypeError, ValueError):
            n = LR_N
        self.lr_n = max(50, min(300, n))
        try:
            k = float(self.lr_k)
        except (TypeError, ValueError):
            k = LR_K
        self.lr_k = max(0.5, min(5.0, k))

    @staticmethod
    def _lr_fit(closes):
        """Regresi linier OLS atas closes (tua -> baru). Kembalikan (line_end, sd, slope_b)."""
        n = len(closes)
        if n < 2:
            return None, None, None
        xm = (n - 1) / 2.0
        ym = sum(closes) / n
        den = sum((i - xm) ** 2 for i in range(n))
        if den <= 0:
            return None, None, None
        b = sum((i - xm) * (closes[i] - ym) for i in range(n)) / den
        line_end = ym + b * ((n - 1) - xm)
        res = [closes[i] - (ym + b * (i - xm)) for i in range(n)]
        sd = (sum(r * r for r in res) / n) ** 0.5
        return line_end, sd, b

    def refresh_lr(self, force=False):
        """Ambil candle 1H publik; hitung channel dari N candle selesai. Refresh tiap candle 1H baru."""
        if not self.lr_on:
            return
        now = time.time()
        if self.lr.get("inst") not in (None, self.inst):
            self.lr, self._lr_next = {}, 0.0
        if not force and now < self._lr_next:
            return
        need = max(self.lr_n + 5, 60)
        why = ""
        try:
            r = self.market.get_candlesticks(instId=self.inst, bar=LR_BAR, limit=str(min(300, need)))
            if r.get("code") != "0" or not r.get("data"):
                why = str(r.get("msg") or r)[:200]
        except Exception as e:
            r, why = {}, f"{type(e).__name__}: {e}"[:200]
        if why:
            self._lr_next = now + 60
            if not self._lr_warned:
                log.warning("Candle LR gagal (%s): filter LR tidak memblokir sampai data kembali", why)
                self._lr_warned = True
            return
        if self._lr_warned:
            log.info("Candle LR kembali normal")
            self._lr_warned = False
        closes = []  # tua -> baru
        last_ts = None
        for row in reversed(r.get("data") or []):
            try:
                if len(row) > 8 and str(row[8]) != "1":
                    continue
                ts, cl = int(row[0]), float(row[4])
            except (TypeError, ValueError, IndexError):
                continue
            closes.append(cl)
            last_ts = ts
        if len(closes) < self.lr_n or last_ts is None:
            self.lr = {"inst": self.inst}  # kosong: fail-open
            self._lr_next = now + 60
            log.warning("Data LR belum cukup (%d/%d candle 1H selesai): tidak memblokir",
                        len(closes), self.lr_n)
            return
        window = closes[-self.lr_n:]
        mid, sd, _b = self._lr_fit(window)
        if mid is None or sd is None or sd <= 0:
            self.lr = {"inst": self.inst}
            self._lr_next = now + 60
            return
        z = (window[-1] - mid) / sd
        k = self.lr_k
        self.lr = {"inst": self.inst, "n": self.lr_n, "k": k, "mid": mid, "sd": sd,
                   "upper": mid + k * sd, "lower": mid - k * sd, "z": z, "close": window[-1],
                   "ts": last_ts, "bars": len(closes)}
        nxt = (last_ts + LR_BAR_MS) / 1000.0 + 20  # ~20 dtk setelah candle 1H berikutnya selesai
        self._lr_next = max(now + 60, min(nxt, now + 3600))
        shown = (round(mid, 2), round(self.lr["upper"], 2), round(self.lr["lower"], 2), round(z, 3))
        if shown != self._lr_shown:
            self._lr_shown = shown
            log.info("Channel LR dimuat (%d candle 1H, N=%d k=%.2f): %s",
                     len(closes), self.lr_n, k, self.lr_text())

    def lr_ready(self):
        """True kalau channel lengkap & masih untuk instrument ini."""
        lv = self.lr
        return bool(self.lr_on and lv.get("inst") == self.inst and "z" in lv)

    def lr_hit(self, side, k):
        """Predikat murni: safety L k diblokir channel LR. Dipakai engine dan dashboard."""
        if not self.lr_on or k == 0 or not self.lr_ready():
            return False
        z, kk = self.lr["z"], self.lr_k
        return (side == "long" and z < -kk) or (side == "short" and z > kk)

    def lr_blocks(self, side, k):
        """True kalau safety order level k (k >= 1) diblokir karena harga 1H terakhir terentang (ext_so)."""
        if not self.lr_hit(side, k):
            return False
        z, kk = self.lr["z"], self.lr_k
        band = self.lr["lower"] if side == "long" else self.lr["upper"]
        now = time.time()
        self.lr_last_block = (now, side, k, z, band)
        tag = (side, k)
        if now - self._lr_logged.get(tag, 0) > 300:
            self._lr_logged[tag] = now
            log.info("Skip %s safety L%d: z=%.2f (band %s %.4f, N=%d k=%.2f, filter LR)",
                     side, k, z, "bawah" if side == "long" else "atas", band, self.lr_n, kk)
        return True

    def lr_text(self):
        if not self.lr_on:
            return "filter LR mati"
        if not self.lr_ready():
            return "menunggu data candle (tidak memblokir)"
        lv = self.lr
        blk = []
        if lv["z"] < -self.lr_k:
            blk.append("long SO")
        if lv["z"] > self.lr_k:
            blk.append("short SO")
        blk_txt = ("blokir " + " & ".join(blk)) if blk else "tidak memblokir"
        txt = (f"1H N={lv['n']} k={lv['k']:g} | mid {self.fmt_px(lv['mid'])} | "
               f"atas {self.fmt_px(lv['upper'])} | bawah {self.fmt_px(lv['lower'])} | "
               f"z={lv['z']:+.2f} → {blk_txt}")
        lb = self.lr_last_block
        if lb and time.time() - lb[0] < 6 * 3600:
            txt += (f" | blokir terakhir {datetime.fromtimestamp(lb[0]).strftime('%H:%M')} "
                    f"{lb[1]} L{lb[2]} z={lb[3]:+.2f}")
        return txt

    def lr_info(self):
        lv = self.lr if self.lr_ready() else {}
        lb = self.lr_last_block
        z = lv.get("z")
        kk = self.lr_k
        return {"on": self.lr_on, "n": self.lr_n, "k": self.lr_k,
                "mid": lv.get("mid"), "upper": lv.get("upper"), "lower": lv.get("lower"),
                "z": z, "sd": lv.get("sd"), "close": lv.get("close"), "ts": lv.get("ts"),
                "blockLong": bool(z is not None and z < -kk),
                "blockShort": bool(z is not None and z > kk),
                "lastBlock": ({"t": lb[0], "side": lb[1], "lvl": lb[2], "z": lb[3], "band": lb[4]} if lb else None)}

    # ---------- cooldown tersimpan ----------
    def save_cooldown(self):
        try:
            data = json.loads(COOL_PATH.read_text(encoding="utf-8")) if COOL_PATH.exists() else {}
        except (OSError, ValueError):
            data = {}
        data[self.lots_key()] = {"long": float(self.closed_at.get("long", 0)), "short": float(self.closed_at.get("short", 0))}
        try:
            COOL_PATH.write_text(json.dumps(data), encoding="utf-8")
        except OSError as e:
            log.warning("Simpan cooldown gagal: %s", e)

    def load_cooldown(self):
        self.closed_at = {"long": 0.0, "short": 0.0}
        try:
            data = json.loads(COOL_PATH.read_text(encoding="utf-8")) if COOL_PATH.exists() else {}
            saved = data.get(self.lots_key()) or {}
            for sd in ("long", "short"):
                self.closed_at[sd] = float(saved.get(sd, 0) or 0)
        except (OSError, ValueError, TypeError, AttributeError) as e:
            log.warning("Baca cooldown gagal: %s", e)
        for sd in ("long", "short"):
            left = self.cooldown_sec - (time.time() - self.closed_at[sd])
            if self.closed_at[sd] and left > 0:
                log.info("Cooldown %s dipulihkan: sisa %.0fs", sd, left)

    def mark_closed(self, side, ts=None):
        self.closed_at[side] = ts or time.time()
        self.save_cooldown()

    def split_active(self):
        return self.split and self.grid == "dca"

    def save_lots(self):
        try:
            data = json.loads(LOTS_PATH.read_text(encoding="utf-8")) if LOTS_PATH.exists() else {}
        except (OSError, ValueError):
            data = {}
        data[self.lots_key()] = self.lots
        try:
            LOTS_PATH.write_text(json.dumps(data), encoding="utf-8")
        except OSError as e:
            log.warning("Simpan entry gagal: %s", e)

    def lots_key(self):
        return ("demo:" if self.flag == "1" else "live:") + self.inst

    def load_lots(self):
        self.lots = {"long": {"anchor": 0.0, "lots": []}, "short": {"anchor": 0.0, "lots": []}}
        try:
            data = json.loads(LOTS_PATH.read_text(encoding="utf-8")) if LOTS_PATH.exists() else {}
            saved = data.get(self.lots_key())
            if saved is None and self.flag == "1":
                saved = data.get(self.inst)  # file lama (sebelum ada demo/live)
            if saved and "long" in saved and "short" in saved:
                self.lots = saved
                n = len(saved["long"]["lots"]) + len(saved["short"]["lots"])
                if n:
                    log.info("Entry tersimpan dimuat: %d entry (%s)", n, self.inst)
                mk = [sd for sd in ("long", "short") if saved[sd].get("mk")]
                if mk:
                    self._mk_scan = True
                    log.info("Order maker tersimpan (%s): dicek di putaran pertama", ", ".join(mk))
        except (OSError, ValueError) as e:
            log.warning("Baca entry gagal: %s", e)

    def level_px(self, side, k):
        sign = 1 if side == "long" else -1
        px = self.lots[side]["anchor"]
        for i in range(1, k + 1):
            px *= (1 - sign * self.dca_gap(i))
        return px

    def lot_tp_pct(self, lvl):
        k = lvl or 0
        if self.tp_mode == "arith":
            return self.tp_pct + k * self.tp_add          # arithmetic: + tambahan tetap per level
        return self.tp_pct * (self.tp_ratio ** k)          # geometric: x rasio per level

    def lot_sl_pct(self, lvl):
        k = lvl or 0
        if self.sl_mode == "arith":
            return self.sl_pct + k * self.sl_add
        return self.sl_pct * (self.sl_ratio ** k)

    def sanitize_trail(self):
        try:
            self.trail_pct = float(self.trail_pct)
        except (TypeError, ValueError):
            self.trail_pct = 0.002
        if not (0.0005 <= self.trail_pct <= 0.02):
            self.trail_pct = min(0.02, max(0.0005, self.trail_pct))
        self.trail_on = bool(self.trail_on)

    def trail_active(self):
        return bool(self.trail_on) and self.trail_pct > 0

    def trail_stop(self, side, lot):
        """Harga tutup lot trailing: puncak -/+ trailPct, tidak pernah lebih buruk dari TP asli -/+ trailPct (floor)."""
        tr = self.trail_pct
        pk, tpx = float(lot["peak"]), float(lot.get("tpx") or lot["peak"])
        if side == "long":
            return max(pk * (1 - tr), tpx * (1 - tr))
        return min(pk * (1 + tr), tpx * (1 + tr))

    def lot_tp(self, side, entry, lvl=None):
        pct = self.lot_tp_pct(lvl)
        return entry * (1 + pct) if side == "long" else entry * (1 - pct)

    def lot_sl(self, side, entry, lvl=None):
        pct = self.lot_sl_pct(lvl)
        return entry * (1 - pct) if side == "long" else entry * (1 + pct)

    def reconcile_lots(self, side, pos):
        """Samakan catatan entry dengan posisi asli di OKX."""
        book = self.lots[side]
        if not pos.get("ok", True):
            return False
        actual = pos[side]
        recorded = sum(l["sz"] for l in book["lots"])
        tol = self.lot_sz / 2
        changed = False
        if actual <= 0:
            if book["lots"] or book["anchor"]:
                if book["lots"]:
                    log.info("Posisi %s sudah kosong, %d catatan entry dihapus", side, len(book["lots"]))
                    self.mark_closed(side)  # ditutup di luar bot (mis. SL darurat): cooldown tetap berlaku
                book["lots"], book["anchor"], book["locked"] = [], 0.0, []
                changed = True
        elif recorded > actual + tol:
            # posisi berkurang dari luar bot: kurangi entry terjauh dari harga (paling rugi) dulu
            excess = recorded - actual
            for lot in sorted(book["lots"], key=lambda l: -l["entry"] if side == "long" else l["entry"]):
                if excess <= tol:
                    break
                if lot["sz"] <= excess + tol:
                    book["lots"].remove(lot)
                    excess -= lot["sz"]
                    log.info("Entry %s L%s @%.4f dihapus (posisi berkurang di luar bot)", side, lot["lvl"], lot["entry"])
                else:
                    lot["sz"] = round(lot["sz"] - excess, 8)
                    log.info("Entry %s L%s @%.4f dikurangi jadi %.4f (posisi berkurang di luar bot)",
                             side, lot["lvl"], lot["entry"], lot["sz"])
                    excess = 0
            changed = True
        elif actual > recorded + tol and book.get("mk") and \
                round(actual - recorded, 8) <= book["mk"]["sz"] - book["mk"]["fill"] + tol:
            log.info("Posisi %s +%.4f menunggu adopsi isi maker %s", side, round(actual - recorded, 8), book["mk"]["cl"])
        elif actual > recorded + tol:
            diff = round(actual - recorded, 8)
            book["lots"].append({"lvl": None, "entry": pos[side + "_avg"], "sz": diff})
            if not book["anchor"]:
                book["anchor"] = pos[side + "_avg"]
            log.info("Entry %s tambahan %.4f kontrak @%.4f (dari luar catatan bot)", side, diff, pos[side + "_avg"])
            changed = True
        if changed:
            self.save_lots()
        return changed

    def close_lot(self, side, lot, last, reason="TP"):
        params = {
            "instId": self.inst, "tdMode": self.side_mgn.get(side, TD_MODE), "posSide": side, "ordType": "market",
            "side": "sell" if side == "long" else "buy", "sz": self.fmt_size(lot["sz"]),
        }
        r = self.trade.place_order(**params)
        if r.get("code") == "0":
            gain = (last - lot["entry"]) * lot["sz"] * self.ct_val * (1 if side == "long" else -1)
            log.info("%s ENTRY %s L%s entry=%.4f px=%.4f sz=%s ~%+.4f USDT",
                     reason, side, lot["lvl"], lot["entry"], last, params["sz"], gain)
            self._tg_bot_close[side] = time.time()
            self.notify(tg_close_text(self.inst, side, params["sz"], self.fmt_px(last),
                                      f"{reason} L{lot['lvl'] if lot['lvl'] is not None else '?'} "
                                      f"(entry {self.fmt_px(lot['entry'])})", gain), trade=True)
            self.lots[side]["lots"].remove(lot)
            if not self.lots[side]["lots"]:
                self.mark_closed(side)
                self.lots[side]["anchor"] = 0.0
                self.lots[side]["locked"] = []
                self.cancel_leftover_sl(side)
                if self.lots[side].get("mk"):
                    self.maker_cancel_all("sisi kosong", (side,))
            self.save_lots()
            return True
        data = r.get("data") or [{}]
        why = (data[0].get("sMsg") if data else None) or r.get("msg") or r
        log.error("%s ENTRY %s gagal: %s | respons=%s", reason, side, why, self._resp_txt(r))
        self.notify(f"\u274C GAGAL TUTUP {tg_side(side)} ({reason} L{lot['lvl']}): {str(why)[:200]}. Cek posisi di OKX!",
                    key=f"closefail:{side}", every=600)
        return False

    def refresh_side_sl(self, side, pos=None):
        """Satu SL darurat untuk seluruh sisi (tutup 100%), dihitung dari harga rata-rata."""
        if self.emergency_sl <= 0:
            return
        pos = pos or self.get_positions()
        if not pos.get("ok", True):
            return
        self.cancel_leftover_sl(side)
        if pos[side] <= 0 or pos[side + "_avg"] <= 0:
            return
        avg = pos[side + "_avg"]
        trig = avg * (1 - self.emergency_sl) if side == "long" else avg * (1 + self.emergency_sl)
        lot_sls = [self.lot_sl(side, l["entry"], l["lvl"]) for l in self.lots[side]["lots"]]
        if lot_sls:  # selalu lebih jauh dari SL entry terjauh (+1%), supaya SL bot bekerja duluan
            trig = min(trig, min(lot_sls) * 0.99) if side == "long" else max(trig, max(lot_sls) * 1.01)
        try:
            r = self.trade.place_algo_order(
                instId=self.inst, tdMode=pos.get(side + "_mgn") or TD_MODE, side="sell" if side == "long" else "buy",
                ordType="conditional", posSide=side, slTriggerPx=self.fmt_px(trig), slOrdPx="-1",
                slTriggerPxType="last", closeFraction="1", reduceOnly="true",
            )
            if r.get("code") == "0":
                log.info("SL darurat %s dipasang @ %s (seluruh posisi)", side, self.fmt_px(trig))
            else:
                data = r.get("data") or [{}]
                log.warning("SL darurat %s gagal: %s", side, (data[0].get("sMsg") if data else None) or r.get("msg") or r)
        except Exception as e:
            log.warning("SL darurat %s gagal: %s", side, e)

    def split_manage(self, pos, last):
        """Dijalankan tiap loop (juga saat jeda): cocokkan catatan, lalu TP per entry."""
        for side in ("long", "short"):
            self.reconcile_lots(side, pos)
            if not self.auto_close:
                continue
            closed = False
            book = self.lots[side]
            for lot in list(book["lots"]):
                tp = self.lot_tp(side, lot["entry"], lot["lvl"])
                sl = self.lot_sl(side, lot["entry"], lot["lvl"])
                if lot.get("trail"):
                    if not self.trail_active():   # trailing dimatikan saat lot sudah armed: tutup sebagai TP
                        closed = self.close_lot(side, lot, last, "TP trailing") or closed
                        continue
                    pk = float(lot["peak"])
                    npk = max(pk, last) if side == "long" else min(pk, last)
                    if npk != pk:
                        lot["peak"] = npk
                        self.save_lots()
                    stop = self.trail_stop(side, lot)
                    if (side == "long" and last <= stop) or (side == "short" and last >= stop):
                        log.info("TRAIL %s L%s puncak=%.4f stop=%.4f px=%.4f -> tutup", side, lot["lvl"], lot["peak"], stop, last)
                        closed = self.close_lot(side, lot, last, "TP trailing") or closed
                        continue
                    # SL per entry tetap berlaku di bawah (praktis tidak tersentuh: stop trailing > entry)
                if (not lot.get("trail")) and ((side == "long" and last >= tp) or (side == "short" and last <= tp)):
                    if self.trail_active():
                        lot["trail"], lot["peak"], lot["tpx"] = True, last, tp
                        log.info("TRAIL ARM %s L%s entry=%.4f tp=%.4f px=%.4f trail=%.2f%%", side, lot["lvl"],
                                 lot["entry"], tp, last, self.trail_pct * 100)
                        self.save_lots()
                    else:
                        closed = self.close_lot(side, lot, last, "TP") or closed
                elif (side == "long" and last <= sl) or (side == "short" and last >= sl):
                    if self.close_lot(side, lot, last, "SL"):
                        closed = True
                        if lot["lvl"] is not None and book["lots"]:
                            # level dikunci sampai harga kembali ke level itu, supaya tidak langsung beli lagi
                            book.setdefault("locked", [])
                            if lot["lvl"] not in book["locked"]:
                                book["locked"].append(lot["lvl"])
                            self.save_lots()
            if closed and self.lots[side]["lots"]:
                self.refresh_side_sl(side)

    def split_entries(self, pos, last):
        for side in ("long", "short"):
            book = self.lots[side]
            if not book["lots"]:
                waited = time.time() - self.closed_at.get(side, 0)
                if waited < self.cooldown_sec:
                    log.info("Skip %s: cooldown %.0fs", side, self.cooldown_sec - waited)
                    continue
                if self.trend_blocks(side, 0):
                    continue
                book["anchor"] = last
                book["locked"] = []
                if self.eq_size_on:
                    book["baseEff"] = round(self.eq_base(), 4)
                elif "baseEff" in book:
                    book.pop("baseEff", None)
            locked = book.setdefault("locked", [])
            for k in list(locked):
                back = last >= self.level_px(side, k) if side == "long" else last <= self.level_px(side, k)
                if back:
                    locked.remove(k)
                    log.info("Level %s L%d dibuka lagi (harga kembali ke level)", side, k)
                    self.save_lots()
            filled = {l["lvl"] for l in book["lots"]}
            opened = False
            for k in range(self.dca_max + 1):
                if k in filled or k in locked:
                    continue
                if self.trend_blocks(side, k):
                    continue
                lvl = self.level_px(side, k)
                hit = last <= lvl if side == "long" else last >= lvl
                if not hit:
                    continue
                if self.maker_on and k >= 1 and (not self.maker_fb or book.get("mk")):
                    break           # safety diisi order maker (post-only), bukan market
                if self.snr_blocks(side, k, lvl):
                    continue
                if self.lr_blocks(side, k):
                    continue
                sz = self.side_contracts(side, k, lvl)
                if self.maker_on and k >= 1:
                    self.mk_stats["fallback"] += 1
                log.info("ENTRY %s L%d sz=%s level=%.4f px=%.4f", side, k, self.fmt_size(sz), lvl, last)
                why = "L0 entry awal" if k == 0 else f"L{k} safety {k}/{self.dca_max}"
                res = self.open_long(sz, why, last) if side == "long" else self.open_short(sz, why, last)
                if isinstance(res, dict) and res.get("code") == "0":
                    book["lots"].append({"lvl": k, "entry": last, "sz": float(self.fmt_size(sz))})
                    opened = True
                break  # maksimal satu entry per sisi per putaran, level lain menyusul di putaran berikutnya
            if opened:
                self.save_lots()
                self.refresh_side_sl(side)
            elif book["lots"]:
                nxt = [(k, self.level_px(side, k)) for k in range(self.dca_max + 1) if k not in filled]
                if nxt:
                    k, px = nxt[0]
                    log.info("Skip %s: level kosong berikutnya L%d @ %.4f", side, k, px)

    def split_info(self, pos, last):
        out = {}
        for side in ("long", "short"):
            book = self.lots[side]
            filled = {l["lvl"] for l in book["lots"]}
            lots = []
            for l in sorted(book["lots"], key=lambda x: (x["lvl"] is None, x["lvl"] if x["lvl"] is not None else 0)):
                tp = self.lot_tp(side, l["entry"], l["lvl"])
                sl = self.lot_sl(side, l["entry"], l["lvl"])
                pnl = (last - l["entry"]) * l["sz"] * self.ct_val * (1 if side == "long" else -1) if last else 0
                row = {"lvl": l["lvl"], "entry": l["entry"], "sz": l["sz"], "tp": tp, "sl": sl, "pnl": pnl}
                if l.get("trail"):
                    row.update(trail=True, peak=l.get("peak"), tp=self.trail_stop(side, l), tpx=l.get("tpx"))
                lots.append(row)
            empty = []
            if book["anchor"]:
                locked = set(book.get("locked") or [])
                nxt = None
                for k in range(self.dca_max + 1):
                    if k not in filled:
                        lp = self.level_px(side, k)
                        sz = self.side_contracts(side, k, lp)
                        blk = "" if k in locked else self.level_block(side, k, lp, sz)
                        if nxt is None and k not in locked:
                            nxt = k
                        empty.append({"lvl": k, "px": lp, "sz": sz, "locked": k in locked,
                                      "tp": self.lot_tp(side, lp, k), "sl": self.lot_sl(side, lp, k),
                                      "block": blk, "next": k == nxt})
            out[side] = {"anchor": book["anchor"], "lots": lots, "empty": empty}
        return out

    def level_block(self, side, k, px, sz):
        """Alasan level k akan dilewati engine kalau harga sampai di sana sekarang ('' = tidak diblokir). Read-only."""
        if self.trend_hit(side, k):
            return "tren"
        lv = self.snr_near(side, k, px)
        if lv is not None:
            return "SnR " + ("R " if side == "long" else "S ") + self.fmt_px(lv)
        if self.lr_hit(side, k):
            return "LR"
        return self.extra_block(side, sz)

    def extra_block(self, side, sz):
        return ""

    def chart_levels(self):
        """Garis untuk grafik harga: entry/TP/SL per entry, level safety kosong, rata-rata, SL darurat (perkiraan),
        SnR. Hanya membaca catatan bot (okx_lots.json di memori), tanpa request ke OKX. Tidak mengubah state."""
        out = []
        def add(kind, side, px, label, **kw):
            try:
                px = float(px)
            except (TypeError, ValueError):
                return
            if px > 0:
                out.append(dict(kw, kind=kind, side=side, px=px, label=label))
        if self.split_active():
            for side in ("long", "short"):
                book = self.lots.get(side) or {}
                lots = [dict(l) for l in list(book.get("lots") or [])]
                n_man = 0
                for i, l in enumerate(lots):
                    if l.get("lvl") is not None:
                        name = f"L{l['lvl']}"
                    else:  # entry dari luar catatan bot (manual / diadopsi)
                        n_man += 1
                        name = f"M{n_man}"
                    add("entry", side, l["entry"], name, sz=l.get("sz"), lot=i)
                    if l.get("trail"):
                        add("tp", side, self.trail_stop(side, l), "TP trailing " + name + " (puncak " + self.fmt_px(l.get("peak")) + ")",
                            lot=i, trail=True, peak=l.get("peak"))
                    else:
                        add("tp", side, self.lot_tp(side, l["entry"], l.get("lvl")), "TP " + name, lot=i)
                    add("sl", side, self.lot_sl(side, l["entry"], l.get("lvl")), "SL " + name, lot=i)
                qty = sum(float(l.get("sz") or 0) for l in lots)
                if qty > 0:
                    avg = sum(float(l["sz"]) * float(l["entry"]) for l in lots) / qty
                    add("avg", side, avg, "rata-rata", sz=round(qty, 8))
                    if self.emergency_sl > 0:  # rumus sama dengan refresh_side_sl (dari rata-rata catatan entry)
                        trig = avg * (1 - self.emergency_sl) if side == "long" else avg * (1 + self.emergency_sl)
                        sls = [self.lot_sl(side, l["entry"], l.get("lvl")) for l in lots]
                        trig = min(trig, min(sls) * 0.99) if side == "long" else max(trig, max(sls) * 1.01)
                        add("esl", side, trig, "SL darurat (perk.)")
                if book.get("anchor"):
                    filled = {l.get("lvl") for l in lots}
                    locked = set(book.get("locked") or [])
                    for k in range(self.dca_max + 1):
                        if k not in filled:
                            add("pend", side, self.level_px(side, k), f"L{k}" + (" (kunci)" if k in locked else ""),
                                locked=k in locked)
        sup, res = self.snr_levels()
        for x in res:
            add("res", None, x, "R")
        for x in sup:
            add("sup", None, x, "S")
        if self.lr_ready():
            add("lrU", None, self.lr["upper"], "LR atas")
            add("lrL", None, self.lr["lower"], "LR bawah")
            add("lrM", None, self.lr["mid"], "LR mid")
        return out

    def dca_info(self, pos, last):
        """Ringkasan DCA untuk dashboard. Tidak mengubah state."""
        out = {}
        for side in ("long", "short"):
            size = pos.get(side, 0)
            avg = pos.get(side + "_avg", 0)
            st = self.dca_state[side]
            if size <= 0:
                out[side] = {"n": 0, "max": self.dca_max, "next": None, "nextSz": self.dca_contracts(0, last)}
                continue
            n = st["n"] if st["n"] > 0 else 1
            ref = st["last"] or avg
            if n > self.dca_max:
                out[side] = {"n": self.dca_max, "max": self.dca_max, "next": None, "nextSz": 0}
                continue
            gap = self.dca_gap(n)
            trig = ref * (1 - gap) if side == "long" else ref * (1 + gap)
            out[side] = {"n": n - 1, "max": self.dca_max, "next": trig, "nextSz": self.dca_contracts(n, ref)}
        return out

    def tf_preset_now(self):
        for name, p in TF_PRESETS.items():
            ok = all((bool(getattr(self, a)) == bool(p[k])) if isinstance(p[k], bool) else abs(float(getattr(self, a)) - float(p[k])) < 1e-9
                     for k, a in TFP_KEYS if not (k == "trailPct" and not p["trailOn"]))
            if ok and self.tp_mode == "geo" and self.sl_mode == "arith":
                return name
        return "custom"

    def apply_tf_preset(self, name):
        p = TF_PRESETS.get(name)
        if not p:
            return False, "preset timeframe tidak dikenal"
        for k, a in TFP_KEYS:
            setattr(self, a, bool(p[k]) if isinstance(p[k], bool) else float(p[k]))
        self.tp_mode, self.sl_mode = "geo", "arith"
        self.clear_preset()
        self.sync_dca_sizes()
        for k, a in TFP_KEYS:   # hanya kunci preset yang ditulis
            save_setting(TFP_SET[k], getattr(self, a))
        save_setting("tpMode", "geo"); save_setting("slMode", "arith")
        self.refresh_trend(force=True)
        self.refresh_snr(force=True)
        self.refresh_lr(force=True)
        if self.split_active() and (self.lots["long"]["lots"] or self.lots["short"]["lots"]):
            pos = self.get_positions()
            if pos.get("ok"):
                for sd in ("long", "short"):
                    if pos[sd] > 0:
                        self.refresh_side_sl(sd, pos)   # SL darurat mengikuti SL per entry yang baru
        msg = (f"Preset timeframe {p['label']}: jarak {p['step']*100:.2f}% x{p['scale']:g}, TP {p['tp']*100:.2f}% geo x{p['tpRatio']:g}, "
               f"SL {p['sl']*100:.2f}% arith, trailing {('%.2f%%' % (p['trailPct']*100)) if p['trailOn'] else 'mati'}, "
               f"tren {p['trendTh']*100:.2f}%, SnR {'hidup' if p['snrOn'] else 'mati'}, LR {'hidup' if p['lrOn'] else 'mati'}")
        log.info(msg)
        return True, msg

    def clear_preset(self):
        if self.preset:
            log.info("Parameter diubah manual, preset %s dilepas", self.preset)
        self.preset = ""
        try:
            PRESET_PATH.unlink()
        except OSError:
            pass

    def load_spec(self, inst=None):
        """Ambil ctVal, lotSz, minSz, tickSz dari OKX untuk market aktif."""
        inst = inst or self.inst
        try:
            r = self.public.get_instruments(instType="SWAP", instId=inst)
        except Exception as e:
            log.warning("Spec %s gagal: %s", inst, e)
            return False
        if r.get("code") != "0" or not r.get("data"):
            log.warning("Spec %s gagal: %s", inst, r.get("msg") or r)
            return False
        d = r["data"][0]
        try:
            self.ct_val = float(d.get("ctVal") or 0.01)
            self.lot_sz = float(d.get("lotSz") or 0.01)
            self.min_sz = float(d.get("minSz") or self.lot_sz)
            self.tick_sz = float(d.get("tickSz") or 0.1)
        except (TypeError, ValueError):
            log.warning("Spec %s tidak valid: %s", inst, d)
            return False
        log.info("Spec %s | ctVal=%g lotSz=%g minSz=%g tickSz=%g",
                 inst, self.ct_val, self.lot_sz, self.min_sz, self.tick_sz)
        return True

    @staticmethod
    def _decimals(step):
        txt = f"{step:.10f}".rstrip("0")
        return len(txt.split(".")[1]) if "." in txt else 0

    def fmt_size(self, sz):
        """Bulatkan ke bawah sesuai lotSz, minimal minSz, hasil string."""
        sz = float(sz)
        steps = int(sz / self.lot_sz + 1e-9)
        val = max(self.min_sz, steps * self.lot_sz)
        return f"{val:.{self._decimals(self.lot_sz)}f}"

    def fmt_px(self, px):
        steps = round(px / self.tick_sz)
        return f"{steps * self.tick_sz:.{self._decimals(self.tick_sz)}f}"

    def set_market(self, inst):
        inst = (inst or "").strip().upper()
        if inst.count("-") < 2:
            return False, "Format market: BTC-USDT-SWAP"
        result = self.market.get_ticker(instId=inst)
        if result.get("code") != "0" or not result.get("data"):
            return False, result.get("msg") or "Market tidak ditemukan"
        cur = self.get_positions()
        if not cur["ok"]:
            return False, "Posisi gagal dibaca dari OKX, market tidak diganti"
        if cur["long"] > 0 or cur["short"] > 0:
            return False, (
                f"Masih ada posisi di {self.inst} (L {cur['long']:g} / S {cur['short']:g}). "
                "Tutup dulu sebelum ganti market."
            )
        old = self.inst
        self.inst = inst
        if not self.load_spec():
            self.inst = old
            self.load_spec()
            return False, "Spec market gagal dibaca, market tidak diganti"
        self.dca_state = {"long": {"n": 0, "last": 0.0}, "short": {"n": 0, "last": 0.0}}
        self.load_lots()
        if self.grid == "dca":
            self.sync_dca_sizes()
        self.grid_low = 0.0
        self.grid_high = 0.0
        self.load_cooldown()
        if self.pos_mode == "long_short_mode" and TD_MODE == "isolated":
            self.set_leverage("long")
            self.set_leverage("short")
        else:
            self.set_leverage()
        log.info("Market %s -> %s. Posisi market lama tidak ditutup.", old, inst)
        return True, "Market " + inst

    def get_ticker(self):
        result = self.market.get_ticker(instId=self.inst)
        if result.get("code") != "0" or not result.get("data"):
            log.warning("Ticker gagal: %s", result)
            self.last_fail = f"ticker: {result.get('msg') or result}"[:300]
            return None
        d = result["data"][0]
        self.last_px = float(d["last"])
        return {"last": float(d["last"]), "bid": float(d["bidPx"]), "ask": float(d["askPx"])}

    def get_positions(self):
        """Posisi dari OKX. Kalau API gagal, out["ok"] = False: JANGAN dianggap posisi kosong."""
        out = {
            "long": 0.0, "short": 0.0,
            "long_avg": 0.0, "short_avg": 0.0,
            "upl": 0.0, "long_upl": 0.0, "short_upl": 0.0,
            "raw_side": None, "mgn": TD_MODE, "ok": False,
        }
        try:
            result = self.account.get_positions(instId=self.inst)
        except Exception as e:
            log.warning("Positions gagal: %s", e)
            self.last_fail = f"posisi: {e}"[:300]
            return out
        if result.get("code") != "0":
            log.warning("Positions gagal: %s", result)
            self.last_fail = f"posisi: {result.get('msg') or result}"[:300]
            return out
        out["ok"] = True
        out["long_mgn"] = out["short_mgn"] = None
        out["long_pside"] = out["short_pside"] = None
        for pos in result.get("data") or []:
            if pos.get("instId") and pos.get("instId") != self.inst:
                continue  # posisi instrumen lain (mis. ETH vs BTC) tidak pernah dihitung
            side = pos.get("posSide") or "net"
            signed = float(pos.get("pos") or 0)
            size = abs(signed)
            if size <= 0:
                continue  # baris pos=0 tidak boleh menimpa sisi yang benar-benar terbuka
            avg = float(pos.get("avgPx") or 0)
            upl = float(pos.get("upl") or 0)
            mgn = pos.get("mgnMode") or TD_MODE
            out["upl"] += upl
            out["raw_side"] = side
            if side == "long" or (side == "net" and signed > 0):
                sd = "long"
            elif side == "short" or (side == "net" and signed < 0):
                sd = "short"
            else:
                continue
            if out[sd] > 0:  # lebih dari satu baris untuk sisi yang sama (mis. cross + isolated): jumlahkan
                log.warning("Ada lebih dari satu posisi %s (%s dan %s) untuk %s", sd, out[sd + "_mgn"], mgn, self.inst)
                tot = out[sd] + size
                avg = (out[sd + "_avg"] * out[sd] + avg * size) / tot if tot else avg
                upl, size = out[sd + "_upl"] + upl, tot
            out[sd], out[sd + "_avg"], out[sd + "_upl"] = size, avg, upl
            out[sd + "_mgn"], out[sd + "_pside"] = mgn, side
            self.side_mgn[sd] = mgn
        out["mgn"] = out["long_mgn"] or out["short_mgn"] or TD_MODE  # kompatibilitas tampilan saja
        return out

    def get_balance_usdt(self):
        result = self.account.get_account_balance(ccy="USDT")
        if result.get("code") != "0" or not result.get("data"):
            return None
        for row in result["data"][0].get("details") or []:
            if row.get("ccy") == "USDT":
                return float(row.get("availBal") or row.get("availEq") or 0)
        return None

    @staticmethod
    def _resp_txt(r):
        """Respons OKX lengkap (code, msg, sCode/sMsg) untuk log."""
        try:
            return json.dumps(r, ensure_ascii=False)[:1500]
        except Exception:
            return str(r)[:1500]

    def _place(self, side, pos_side, size, action, emergency_sl=False, td_mode=None):
        params = {
            "instId": self.inst,
            "tdMode": td_mode or TD_MODE,
            "side": side,
            "posSide": pos_side,
            "ordType": "market",
            "sz": self.fmt_size(size),
        }
        if emergency_sl and self.emergency_sl > 0:
            tick = self.get_ticker()
            if tick:
                if side == "buy":
                    trig = tick["last"] * (1 - self.emergency_sl)
                else:
                    trig = tick["last"] * (1 + self.emergency_sl)
                params["attachAlgoOrds"] = [{
                    "slTriggerPx": self.fmt_px(trig),
                    "slOrdPx": "-1",
                    "slTriggerPxType": "last",
                }]
            else:
                log.warning("%s: ticker gagal, order tanpa SL darurat", action)
        result = self.trade.place_order(**params)
        if result.get("code") == "0":
            log.info("%s ok %s", action, result.get("data"))
        else:
            data = result.get("data") or [{}]
            msg = (data[0].get("sMsg") if data else None) or result.get("msg") or result
            log.error("%s gagal: %s | params=%s | respons=%s", action, msg,
                      {k: v for k, v in params.items() if k != "attachAlgoOrds"}, self._resp_txt(result))
            self.notify(f"\u274C {action} {params['sz']} gagal: {str(msg)[:200]}", key="order:" + action, every=600)
        return result

    def open_long(self, size=None, reason="manual", px=None):
        pos_side = "long" if self.pos_mode == "long_short_mode" else "net"
        sz = size or self.order_size
        r = self._place("buy", pos_side, sz, "OPEN LONG", emergency_sl=not self.split_active())
        self._tg_opened("long", sz, r, reason, px)
        return r

    def open_short(self, size=None, reason="manual", px=None):
        pos_side = "short" if self.pos_mode == "long_short_mode" else "net"
        sz = size or self.order_size
        r = self._place("sell", pos_side, sz, "OPEN SHORT", emergency_sl=not self.split_active())
        self._tg_opened("short", sz, r, reason, px)
        return r

    # ---------- Telegram ----------
    def notify(self, text, key=None, every=0.0, trade=False):
        """Antrekan pesan Telegram. Tidak pernah melempar error ke loop trading."""
        try:
            if trade and not self.tg_trades:
                return False
            return TG.send(text, key=key, every=every)
        except Exception as e:
            log.debug("notify gagal: %s", e)
            return False

    def _tg_opened(self, side, sz, res, reason, px):
        try:
            if isinstance(res, dict) and res.get("code") == "0":
                p = px or self.last_px
                self.notify(tg_open_text(self.inst, side, self.fmt_size(sz), self.fmt_px(p) if p else None, reason),
                            trade=True)
        except Exception as e:
            log.debug("notify buka gagal: %s", e)

    def get_equity(self):
        """(equity USDT, saldo tersedia USDT) atau (None, None)."""
        try:
            r = self.account.get_account_balance(ccy="USDT")
            if r.get("code") != "0" or not r.get("data"):
                return None, None
            for row in r["data"][0].get("details") or []:
                if row.get("ccy") == "USDT":
                    return float(row.get("eq") or 0), float(row.get("availBal") or row.get("availEq") or 0)
        except Exception as e:
            log.warning("Saldo gagal: %s", e)
        return None, None

    def status_text(self):
        pos = self.get_positions()
        t = self.get_ticker()
        eq, avail = self.get_equity()
        acc = "LIVE" if self.flag == "0" else "DEMO"
        lines = [f"{self.inst} | {acc} | {'DIJEDA' if self.paused else 'berjalan'}"]
        px = self.fmt_px(t["last"]) if t else "?"
        eq_t = f"{eq:.2f}" if eq is not None else "?"
        av_t = f"{avail:.2f}" if avail is not None else "?"
        lines.append(f"Harga {px} | Equity {eq_t} USDT | tersedia {av_t}")
        if not pos.get("ok"):
            lines.append("Posisi gagal dibaca dari OKX")
        else:
            for sd in ("long", "short"):
                if pos[sd] > 0:
                    lines.append(f"{tg_side(sd)} {self.fmt_size(pos[sd])} @ {self.fmt_px(pos[sd + '_avg'])} | "
                                 f"uPnL {pos[sd + '_upl']:+.4f}")
                else:
                    lines.append(f"{tg_side(sd)} kosong")
            lines.append(f"Total uPnL {pos['upl']:+.4f} USDT")
        lines.append("Tren: " + self.trend_text())
        lines.append("SnR: " + self.snr_text())
        lines.append("LR: " + self.lr_text())
        mode = self.grid + (" (entry terpisah)" if self.split_active() else "")
        lines.append(f"Mode {mode} | TP {self.base_tp * 100:.2f}% SL {self.base_sl * 100:.2f}% | lev {self.leverage}x")
        return "\n".join(lines)

    def set_paused(self, flag, src="dashboard", notify=True):
        self.paused = bool(flag)
        if self.paused:
            try:
                self.maker_cancel_all("jeda")
            except Exception as e:
                log.warning("Maker: batal saat jeda gagal: %s", e)
        msg = "Bot dijeda" if self.paused else "Bot dilanjutkan"
        log.info("%s (%s)", msg, src)
        if notify:
            self.notify(("\u23F8 " if self.paused else "\u25B6 ") + f"{msg} lewat {src}")
        return msg

    def _tg_watch(self, pos, last):
        """Posisi hilang tanpa ditutup bot (SL darurat di exchange / tutup manual di OKX) -> kabari."""
        now = time.time()
        for sd in ("long", "short"):
            prev = self._tg_prev.get(sd)
            if prev and prev[0] > 0 and pos[sd] <= 0 and now - self._tg_bot_close.get(sd, 0) > 3 * LOOP_SEC + 60:
                sz, avg, upl = prev
                self.notify(f"\u26A0 Posisi {tg_side(sd)} {self.fmt_size(sz)} @ {self.fmt_px(avg)} tertutup di luar bot "
                            f"(SL darurat / manual di OKX). PnL ~{upl:+.4f} USDT (uPnL terakhir), harga {self.fmt_px(last)}")
            self._tg_prev[sd] = (pos[sd], pos[sd + "_avg"], pos[sd + "_upl"])

    def _tg_health(self, err=""):
        """Dipanggil tiap putaran: kabari kalau API/jaringan gagal >= 3 putaran berturut-turut (maks 1x/10 menit)."""
        if self._tick_ok:
            if self._fail_alerted:
                self.notify(f"\u2705 Koneksi OKX pulih (setelah {self._fail_streak} putaran gagal)")
            self._fail_streak, self._fail_alerted = 0, False
            return
        self._fail_streak += 1
        if err:
            self.last_fail = str(err)[:300]
        if self._fail_streak >= 3:
            if self.notify(f"\u26A0 OKX/jaringan bermasalah: {self._fail_streak} putaran gagal berturut-turut. "
                           f"Terakhir: {self.last_fail or 'harga/posisi gagal dibaca'}", key="apifail", every=600):
                self._fail_alerted = True

    def _tg_periodic(self):
        h = float(self.tg_status_hours or 0)
        if h <= 0 or not TG.enabled:
            return
        if time.time() - self._tg_status_at >= h * 3600:
            self._tg_status_at = time.time()
            self.notify("\U0001F4CA Status berkala\n" + self.status_text())

    def close_side(self, which, reason="manual"):
        if self.lots.get("long", {}).get("mk") or self.lots.get("short", {}).get("mk"):
            self.maker_cancel_all("tutup " + which, ("long", "short") if which not in ("long", "short") else (which,))
        pos = self.get_positions()
        if not pos["ok"]:
            log.error("CLOSE %s dibatalkan: posisi gagal dibaca", which.upper())
            return None
        if self.pos_mode != "long_short_mode":
            if pos["long"] <= 0 and pos["short"] <= 0:
                log.info("Tidak ada posisi net untuk ditutup")
                return None
            pos_side = "net"
            action = "CLOSE NET"
            sd_open = "long" if pos["long"] > 0 else "short"
            mgn = pos.get(sd_open + "_mgn") or TD_MODE
        else:
            if which == "long" and pos["long"] <= 0:
                log.info("Tidak ada long")
                return None
            if which == "short" and pos["short"] <= 0:
                log.info("Tidak ada short")
                return None
            pos_side = which
            action = "CLOSE " + which.upper()
            mgn = pos.get(which + "_mgn") or TD_MODE
        params = {"instId": self.inst, "mgnMode": mgn, "posSide": pos_side, "autoCxl": "true"}
        try:
            result = self.trade.close_positions(**params)
        except Exception as e:
            result = {"code": "-1", "msg": f"exception: {e}", "data": []}
        if result.get("code") != "0":
            log.error("%s via close-position gagal | params=%s | respons=%s", action, params, self._resp_txt(result))
            result = self._close_market(pos, pos_side, mgn, action)
        if result.get("code") == "0":
            now = time.time()
            if pos_side == "net":
                self.closed_at["long"] = self.closed_at["short"] = now
                self.save_cooldown()
            elif pos_side in self.closed_at:
                self.mark_closed(pos_side, now)
            log.info("%s ok via %s (%s, %s)", action, result.get("_via", "close-position"), pos_side, mgn)
            for sd in (("long", "short") if pos_side == "net" else (pos_side,)):
                if pos[sd] > 0:
                    self._tg_bot_close[sd] = now
                    self.notify(tg_close_text(self.inst, sd, self.fmt_size(pos[sd]),
                                              self.fmt_px(self.last_px) if self.last_px else None, reason,
                                              pos[sd + "_upl"]), trade=True)
            self.cancel_leftover_sl(pos_side)
            for sd in (("long", "short") if pos_side == "net" else (pos_side,)):
                if self.lots[sd]["lots"] or self.lots[sd]["anchor"]:
                    self.lots[sd] = {"anchor": 0.0, "lots": []}
                    self.save_lots()
        else:
            log.error("%s GAGAL TOTAL (close-position dan order market). Tutup manual di OKX! respons=%s",
                      action, self._resp_txt(result))
            self.notify(f"\u274C {action} GAGAL ({reason}): {str(result.get('msg') or '')[:150]}. Tutup manual di OKX!",
                        key=f"closefail:{pos_side}", every=600)
        return result

    def _close_market(self, pos, pos_side, mgn, action):
        """Cadangan kalau close-position ditolak: order market sebesar posisi persis.
        Hedge mode: sell+posSide=long / buy+posSide=short hanya bisa MENUTUP (tidak bisa membuka arah baru),
        jadi reduceOnly tidak dikirim (menurut dokumen OKX reduceOnly hanya untuk net mode).
        Net mode: reduceOnly=true supaya tidak berbalik arah."""
        if pos_side == "net":
            sd = "long" if pos["long"] > 0 else "short"
        else:
            sd = pos_side
        size = pos[sd]
        if size <= 0:
            return {"code": "-1", "msg": "posisi kosong", "data": []}
        params = {
            "instId": self.inst, "tdMode": mgn, "side": "sell" if sd == "long" else "buy",
            "posSide": pos_side, "ordType": "market", "sz": self.fmt_size(size),
        }
        if pos_side == "net":
            params["reduceOnly"] = "true"
        try:
            r = self.trade.place_order(**params)
        except Exception as e:
            r = {"code": "-1", "msg": f"exception: {e}", "data": []}
        if r.get("code") == "0":
            log.warning("%s: cadangan order market berhasil sz=%s tdMode=%s posSide=%s | %s",
                        action, params["sz"], mgn, pos_side, self._resp_txt(r))
            r = dict(r, _via="order market (cadangan)")
        else:
            log.error("%s: cadangan order market gagal | params=%s | respons=%s", action, params, self._resp_txt(r))
        return r

    def cancel_leftover_sl(self, pos_side):
        """Batalkan SL darurat (algo) yang tertinggal setelah posisi ditutup."""
        try:
            r = self.trade.order_algos_list(ordType="conditional", instId=self.inst)
        except Exception as e:
            log.warning("List algo gagal: %s", e)
            return
        if r.get("code") != "0":
            return
        orders = [
            {"instId": a["instId"], "algoId": a["algoId"]}
            for a in (r.get("data") or [])
            if (pos_side == "net" or a.get("posSide") == pos_side) and a.get("instId", self.inst) == self.inst
        ]
        if not orders:
            return
        try:
            c = self.trade.cancel_algo_order(orders[:10])
            if c.get("code") == "0":
                log.info("SL darurat %s dibatalkan: %d", pos_side, len(orders[:10]))
            else:
                log.warning("Batal SL darurat gagal: %s", c.get("msg") or c)
        except Exception as e:
            log.warning("Batal SL darurat gagal: %s", e)

    def close_long(self, size=None, reason="manual"):
        if self.pos_mode != "long_short_mode":
            return self.close_side("net", reason)
        if size:
            return self._place("sell", "long", size, "CLOSE LONG", td_mode=self.side_mgn["long"])
        return self.close_side("long", reason)

    def close_short(self, size=None, reason="manual"):
        if self.pos_mode != "long_short_mode":
            return self.close_side("net", reason)
        if size:
            return self._place("buy", "short", size, "CLOSE SHORT", td_mode=self.side_mgn["short"])
        return self.close_side("short", reason)

    def allow_add(self, side, pos, last, add=0.0):
        """Grid mati: tambah posisi selama di bawah batas; sisi kosong menunggu cooldown."""
        size = pos["long"] if side == "long" else pos["short"]
        cap = self.max_long if side == "long" else self.max_short
        if size + float(add) > cap + self.lot_sz / 2:
            log.info("Skip %s: %.4f + order %.4f melebihi batas %.4g", side, size, float(add), cap)
            return False
        waited = time.time() - self.closed_at.get(side, 0)
        if size <= 0 and waited < self.cooldown_sec:
            log.info("Skip %s: cooldown %.0fs", side, self.cooldown_sec - waited)
            return False
        return True

    def _swing(self):
        try:
            result = self.market.get_candlesticks(instId=self.inst, bar=SWING_BAR, limit="60")
        except Exception as e:
            log.warning("Swing gagal: %s", e)
            return None
        if result.get("code") != "0":
            return None
        highs, lows = [], []
        for row in (result.get("data") or [])[:60]:
            try:
                highs.append(float(row[2]))
                lows.append(float(row[3]))
            except (TypeError, ValueError, IndexError):
                continue
        if len(highs) < 5:
            return None
        return min(lows), max(highs)

    def sync_grid(self, last):
        if self.grid in ("off", "dca") or last <= 0:
            return
        if self.grid_high <= 0 or self.grid_low <= 0:
            if self.grid == "auto" and self.grid_step_cfg <= 0:
                swing = self._swing()
                if swing:
                    lo, hi = swing
                    if hi - lo < last * 0.002:
                        mid = (lo + hi) / 2
                        lo, hi = mid - last * 0.001, mid + last * 0.001
                    self.grid_low, self.grid_high = lo, hi
                    self.step = (hi - lo) / self.grid_levels
                    log.info("Auto grid step %.2f range %.2f-%.2f", self.step, lo, hi)
                    return
            step = self.grid_step_cfg if self.grid_step_cfg > 0 else last * 0.002
            self.step = step
            half = self.grid_levels * step / 2
            self.grid_low = last - half
            self.grid_high = last + half
            log.info("Grid %s step %.2f range %.2f-%.2f", self.grid, step, self.grid_low, self.grid_high)
            return
        step = self.step or (self.grid_step_cfg if self.grid_step_cfg > 0 else last * 0.002)
        self.step = step
        if self.grid in ("up", "auto") and last > self.grid_high:
            shift = last - self.grid_high
            self.grid_low += shift
            self.grid_high = last
            log.info("Trailing up %.2f-%.2f step %.2f", self.grid_low, self.grid_high, step)
        elif self.grid in ("down", "auto") and last < self.grid_low:
            shift = self.grid_low - last
            self.grid_high -= shift
            self.grid_low = last
            log.info("Trailing down %.2f-%.2f step %.2f", self.grid_low, self.grid_high, step)

    def grid_allow(self, side, pos, last, add=0.0):
        if last < self.grid_low or last > self.grid_high:
            log.info("Skip %s: di luar grid %.2f-%.2f", side, self.grid_low, self.grid_high)
            return False
        size = pos["long"] if side == "long" else pos["short"]
        avg = pos["long_avg"] if side == "long" else pos["short_avg"]
        cap = self.max_long if side == "long" else self.max_short
        if size + float(add) > cap + self.lot_sz / 2:
            log.info("Skip %s: %.4f + order %.4f melebihi batas %.4g", side, size, float(add), cap)
            return False
        waited = time.time() - self.closed_at.get(side, 0)
        if size <= 0 and waited < self.cooldown_sec:
            log.info("Skip %s: cooldown %.0fs", side, self.cooldown_sec - waited)
            return False
        if size <= 0 or avg <= 0:
            mid = (self.grid_low + self.grid_high) / 2
            if side == "long" and last > mid:
                return False
            if side == "short" and last < mid:
                return False
            return True
        if side == "long" and last > avg - self.step:
            log.info("Skip long: belum turun 1 step %.2f", self.step)
            return False
        if side == "short" and last < avg + self.step:
            log.info("Skip short: belum naik 1 step %.2f", self.step)
            return False
        return True

    def manage_tpsl(self, last_price):
        if not self.auto_close:
            return
        pos = self.get_positions()
        if not pos["ok"]:
            return
        if pos["long"] > 0 and pos["long_avg"] > 0:
            tp = float("inf") if self.split_active() else pos["long_avg"] * (1 + self.tp_pct)
            sl = -1.0 if self.split_active() else pos["long_avg"] * (1 - self.sl_pct)
            if last_price >= tp or last_price <= sl:
                tag = "TP" if last_price >= tp else "SL"
                log.info("%s LONG px=%.2f avg=%.2f", tag, last_price, pos["long_avg"])
                self.close_long(reason=tag)
        if pos["short"] > 0 and pos["short_avg"] > 0:
            tp = -1.0 if self.split_active() else pos["short_avg"] * (1 - self.tp_pct)
            sl = float("inf") if self.split_active() else pos["short_avg"] * (1 + self.sl_pct)
            if last_price <= tp or last_price >= sl:
                tag = "TP" if last_price <= tp else "SL"
                log.info("%s SHORT px=%.2f avg=%.2f", tag, last_price, pos["short_avg"])
                self.close_short(reason=tag)

    def close_if_win(self, pos, last):
        if not self.close_on_win:
            return
        def worth(side, upl, size):
            if size <= 0:
                return False
            fee = last * self.ct_val * size * 0.001  # taker ~0.05% x 2 sisi
            need = max(self.win_usdt, fee)
            if upl >= need:
                return True
            if upl >= self.win_usdt:
                log.info("WIN %s %.4f di bawah fee bolak-balik %.4f, tidak ditutup", side, upl, need)
            return False
        if worth("long", pos["long_upl"], pos["long"]):
            log.info("WIN long uPnL %.4f, tutup", pos["long_upl"])
            self.close_long(reason="WIN")
        if worth("short", pos["short_upl"], pos["short"]):
            log.info("WIN short uPnL %.4f, tutup", pos["short_upl"])
            self.close_short(reason="WIN")

    def close_if_loss(self, pos):
        """Batas rugi USDT. Selalu aktif, tidak tergantung toggle win/TP-SL."""
        if self.max_loss <= 0:
            return
        if pos["long"] > 0 and pos["long_upl"] <= -self.max_loss:
            log.info("LOSS long uPnL %.4f", pos["long_upl"])
            self.close_long(reason=f"maxLoss {self.max_loss:g} USDT")
        if pos["short"] > 0 and pos["short_upl"] <= -self.max_loss:
            log.info("LOSS short uPnL %.4f", pos["short_upl"])
            self.close_short(reason=f"maxLoss {self.max_loss:g} USDT")

    def strategy_tick(self):
        self._tick_ok = False
        tick = self.get_ticker()
        if not tick:
            return
        last = tick["last"]
        self.manage_tpsl(last)
        pos = self.get_positions()
        if not pos["ok"]:
            log.warning("Posisi gagal dibaca, putaran ini dilewati (tidak buka/tutup apa pun)")
            return
        self._tick_ok = True
        self._tg_watch(pos, last)
        self.close_if_loss(pos)
        pos = self.get_positions()
        if not pos["ok"]:
            log.warning("Posisi gagal dibaca, putaran ini dilewati (tidak buka/tutup apa pun)")
            return
        self.close_if_win(pos, last)
        pos = self.get_positions()
        if not pos["ok"]:
            log.warning("Posisi gagal dibaca, putaran ini dilewati (tidak buka/tutup apa pun)")
            return
        for sd, cap in (("long", self.max_long), ("short", self.max_short)):
            if pos[sd] > cap + self.lot_sz / 2:
                log.warning("Posisi %s %.4f melebihi batas %.4f: tidak ada tambahan, posisi tidak dikurangi otomatis",
                            sd, pos[sd], cap)
        log.info(
            "mode=%s px=%.2f long=%.4f@%.2f short=%.4f@%.2f upl=%.4f",
            self.pos_mode, last, pos["long"], pos["long_avg"], pos["short"], pos["short_avg"], pos["upl"],
        )
        self.record_equity()
        if self.split_active():
            if self.maker_on or self.lots["long"].get("mk") or self.lots["short"].get("mk") or getattr(self, "_mk_scan", False):
                if self.maker_tick(last):
                    pos = self.get_positions()          # isi maker baru: baca posisi lagi sebelum dicocokkan
                    if not pos["ok"]:
                        log.warning("Posisi gagal dibaca, putaran ini dilewati")
                        return
                    for sd in ("long", "short"):
                        if any(l.get("mk") for l in self.lots[sd]["lots"]):
                            self.refresh_side_sl(sd, pos)
            self.split_manage(pos, last)
            pos = self.get_positions()
            if not pos["ok"]:
                log.warning("Posisi gagal dibaca, entry baru dilewati")
                return
        if self.paused:
            log.info("Bot jeda, tidak buka posisi baru")
            return
        if self.pos_mode != "long_short_mode":
            log.info("Bukan hedge, tidak buka posisi baru")
            return
        if self.entry != "both":
            log.info("ENTRY=%s, tidak buka posisi baru", self.entry)
            return
        if self.grid == "dca":
            self.refresh_trend()
            self.refresh_snr()
            self.refresh_lr()
            if self.split:
                self.split_entries(pos, last)
            else:
                self.dca_tick(pos, last)
            return
        self.sync_grid(last)
        sz = self.fmt_size(self.order_size)
        allow = self.grid_allow if self.grid != "off" else self.allow_add
        if allow("long", pos, last, sz):
            log.info("Buka long sz=%s grid=%s", sz, self.grid)
            self.open_long(sz, f"grid {self.grid}", last)
        if allow("short", pos, last, sz):
            log.info("Buka short sz=%s grid=%s", sz, self.grid)
            self.open_short(sz, f"grid {self.grid}", last)

    def record_equity(self):
        result = self.account.get_account_balance()
        if result.get("code") != "0" or not result.get("data"):
            return
        try:
            eq = 0.0
            for row in result["data"][0].get("details") or []:
                if row.get("ccy") == "USDT":
                    eq = float(row.get("eq") or 0)
                    break
            if eq == 0:
                eq = float(result["data"][0].get("totalEq") or 0)
        except (TypeError, ValueError):
            return
        now = time.time()
        if self.equity:
            prev = float(self.equity[-1].get("eq") or 0)
            if prev > 0 and (eq > prev * 3 or eq < prev / 3):
                log.info("Grafik direset: equity USDT %.2f, titik lama %.2f", eq, prev)
                self.equity = []
        if self.equity and now - float(self.equity[-1].get("t", 0)) < 20:
            return
        self.equity.append({"t": now, "eq": round(eq, 4)})
        self.equity = self.equity[-400:]
        try:
            self.eq_path().write_text(json.dumps(self.equity), encoding="utf-8")
        except OSError as e:
            log.warning("Simpan grafik gagal: %s", e)

    def run(self):
        log.info("Loop %ss. Ctrl+C berhenti. AUTO_CLOSE=%s ENTRY=%s", LOOP_SEC, AUTO_CLOSE_ON_TPSL, ENTRY)
        while True:
            err = ""
            try:
                with self.lock:
                    self.strategy_tick()
            except KeyboardInterrupt:
                log.info("Stop")
                break
            except Exception as e:
                log.exception("Error loop: %s", e)
                self._tick_ok, err = False, f"{type(e).__name__}: {e}"
            try:
                self._tg_health(err)
                self._tg_periodic()
            except Exception as e:
                log.warning("Telegram (loop) gagal: %s", e)
            time.sleep(LOOP_SEC)


# ---------- ETH LIVE (ETH-USDT-SWAP, order SUNGGUHAN, ukuran minimum, batas risiko sendiri) ----------
# Instance OKXHedgeBot kedua dengan klien OKX sendiri, instId tetap ETH-USDT-SWAP. Semua query/order/algo memakai
# self.inst, jadi BTC dan ETH tidak pernah saling menyentuh posisi/order/SL. Strategi = salinan setting BTC (split DCA,
# jarak, TP/SL, SL darurat, cooldown, filter tren/SnR/LR) tiap putaran; ukuran tiap level = minSz kontrak (tetap).
# Risiko sendiri: ethMaxLoss (USDT/sisi), ethMaxContracts (kontrak/sisi), ethMinFreeUsdt (entry baru ETH dilewati
# kalau saldo tersedia akun di bawahnya, supaya BTC tetap kebagian margin). Default mati (ethLiveOn tidak ada = mati).
# File sendiri: okx_eth_lots.json, okx_eth_cooldown.json. Tidak pernah menulis setting BTC.
import math as _math

ETH_INST = "ETH-USDT-SWAP"
ETH_LEV = "5"
ETH_LOTS_PATH = BASE_DIR / "okx_eth_lots.json"
ETH_COOL_PATH = BASE_DIR / "okx_eth_cooldown.json"
ETH_LOOP_OFFSET = 15.0
ETH_LEVELS = 6            # L0 + 5 safety
ETH = None


def _on(v):
    return v is True or (not isinstance(v, bool) and str(v).strip().lower() in ("1", "1.0", "true", "on", "yes"))


def eth_cfg(st=None, min_sz=0.01):
    st = load_settings() if st is None else st

    def num(key, dflt, lo, hi):
        try:
            v = float(st.get(key, dflt))
        except (TypeError, ValueError):
            v = dflt
        if not _math.isfinite(v):
            v = dflt
        return min(hi, max(lo, v))
    return {"on": _on(st.get("ethLiveOn", False)),
            "maxLoss": num("ethMaxLoss", 2.0, 0.1, 1000.0),
            "maxContracts": num("ethMaxContracts", round(min_sz * ETH_LEVELS, 8), 0.0, 1e6),
            "minFree": num("ethMinFreeUsdt", 10.0, 0.0, 1e9),
            "maker": _on(st.get("ethMakerOn", False))}


class eth_ctx:
    """Baris log di dalam blok ini diberi awalan [ETH] (untuk request dashboard yang menyentuh ETH)."""
    def __enter__(self):
        self.prev = getattr(_CTX, "eth", False)
        _CTX.eth = True

    def __exit__(self, *a):
        _CTX.eth = self.prev


class EthLiveBot(OKXHedgeBot):
    MIRROR = ("dca_max", "dca_step", "dca_scale", "base_tp", "base_sl", "emergency_sl", "cooldown_sec",
              "tp_mode", "sl_mode", "tp_add", "tp_ratio", "sl_add", "sl_ratio",
              "trend_on", "trend_tf", "trend_fast", "trend_slow", "trend_th", "trend_block_l0",
              "snr_on", "snr_zone", "snr_src", "lr_on", "lr_n", "lr_k",
              "auto_close", "tg_on", "tg_trades", "pos_mode", "trail_on", "trail_pct", "maker_fb")

    def __init__(self, btc):
        prev = getattr(_CTX, "mute", False)
        _CTX.mute = True          # log "Bot siap | BTC..." dari __init__ dasar tidak relevan untuk ETH
        try:
            super().__init__()
        finally:
            _CTX.mute = prev
        self.btc = btc
        self.inst = ETH_INST
        self.leverage = ETH_LEV
        self.equity = []
        self.on = False
        self.cap = 0.0
        self.min_free = 10.0
        self.max_loss = 2.0
        self.ready = False
        self.spec_ok = False
        self.lev_ok = False
        self._lev_try = 0.0
        self.err = ""
        self.block = None
        self.avail = None
        self._entry_pos = None
        self._last_pos = None
        self._block_log = {}
        self._disk_checked = False
        if self.flag != btc.flag:
            self.flag = btc.flag
            self.build_clients(self.flag)
        self.mirror()

    # ----- konfigurasi
    def mirror(self):
        for a in self.MIRROR:
            if hasattr(self.btc, a):
                setattr(self, a, getattr(self.btc, a))
        self.grid, self.split, self.close_on_win, self.entry = "dca", True, False, "both"
        self.max_long = self.max_short = self.cap
        self.order_size = self.min_sz

    def apply_cfg(self, cfg):
        was = self.on
        self.on = bool(cfg["on"])
        self.max_loss = float(cfg["maxLoss"])
        self.cap = float(cfg["maxContracts"])
        self.min_free = float(cfg["minFree"])
        mk = bool(cfg.get("maker", False))
        if mk != self.maker_on:
            log.info("ETH maker (post-only safety) %s", "HIDUP" if mk else "MATI")
        self.maker_on = mk
        if self.on != was:
            log.info("ETH live %s", "HIDUP: order sungguhan, ukuran minimum" if self.on
                     else "MATI: tidak buka posisi baru (posisi yang ada tetap dikelola TP/SL/batas rugi)")

    # ----- tidak ikut sizing/state/grafik BTC
    def dca_contracts(self, k, price):
        return self.min_sz

    def sync_dca_sizes(self):
        self.order_size = self.min_sz

    def save_state(self):
        pass

    def load_state(self):
        return None

    def record_equity(self):
        pass

    def _tg_periodic(self):
        pass

    def notify(self, text, key=None, every=0.0, trade=False):
        return super().notify("[ETH] " + str(text), key=("eth:" + key) if key else None, every=every, trade=trade)

    # ----- file entry/cooldown sendiri
    @staticmethod
    def _read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            return {}

    def save_lots(self):
        data = self._read(ETH_LOTS_PATH)
        data[self.lots_key()] = self.lots
        try:
            ETH_LOTS_PATH.write_text(json.dumps(data), encoding="utf-8")
        except OSError as e:
            log.warning("Simpan entry gagal: %s", e)

    def load_lots(self):
        self.lots = {"long": {"anchor": 0.0, "lots": []}, "short": {"anchor": 0.0, "lots": []}}
        saved = self._read(ETH_LOTS_PATH).get(self.lots_key())
        if saved and "long" in saved and "short" in saved:
            self.lots = saved
            n = len(saved["long"]["lots"]) + len(saved["short"]["lots"])
            if n:
                log.info("Entry tersimpan dimuat: %d entry (%s)", n, self.inst)

    def lots_on_disk(self):
        saved = self._read(ETH_LOTS_PATH).get(self.lots_key()) or {}
        return any((saved.get(sd) or {}).get("lots") for sd in ("long", "short"))

    def save_cooldown(self):
        data = self._read(ETH_COOL_PATH)
        data[self.lots_key()] = {"long": float(self.closed_at.get("long", 0)), "short": float(self.closed_at.get("short", 0))}
        try:
            ETH_COOL_PATH.write_text(json.dumps(data), encoding="utf-8")
        except OSError as e:
            log.warning("Simpan cooldown gagal: %s", e)

    def load_cooldown(self):
        self.closed_at = {"long": 0.0, "short": 0.0}
        saved = self._read(ETH_COOL_PATH).get(self.lots_key()) or {}
        for sd in ("long", "short"):
            try:
                self.closed_at[sd] = float(saved.get(sd, 0) or 0)
            except (TypeError, ValueError):
                pass
            left = self.cooldown_sec - (time.time() - self.closed_at[sd])
            if self.closed_at[sd] and left > 0:
                log.info("Cooldown %s dipulihkan: sisa %.0fs", sd, left)

    # ----- posisi
    def get_positions(self):
        out = super().get_positions()
        if out.get("ok"):
            self._last_pos = dict(out, ts=time.time())
        return out

    def has_pos(self):
        p = self._last_pos or {}
        return bool(self.lots["long"]["lots"] or self.lots["short"]["lots"]
                    or (p.get("long") or 0) > 0 or (p.get("short") or 0) > 0)

    def _has_algo(self, side):
        try:
            r = self.trade.order_algos_list(ordType="conditional", instId=self.inst)
        except Exception as e:
            log.warning("List algo gagal: %s", e)
            return True  # jangan dobel SL kalau tidak bisa dicek
        if r.get("code") != "0":
            return True
        return any(a.get("posSide") == side and a.get("instId", self.inst) == self.inst for a in (r.get("data") or []))

    # ----- pengaman entry baru
    def _note_block(self, side, why):
        self.block = {"t": time.time(), "side": side, "why": why}
        k = side + "|" + why.split(" ")[0]
        if time.time() - self._block_log.get(k, 0) >= 600:
            self._block_log[k] = time.time()
            log.info("Entry baru %s diblokir: %s", side, why)

    def entry_block(self):
        if not self.on:
            return "ethLiveOn mati"
        if self.btc.paused:
            return "bot dijeda"
        if not self.lev_ok:
            if time.time() - self._lev_try >= 600:
                self._set_lev()
            if not self.lev_ok:
                return f"leverage {ETH_LEV}x isolated belum terpasang"
        try:
            bal = self.get_balance_usdt()
        except Exception as e:
            log.warning("Saldo gagal: %s", e)
            bal = None
        if bal is None:
            return "saldo tersedia gagal dibaca"
        self.avail = bal
        if bal < self.min_free:
            return f"saldo tersedia {bal:.2f} < batas {self.min_free:g} USDT (BTC prioritas)"
        return ""

    def extra_block(self, side, sz):
        return "batas kontrak" if self._cap_block(side, sz) else ""

    def maker_ok(self):
        return bool(self.on and self.lev_ok and not self.btc.paused and self.avail is not None and self.avail >= self.min_free)

    def _cap_block(self, side, sz):
        pos = self._entry_pos or self._last_pos or {}
        have = max(float(pos.get(side) or 0), sum(float(l["sz"]) for l in self.lots[side]["lots"]))
        if have + float(sz) > self.cap + self.lot_sz / 2:
            return f"posisi {have:g} + {float(sz):g} melebihi batas {self.cap:g} kontrak"
        return ""

    def split_entries(self, pos, last):
        why = self.entry_block()
        if why:
            self._note_block("long/short", why)
            return
        self.block = None
        self._entry_pos = pos
        try:
            super().split_entries(pos, last)
        finally:
            self._entry_pos = None

    def _guarded_open(self, side, size, reason, px):
        sz = size or self.min_sz
        why = self._cap_block(side, sz) or ("" if self._entry_pos is not None else self.entry_block())
        if why:
            self._note_block(side, why)
            return {"code": "-1", "msg": "ETH: " + why, "data": []}
        fn = super().open_long if side == "long" else super().open_short
        return fn(sz, reason, px)

    def open_long(self, size=None, reason="manual", px=None):
        return self._guarded_open("long", size, reason, px)

    def open_short(self, size=None, reason="manual", px=None):
        return self._guarded_open("short", size, reason, px)

    # ----- start / putaran
    def _set_lev(self):
        self._lev_try = time.time()
        ok = True
        for sd in ("long", "short"):
            r = self.set_leverage(sd)
            ok = ok and r.get("code") == "0"
        self.lev_ok = ok
        return ok

    def setup(self):
        self.ready = False
        if TD_MODE != "isolated":
            self.err = f"TD_MODE={TD_MODE}: ETH live hanya untuk isolated"
            log.warning(self.err)
            return False
        if self.pos_mode != "long_short_mode":
            self.err = "akun bukan hedge mode (long_short_mode)"
            log.warning(self.err)
            return False
        if not self.load_spec():
            self.err = "spec ETH gagal dibaca"
            return False
        self.spec_ok = True
        if "ethMaxContracts" not in load_settings():
            self.cap = round(self.min_sz * ETH_LEVELS, 8)
        self.mirror()
        self.load_lots()
        self.load_cooldown()
        self._set_lev()
        pos = self.get_positions()
        if not pos["ok"]:
            self.err = "posisi ETH gagal dibaca"
            return False
        log.info("Posisi awal: long %g @ %.2f | short %g @ %.2f | uPnL %.4f",
                 pos["long"], pos["long_avg"], pos["short"], pos["short_avg"], pos["upl"])
        for sd in ("long", "short"):
            self.reconcile_lots(sd, pos)
            if pos[sd] > 0 and not self._has_algo(sd):
                log.warning("Posisi %s tanpa SL darurat di OKX: dipasang", sd)
                self.refresh_side_sl(sd, pos)
        t = self.get_ticker()
        lvl = self.min_sz * self.ct_val * (t["last"] if t else 0)
        log.info("ETH live siap: ctVal=%g minSz=%g lotSz=%g | 1 level = %s kontrak (~%.2f USDT, margin ~%.2f) | "
                 "lev %sx isolated %s | batas %g kontrak/sisi | rugi maks %g USDT/sisi | saldo bebas min %g USDT",
                 self.ct_val, self.min_sz, self.lot_sz, self.fmt_size(self.min_sz), lvl, lvl / float(ETH_LEV),
                 ETH_LEV, "OK" if self.lev_ok else "GAGAL", self.cap, self.max_loss, self.min_free)
        self.err = "" if self.lev_ok else f"leverage {ETH_LEV}x isolated gagal diset (entry baru diblokir)"
        self.ready = True
        return True

    def tick(self):
        self.apply_cfg(eth_cfg(min_sz=self.min_sz))
        if self.flag != self.btc.flag:
            log.warning("Akun BTC sekarang %s: ETH live ikut", "DEMO" if self.btc.flag == "1" else "LIVE")
            self.flag = self.btc.flag
            self.build_clients(self.flag)
            self.ready = False
            self._last_pos = None
        self.mirror()
        if not self.ready:
            disk = False
            if not self.on and not self._disk_checked:
                disk = self.lots_on_disk()
                self._disk_checked = True
            if not self.on and not disk:
                self._tick_ok = True
                return "idle"
            if not self.setup():
                self._tick_ok = False
                return "setup gagal"
        if not self.on and not self.has_pos():
            self._tick_ok = True
            return "idle"
        self.paused = (not self.on) or bool(self.btc.paused)
        self.strategy_tick()
        return "tick"

    # ----- tombol manual dashboard (hanya ETH: client ETH sendiri, instId = self.inst = ETH-USDT-SWAP)
    def manual_open(self, side):
        """Buka 1 x minSz di sisi ini atas perintah user. Dijaga ethMaxContracts + ethMinFreeUsdt, lalu dicatat sebagai
        entry level berikutnya (TP/SL per entry) dan SL darurat sisi dipasang ulang, sama seperti entry otomatis."""
        if side not in ("long", "short"):
            return False, "sisi tidak valid"
        if not self.ready:
            return False, "ETH live belum siap"
        if not self.on:
            return False, "ETH live mati (hidupkan di tab Kontrol)"
        if not self.lev_ok:
            return False, f"leverage {ETH_LEV}x isolated belum terpasang"
        pos = self.get_positions()
        if not pos.get("ok"):
            return False, "Posisi ETH gagal dibaca dari OKX, order tidak dikirim"
        for sd in ("long", "short"):
            self.reconcile_lots(sd, pos)
        sz = self.min_sz
        have = max(float(pos.get(side) or 0), sum(float(l["sz"]) for l in self.lots[side]["lots"]))
        if have + sz > self.cap + self.lot_sz / 2:
            return False, f"Ditolak: {side} {have:g} + {sz:g} melebihi batas {self.cap:g} kontrak (ethMaxContracts)"
        bal = self.get_balance_usdt()
        if bal is None:
            return False, "Saldo tersedia gagal dibaca, order tidak dikirim"
        self.avail = bal
        if bal < self.min_free:
            return False, f"Ditolak: saldo tersedia {bal:.2f} < batas {self.min_free:g} USDT (ethMinFreeUsdt, BTC prioritas)"
        t = self.get_ticker()
        if not t:
            return False, "Harga ETH gagal dibaca, order tidak dikirim"
        last = t["last"]
        book = self.lots[side]
        if not book["lots"]:
            book["anchor"], book["locked"] = last, []
        locked = book.setdefault("locked", [])
        filled = {l["lvl"] for l in book["lots"]}
        k = next((i for i in range(self.dca_max + 1) if i not in filled and i not in locked), None)
        fn = OKXHedgeBot.open_long if side == "long" else OKXHedgeBot.open_short
        r = fn(self, sz, f"manual dashboard L{k}" if k is not None else "manual dashboard", last)
        if not (isinstance(r, dict) and r.get("code") == "0"):
            if not book["lots"]:
                book["anchor"] = 0.0
            d = ((r or {}).get("data") or [{}])[0] if (r or {}).get("data") else {}
            return False, f"Order ETH {side} gagal: {d.get('sMsg') or (r or {}).get('msg') or r}"
        book["lots"].append({"lvl": k, "entry": last, "sz": float(self.fmt_size(sz))})
        if k is not None:   # format sama dengan entry otomatis -> ikut tanda isi di grafik ETH
            log.info("ENTRY %s L%d sz=%s level=%.4f px=%.4f (manual dashboard)", side, k, self.fmt_size(sz), last, last)
        else:
            log.info("ENTRY %s manual sz=%s px=%.4f (dashboard)", side, self.fmt_size(sz), last)
        self.save_lots()
        self.refresh_side_sl(side)
        return True, f"ETH {side} {self.fmt_size(sz)} dibuka @ {self.fmt_px(last)} (L{k if k is not None else '?'}, TP/SL + SL darurat dipasang)"

    def manual_close(self, side):
        if side not in ("long", "short"):
            return False, "sisi tidak valid"
        if not self.ready:
            return False, "ETH live belum siap"
        r = self.close_long() if side == "long" else self.close_short()
        if r is None:
            return False, f"ETH {side}: tidak ada posisi / posisi gagal dibaca"
        if r.get("code") != "0":
            d = (r.get("data") or [{}])[0] if r.get("data") else {}
            return False, f"ETH {side}: GAGAL {r.get('code')} {d.get('sCode', '')} {d.get('sMsg') or r.get('msg')} | Cek posisi di OKX."
        pos = self.get_positions()
        if pos.get("ok"):
            self.reconcile_lots(side, pos)
        return True, f"ETH {side}: ditutup ({r.get('_via', 'close-position')})"

    def status_line(self):
        p = self._last_pos or {}
        txt = (f"[ETH LIVE] {'hidup' if self.on else 'mati'} | L {p.get('long', 0):g} @ {p.get('long_avg', 0):.2f} | "
               f"S {p.get('short', 0):g} @ {p.get('short_avg', 0):.2f} | uPnL {p.get('upl', 0):+.4f}")
        if self.err:
            txt += "\nError: " + self.err
        elif self.block and self.on:
            txt += "\nEntry baru diblokir: " + self.block["why"]
        return txt


def eth_loop(eth, stop):
    _CTX.eth = True
    stop.wait(ETH_LOOP_OFFSET)   # bergeser dari putaran BTC
    while not stop.is_set():
        err = ""
        try:
            with eth.lock:
                eth.tick()
        except Exception as e:
            log.exception("Error loop: %s", e)
            eth._tick_ok, err = False, f"{type(e).__name__}: {e}"
        try:
            if eth.ready:
                eth._tg_health(err)
        except Exception as e:
            log.warning("Telegram (loop) gagal: %s", e)
        stop.wait(LOOP_SEC)


def eth_status_suffix():
    e = ETH
    try:
        if e is None or not (e.on or e.has_pos()):
            return ""
        return "\n\n" + e.status_line()
    except Exception:
        return ""


PAGE = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<meta name="theme-color" content="#0b0e11"/>
<title>OKX Hedge</title>
<style>
  :root {
    --bg:#0b0e11; --panel:#14181f; --panel2:#1b2029; --line:#232a35;
    --text:#eaecef; --mute:#848e9c; --up:#0ecb81; --down:#f6465d; --warn:#f0b90b;
    --upbg:rgba(14,203,129,.12); --downbg:rgba(246,70,93,.12);
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html,body { margin:0; background:var(--bg); color:var(--text); }
  body { font:14px/1.4 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
         font-variant-numeric:tabular-nums; padding-bottom:calc(64px + env(safe-area-inset-bottom,0px)); }
  .wrap { max-width:560px; margin:0 auto; }
  .num { font-variant-numeric:tabular-nums; }
  .up { color:var(--up); } .down { color:var(--down); } .mute { color:var(--mute); }

  /* top bar */
  .top { position:sticky; top:0; z-index:5; background:var(--bg); padding:calc(10px + env(safe-area-inset-top,0px)) 16px 10px;
         border-bottom:1px solid var(--line); }
  .pairrow { display:flex; align-items:center; gap:8px; }
  .pair { font-size:17px; font-weight:700; }
  .badge { font-size:11px; padding:2px 6px; border-radius:4px; background:var(--panel2); color:var(--mute); }
  .badge.demo { color:var(--warn); background:rgba(240,185,11,.12); }
  .badge.live { color:var(--down); background:var(--downbg); }
  .state { margin-left:auto; display:flex; align-items:center; gap:6px; font-size:12px; color:var(--mute); }
  .dot { width:8px; height:8px; border-radius:50%; background:var(--up); box-shadow:0 0 0 3px var(--upbg); }
  .dot.off { background:var(--warn); box-shadow:0 0 0 3px rgba(240,185,11,.15); }
  .dot.err { background:var(--down); box-shadow:0 0 0 3px var(--downbg); }
  .pricerow { display:flex; align-items:flex-end; justify-content:space-between; margin-top:6px; }
  .price { font-size:32px; font-weight:700; letter-spacing:-.5px; transition:color .3s; }
  .stats { text-align:right; font-size:12px; color:var(--mute); line-height:1.6; }
  .stats b { color:var(--text); font-weight:600; }
  .stats b.up { color:var(--up); } .stats b.down { color:var(--down); }

  /* sections */
  .viewsel { margin:0 0 10px; } .viewsel select { margin-left:6px; padding:6px 8px; border-radius:8px; font:inherit; }
  section.tab { display:none; padding:12px 16px 16px; }
  section.tab.on { display:block; }
  .panel { background:var(--panel); border-radius:10px; padding:14px; margin-bottom:12px; }
  .ptitle { display:flex; justify-content:space-between; align-items:center; font-size:13px; color:var(--mute); margin-bottom:10px; }
  .ptitle b { color:var(--text); font-size:15px; font-weight:600; }

  /* summary strip */
  .strip { display:grid; grid-template-columns:repeat(3,1fr); gap:1px; background:var(--line); border-radius:10px; overflow:hidden; margin-bottom:12px; }
  .strip div { background:var(--panel); padding:10px 12px; }
  .strip small { display:block; color:var(--mute); font-size:11px; }
  .strip span { font-size:13px; font-weight:600; white-space:nowrap; }

  /* position card */
  .pos { background:var(--panel); border-radius:10px; padding:14px; margin-bottom:10px; border-left:3px solid var(--line); }
  .pos.long { border-left-color:var(--up); } .pos.short { border-left-color:var(--down); }
  .poshead { display:flex; align-items:center; gap:8px; }
  .side { font-weight:700; font-size:15px; }
  .lev { font-size:11px; padding:1px 5px; border-radius:3px; background:var(--panel2); color:var(--mute); }
  .pnl { margin-left:auto; text-align:right; }
  .pnl .big { font-size:18px; font-weight:700; }
  .pnl .roe { font-size:12px; }
  .grid3 { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-top:12px; }
  .grid3 small { display:block; color:var(--mute); font-size:11px; }
  .grid3 span { font-size:13px; }
  .meter { margin-top:14px; }
  .bar { position:relative; height:6px; border-radius:3px;
         background:linear-gradient(90deg,var(--downbg),var(--panel2) 50%,var(--upbg)); }
  .bar i { position:absolute; top:-4px; width:3px; height:14px; border-radius:2px; background:var(--text); transform:translateX(-1px); transition:left .4s; }
  .bar em { position:absolute; top:0; bottom:0; width:1px; background:var(--mute); opacity:.6; }
  .ends { display:flex; justify-content:space-between; font-size:11px; color:var(--mute); margin-top:6px; }
  .ends .r { text-align:right; }
  .posfoot { display:flex; justify-content:flex-end; margin-top:12px; }
  .empty { text-align:center; color:var(--mute); padding:22px 10px; background:var(--panel); border-radius:10px; margin-bottom:10px; }

  /* buttons */
  button { font:inherit; border:0; border-radius:6px; cursor:pointer; color:var(--text); background:var(--panel2); height:34px; padding:0 14px; }
  button:disabled { opacity:.45; cursor:wait; }
  button:focus-visible { outline:2px solid var(--warn); outline-offset:2px; }
  .trade { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:4px 0 12px; }
  .trade button { height:46px; font-size:15px; font-weight:700; }
  .buy { background:var(--up); color:#05140d; } .sell { background:var(--down); color:#fff; }
  .ghost { background:transparent; border:1px solid var(--line); color:var(--mute); }
  .closeall { width:100%; height:40px; background:transparent; border:1px solid var(--line); color:var(--text); }

  /* chart */
  .rng { display:flex; gap:6px; }
  .rng button { height:26px; padding:0 10px; font-size:12px; background:transparent; color:var(--mute); }
  .rng button.on { background:var(--panel2); color:var(--text); }
  .chartbox { position:relative; }
  #chart { width:100%; height:220px; display:block; touch-action:none; }
  .eqhead { display:flex; align-items:baseline; gap:10px; margin-bottom:6px; }
  .eqhead .big { font-size:24px; font-weight:700; }
  .tip { position:absolute; top:0; pointer-events:none; font-size:11px; background:var(--panel2); padding:3px 6px; border-radius:4px; white-space:nowrap; display:none; }
  .xax { display:flex; justify-content:space-between; font-size:11px; color:var(--mute); margin-top:4px; }
  #pxChart { width:100%; height:320px; display:block; touch-action:none; cursor:crosshair; user-select:none; }
  .pxtog { display:flex; flex-wrap:wrap; gap:4px 10px; font-size:12px; color:var(--mute); margin-bottom:8px; }
  .pxtog label { display:flex; align-items:center; gap:4px; cursor:pointer; }
  .pxtog input { margin:0; accent-color:#0ecb81; }
  .pxfoot { display:flex; justify-content:space-between; align-items:center; gap:8px; font-size:11px; color:var(--mute); margin-top:6px; }
  .pxfoot button { height:24px; min-width:28px; padding:0 8px; font-size:12px; background:var(--panel2); color:var(--text); }
  .pxleg { font-size:12px; color:var(--mute); line-height:1.7; margin-top:8px; }
  .pxleg b { color:var(--text); font-weight:600; }
  .pxleg i { display:inline-block; width:14px; height:0; border-top:2px solid; vertical-align:middle; margin-right:4px; }

  /* control */
  .row { display:flex; align-items:center; justify-content:space-between; padding:12px 0; border-bottom:1px solid var(--line); gap:12px; }
  .row:last-child { border-bottom:0; }
  .row small { display:block; color:var(--mute); font-size:12px; }
  .sw { width:44px; height:26px; border-radius:13px; background:var(--panel2); position:relative; padding:0; flex:none; }
  .sw::after { content:""; position:absolute; top:3px; left:3px; width:20px; height:20px; border-radius:50%; background:var(--mute); transition:left .2s, background .2s; }
  .sw.on { background:var(--upbg); }
  .sw.on::after { left:21px; background:var(--up); }
  .chip { height:30px; font-size:13px; }
  .form { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:10px; }
  .form label { min-width:0; }
  .form label { font-size:12px; color:var(--mute); display:flex; flex-direction:column; gap:4px; }
  .form input, .form select { height:40px; border-radius:6px; border:1px solid var(--line); background:var(--bg); color:var(--text); padding:0 10px; font:inherit; width:100%; min-width:0; }
  .form input:focus, .form select:focus { outline:none; border-color:var(--warn); }
  .presets { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; }
  .pre { height:auto; display:flex; flex-direction:column; justify-content:flex-start; padding:10px 8px; text-align:left; background:var(--bg); border:1px solid var(--line); border-radius:8px; }
  .pre b { display:block; font-size:14px; }
  .pre small { display:block; color:var(--mute); font-size:10.5px; margin-top:2px; line-height:1.5; }
  .pre.on { border-color:var(--warn); background:rgba(240,185,11,.08); }
  .pre.on b { color:var(--warn); }
  .accs { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .accs button { height:46px; font-weight:700; letter-spacing:.5px; background:var(--bg); border:1px solid var(--line); color:var(--mute); }
  .accs button.on.demo { border-color:var(--warn); color:var(--warn); background:rgba(240,185,11,.08); }
  .accs button.on.live { border-color:var(--down); color:#fff; background:var(--down); }
  .accs button.na { opacity:.35; }
  body.live .top { border-bottom:2px solid var(--down); }
  body.live .badge.live { background:var(--down); color:#fff; font-weight:700; }
  .levs { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:8px; }
  .levs button { height:40px; background:var(--bg); border:1px solid var(--line); font-weight:600; }
  .levs button.on { border-color:var(--warn); color:var(--warn); background:rgba(240,185,11,.08); }
  .levs button.bad { color:var(--down); }
  .levinfo { margin-top:10px; font-size:12px; color:var(--mute); line-height:1.6; }
  .levinfo b { color:var(--text); font-weight:600; }
  .levinfo .warn { color:var(--down); }
  .levinfo .ok { color:var(--up); }
  .winrows { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:10px; }
  .winrows div { background:var(--bg); border-radius:6px; padding:8px 10px; }
  .winrows small { display:block; color:var(--mute); font-size:11px; }
  .winrows span { font-size:15px; font-weight:600; }
  .winbtns { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .winbtns .winall { grid-column:1 / -1; height:44px; }
  .winbtns button { height:40px; }
  .winall { background:var(--upbg); color:var(--up); font-weight:700; }
  .winbtns button.off { opacity:.35; }
  .plan { grid-column:1 / -1; font-size:12px; color:var(--mute); background:var(--bg); border-radius:6px; padding:8px 10px; line-height:1.7; }
  .plan b { color:var(--text); font-weight:600; }
  .lots { margin-top:12px; border-top:1px solid var(--line); padding-top:8px; }
  .lot { display:grid; grid-template-columns:34px 1fr 1fr auto; gap:6px; font-size:12px; padding:5px 0; align-items:center; }
  .lot .lv { color:var(--mute); font-weight:600; }
  .lot small { color:var(--mute); font-size:10.5px; display:block; }
  .lot.wait { color:var(--mute); }
  .dcaline { margin-top:10px; font-size:12px; color:var(--mute); }
  .dcaline b { color:var(--text); font-weight:600; }
  .pnote { font-size:12px; color:var(--mute); margin:10px 0 0; }
  .save { grid-column:1 / -1; height:44px; background:var(--warn); color:#1a1400; font-weight:700; }

  pre { margin:0; font:12px/1.5 ui-monospace,Consolas,monospace; white-space:pre-wrap; word-break:break-word; color:#b7bdc6; max-height:65vh; overflow:auto; }

  /* toast + nav */
  #toast { position:fixed; left:50%; bottom:calc(76px + env(safe-area-inset-bottom,0px)); transform:translateX(-50%); background:var(--panel2);
           padding:10px 14px; border-radius:8px; font-size:13px; max-width:90vw; display:none; z-index:9; border:1px solid var(--line); }
  nav { position:fixed; left:0; right:0; bottom:0; z-index:6; background:var(--panel); border-top:1px solid var(--line);
        padding-bottom:env(safe-area-inset-bottom,0px); }
  nav .in { max-width:560px; margin:0 auto; display:grid; grid-template-columns:repeat(5,1fr); }
  nav button { height:56px; background:transparent; color:var(--mute); border-radius:0; font-size:12px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:3px; }
  nav button svg { width:20px; height:20px; stroke:currentColor; fill:none; stroke-width:1.8; }
  nav button.on { color:var(--text); }
  /* backtest */
  [hidden] { display:none !important; }
  .btnote { font-size:12px; color:var(--mute); margin:0 0 10px; line-height:1.5; }
  .form .sub { grid-column:1 / -1; font-size:11px; color:var(--mute); text-transform:uppercase; letter-spacing:.6px; margin-top:6px; border-top:1px solid var(--line); padding-top:10px; }
  .btacts { grid-column:1 / -1; display:grid; grid-template-columns:1fr 2fr; gap:8px; }
  .btacts button { height:44px; }
  .btprog { height:8px; border-radius:4px; background:var(--panel2); overflow:hidden; margin:8px 0; }
  .btprog i { display:block; height:100%; width:0; background:var(--warn); transition:width .4s; }
  .btpline { display:flex; justify-content:space-between; align-items:center; gap:10px; font-size:12px; color:var(--mute); }
  .btpline button { height:30px; flex:none; }
  .btcards { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }
  @media (min-width:480px) { .btcards { grid-template-columns:repeat(4,minmax(0,1fr)); } }
  .btcards div { background:var(--bg); border-radius:8px; padding:8px 10px; min-width:0; }
  .btcards small { display:block; color:var(--mute); font-size:11px; }
  .btcards span { display:block; font-size:16px; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .btcards em { display:block; font-style:normal; font-size:11px; color:var(--mute); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .bttbl { width:100%; border-collapse:collapse; font-size:12px; }
  .bttbl th { text-align:right; color:var(--mute); font-weight:500; padding:4px 4px; border-bottom:1px solid var(--line); white-space:nowrap; }
  .bttbl td { text-align:right; padding:5px 4px; border-bottom:1px solid var(--line); white-space:nowrap; }
  .bttbl th:first-child, .bttbl td:first-child { text-align:left; }
  .bttbl tr.hl td { font-weight:700; }
  .bttbl small { color:var(--mute); font-size:10.5px; font-weight:400; }
  .bttw { overflow-x:auto; margin:0 -4px; padding:0 4px; }
  .btmeta { font-size:12px; color:var(--mute); line-height:1.6; margin-bottom:10px; }
  .btmeta b { color:var(--text); font-weight:600; }
  .btch { position:relative; }
  .btch svg { width:100%; display:block; }
  .btch .yl { position:absolute; left:2px; font-size:10.5px; color:var(--mute); background:rgba(20,24,31,.7); padding:0 3px; border-radius:3px; pointer-events:none; }
  .btleg { display:flex; flex-wrap:wrap; gap:4px 12px; font-size:11px; color:var(--mute); margin:6px 0 2px; }
  .btleg i { display:inline-block; width:14px; height:0; border-top:2px solid; vertical-align:middle; margin-right:4px; }
  .btrun { display:grid; grid-template-columns:1fr auto; gap:2px 8px; width:100%; height:auto; text-align:left; padding:8px 10px; margin-bottom:6px; background:var(--bg); border:1px solid var(--line); border-radius:8px; }
  .btrun.on { border-color:var(--warn); }
  .btrun small { color:var(--mute); font-size:11px; }
  .btrun .r { text-align:right; font-weight:700; }
  .panel, .pos, .empty, .strip { border:1px solid var(--line); }
  #elPanel { border:1px solid var(--line) !important; border-left:3px solid var(--down) !important; }
  @media (min-width:960px) {
    body { display:flex; flex-direction:column; padding-bottom:24px; }
    nav { order:0; position:sticky; top:0; bottom:auto; border-top:0; border-bottom:1px solid var(--line); }
    .wrap { order:1; max-width:1120px; width:100%; }
    nav .in { max-width:1120px; }
    nav button { height:48px; flex-direction:row; gap:8px; font-size:13px; }
    .top { top:48px; background:rgba(11,14,17,.94); }
    section.tab { padding:16px 8px 8px; }
    #tab-pos.on { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(300px,.65fr); column-gap:16px; align-items:start; }
    #tab-pos > .strip, #tab-pos > .viewsel, #viewBtc > .strip { grid-column:1 / -1; }
    #viewBtc { display:contents; } #tab-pos > #viewEth { grid-column:1 / -1; }
    #viewBtc > #positions { grid-column:1; grid-row:3 / span 6; }
    #viewBtc > #winPanel, #viewBtc > .trade, #viewBtc > .closeall { grid-column:2; }
    #tab-pos > #positions { grid-column:1; grid-row:2 / span 6; }
    #tab-pos > #winPanel, #tab-pos > .trade, #tab-pos > .closeall, #tab-pos > #elPanel { grid-column:2; }
    #tab-chart.on { display:grid; grid-template-columns:minmax(0,1.45fr) minmax(280px,.55fr); gap:16px; align-items:start; }
    #tab-chart > #pxPanel { grid-row:1 / span 2; }
    #tab-ctl.on { display:grid; grid-template-columns:1fr 1fr; gap:16px; align-items:start; }
    #tab-ctl > .panel:nth-child(5), #tab-ctl > .panel:nth-child(6) { grid-column:1 / -1; }
    #tab-bt.on { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(280px,.85fr); gap:16px; align-items:start; }
    #tab-bt > .panel:first-child { grid-row:1 / span 3; }
    #pxChart { height:420px; }
  }
  @media (prefers-reduced-motion:reduce) { * { transition:none !important; } }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="pairrow">
      <span class="pair" id="inst">-</span>
      <span class="badge" id="mode">-</span>
      <span class="badge" id="pmode">-</span>
      <span class="state"><span class="dot" id="dot"></span><span id="stateTxt">-</span></span>
    </div>
    <div class="pricerow">
      <div class="price num" id="last">-</div>
      <div class="stats num">
        Saldo <b id="usdt">-</b><br/>
        uPnL <b id="uplAll">-</b>
      </div>
    </div>
  </header>

  <!-- Posisi -->
  <section class="tab on" id="tab-pos">
    <div class="viewsel"><label>Instrumen <select id="viewSel"><option value="BTC">BTC-USDT-SWAP</option><option value="ETH">ETH-USDT-SWAP</option></select></label></div>
    <div id="viewBtc">
    <div class="strip num">
      <div><small>Mode</small><span id="gridTxt">-</span></div>
      <div><small>TP / SL</small><span id="tpslTxt">-</span></div>
      <div><small>Update</small><span id="ts">-</span></div>
    </div>
    <div id="positions"></div>
    <div class="panel" id="winPanel">
      <div class="ptitle"><b>Close win</b><span>untung bersih setelah fee</span></div>
      <div class="winrows num" id="winRows"></div>
      <div class="winbtns">
        <button class="ghost" data-act="close_win_long" id="cwLong">Tutup long untung</button>
        <button class="ghost" data-act="close_win_short" id="cwShort">Tutup short untung</button>
        <button class="winall" data-act="close_win" id="cwAll">Tutup semua yang untung</button>
      </div>
    </div>
    <div class="trade">
      <button class="buy" data-act="open_long">Buka long</button>
      <button class="sell" data-act="open_short">Buka short</button>
    </div>
    <button class="closeall" data-act="close_all" data-confirm="Tutup semua posisi?">Tutup semua posisi</button>
    </div>
    <div id="viewEth" style="display:none">
    <div class="strip num">
      <div><small>Mode</small><span id="eMode">-</span></div>
      <div><small>TP / SL</small><span id="eTpsl">-</span></div>
      <div><small>Update</small><span id="eTs">-</span></div>
    </div>
    <div id="elPositions"></div>
    <div class="trade">
      <button class="buy" data-eact="open_long">Buka long</button>
      <button class="sell" data-eact="open_short">Buka short</button>
    </div>
    </div>
  </section>

  <!-- Grafik -->
  <section class="tab" id="tab-chart">
    <div class="panel" id="pxPanel">
      <div class="ptitle"><span>Harga <b id="pxInst">-</b> <span class="rng" id="pxCoin" style="display:inline-flex;margin-left:6px"><button data-c="BTC">BTC</button><button data-c="ETH">ETH</button></span></span>
        <div class="rng" id="pxBar">
          <button data-b="1m">1m</button><button data-b="5m">5m</button><button data-b="15m">15m</button><button data-b="1H">1J</button>
        </div>
      </div>
      <div class="pxtog" id="pxTog">
        <label><input type="checkbox" data-k="entry">Entry</label>
        <label><input type="checkbox" data-k="tp">TP</label>
        <label><input type="checkbox" data-k="sl">SL</label>
        <label><input type="checkbox" data-k="pend">Safety kosong</label>
        <label><input type="checkbox" data-k="avg">Rata-rata</label>
        <label><input type="checkbox" data-k="esl">SL darurat</label>
        <label><input type="checkbox" data-k="snr">SnR</label>
        <label><input type="checkbox" data-k="lr">LR channel</label>
        <label><input type="checkbox" data-k="marks">Isi/tutup</label>
        <label><input type="checkbox" data-k="fit">Muat semua garis</label>
      </div>
      <div class="chartbox">
        <svg id="pxChart" viewBox="0 0 600 320"></svg>
        <div class="tip num" id="pxTip"></div>
      </div>
      <div class="pxfoot num"><span id="pxInfo">Memuat grafik...</span>
        <span><button id="pxOut" type="button" title="Perkecil">−</button> <button id="pxIn" type="button" title="Perbesar">+</button> <button id="pxNow" type="button">Terbaru</button></span>
      </div>
      <div class="pxleg num" id="pxLeg"></div>
    </div>
    <div class="panel">
      <div class="ptitle"><span>Equity USDT</span>
        <div class="rng" id="rng">
          <button data-r="1800">30m</button><button data-r="3600">1J</button><button data-r="0" class="on">Semua</button>
        </div>
      </div>
      <div class="eqhead num"><span class="big" id="eqNow">-</span><span id="eqChg">-</span></div>
      <div class="chartbox">
        <svg id="chart" viewBox="0 0 600 220"></svg>
        <div class="tip num" id="tip"></div>
      </div>
      <div class="xax num"><span id="x0">-</span><span id="x1">-</span></div>
    </div>
    <div class="strip num">
      <div><small>Tertinggi</small><span id="eqHi">-</span></div>
      <div><small>Terendah</small><span id="eqLo">-</span></div>
      <div><small>Turun dari puncak</small><span id="eqDd">-</span></div>
    </div>
  </section>

  <!-- Kontrol -->
  <section class="tab" id="tab-ctl">
    <div class="panel">
      <div class="ptitle"><b>Akun</b><span id="accNow">-</span></div>
      <div class="accs">
        <button id="accDemo" data-acc="demo">DEMO</button>
        <button id="accLive" data-acc="live">LIVE</button>
      </div>
      <p class="pnote" id="accNote"></p>
    </div>
    <div class="panel">
      <div class="ptitle"><b>Strategi</b></div>
      <div class="row"><div>Bot berjalan<small>Matikan untuk menjeda semua aksi otomatis</small></div><button class="sw" id="swPause" data-act="toggle_pause" aria-label="Jeda bot"></button></div>
      <div class="row"><div>Isi kedua sisi<small>Mati = hanya kelola posisi yang ada</small></div><button class="sw" id="swEntry" data-act="toggle_entry" aria-label="Isi kedua sisi"></button></div>
      <div class="row"><div>TP / SL otomatis<small id="tpslSub">-</small></div><button class="sw" id="swTpsl" data-act="toggle_tpsl" aria-label="TP/SL"></button></div>
      <div class="row"><div>Tutup jika untung<small id="winSub">-</small></div><button class="sw" id="swWin" data-act="toggle_win" aria-label="Tutup jika win"></button></div>
      <div class="row"><div>Entry terpisah (DCA)<small id="splitSub">-</small></div><button class="sw" id="swSplit" data-act="toggle_split" data-confirm="Ganti cara kelola entry DCA?" aria-label="Entry terpisah"></button></div>
      <div class="row"><div>Mode grid<small id="gridSub">-</small></div><button class="chip" id="btnGrid" data-act="toggle_grid">-</button></div>
    </div>
    <div class="panel">
      <div class="ptitle"><b>Preset</b><span id="presetNow">Manual</span></div>
      <div class="presets" id="presets"></div>
      <p class="pnote">Preset memakai mode DCA. Ukuran dihitung dalam USDT dan jarak dalam persen, jadi cocok untuk market mana pun. Preset juga mematikan Tutup jika untung, supaya posisi ditutup oleh TP dari harga rata-rata. Leverage tidak diubah.</p>
    </div>
    <div class="panel">
      <div class="ptitle"><b>Leverage</b><span id="levNow">-</span></div>
      <div class="levs" id="levs"></div>
      <div class="levinfo num" id="levInfo"></div>
    </div>
    <div class="panel">
      <div class="ptitle"><b>DCA</b><span id="dcaOn">-</span></div>
      <div class="form"><label>Preset timeframe<select id="dTfp"><option value="custom">Custom</option></select></label>
        <button class="save" id="dTfpApply" type="button">Terapkan preset</button></div>
      <div class="form" id="dform">
        <label>Order awal (USDT)<input id="dBase" type="number" min="1" step="1" inputmode="decimal"></label>
        <label>Jumlah safety order<input id="dMax" type="number" min="0" max="10" step="1" inputmode="numeric"></label>
        <label>Pengali ukuran<input id="dMult" type="number" min="1" max="3" step="0.05" inputmode="decimal"></label>
        <label>Jarak safety pertama (%)<input id="dStep" type="number" min="0.05" max="10" step="0.05" inputmode="decimal"></label>
        <label>Pengali jarak<input id="dScale" type="number" min="1" max="3" step="0.05" inputmode="decimal"></label>
        <span></span>
        <label>Mode TP per level<select id="dTpMode"><option value="arith">Arithmetic (+ tetap)</option><option value="geo">Geometric (× rasio)</option></select></label>
        <label>Mode SL per level<select id="dSlMode"><option value="arith">Arithmetic (+ tetap)</option><option value="geo">Geometric (× rasio)</option></select></label>
        <label><span id="lTpVal">-</span><input id="dTpVal" type="number" min="0" max="3" step="0.05" inputmode="decimal"></label>
        <label><span id="lSlVal">-</span><input id="dSlVal" type="number" min="0" max="5" step="0.05" inputmode="decimal"></label>
        <label>Filter tren<select id="dTrend"><option value="1">Hidup</option><option value="0">Mati</option></select></label>
        <label>TF tren<select id="dTrendTf"><option value="15m">15m</option><option value="1H">1H</option></select></label>
        <label>EMA cepat<input id="dTrendFast" type="number" min="2" max="400" step="1" inputmode="numeric"></label>
        <label>EMA lambat<input id="dTrendSlow" type="number" min="3" max="500" step="1" inputmode="numeric"></label>
        <label>Ambang tren (%)<input id="dTrendTh" type="number" min="0" max="5" step="0.05" inputmode="decimal"></label>
        <label>Blokir entry awal (L0)<select id="dTrendL0"><option value="1">Ya</option><option value="0">Tidak</option></select></label>
        <div class="plan num" id="dTrendInfo"></div>
        <label>Filter SnR (safety)<select id="dSnr"><option value="1">Hidup</option><option value="0">Mati</option></select></label>
        <label>Zona SnR (%)<input id="dSnrZone" type="number" min="0.05" max="3" step="0.05" inputmode="decimal"></label>
        <div class="plan num" id="dSnrInfo"></div>
        <label>Filter LR channel (safety)<select id="dLr"><option value="1">Hidup</option><option value="0">Mati</option></select></label>
        <label>LR N (candle 1H)<input id="dLrN" type="number" min="50" max="300" step="10" inputmode="numeric"></label>
        <label>LR k (lebar band)<input id="dLrK" type="number" min="0.5" max="5" step="0.1" inputmode="decimal"></label>
        <div class="plan num" id="dLrInfo"></div>
        <label>TP trailing<select id="dTrail"><option value="1">Hidup</option><option value="0">Mati</option></select></label>
        <label>Jarak trailing (%)<input id="dTrailPct" type="number" min="0.05" max="2" step="0.05" inputmode="decimal"></label>
        <label>Safety maker (post-only)<select id="dMaker"><option value="0">Mati</option><option value="1">Hidup</option></select></label>
        <label>Fallback market jika tembus<select id="dMakerFb"><option value="0">Tidak</option><option value="1">Ya</option></select></label>
        <label>Ukuran ikut equity<select id="dEqOn"><option value="0">Mati</option><option value="1">Hidup</option></select></label>
        <label>Equity acuan (USDT)<input id="dEqRef" type="number" min="1" step="1" inputmode="decimal"></label>
        <label>Ukuran min (x dcaBase)<input id="dEqMin" type="number" min="0.1" max="1" step="0.05" inputmode="decimal"></label>
        <label>Ukuran maks (x dcaBase)<input id="dEqMax" type="number" min="1" max="5" step="0.05" inputmode="decimal"></label>
        <div class="plan num" id="dMakerInfo"></div>
        <label>Telegram<select id="dTg"><option value="1">Hidup</option><option value="0">Mati</option></select></label>
        <label>Status Telegram tiap (jam, 0 = mati)<input id="dTgHours" type="number" min="0" max="168" step="1" inputmode="decimal"></label>
        <label>Notif buka/tutup order<select id="dTgTrades"><option value="1">Ya</option><option value="0">Tidak</option></select></label>
        <button class="chip" id="btnTgTest" type="button" style="height:40px;align-self:end">Tes Telegram</button>
        <div class="plan num" id="dTgInfo"></div>
        <div class="plan num" id="dPlan"></div>
        <button class="save" id="btnDca">Simpan DCA</button>
      </div>
    </div>
    <div class="panel">
      <div class="ptitle"><b>Parameter</b></div>
      <div class="form" id="form">
        <label>Market<select id="pInst"></select></label>
        <label>Ukuran order<input id="pSize" type="number" min="0.01" step="0.01" inputmode="decimal"></label>
        <label>Maks long<input id="pMaxL" type="number" min="0.01" step="0.01" inputmode="decimal"></label>
        <label>Maks short<input id="pMaxS" type="number" min="0.01" step="0.01" inputmode="decimal"></label>
        <label>Win USDT<input id="pWin" type="number" min="0" step="0.01" inputmode="decimal"></label>
        <label>Max rugi USDT<input id="pLoss" type="number" min="0" step="0.01" inputmode="decimal"></label>
        <label>Cooldown (detik)<input id="pCool" type="number" min="0" step="1" inputmode="numeric"></label>
        <button class="save" id="btnSave">Simpan parameter</button>
      </div>
    </div>
    <div class="panel">
      <div class="ptitle"><b>Tutup per sisi</b></div>
      <div class="trade" style="margin:0">
        <button class="ghost" data-act="close_long" data-confirm="Tutup semua posisi long?">Tutup long</button>
        <button class="ghost" data-act="close_short" data-confirm="Tutup semua posisi short?">Tutup short</button>
      </div>
    </div>
    <div class="panel" id="elPanel">
      <div class="ptitle"><b>ETH</b><span><span class="badge live">LIVE</span></span></div>
      <div class="plan num" id="elHead">Memuat...</div>
      <div class="form" id="elForm">
        <label>Rugi maks/sisi (USDT)<input id="elLoss" type="number" min="0.1" step="0.1" inputmode="decimal"></label>
        <label>Maks kontrak/sisi<input id="elCap" type="number" min="0" step="0.01" inputmode="decimal"></label>
        <label>Saldo bebas min (USDT)<input id="elFree" type="number" min="0" step="0.5" inputmode="decimal"></label>
        <label>Safety maker ETH<select id="elMaker"><option value="0">Mati</option><option value="1">Hidup</option></select></label>
        <button class="save" id="elSave" type="button">Simpan risiko ETH</button>
      </div>
      <div class="winbtns"><button class="ghost" id="elToggle" type="button">-</button></div>
    </div>
  </section>

  <!-- Backtest -->
  <section class="tab" id="tab-bt">
    <div class="panel">
      <div class="ptitle"><b>Backtest</b><span id="btState">-</span></div>
      <p class="btnote">Simulasi strategi entry terpisah (split DCA) memakai candle 5m publik OKX, mesin yang sama dengan backtest box. Hanya simulasi: tidak mengubah setting live dan tidak mengirim order. Form diisi dari setting live sekarang.</p>
      <div class="form" id="btForm">
        <label>Preset timeframe<select id="bTfp"><option value="">(isi manual)</option></select></label>
        <label>Instrumen<input id="bInst" list="bInstList" autocomplete="off" spellcheck="false"></label>
        <datalist id="bInstList"><option value="BTC-USDT-SWAP"><option value="ETH-USDT-SWAP"><option value="XAU-USDT-SWAP"><option value="XRP-USDT-SWAP"><option value="SOL-USDT-SWAP"></datalist>
        <label>Periode<select id="bDays"><option value="30">30 hari</option><option value="90">90 hari</option><option value="180">180 hari</option><option value="range">Rentang tanggal</option></select></label>
        <label id="bFromL" hidden>Dari tanggal<input id="bFrom" type="date"></label>
        <label id="bToL" hidden>Sampai tanggal<input id="bTo" type="date"></label>
        <label>Bagi IS / OOS<select id="bSplit"><option value="1">Ya (blok 1-4 / 5-6)</option><option value="0">Tidak</option></select></label>
        <label>Equity awal (USDT)<input id="bEq" type="number" min="1" step="1" inputmode="decimal"></label>
        <div class="sub">Ukuran &amp; jarak</div>
        <label>Mode ukuran<select id="bSizeMode"><option value="dcaBase">USDT per level</option><option value="fixed">Kontrak tetap</option></select></label>
        <label id="bBaseL">Order awal (USDT)<input id="bBase" type="number" min="1" step="1" inputmode="decimal"></label>
        <label id="bFixedL" hidden>Kontrak per level<input id="bFixed" type="number" min="0.01" step="0.01" inputmode="decimal"></label>
        <label id="bMultL">Pengali ukuran<input id="bMult" type="number" min="1" max="3" step="0.05" inputmode="decimal"></label>
        <label>Jumlah safety order<input id="bSo" type="number" min="0" max="10" step="1" inputmode="numeric"></label>
        <label>Jarak safety pertama (%)<input id="bStep" type="number" min="0.05" max="10" step="0.05" inputmode="decimal"></label>
        <label>Pengali jarak<input id="bScale" type="number" min="1" max="3" step="0.05" inputmode="decimal"></label>
        <label>Leverage<input id="bLev" type="number" min="1" max="100" step="1" inputmode="decimal"></label>
        <div class="sub">TP / SL</div>
        <label>TP awal (%)<input id="bTp" type="number" min="0.05" max="10" step="0.05" inputmode="decimal"></label>
        <label>Mode TP<select id="bTpMode"><option value="geo">Geometric (× rasio)</option><option value="arith">Arithmetic (+ tetap)</option></select></label>
        <label id="bTpRatioL">Rasio TP per level<input id="bTpRatio" type="number" min="0.5" max="3" step="0.05" inputmode="decimal"></label>
        <label id="bTpAddL" hidden>Tambahan TP per level (%)<input id="bTpAdd" type="number" min="0" max="3" step="0.05" inputmode="decimal"></label>
        <label>SL awal (%)<input id="bSl" type="number" min="0.1" max="50" step="0.1" inputmode="decimal"></label>
        <label>Mode SL<select id="bSlMode"><option value="arith">Arithmetic (+ tetap)</option><option value="geo">Geometric (× rasio)</option></select></label>
        <label id="bSlAddL">Tambahan SL per level (%)<input id="bSlAdd" type="number" min="0" max="5" step="0.05" inputmode="decimal"></label>
        <label id="bSlRatioL" hidden>Rasio SL per level<input id="bSlRatio" type="number" min="0.5" max="3" step="0.05" inputmode="decimal"></label>
        <label>SL darurat (%)<input id="bEm" type="number" min="0" max="50" step="0.5" inputmode="decimal"></label>
        <label>Max rugi per sisi (USDT)<input id="bMl" type="number" min="0" step="0.5" inputmode="decimal"></label>
        <label>Cooldown (detik)<input id="bCool" type="number" min="0" step="60" inputmode="numeric"></label>
        <span></span>
        <div class="sub">Filter</div>
        <label>Filter tren<select id="bTrend"><option value="1">Hidup</option><option value="0">Mati</option></select></label>
        <label>TF tren<select id="bTrendTf"><option value="5m">5m</option><option value="15m">15m</option><option value="1H">1H</option><option value="4H">4H</option></select></label>
        <label>EMA cepat<input id="bFast" type="number" min="2" max="400" step="1" inputmode="numeric"></label>
        <label>EMA lambat<input id="bSlow" type="number" min="3" max="400" step="1" inputmode="numeric"></label>
        <label>Ambang tren (%)<input id="bTh" type="number" min="0" max="5" step="0.05" inputmode="decimal"></label>
        <label>Blokir entry awal (L0)<select id="bL0"><option value="1">Ya</option><option value="0">Tidak</option></select></label>
        <label>Filter SnR (safety)<select id="bSnr"><option value="1">Hidup</option><option value="0">Mati</option></select></label>
        <label>Zona SnR (%)<input id="bZone" type="number" min="0.05" max="3" step="0.05" inputmode="decimal"></label>
        <label>Filter LR channel (safety)<select id="bLr"><option value="1">Hidup</option><option value="0">Mati</option></select></label>
        <label>LR N (candle 1H)<input id="bLrN" type="number" min="50" max="300" step="10" inputmode="numeric"></label>
        <label>LR k (lebar band)<input id="bLrK" type="number" min="0.5" max="5" step="0.1" inputmode="decimal"></label>
        <label>TP trailing<select id="bTrail"><option value="1">Hidup</option><option value="0">Mati</option></select></label>
        <label>Jarak trailing (%)<input id="bTrailPct" type="number" min="0.05" max="2" step="0.05" inputmode="decimal"></label>
        <label>Safety maker (post-only)<select id="bMaker"><option value="1">Hidup</option><option value="0">Mati</option></select></label>
        <label>Maker harus tembus (%; 0 = 1 tick)<input id="bMakerThr" type="number" min="0" max="1" step="0.01" inputmode="decimal"></label>
        <label>Ukuran ikut equity<select id="bEqOn"><option value="1">Hidup</option><option value="0">Mati</option></select></label>
        <label>Equity acuan (USDT)<input id="bEqRef" type="number" min="1" step="1" inputmode="decimal"></label>
        <label>Ukuran min/maks (x)<span style="display:flex;gap:4px"><input id="bEqMin" type="number" min="0.1" max="1" step="0.05" inputmode="decimal"><input id="bEqMax" type="number" min="1" max="5" step="0.05" inputmode="decimal"></span></label>
        <div class="sub">Biaya</div>
        <label>Fee per sisi (%)<input id="bFee" type="number" min="0" max="1" step="0.01" inputmode="decimal"></label>
        <label>Slippage + funding<select id="bSlip"><option value="0">Tidak</option><option value="1">Ya (0.02% + funding 8 jam)</option></select></label>
        <label>Bandingkan dengan live<select id="bCmp"><option value="1">Ya</option><option value="0">Tidak</option></select></label>
        <span></span>
        <div class="btacts">
          <button class="chip" id="btnBtLive" type="button">Isi dari live</button>
          <button class="save" id="btnBtRun" type="button">Jalankan backtest</button>
        </div>
      </div>
    </div>
    <div class="panel" id="btProgP" hidden>
      <div class="ptitle"><b>Berjalan</b><span id="btPct">0%</span></div>
      <div class="btprog"><i id="btBar"></i></div>
      <div class="btpline"><span id="btMsg">-</span><button class="ghost" id="btnBtCancel" type="button">Batalkan</button></div>
    </div>
    <div id="btRes"></div>
    <div class="panel">
      <div class="ptitle"><b>Riwayat backtest</b><span id="btRunsN"></span></div>
      <div id="btRuns"><p class="btnote">Belum ada.</p></div>
    </div>
  </section>

  <!-- Log -->
  <section class="tab" id="tab-log">
    <div class="panel"><pre id="log">-</pre></div>
  </section>
</div>

<div id="toast"></div>
<nav><div class="in">
  <button data-tab="pos" class="on"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/></svg>Posisi</button>
  <button data-tab="chart"><svg viewBox="0 0 24 24"><path d="M3 20h18M5 16l4-5 4 3 6-8"/></svg>Grafik</button>
  <button data-tab="ctl"><svg viewBox="0 0 24 24"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg>Kontrol</button>
  <button data-tab="bt"><svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v4h4M12 8v4l3 2"/></svg>Backtest</button>
  <button data-tab="log"><svg viewBox="0 0 24 24"><path d="M5 4h14v16H5zM8 9h8M8 13h8M8 17h5"/></svg>Log</button>
</div></nav>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const $ = id => document.getElementById(id);
let D = null, prevLast = null, range = 0, failCount = 0;

function fmt(v, d) { if (v == null || v === '' || isNaN(v)) return '-'; return Number(v).toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d}); }
function sign(v, d) { const n = Number(v); return (n > 0 ? '+' : '') + fmt(n, d); }
function cls(v) { return Number(v) > 0 ? 'up' : (Number(v) < 0 ? 'down' : ''); }
function pxDec(p) { return p >= 1000 ? 1 : p >= 1 ? 4 : 8; }
function toast(msg) { const t = $('toast'); t.textContent = msg; t.style.display = 'block'; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', 2600); }

async function api(url, opt) {
  opt = opt || {};
  opt.headers = Object.assign({'X-Token': TOKEN}, opt.headers || {});
  const r = await fetch(url, opt);
  if (r.status === 401) throw new Error('Token salah atau kosong. Buka dashboard dengan ?token=...');
  return r.json();
}

/* ---------- helper format (dipakai panel ETH live) ---------- */
function ethF(x, n) { return (x === null || x === undefined || isNaN(x)) ? '-' : Number(x).toFixed(n); }
function ethEsc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
/* ---------- ETH LIVE (endpoint sendiri, order sungguhan) ---------- */
let EL = null, elBusy = false, elDirty = false;
async function elLoad() {
  if (elBusy || document.hidden || !($('tab-pos').classList.contains('on') || $('tab-ctl').classList.contains('on') || (VIEW === 'ETH' && $('tab-chart').classList.contains('on')))) return;
  elBusy = true;
  try { EL = await api('/api/ethlive'); elRender(EL); } catch (e) { $('elHead').textContent = 'ETH live gagal dimuat: ' + (e.message || ''); }
  elBusy = false;
}
function elRender(d) {
  const c = d.cfg || {};
  $('elToggle').textContent = d.on ? 'Matikan ETH live' : 'Hidupkan ETH live';
  if (!elDirty && !(document.activeElement && document.activeElement.closest && document.activeElement.closest('#elForm'))) {
    $('elLoss').value = c.maxLoss; $('elCap').value = c.maxContracts; $('elFree').value = c.minFree; $('elMaker').value = c.maker ? '1' : '0';
  }
  if (!d.available) { $('elHead').textContent = 'ETH live belum dimuat.'; $('elPositions').innerHTML = ''; return; }
  let h = '<b style="color:' + (d.on ? '#f6465d' : '#848e9c') + '">' + (d.on ? 'HIDUP' : 'MATI') + '</b>'
    + (!d.on && d.managing ? ' (posisi yang ada tetap dikelola TP/SL/batas rugi)' : '')
    + ' | ' + d.inst + ' ' + ethF(d.last, 2) + ' | ' + d.lev + 'x isolated' + (d.ready ? '' : ' | belum siap');
  if (d.spec) h += '<br/>1 level = ' + d.spec.minSz + ' kontrak (' + ethF(d.spec.minSz * d.spec.ctVal, 4) + ' ETH, ~' + ethF(d.lvlUsdt, 2)
    + ' USDT) | batas ' + c.maxContracts + ' kontrak/sisi | rugi maks ' + c.maxLoss + ' USDT/sisi | saldo bebas min ' + c.minFree + ' USDT'
    + (d.maker && d.maker.stats ? ' | maker ' + (c.maker ? 'hidup' : 'mati') + ' (pasang ' + d.maker.stats.placed + ', isi ' + d.maker.stats.filled + ', fill rate ' + (d.maker.stats.fillRate == null ? '-' : d.maker.stats.fillRate + '%') + ')' : '')
    + (d.avail !== null && d.avail !== undefined ? ' (terakhir ' + ethF(d.avail, 2) + ')' : '');
  if (d.err) h += '<br/><span style="color:#f6465d">Error: ' + ethEsc(d.err) + '</span>';
  if (d.block && d.on) h += '<br/><span style="color:#f0b90b">Entry baru diblokir: ' + ethEsc(d.block.why) + '</span>';
  if (d.filters) h += '<br/>' + ethEsc(d.filters);
  $('elHead').innerHTML = h;
  const p = d.pos || {}, dec = pxDec(d.last || 0);
  const dd = Object.assign({}, p, {lev: d.lev, ctVal: d.ctVal, last: d.last, tp: d.tp, sl: d.sl, autoClose: d.autoClose,
                                   mgn: d.mgn, split: true, grid: 'dca', splitInfo: d.lots || {}});
  $('eMode').textContent = 'DCA' + (d.on ? '' : ' (mati)');
  $('eTpsl').textContent = d.autoClose ? ((d.tp * 100).toFixed(2) + '% / ' + (d.sl * 100).toFixed(2) + '%') : 'Mati';
  $('eTs').textContent = d.posTs ? new Date(d.posTs * 1000).toTimeString().slice(0, 8) : '-';
  let ph = '';
  for (const sd of ['long', 'short']) if (Number(p[sd]) > 0) ph += posCard(sd, dd, dec, true);
  $('elPositions').innerHTML = ph || '<div class="empty">Tidak ada posisi ETH terbuka.<br/>Bot ETH membuka posisi sesuai DCA (ukuran minimum) kalau tidak diblokir.</div>';
  viewHdr();
}
/* ---------- pilihan tampilan Posisi: BTC / ETH (disimpan di localStorage) ---------- */
let VIEW = (() => { try { return localStorage.getItem('posView') === 'ETH' ? 'ETH' : 'BTC'; } catch (e) { return 'BTC'; } })();
function viewHdr() {
  if (VIEW !== 'ETH') return;
  $('inst').textContent = 'ETH-USDT-SWAP';
  const d = EL || {}, p = d.pos || {};
  $('last').textContent = d.last != null ? fmt(d.last, pxDec(d.last)) : '-'; $('last').className = 'price num';
  const u = (Number(p.long_upl) || 0) + (Number(p.short_upl) || 0);
  $('uplAll').textContent = sign(u, 4); $('uplAll').className = cls(u);
}
function setView(v, save) {
  VIEW = v === 'ETH' ? 'ETH' : 'BTC'; $('viewSel').value = VIEW;
  $('viewBtc').style.display = VIEW === 'BTC' ? '' : 'none';
  $('viewEth').style.display = VIEW === 'ETH' ? '' : 'none';
  if (save) { try { localStorage.setItem('posView', VIEW); } catch (e) {} }
  if (VIEW === 'ETH') { viewHdr(); elLoad(); } else if (D) { prevLast = null; render(D); }
  if (window.__pxReady) pxCoinSync(true);
}
$('viewSel').addEventListener('change', e => setView(e.target.value, true));
setView(VIEW, false);
['input', 'change'].forEach(ev => $('elForm').addEventListener(ev, () => { elDirty = true; }));
let eBusy = false;
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-eact]');
  if (!b || eBusy) return;
  if (b.dataset.confirm && !confirm(b.dataset.confirm)) return;
  eBusy = true;
  document.querySelectorAll('[data-eact]').forEach(x => x.disabled = true);
  try {
    const r = await api('/api/ethlive/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:b.dataset.eact})});
    toast(r.msg || 'Selesai');
  } catch (err) { toast(err.message || 'Gagal mengirim perintah ETH'); }
  setTimeout(() => { eBusy = false; document.querySelectorAll('[data-eact]').forEach(x => x.disabled = false); elLoad(); }, 1200);
});
$('elSave').addEventListener('click', async () => {
  const body = {maxLoss: $('elLoss').value, maxContracts: $('elCap').value, minFree: $('elFree').value, maker: $('elMaker').value};
  try { const r = await api('/api/ethlive/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); toast(r.msg || 'OK'); elDirty = false; }
  catch (e) { toast(e.message || 'Gagal'); }
  setTimeout(elLoad, 500);
});
$('elToggle').addEventListener('click', async () => {
  const on = !(EL && EL.on);
  if (on && !confirm('Hidupkan ETH LIVE? Order SUNGGUHAN di ETH-USDT-SWAP (ukuran minimum). Backtest ETH tidak lolos.')) return;
  if (!on && !confirm('Matikan ETH live? Posisi ETH yang ada tidak ditutup, tetap dikelola TP/SL/batas rugi.')) return;
  try { const r = await api('/api/ethlive/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({on: on ? 1 : 0})}); toast(r.msg || 'OK'); } catch (e) { toast(e.message || 'Gagal'); }
  setTimeout(elLoad, 500);
});
setInterval(elLoad, 15000); setTimeout(elLoad, 1200);

async function tick() {
  try {
    D = await api('/api/snapshot');
    failCount = 0;
    render(D);
  } catch (e) {
    failCount++;
    $('dot').className = 'dot err';
    $('stateTxt').textContent = failCount > 1 ? 'Terputus' : 'Memuat';
    if (failCount === 2) toast(e.message || 'Bot tidak merespons');
  }
}

function render(d) {
  const dec = pxDec(d.last || 0);
  $('inst').textContent = d.inst;  // ditimpa viewHdr() saat tampilan ETH
  $('mode').textContent = d.demo ? 'Demo' : 'Live';
  $('mode').className = 'badge ' + (d.demo ? 'demo' : 'live');
  $('pmode').textContent = d.posMode === 'long_short_mode' ? 'Hedge' : (d.posMode || '-');
  $('dot').className = 'dot' + (d.paused ? ' off' : '');
  $('stateTxt').textContent = d.paused ? 'Dijeda' : 'Berjalan';

  const p = $('last');
  p.textContent = fmt(d.last, dec);
  if (prevLast != null && d.last != null && d.last !== prevLast) p.className = 'price num ' + (d.last > prevLast ? 'up' : 'down');
  prevLast = d.last;
  $('usdt').textContent = fmt(d.usdt, 2);
  $('uplAll').textContent = sign(d.upl, 4);
  $('uplAll').className = cls(d.upl);

  $('gridTxt').textContent = d.grid === 'dca' ? 'DCA' : d.grid === 'off' ? 'Mati' : (fmt(d.gridLow, 0) + '–' + fmt(d.gridHigh, 0));
  $('tpslTxt').textContent = d.autoClose ? (d.tpPct + ' / ' + d.slPct) : 'Mati';
  $('ts').textContent = d.ts;

  renderPositions(d, dec);
  renderWin(d);
  renderControls(d);
  renderPresets(d);
  $('log').textContent = d.log || 'Belum ada log.';
  if ($('tab-chart').classList.contains('on')) draw();
  viewHdr();
}

function posCard(side, d, dec, ro) {
  const size = Number(d[side]), avg = Number(d[side + '_avg']), upl = Number(d[side + '_upl']);
  const lev = Number(d.lev) || 1, ct = Number(d.ctVal) || 0.01, mark = Number(d.last) || avg;
  const margin = size * ct * avg / lev;
  const roe = margin > 0 ? upl / margin * 100 : 0;
  const tp = Number(d.tp), sl = Number(d.sl);
  const isLong = side === 'long';
  const si = (d.split && d.grid === 'dca' && d.splitInfo) ? d.splitInfo[side] : null;
  let tpPx = isLong ? avg * (1 + tp) : avg * (1 - tp);
  if (si && si.lots.length) {
    const tps = si.lots.map(l => l.tp);
    tpPx = isLong ? Math.min(...tps) : Math.max(...tps);   // TP entry terdekat
  }
  let slSplit = null;
  if (si && si.lots.length) {
    const sls = si.lots.map(l => l.sl);
    slSplit = isLong ? Math.max(...sls) : Math.min(...sls);  // SL entry terdekat
  }
  let slPx = isLong ? avg * (1 - sl) : avg * (1 + sl);
  if (slSplit != null) slPx = slSplit;
  // 0 = SL, 1 = TP
  let f = (mark - slPx) / (tpPx - slPx);
  f = Math.max(0, Math.min(1, isNaN(f) ? 0.5 : f));
  const entryF = Math.max(0, Math.min(1, (avg - slPx) / (tpPx - slPx)));
  const meter = d.autoClose ? `
    <div class="meter">
      <div class="bar"><em style="left:${(entryF*100).toFixed(1)}%"></em><i style="left:${(f*100).toFixed(1)}%"></i></div>
      <div class="ends num"><span>${si ? 'SL terdekat' : 'SL'} ${fmt(slPx, dec)}</span><span class="r">${si ? 'TP terdekat' : 'TP'} ${fmt(tpPx, dec)}</span></div>
    </div>` : `<div class="ends" style="margin-top:12px">TP/SL otomatis mati. Posisi hanya ditutup manual atau oleh batas rugi.</div>`;
  return `
  <div class="pos ${side}">
    <div class="poshead">
      <span class="side ${isLong ? 'up' : 'down'}">${isLong ? 'Long' : 'Short'}</span>
      <span class="lev">${d.mgn === 'isolated' ? 'Isolated' : 'Cross'} ${lev}x</span>
      <div class="pnl num"><div class="big ${cls(upl)}">${sign(upl, 4)}</div><div class="roe ${cls(roe)}">ROE ${sign(roe, 2)}%</div></div>
    </div>
    <div class="grid3 num">
      <div><small>Ukuran (kontrak)</small><span>${fmt(size, 2)}</span></div>
      <div><small>Harga entry</small><span>${fmt(avg, dec)}</span></div>
      <div><small>Margin</small><span>${fmt(margin, 2)}</span></div>
    </div>
    ${meter}
    ${si ? lotList(si, dec) : dcaLine(side, d, dec)}
    ${ro ? `<div class="posfoot"><button class="ghost" data-eact="close_${side}" data-confirm="Tutup posisi ETH ${side}?">Tutup ${side}</button></div>` : `<div class="posfoot"><button class="ghost" data-act="close_${side}" data-confirm="Tutup posisi ${side}?">Tutup ${side}</button></div>`}
  </div>`;
}

function lotList(si, dec) {
  let h = '<div class="lots">';
  si.lots.forEach(l => {
    h += '<div class="lot num"><span class="lv">' + (l.lvl == null ? 'M' : 'L' + l.lvl) + '</span>' +
         '<span>' + fmt(l.sz, 2) + ' @ ' + fmt(l.entry, dec) + '<small>masuk</small></span>' +
         (l.trail ? '<span class="up">' + fmt(l.tp, dec) + '<small>trailing · puncak ' + fmt(l.peak, dec) + '</small></span>'
                  : '<span>' + fmt(l.tp, dec) + '<small>SL ' + fmt(l.sl, dec) + '</small></span>') +
         '<span class="' + cls(l.pnl) + '">' + sign(l.pnl, 4) + '</span></div>';
  });
  si.empty.forEach(e => {
    const st = e.locked ? 'dikunci setelah SL' : e.block ? 'diblokir ' + e.block : e.next ? 'berikutnya' : 'menunggu';
    h += '<div class="lot wait num"><span class="lv">L' + e.lvl + '</span><span>' + fmt(e.sz, 2) + ' @ ' + fmt(e.px, dec) +
         '<small>' + st + '</small></span><span>' + (e.tp != null ? fmt(e.tp, dec) + '<small>SL ' + fmt(e.sl, dec) + '</small>' : '-') + '</span><span' +
         (e.block ? ' class="down"' : '') + '>' + (e.locked ? 'terkunci' : e.block ? 'diblokir' : 'kosong') + '</span></div>';
  });
  return h + '</div>';
}

function dcaLine(side, d, dec) {
  if (d.grid !== 'dca' || !d.dcaState || !d.dcaState[side]) return '';
  const st = d.dcaState[side];
  const used = 'Safety <b>' + st.n + '/' + st.max + '</b>';
  if (st.next == null) return '<div class="dcaline">' + used + ' · semua safety terpakai, tinggal TP/SL</div>';
  return '<div class="dcaline num">' + used + ' · berikutnya <b>' + fmt(st.nextSz, 2) + '</b> kontrak di <b>' + fmt(st.next, dec) + '</b></div>';
}

function dcaPlan(base, mult, step, scale, max, d) {
  const px = Number(d.last) || 0, ct = Number(d.ctVal) || 0.01;
  const split = d.split, tpM = $('dTpMode').value, slM = $('dSlMode').value;
  const tpV = Number($('dTpVal').value) || 0, slV = Number($('dSlVal').value) || 0;
  const tp0 = Number(d.tp) * 100, sl0 = Number(d.sl) * 100;
  const tpAt = k => tpM === 'arith' ? tp0 + k * tpV : tp0 * Math.pow(tpV || 1, k);
  const slAt = k => slM === 'arith' ? sl0 + k * slV : sl0 * Math.pow(slV || 1, k);
  let usdt = 0, dev = 0, rows = [];
  for (let k = 0; k <= max; k++) {
    const u = base * Math.pow(mult, k);
    usdt += u;
    if (k > 0) dev += step * Math.pow(scale, k - 1);
    if (k <= 6) rows.push((split ? 'L' + k : (k === 0 ? 'Awal' : 'S' + k)) + ' ' + fmt(u, 1) + ' USDT' + (k ? ' @ −' + dev.toFixed(2) + '%' : '') +
      (split ? ' · TP +' + tpAt(k).toFixed(2) + '% · SL −' + slAt(k).toFixed(2) + '%' : ''));
  }
  return rows.join('<br/>') + '<br/>Total maks <b>' + fmt(usdt, 1) + ' USDT</b> · rentang <b>' + dev.toFixed(2) + '%</b>' +
         (px ? ' · ≈ ' + fmt(usdt / (ct * px), 2) + ' kontrak' : '');
}
/* ---------- leverage ---------- */
function liqPct(lev) { return Math.max(0, 100 / lev - 0.5); }   // perkiraan kasar, isolated
function levNeed(d) {
  // jarak terjauh yang harus aman: SL darurat, dan (mode DCA) rentang safety + SL bot
  let need = Number(d.emSl || 0) * 100;
  if (d.grid === 'dca' && d.dca) {
    let dev = 0;
    for (let k = 1; k <= d.dca.max; k++) dev += d.dca.step * Math.pow(d.dca.scale, k - 1) * 100;
    need = Math.max(need, dev + Number(d.sl || 0) * 100);
  }
  return need;
}
function renderAcc(d) {
  document.body.classList.toggle('live', !d.demo);
  $('accNow').textContent = d.demo ? 'Demo (uang virtual)' : 'LIVE (uang sungguhan)';
  const dm = $('accDemo'), lv = $('accLive');
  dm.className = 'demo' + (d.demo ? ' on' : '') + (d.hasDemo ? '' : ' na');
  lv.className = 'live' + (!d.demo ? ' on' : '') + (d.hasLive ? '' : ' na');
  let n = 'Pindah akun hanya bisa saat tidak ada posisi terbuka. Setelah pindah, bot otomatis dijeda.';
  if (!d.hasLive) n += ' Key LIVE belum diisi di .env (OKX_LIVE_API_KEY, OKX_LIVE_SECRET_KEY, OKX_LIVE_PASSPHRASE).';
  if (!d.hasDemo) n += ' Key DEMO belum diisi di .env.';
  $('accNote').textContent = n;
}

function renderLev(d) {
  const lev = Number(d.lev) || 0, need = levNeed(d), choices = d.levChoices || [];
  $('levNow').textContent = lev + 'x ' + (d.tdMode === 'isolated' ? 'isolated' : 'cross');
  const key = choices.join(',') + '|' + lev + '|' + need.toFixed(2);
  const box = $('levs');
  if (box.dataset.k !== key) {
    box.dataset.k = key;
    box.innerHTML = choices.map(x => '<button data-lev="' + x + '" class="' + (x === lev ? 'on' : '') + (liqPct(x) <= need ? ' bad' : '') + '">' + x + 'x</button>').join('');
  }
  const liq = liqPct(lev);
  let h = 'Likuidasi kira-kira <b>−' + liq.toFixed(1) + '%</b> dari harga rata-rata. Batas yang harus aman: <b>' + need.toFixed(1) + '%</b> (SL darurat' + (d.grid === 'dca' ? ', serta rentang safety + SL' : '') + ').<br/>';
  h += liq > need * 1.5 ? '<span class="ok">Aman: likuidasi jauh di luar SL.</span>'
     : liq > need ? '<span class="warn">Tipis: likuidasi hanya sedikit di luar SL. Pertimbangkan leverage lebih kecil.</span>'
     : '<span class="warn">Bahaya: posisi bisa terlikuidasi sebelum SL bekerja.</span>';
  if (d.tdMode !== 'isolated') h += '<br/>Mode cross: likuidasi bergantung pada seluruh saldo, angka di atas hanya gambaran.';
  h += '<br/>Leverage tidak mengubah untung/rugi dalam USDT, hanya margin yang terkunci.';
  $('levInfo').innerHTML = h;
}

function fillTpSl(d) {
  // isi input sesuai mode yang dipilih di dropdown (nilai tambahan dalam %, rasio apa adanya)
  const tpM = $('dTpMode').value, slM = $('dSlMode').value;
  $('lTpVal').textContent = tpM === 'arith' ? 'TP tambah per level (%)' : 'TP rasio per level';
  $('lSlVal').textContent = slM === 'arith' ? 'SL tambah per level (%)' : 'SL rasio per level';
  $('dTpVal').value = tpM === 'arith' ? +(Number(d.tpAdd) * 100).toFixed(3) : d.tpRatio;
  $('dSlVal').value = slM === 'arith' ? +(Number(d.slAdd) * 100).toFixed(3) : d.slRatio;
}
['dTpMode','dSlMode'].forEach(id => $(id).addEventListener('change', () => {
  if (!D) return;
  fillTpSl(D);
  $('dPlan').innerHTML = dcaPlan(+$('dBase').value, +$('dMult').value, +$('dStep').value, +$('dScale').value, +$('dMax').value, D);
}));

function renderTrend(d) {
  const t = d.trend || {};
  let h;
  if (!t.tf) h = '';
  else if (!t.on) h = 'Filter tren <b>mati</b>: safety order selalu boleh.';
  else if (t.val === null || t.val === undefined) h = 'Filter tren hidup, menunggu data candle ' + t.tf + ' (' + (t.bars || 0) + ' candle). Sementara tidak memblokir.';
  else {
    const v = (Number(t.val) * 100).toFixed(2), th = (Number(t.th) * 100).toFixed(2);
    const blk = t.blockLong ? 'long diblokir (tren turun)' : t.blockShort ? 'short diblokir (tren naik)' : 'long dan short boleh';
    h = 'Tren EMA' + t.fast + '/' + t.slow + ' ' + t.tf + ': <b>' + (t.val >= 0 ? '+' : '') + v + '%</b> (ambang ±' + th + '%) → <b>' + blk + '</b>'
      + (t.blockL0 ? ', termasuk entry awal' : ', safety saja');
  }
  $('dTrendInfo').innerHTML = h;
}
function renderLr(d) {
  const s = d.lr || {};
  let h;
  if (!('on' in s)) h = '';
  else if (!s.on) h = 'Filter LR <b>mati</b>: safety order tidak dicek terhadap channel regresi linier.';
  else if (s.z === null || s.z === undefined) h = 'Filter LR hidup, menunggu data candle 1H. Sementara tidak memblokir.';
  else {
    const blk = s.blockLong ? 'long SO diblokir (terentang ke bawah)' : s.blockShort ? 'short SO diblokir (terentang ke atas)' : 'long dan short SO boleh';
    h = 'LR channel 1H N=' + s.n + ' k=' + Number(s.k).toFixed(2) + ': mid <b>' + Number(s.mid).toFixed(1)
      + '</b> | atas <b>' + Number(s.upper).toFixed(1) + '</b> | bawah <b>' + Number(s.lower).toFixed(1)
      + '</b> | z=<b>' + (s.z >= 0 ? '+' : '') + Number(s.z).toFixed(2) + '</b> → <b>' + blk + '</b><br/>'
      + 'Safety long diblokir bila z &lt; −k, safety short bila z &gt; +k. L0 tidak diblokir.';
    if (s.lastBlock) h += '<br/>Blokir terakhir: ' + new Date(s.lastBlock.t * 1000).toLocaleTimeString()
      + ' ' + s.lastBlock.side + ' L' + s.lastBlock.lvl + ' z=' + Number(s.lastBlock.z).toFixed(2);
  }
  $('dLrInfo').innerHTML = h;
}
function renderSnr(d) {
  const s = d.snr || {};
  let h;
  if (!('on' in s)) h = '';
  else if (!s.on) h = 'Filter SnR <b>mati</b>: safety order tidak dicek terhadap support/resistance.';
  else if (!(s.res || []).length && !(s.sup || []).length) h = 'Filter SnR hidup, menunggu data candle. Sementara tidak memblokir.';
  else {
    const f = a => (a || []).map(x => Number(x).toFixed(1)).join(' / ');
    h = 'SnR (' + (s.src === 'pd' ? 'hari kemarin' : 'hari kemarin + 4H') + ', zona ' + (Number(s.zone) * 100).toFixed(2) + '%): '
      + 'R <b>' + f(s.res) + '</b> | S <b>' + f(s.sup) + '</b><br/>Safety long diblokir tepat di bawah R, safety short tepat di atas S. L0 tidak diblokir.';
    if (s.lastBlock) h += '<br/>Blokir terakhir: ' + new Date(s.lastBlock.t * 1000).toLocaleTimeString() + ' ' + s.lastBlock.side + ' L' + s.lastBlock.lvl + ' @ ' + Number(s.lastBlock.px).toFixed(1);
  }
  $('dSnrInfo').innerHTML = h;
}
function renderTg(d) {
  const g = d.tg || {};
  let h;
  if (!g.configured) h = 'Telegram <b>belum diatur</b>: isi OKX_TG_TOKEN dan OKX_TG_CHAT_ID di .env lalu restart bot.';
  else {
    h = 'Telegram tersambung (chat ' + (g.chat || '-') + '), notifikasi <b>' + (g.on ? 'hidup' : 'mati') + '</b>. Terkirim ' + (g.sent || 0)
      + ', gagal ' + (g.failed || 0) + '. Perintah: /status /pause /resume /help';
    if (g.lastErr) h += '<br/>Error terakhir: ' + String(g.lastErr).replace(/</g, '&lt;');
  }
  $('dTgInfo').innerHTML = h;
}
let dDirty = false, pDirty = false;  // isian yang sudah diedit tidak ditimpa refresh sampai disimpan
['input','change'].forEach(ev => { $('dform').addEventListener(ev, () => { dDirty = true; }); $('form').addEventListener(ev, () => { pDirty = true; }); });
function renderDca(d) {
  if (!d.dca) return;
  $('dcaOn').textContent = d.grid === 'dca' ? 'Aktif' : 'Tidak aktif (pilih mode DCA)';
  const ae = document.activeElement;
  if (!dDirty && !(ae && ae.closest && ae.closest('#dform'))) {
    $('dBase').value = d.dca.base; $('dMax').value = d.dca.max; $('dMult').value = d.dca.mult;
    $('dStep').value = +(d.dca.step * 100).toFixed(3); $('dScale').value = d.dca.scale;
    $('dTpMode').value = d.tpMode; $('dSlMode').value = d.slMode;
    fillTpSl(d);
    const t = d.trend || {};
    if (t.tf) {
      $('dTrend').value = t.on ? '1' : '0'; $('dTrendTf').value = t.tf;
      $('dTrendFast').value = t.fast; $('dTrendSlow').value = t.slow;
      $('dTrendTh').value = +(Number(t.th) * 100).toFixed(3); $('dTrendL0').value = t.blockL0 ? '1' : '0';
    }
    const s = d.snr || {};
    if ('on' in s) { $('dSnr').value = s.on ? '1' : '0'; $('dSnrZone').value = +(Number(s.zone) * 100).toFixed(3); }
    const lr = d.lr || {};
    if ('on' in lr) { $('dLr').value = lr.on ? '1' : '0'; $('dLrN').value = lr.n; $('dLrK').value = Number(lr.k).toFixed(2); }
    const mk = d.maker || {}, eqs = d.eqSize || {};
    if ('on' in mk) { $('dMaker').value = mk.on ? '1' : '0'; $('dMakerFb').value = mk.fallback ? '1' : '0'; }
    if ('on' in eqs) { $('dEqOn').value = eqs.on ? '1' : '0'; $('dEqRef').value = eqs.ref; $('dEqMin').value = eqs.min; $('dEqMax').value = eqs.max; }
    const tr = d.trail || {};
    if ('on' in tr) { $('dTrail').value = tr.on ? '1' : '0'; $('dTrailPct').value = +(Number(tr.pct) * 100).toFixed(3); }
    const g = d.tg || {};
    if ('on' in g) {
      $('dTg').value = g.on ? '1' : '0'; $('dTgHours').value = g.hours; $('dTgTrades').value = g.trades ? '1' : '0';
    }
  }
  renderTrend(d);
  renderSnr(d);
  renderLr(d);
  renderTg(d);
  tfpOpts($('dTfp'), d, d.tfPreset || 'custom', true); tfpOpts($('bTfp'), d, '', false);
  { const mk = d.maker || {}, s = mk.stats || {}, eqs = d.eqSize || {}, o = mk.orders || {};
    const od = sd => o[sd] ? sd + ' L' + o[sd].lvl + ' ' + o[sd].sz + ' @ ' + o[sd].px + (o[sd].fill ? ' (isi ' + o[sd].fill + ')' : '') : '';
    $('dMakerInfo').innerHTML = 'Maker: ' + (mk.on ? '<b>hidup</b>' : 'mati') + (s.placed != null ? ' · pasang ' + s.placed + ' · isi ' + s.filled +
      ' (sebagian ' + s.partial + ') · batal ' + s.canceled + ' · tolak ' + s.rejected + ' · fill rate ' + (s.fillRate == null ? '-' : s.fillRate + '%') +
      (s.fallback ? ' · fallback ' + s.fallback : '') : '') + ([od('long'), od('short')].filter(Boolean).join(' | ') ? '<br/>Order: ' + [od('long'), od('short')].filter(Boolean).join(' | ') : '') +
      '<br/>Ukuran L0: ' + (eqs.on ? '<b>' + Number(eqs.baseNow).toFixed(2) + ' USDT</b> (dcaBase ' + eqs.dcaBase + ' × equity/' + eqs.ref + ', ' + eqs.min + '-' + eqs.max + 'x)' +
      ((eqs.baseEff || {}).long || (eqs.baseEff || {}).short ? ' · tangga aktif L ' + ((eqs.baseEff || {}).long || '-') + ' / S ' + ((eqs.baseEff || {}).short || '-') : '') : (eqs.dcaBase != null ? eqs.dcaBase + ' USDT (tetap)' : '-')); }
  $('dPlan').innerHTML = dcaPlan(+$('dBase').value, +$('dMult').value, +$('dStep').value, +$('dScale').value, +$('dMax').value, d);
}
['dBase','dMax','dMult','dStep','dScale','dTpVal','dSlVal'].forEach(id => $(id).addEventListener('input', () => { if (D) $('dPlan').innerHTML = dcaPlan(+$('dBase').value, +$('dMult').value, +$('dStep').value, +$('dScale').value, +$('dMax').value, D); }));
$('btnDca').addEventListener('click', async () => {
  const body = {base:$('dBase').value, max:$('dMax').value, mult:$('dMult').value, step:$('dStep').value, scale:$('dScale').value,
                tpMode:$('dTpMode').value, slMode:$('dSlMode').value,
                trendOn:$('dTrend').value, trendTf:$('dTrendTf').value, trendFast:$('dTrendFast').value,
                trendSlow:$('dTrendSlow').value, trendTh:$('dTrendTh').value, trendBlockL0:$('dTrendL0').value,
                snrOn:$('dSnr').value, snrZone:$('dSnrZone').value,
                lrOn:$('dLr').value, lrN:$('dLrN').value, lrK:$('dLrK').value,
                trailOn:$('dTrail').value, trailPct:$('dTrailPct').value,
                makerOn:$('dMaker').value, makerFallbackTaker:$('dMakerFb').value,
                eqSizeOn:$('dEqOn').value, eqRef:$('dEqRef').value, eqSizeMin:$('dEqMin').value, eqSizeMax:$('dEqMax').value,
                tgOn:$('dTg').value, tgStatusHours:$('dTgHours').value, tgTrades:$('dTgTrades').value};
  if ($('dTpMode').value === 'arith') body.tpAdd = $('dTpVal').value; else body.tpRatio = $('dTpVal').value;
  if ($('dSlMode').value === 'arith') body.slAdd = $('dSlVal').value; else body.slRatio = $('dSlVal').value;
  $('btnDca').disabled = true;
  try {
    const r = await api('/api/dca', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    toast(r.msg || 'DCA disimpan');
    dDirty = false;
    document.activeElement && document.activeElement.blur();
    await tick();
  } catch (err) { toast(err.message || 'Gagal menyimpan DCA'); }
  $('btnDca').disabled = false;
});
$('btnTgTest').addEventListener('click', async () => {
  $('btnTgTest').disabled = true;
  try {
    const r = await api('/api/tgtest', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    toast(r.msg || 'Tes dikirim');
  } catch (err) { toast(err.message || 'Tes Telegram gagal'); }
  $('btnTgTest').disabled = false;
});

function winNet(side, d) {
  const size = Number(d[side]) || 0;
  if (size <= 0) return null;
  return Number(d[side + '_upl']) - Number(d.last) * (Number(d.ctVal) || 0.01) * size * 0.001;
}
function renderWin(d) {
  const nl = winNet('long', d), ns = winNet('short', d);
  const cell = (lbl, v) => '<div><small>' + lbl + '</small><span class="' + (v == null ? 'mute' : cls(v)) + '">' + (v == null ? 'Tidak ada' : sign(v, 4)) + '</span></div>';
  $('winRows').innerHTML = cell('Long bersih', nl) + cell('Short bersih', ns);
  $('cwLong').classList.toggle('off', !(nl > 0));
  $('cwShort').classList.toggle('off', !(ns > 0));
  const any = (nl > 0) || (ns > 0);
  $('cwAll').classList.toggle('off', !any);
  const tot = (nl > 0 ? nl : 0) + (ns > 0 ? ns : 0);
  $('cwAll').textContent = any ? ('Tutup yang untung (+' + fmt(tot, 4) + ')') : 'Belum ada yang untung';
  $('cwAll').dataset.confirm = any ? ('Tutup posisi yang untung? Perkiraan bersih +' + fmt(tot, 4) + ' USDT') : '';
  $('cwLong').dataset.confirm = nl > 0 ? ('Tutup long? Perkiraan bersih +' + fmt(nl, 4) + ' USDT') : '';
  $('cwShort').dataset.confirm = ns > 0 ? ('Tutup short? Perkiraan bersih +' + fmt(ns, 4) + ' USDT') : '';
}

function renderPositions(d, dec) {
  let h = '';
  if (d.long > 0) h += posCard('long', d, dec);
  if (d.short > 0) h += posCard('short', d, dec);
  $('positions').innerHTML = h || '<div class="empty">Tidak ada posisi terbuka.<br/>Bot akan membuka posisi sesuai grid, atau buka manual di bawah.</div>';
}

function setSw(id, on) { $(id).classList.toggle('on', !!on); }
function renderControls(d) {
  setSw('swPause', !d.paused);
  setSw('swEntry', d.entry === 'both');
  setSw('swTpsl', d.autoClose);
  setSw('swWin', d.closeOnWin);
  setSw('swSplit', d.split);
  $('splitSub').textContent = d.split
    ? (d.grid === 'dca' ? 'Hidup: tiap entry punya TP sendiri, level diisi ulang' : 'Hidup, tapi baru aktif di mode grid DCA')
    : 'Mati: semua entry digabung, TP dari harga rata-rata';
  const tpRule = d.tpMode === 'arith' ? ('+' + (Number(d.tpAdd) * 100).toFixed(2) + '%') : ('×' + d.tpRatio);
  const slRule = d.slMode === 'arith' ? ('+' + (Number(d.slAdd) * 100).toFixed(2) + '%') : ('×' + d.slRatio);
  $('tpslSub').textContent = (d.split && d.grid === 'dca')
    ? ('Per entry: TP ' + d.tpPct + ' ' + tpRule + '/level · SL ' + d.slPct + ' ' + slRule + '/level')
    : ('TP ' + d.tpPct + ' · SL ' + d.slPct);
  $('winSub').textContent = 'Tutup sisi yang untung ≥ ' + d.winUsdt + ' USDT';
  const gl = {off:'Mati', auto:'Auto', arith:'Arithmetic', up:'Trailing naik', down:'Trailing turun', dca:'DCA'};
  $('btnGrid').textContent = gl[d.grid] || d.grid;
  $('gridSub').textContent = d.grid === 'dca' ? ('Order awal ' + d.dca.base + ' USDT, ' + d.dca.max + ' safety')
    : d.grid === 'off' ? 'Ketuk untuk ganti mode' : ('Rentang ' + fmt(d.gridLow, 1) + ' – ' + fmt(d.gridHigh, 1));
  renderDca(d);
  renderLev(d);
  renderAcc(d);

  if (pDirty || (document.activeElement && document.activeElement.closest && document.activeElement.closest('#form'))) return;
  $('pSize').value = d.orderSize; $('pMaxL').value = d.maxLong; $('pMaxS').value = d.maxShort;
  $('pWin').value = d.winUsdt; $('pLoss').value = d.maxLoss; $('pCool').value = d.cooldown;
  const sel = $('pInst'), m = d.markets || [], key = m.join(',');
  if (sel.dataset.filled !== key) { sel.innerHTML = m.map(x => '<option>' + x + '</option>').join(''); sel.dataset.filled = key; }
  if (d.inst) sel.value = d.inst;
}

/* ---------- presets ---------- */
function pct2(x) { return (Number(x) * 100).toFixed(2).replace(/0$/, '').replace(/[.]0$/, '') + '%'; }
function pct(x) { return (Number(x) * 100).toFixed(1).replace(/[.]0$/, '') + '%'; }
function renderPresets(d) {
  const ps = d.presets || {}, box = $('presets');
  const key = Object.keys(ps).join(',') + '|' + d.preset;
  $('presetNow').textContent = d.preset && ps[d.preset] ? ps[d.preset].label : 'Manual';
  if (box.dataset.k === key) return;
  box.dataset.k = key;
  const order = ['konservatif', 'seimbang', 'agresif'].filter(k => ps[k]).concat(Object.keys(ps).filter(k => !['konservatif', 'seimbang', 'agresif'].includes(k)));
  box.innerHTML = order.map(k => [k, ps[k]]).map(([k, p]) => `
    <button class="pre${d.preset === k ? ' on' : ''}" data-preset="${k}">
      <b>${p.label}</b>
      <small>Awal ${p.baseUsdt} USDT<br/>${p.so} safety ×${p.mult}<br/>Jarak ${pct2(p.step)}<br/>TP ${pct2(p.tp)} / SL ${pct2(p.sl)}<br/>Rugi maks ${p.maxLoss}</small>
    </button>`).join('');
}

/* ---------- equity chart ---------- */
let chartPts = [];
function draw() {
  const svg = $('chart');
  const W = Math.round(svg.clientWidth) || 600, H = 220;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  let pts = (D && D.equity) || [];
  if (range > 0 && pts.length) { const tEnd = pts[pts.length - 1].t; pts = pts.filter(p => p.t >= tEnd - range); }
  chartPts = pts;
  if (pts.length < 2) {
    svg.innerHTML = '<text x="' + (W / 2) + '" y="110" text-anchor="middle" fill="#848e9c" font-size="13">Menunggu data equity (direkam tiap ±30 detik)</text>';
    $('eqNow').textContent = pts.length ? fmt(pts[0].eq, 2) : '-';
    ['eqChg','eqHi','eqLo','eqDd','x0','x1'].forEach(i => $(i).textContent = '-');
    return;
  }
  const v = pts.map(p => Number(p.eq));
  const lo = Math.min(...v), hi = Math.max(...v);
  const padV = Math.max((hi - lo) * 0.15, 0.01);
  const min = lo - padV, max = hi + padV, L = 0, R = 62;
  const X = i => L + i / (v.length - 1) * (W - L - R);
  const Y = y => 10 + (1 - (y - min) / (max - min)) * (H - 20);
  const up = v[v.length - 1] >= v[0];
  const col = up ? '#0ecb81' : '#f6465d';
  const line = v.map((y, i) => X(i).toFixed(1) + ',' + Y(y).toFixed(1)).join(' ');
  const area = 'M' + X(0).toFixed(1) + ',' + (H - 10) + ' L' + line.split(' ').join(' L') + ' L' + X(v.length - 1).toFixed(1) + ',' + (H - 10) + ' Z';
  let grid = '';
  for (let k = 0; k <= 3; k++) {
    const val = min + (max - min) * k / 3, y = Y(val).toFixed(1);
    grid += `<line x1="0" x2="${W - R + 4}" y1="${y}" y2="${y}" stroke="#232a35" stroke-dasharray="3 4"/>`;
    grid += `<text x="${W - 2}" y="${Number(y) + 4}" text-anchor="end" fill="#848e9c" font-size="11">${fmt(val, 2)}</text>`;
  }
  const lastY = Y(v[v.length - 1]).toFixed(1);
  svg.innerHTML = `
    <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${col}" stop-opacity=".28"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
    ${grid}
    <path d="${area}" fill="url(#g)"/>
    <polyline fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round" points="${line}"/>
    <line x1="0" x2="${W - R + 4}" y1="${lastY}" y2="${lastY}" stroke="${col}" stroke-opacity=".5" stroke-dasharray="2 3"/>
    <line id="cross" x1="0" x2="0" y1="0" y2="${H}" stroke="#848e9c" stroke-width="1" vector-effect="non-scaling-stroke" style="display:none"/>`;
  const chg = v[v.length - 1] - v[0], pct = v[0] ? chg / v[0] * 100 : 0;
  let peak = -Infinity, dd = 0;
  v.forEach(y => { peak = Math.max(peak, y); dd = Math.min(dd, y - peak); });
  $('eqNow').textContent = fmt(v[v.length - 1], 2);
  $('eqChg').textContent = sign(chg, 2) + ' (' + sign(pct, 3) + '%)';
  $('eqChg').className = cls(chg);
  $('eqHi').textContent = fmt(hi, 2); $('eqLo').textContent = fmt(lo, 2);
  $('eqDd').textContent = fmt(dd, 2); $('eqDd').className = dd < 0 ? 'down' : '';
  const tf = t => new Date(t * 1000).toLocaleTimeString('id-ID', {hour:'2-digit', minute:'2-digit'});
  $('x0').textContent = tf(pts[0].t); $('x1').textContent = tf(pts[pts.length - 1].t);
}
function hover(ev) {
  if (chartPts.length < 2) return;
  const svg = $('chart'), r = svg.getBoundingClientRect();
  const cx = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
  const plotW = r.width - 62;
  const i = Math.max(0, Math.min(chartPts.length - 1, Math.round(cx / plotW * (chartPts.length - 1))));
  const x = i / (chartPts.length - 1) * plotW;
  const c = svg.querySelector('#cross');
  if (c) { const vx = x.toFixed(1); c.setAttribute('x1', vx); c.setAttribute('x2', vx); c.style.display = ''; }
  const p = chartPts[i], tip = $('tip');
  tip.textContent = fmt(p.eq, 2) + '  ' + new Date(p.t * 1000).toLocaleTimeString('id-ID', {hour:'2-digit', minute:'2-digit'});
  tip.style.display = 'block';
  tip.style.left = Math.max(0, Math.min(r.width - tip.offsetWidth, x - tip.offsetWidth / 2)) + 'px';
}
function unhover() { $('tip').style.display = 'none'; const c = $('chart').querySelector('#cross'); if (c) c.style.display = 'none'; }
['mousemove','touchmove','touchstart'].forEach(e => $('chart').addEventListener(e, hover, {passive:true}));
['mouseleave','touchend'].forEach(e => $('chart').addEventListener(e, unhover));
window.addEventListener('resize', () => { if ($('tab-chart').classList.contains('on')) { draw(); pxDraw(); } });
$('rng').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  range = Number(b.dataset.r);
  document.querySelectorAll('#rng button').forEach(x => x.classList.toggle('on', x === b));
  draw();
});

/* ---------- grafik harga (refresh sendiri, tidak ikut tick 5 detik) ---------- */
const PX = {bar: localStorage.getItem('pxBar') || '5m', data: null, n: 120, off: 0, busy: false, drag: null,
  on: Object.assign({entry:true, tp:true, sl:true, pend:true, avg:true, esl:true, snr:true, lr:true, marks:true, fit:false},
                    (() => { try { return JSON.parse(localStorage.getItem('pxOn') || '{}'); } catch (e) { return {}; } })())};
const PXC = {long:'#0ecb81', short:'#f6465d', esl:'#f0b90b', res:'#c084fc', sup:'#60a5fa', lrU:'#f59e0b', lrL:'#f59e0b', lrM:'#fbbf24'};
const PXK = {entry:'entry', tp:'tp', sl:'sl', pend:'pend', avg:'avg', esl:'esl', res:'snr', sup:'snr', lrU:'lr', lrL:'lr', lrM:'lr'};
function pxShown() { return $('tab-chart').classList.contains('on') && !document.hidden; }
function pxEsc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function pxStyle(l) {
  const col = (l.kind === 'res' || l.kind === 'sup' || l.kind === 'lrU' || l.kind === 'lrL' || l.kind === 'lrM')
    ? PXC[l.kind] : l.kind === 'esl' ? PXC.esl : PXC[l.side] || '#848e9c';
  const dash = {entry:'', tp:'6 4', sl:'2 3', pend:'2 5', avg:'10 3 2 3', esl:'1 3', res:'8 6', sup:'8 6',
                lrU:'4 3', lrL:'4 3', lrM:'1 4'}[l.kind] || '';
  const op = l.kind === 'pend' ? 0.4 : (l.kind === 'res' || l.kind === 'sup') ? 0.75
    : (l.kind === 'lrU' || l.kind === 'lrL' || l.kind === 'lrM') ? 0.7 : 0.9;
  const w = l.kind === 'avg' || l.kind === 'esl' ? 1.6 : 1.1;
  return {col, dash, op, w};
}
function pxCoinQ() { return VIEW === 'ETH' ? '&instId=ETH-USDT-SWAP' : ''; }   // BTC: tanpa parameter (perilaku lama)
function pxCoinSync(reload) {
  document.querySelectorAll('#pxCoin button').forEach(x => x.classList.toggle('on', x.dataset.c === VIEW));
  if (reload && PX.coin !== VIEW) { PX.coin = VIEW; PX.data = null; PX.off = 0; pxDraw(); if (pxShown()) pxLoad(true); }
}
async function pxLoad(force) {
  if (PX.busy) { if (force) PX.again = true; return; }
  if (!force && !pxShown()) return;
  PX.busy = true;
  const bar = PX.bar, coin = VIEW;
  try {
    const d = await api('/api/chart?bar=' + encodeURIComponent(bar) + pxCoinQ());
    if (bar === PX.bar && coin === VIEW && d) {
      const old = PX.data && PX.data.inst === d.inst ? PX.data : null;
      if (d.candles && d.candles.length) {
        if (old && old.bar === d.bar && PX.off > 0 && old.candles.length) {  // sedang melihat ke belakang: tetap di tempat
          const add = Math.round((d.candles[d.candles.length - 1][0] - old.candles[old.candles.length - 1][0]) / (d.barSec * 1000));
          if (add > 0) PX.off += add;
        }
        PX.data = d;
      } else if (!old || old.bar !== d.bar) PX.data = d;
      else { PX.data = Object.assign({}, old, {levels: d.levels || old.levels, marks: d.marks || old.marks, err: d.err, stale: true}); }
      pxDraw();
    }
  } catch (e) { $('pxInfo').textContent = 'Grafik gagal dimuat: ' + (e.message || 'bot tidak merespons') + ' (dicoba lagi)'; }
  PX.busy = false;
  if (PX.again || coin !== VIEW || bar !== PX.bar) { PX.again = false; pxLoad(true); }
}
function pxFmtT(ms, bar) {
  const t = new Date(ms), p = v => String(v).padStart(2, '0');
  return (bar === '1H' ? p(t.getDate()) + '/' + p(t.getMonth() + 1) + ' ' : '') + p(t.getHours()) + ':' + p(t.getMinutes());
}
function pxDraw() {
  const svg = $('pxChart'); if (!svg) return;
  const W = Math.round(svg.clientWidth) || 600, H = 320, R = 92, T = 8, B = 18;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  document.querySelectorAll('#pxBar button').forEach(x => x.classList.toggle('on', x.dataset.b === PX.bar));
  const d = PX.data;
  if (!d || !d.candles || !d.candles.length) {
    svg.innerHTML = '<text x="' + (W / 2) + '" y="160" text-anchor="middle" fill="#848e9c" font-size="13">' +
      (d && d.err ? 'Candle belum tersedia: ' + pxEsc(d.err) : 'Memuat candle ' + PX.bar + '...') + '</text>';
    $('pxLeg').innerHTML = ''; $('pxInfo').textContent = d && d.err ? 'Gagal: ' + d.err : 'Memuat grafik...';
    return;
  }
  $('pxInst').textContent = d.inst || '';
  const C = d.candles, n = Math.max(20, Math.min(PX.n, C.length));
  PX.n = n; PX.off = Math.max(0, Math.min(PX.off, C.length - n));
  const end = C.length - PX.off, vis = C.slice(end - n, end), cw = (W - R) / n;
  const lv = (d.levels || []).filter(l => PX.on[PXK[l.kind]]);
  let lo = Math.min(...vis.map(c => c[3])), hi = Math.max(...vis.map(c => c[2]));
  if (d.last) { lo = Math.min(lo, d.last); hi = Math.max(hi, d.last); }
  // default: posisi (entry, rata-rata, TP) selalu terlihat; SL/safety/SnR jauh -> label tepi. "Muat semua garis" = semuanya.
  lv.forEach(l => { if (PX.on.fit || l.kind === 'entry' || l.kind === 'avg' || l.kind === 'tp') { lo = Math.min(lo, l.px); hi = Math.max(hi, l.px); } });
  const pad = Math.max((hi - lo) * 0.06, hi * 0.0005), min = lo - pad, max = hi + pad;
  const X = i => (i + 0.5) * cw, Y = p => T + (1 - (p - min) / (max - min)) * (H - T - B);
  const dec = pxDec(d.last || hi);
  let g = '';
  for (let k = 0; k <= 4; k++) {
    const val = min + (max - min) * k / 4, y = Y(val).toFixed(1);
    g += `<line x1="0" x2="${W - R}" y1="${y}" y2="${y}" stroke="#232a35"/><text x="3" y="${Number(y) - 3}" fill="#5e6673" font-size="10">${fmt(val, dec)}</text>`;
  }
  const step = Math.max(1, Math.round(n / 4));
  for (let i = step; i < n; i += step) {
    const x = X(i).toFixed(1);
    g += `<line x1="${x}" x2="${x}" y1="${T}" y2="${H - B}" stroke="#1b2029"/><text x="${x}" y="${H - 4}" text-anchor="middle" fill="#848e9c" font-size="10">${pxFmtT(vis[i][0], d.bar)}</text>`;
  }
  let cs = '';
  const bw = Math.max(1, cw * 0.7);
  vis.forEach((c, i) => {
    const up = c[4] >= c[1], col = up ? '#0ecb81' : '#f6465d', x = X(i);
    const y1 = Y(Math.max(c[1], c[4])), y2 = Y(Math.min(c[1], c[4]));
    cs += `<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${Y(c[2]).toFixed(1)}" y2="${Y(c[3]).toFixed(1)}" stroke="${col}" stroke-width="1"/>` +
          `<rect x="${(x - bw / 2).toFixed(1)}" y="${y1.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, y2 - y1).toFixed(1)}" fill="${col}"${c[5] ? '' : ' fill-opacity=".55"'}/>`;
  });
  let ls = ''; const labels = [], up = [], dn = [];
  lv.forEach(l => {
    const st = pxStyle(l), y = Y(l.px);
    const name = (l.side ? (l.side === 'long' ? 'L ' : 'S ') : '') + (l.kind === 'entry' ? 'entry ' : '') + l.label;
    if (y < T || y > H - B) { (y < T ? up : dn).push({px: l.px, col: st.col, op: Math.max(st.op, .7), txt: (y < T ? '▲ ' : '▼ ') + name + ' ' + fmt(l.px, dec)}); return; }
    ls += `<line x1="0" x2="${W - R}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${st.col}" stroke-opacity="${st.op}" stroke-width="${st.w}"${st.dash ? ` stroke-dasharray="${st.dash}"` : ''}/>`;
    labels.push({y, col: st.col, op: st.op, txt: name + ' ' + fmt(l.px, dec)});
  });
  if (d.last) { const y = Y(d.last); ls += `<line x1="0" x2="${W - R}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="#eaecef" stroke-opacity=".6" stroke-dasharray="1 2"/>`;
                labels.push({y, col: '#eaecef', op: 1, txt: fmt(d.last, dec), last: true}); }
  // garis di luar layar: tampilkan maks 3 terdekat per tepi + ringkasan sisanya (detail lengkap ada di legenda)
  up.sort((a, b) => a.px - b.px); dn.sort((a, b) => b.px - a.px);
  [[up, T + 6, '▲'], [dn, H - B - 6, '▼']].forEach(([arr, y, ar]) => {
    const show = arr.slice(0, 3).map(a => Object.assign(a, {y, edge: true}));
    const more = arr.length > 3 ? [{y, col: '#848e9c', op: 1, edge: true, txt: ar + ' +' + (arr.length - 3) + ' garis lagi'}] : [];
    labels.push(...(ar === '▲' ? more.concat(show.reverse()) : show.concat(more)));  // terdekat paling dekat ke grafik
  });
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) if (labels[i].y - labels[i - 1].y < 12) labels[i].y = labels[i - 1].y + 12;
  for (let i = labels.length - 1; i >= 0; i--) { const lim = i === labels.length - 1 ? H - B - 4 : labels[i + 1].y - 12; if (labels[i].y > lim) labels[i].y = lim; }
  let lb = '';
  labels.forEach(a => {
    lb += `<rect x="${W - R + 2}" y="${(a.y - 6).toFixed(1)}" width="${R - 3}" height="12" rx="2" fill="${a.last ? '#eaecef' : '#14181f'}" fill-opacity="${a.last ? 1 : .9}"/>` +
          `<text x="${W - R + 5}" y="${(a.y + 3.5).toFixed(1)}" fill="${a.last ? '#0b0e11' : a.col}" fill-opacity="${a.last ? 1 : a.op}" font-size="9.5"${a.last ? ' font-weight="700"' : ''}>${pxEsc(a.txt)}</text>`;
  });
  let mk = '';
  if (PX.on.marks) (d.marks || []).forEach(m => {
    const i = Math.floor((m.t - vis[0][0]) / (d.barSec * 1000));
    if (i < 0 || i >= n || m.px < min || m.px > max) return;
    const x = X(i), y = Y(m.px), col = PXC[m.side] || '#eaecef';
    if (m.kind === 'open') mk += m.side === 'long' ? `<path d="M${x},${y + 3} l-4,7 h8 z" fill="${col}" stroke="#0b0e11" stroke-width=".6"/>` : `<path d="M${x},${y - 3} l-4,-7 h8 z" fill="${col}" stroke="#0b0e11" stroke-width=".6"/>`;
    else if (m.kind === 'tp') mk += `<circle cx="${x}" cy="${y}" r="3.5" fill="#f0b90b" stroke="#0b0e11" stroke-width=".6"/>`;
    else mk += `<path d="M${x - 3.5},${y - 3.5} l7,7 M${x + 3.5},${y - 3.5} l-7,7" stroke="${col}" stroke-width="1.6"/>`;
  });
  svg.innerHTML = g + cs + ls + mk + lb +
    `<line id="pxX" x1="0" x2="0" y1="${T}" y2="${H - B}" stroke="#848e9c" stroke-dasharray="2 2" style="display:none"/>` +
    `<line id="pxY" x1="0" x2="${W - R}" y1="0" y2="0" stroke="#848e9c" stroke-dasharray="2 2" style="display:none"/>`;
  PX.view = {vis, X, Y, cw, min, max, T, B, H, W, R, dec};
  $('pxInfo').textContent = d.bar + ' · ' + n + ' candle' + (PX.off ? ' · digeser ' + PX.off : ' · terbaru') +
    (d.stale ? ' · data lama' + (d.err ? ' (' + d.err + ')' : '') : '');
  pxLegend(d, dec);
}
function pxLegend(d, dec) {
  const L = d.levels || [], out = [];
  ['long', 'short'].forEach(sd => {
    const ent = L.filter(l => l.side === sd && l.kind === 'entry');
    const rows = ent.map(e => {
      const tp = L.find(l => l.side === sd && l.kind === 'tp' && l.lot === e.lot);
      const sl = L.find(l => l.side === sd && l.kind === 'sl' && l.lot === e.lot);
      return `<b>${pxEsc(e.label)}</b> ${fmt(e.px, dec)}${e.sz ? ' (' + e.sz + ')' : ''} → TP ${tp ? fmt(tp.px, dec) : '-'} · SL ${sl ? fmt(sl.px, dec) : '-'}`;
    });
    const avg = L.find(l => l.side === sd && l.kind === 'avg'), esl = L.find(l => l.side === sd && l.kind === 'esl');
    const pend = L.filter(l => l.side === sd && l.kind === 'pend').map(l => pxEsc(l.label) + ' ' + fmt(l.px, dec));
    const col = PXC[sd];
    if (!rows.length && !pend.length) { out.push(`<span style="color:${col}">${sd === 'long' ? 'Long' : 'Short'}</span>: kosong`); return; }
    out.push(`<span style="color:${col}">${sd === 'long' ? 'Long' : 'Short'}</span>` + (avg ? ` rata-rata <b>${fmt(avg.px, dec)}</b>` : '') +
      (esl ? ` · SL darurat ±${fmt(esl.px, dec)}` : '') + '<br/>' + rows.join('<br/>') + (pend.length ? '<br/>Safety kosong: ' + pend.join(', ') : ''));
  });
  const R_ = L.filter(l => l.kind === 'res').map(l => fmt(l.px, dec)), S_ = L.filter(l => l.kind === 'sup').map(l => fmt(l.px, dec));
  if (R_.length || S_.length) out.push(`<span style="color:${PXC.res}">R</span> ${R_.join(' / ') || '-'} · <span style="color:${PXC.sup}">S</span> ${S_.join(' / ') || '-'}`);
  const lrU = L.find(l => l.kind === 'lrU'), lrL = L.find(l => l.kind === 'lrL'), lrM = L.find(l => l.kind === 'lrM');
  if (lrU || lrL) out.push(`<span style="color:${PXC.lrU}">LR</span> atas ${lrU ? fmt(lrU.px, dec) : '-'} · mid ${lrM ? fmt(lrM.px, dec) : '-'} · bawah ${lrL ? fmt(lrL.px, dec) : '-'}`);
  if (!d.split) out.push('Garis entry/TP/SL tampil di mode DCA entry terpisah.');
  out.push('<i style="border-color:#848e9c"></i>entry <i style="border-top-style:dashed;border-color:#848e9c"></i>TP <i style="border-top-style:dotted;border-color:#848e9c"></i>SL · ▲▼ isi, ● TP, ✕ SL');
  $('pxLeg').innerHTML = out.join('<br/>');
}
document.querySelectorAll('#pxTog input').forEach(cb => {
  cb.checked = !!PX.on[cb.dataset.k];
  cb.addEventListener('change', () => { PX.on[cb.dataset.k] = cb.checked; try { localStorage.setItem('pxOn', JSON.stringify(PX.on)); } catch (e) {} pxDraw(); });
});
PX.coin = VIEW; window.__pxReady = true; pxCoinSync(false);
$('pxCoin').addEventListener('click', e => { const b = e.target.closest('[data-c]'); if (b && b.dataset.c !== VIEW) setView(b.dataset.c, true); });
$('pxBar').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b || b.dataset.b === PX.bar) return;
  PX.bar = b.dataset.b; PX.off = 0; PX.data = null; try { localStorage.setItem('pxBar', PX.bar); } catch (err) {}
  pxDraw(); pxLoad(true);
});
function pxZoom(f) { PX.n = Math.round(Math.max(20, Math.min(200, PX.n * f))); pxDraw(); }
$('pxIn').addEventListener('click', () => pxZoom(1 / 1.3));
$('pxOut').addEventListener('click', () => pxZoom(1.3));
$('pxNow').addEventListener('click', () => { PX.off = 0; pxDraw(); });
$('pxChart').addEventListener('wheel', e => { e.preventDefault(); pxZoom(e.deltaY > 0 ? 1.15 : 1 / 1.15); }, {passive: false});
$('pxChart').addEventListener('pointerdown', e => { PX.drag = {x: e.clientX, off: PX.off}; });
window.addEventListener('pointerup', () => { PX.drag = null; });
$('pxChart').addEventListener('pointermove', e => {
  const v = PX.view, svg = $('pxChart'); if (!v) return;
  const rect = svg.getBoundingClientRect(), sx = v.W / rect.width;
  if (PX.drag) {
    const dc = Math.round((e.clientX - PX.drag.x) * sx / v.cw);
    const nOff = Math.max(0, PX.drag.off + dc);
    if (nOff !== PX.off) { PX.off = nOff; pxDraw(); }
    return;
  }
  const x = (e.clientX - rect.left) * sx, y = (e.clientY - rect.top) * (v.H / rect.height);
  const i = Math.floor(x / v.cw); if (i < 0 || i >= v.vis.length || x > v.W - v.R) { pxUnhover(); return; }
  const c = v.vis[i], px = v.min + (1 - (y - v.T) / (v.H - v.T - v.B)) * (v.max - v.min);
  const cx = $('pxX'), cy = $('pxY');
  if (cx) { cx.setAttribute('x1', v.X(i)); cx.setAttribute('x2', v.X(i)); cx.style.display = ''; }
  if (cy) { cy.setAttribute('y1', y); cy.setAttribute('y2', y); cy.style.display = ''; }
  const tip = $('pxTip');
  tip.innerHTML = pxFmtT(c[0], PX.data.bar) + ' O ' + fmt(c[1], v.dec) + ' H ' + fmt(c[2], v.dec) + ' L ' + fmt(c[3], v.dec) + ' C ' + fmt(c[4], v.dec) + ' · ' + fmt(px, v.dec);
  tip.style.display = 'block'; tip.style.left = '4px';
});
function pxUnhover() { ['pxX', 'pxY'].forEach(id => { const el = $(id); if (el) el.style.display = 'none'; }); $('pxTip').style.display = 'none'; }
$('pxChart').addEventListener('pointerleave', pxUnhover);
document.addEventListener('visibilitychange', () => { if (pxShown()) pxLoad(true); });
setInterval(() => pxLoad(false), 15000);

/* ---------- actions ---------- */
let busy = false;
document.addEventListener('click', async e => {
  const t = e.target.closest('[data-tab]');
  if (t) {
    document.querySelectorAll('nav button').forEach(b => b.classList.toggle('on', b === t));
    document.querySelectorAll('section.tab').forEach(s => s.classList.toggle('on', s.id === 'tab-' + t.dataset.tab));
    if (t.dataset.tab === 'chart') { draw(); pxDraw(); pxLoad(true); }
    if (t.dataset.tab === 'bt') btOpen();
    window.scrollTo(0, 0);
    return;
  }
  const ac = e.target.closest('[data-acc]');
  if (ac && !busy && D) {
    const target = ac.dataset.acc;
    if ((target === 'demo') === !!D.demo) return;
    if (D.long > 0 || D.short > 0) { toast('Tutup semua posisi dulu sebelum pindah akun.'); return; }
    let confirmTxt = '';
    if (target === 'live') {
      confirmTxt = prompt('Pindah ke akun LIVE (uang sungguhan).\nSetting bot sekarang akan dipakai untuk order sungguhan.\n\nKetik LIVE untuk melanjutkan:') || '';
      if (confirmTxt !== 'LIVE') { toast('Dibatalkan'); return; }
    } else if (!confirm('Pindah ke akun DEMO?')) return;
    busy = true;
    try {
      const r = await api('/api/account', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({target, confirm:confirmTxt})});
      toast(r.msg || 'Selesai');
      prevLast = null;
      await tick();
    } catch (err) { toast(err.message || 'Gagal pindah akun'); }
    setTimeout(() => { busy = false; }, 1500);
    return;
  }
  const lv = e.target.closest('[data-lev]');
  if (lv && !busy && D) {
    const x = Number(lv.dataset.lev);
    if (x === Number(D.lev)) return;
    if (D.long > 0 || D.short > 0) { toast('Tutup semua posisi dulu sebelum ganti leverage.'); return; }
    const risky = liqPct(x) <= levNeed(D);
    if (!confirm('Ganti leverage ke ' + x + 'x?' + (risky ? ' PERINGATAN: likuidasi bisa terjadi sebelum SL.' : ''))) return;
    busy = true;
    try {
      const r = await api('/api/leverage', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({lev:x})});
      toast(r.msg || 'Selesai');
      await tick();
    } catch (err) { toast(err.message || 'Gagal ganti leverage'); }
    setTimeout(() => { busy = false; }, 1200);
    return;
  }
  const pr = e.target.closest('[data-preset]');
  if (pr && !busy) {
    const p = (D && D.presets || {})[pr.dataset.preset];
    if (!p || !confirm('Terapkan preset ' + p.label + '?' + ((D.long > 0 || D.short > 0) ? ' Posisi yang terbuka tidak ditutup, tapi TP/SL-nya ikut berubah.' : ''))) return;
    busy = true;
    try {
      const r = await api('/api/preset', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({preset:pr.dataset.preset})});
      toast(r.ok ? ('Preset ' + p.label + ' aktif') : (r.msg || 'Gagal'));
      await tick();
    } catch (err) { toast(err.message || 'Gagal menerapkan preset'); }
    setTimeout(() => { busy = false; }, 1200);
    return;
  }
  const b = e.target.closest('[data-act]');
  if (!b || busy) return;
  if (b.dataset.confirm && !confirm(b.dataset.confirm)) return;
  busy = true;
  document.querySelectorAll('[data-act]').forEach(x => x.disabled = true);
  try {
    const r = await api('/api/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:b.dataset.act})});
    toast(r.msg || 'Selesai');
    await tick();
  } catch (err) { toast(err.message || 'Gagal mengirim perintah'); }
  setTimeout(() => { busy = false; document.querySelectorAll('[data-act]').forEach(x => x.disabled = false); }, 1200);
});

$('btnSave').addEventListener('click', async () => {
  const body = {
    orderSize:$('pSize').value, maxLong:$('pMaxL').value, maxShort:$('pMaxS').value,
    winUsdt:$('pWin').value, maxLoss:$('pLoss').value, cooldown:$('pCool').value,
    inst:$('pInst').value,
  };
  $('btnSave').disabled = true;
  try {
    const r = await api('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    toast(r.msg || 'Parameter disimpan');
    pDirty = false;
    document.activeElement && document.activeElement.blur();
    await tick();
  } catch (err) { toast(err.message || 'Gagal menyimpan'); }
  $('btnSave').disabled = false;
});

/* ---------- backtest (proses terpisah; tidak pernah mengubah setting live) ---------- */
const BT = {live:null, loaded:false, timer:null, res:null, cur:null, runs:[]};
const BT_F = [ // [id input, kunci parameter, jenis]  pct = ditampilkan dalam %
  ['bInst','inst','str'], ['bSplit','split','bool'], ['bEq','equity','num'], ['bSizeMode','sizeMode','str'],
  ['bBase','baseUsdt','num'], ['bFixed','fixedSize','num'], ['bMult','mult','num'], ['bSo','so','int'],
  ['bStep','step','pct'], ['bScale','scale','num'], ['bLev','leverage','num'],
  ['bTp','tp','pct'], ['bTpMode','tpMode','str'], ['bTpRatio','tpRatio','num'], ['bTpAdd','tpAdd','pct'],
  ['bSl','sl','pct'], ['bSlMode','slMode','str'], ['bSlAdd','slAdd','pct'], ['bSlRatio','slRatio','num'],
  ['bEm','emSl','pct'], ['bMl','maxLoss','num'], ['bCool','cooldown','int'],
  ['bTrend','trendOn','bool'], ['bTrendTf','trendTf','str'], ['bFast','trendFast','int'], ['bSlow','trendSlow','int'],
  ['bTh','trendTh','pct'], ['bL0','trendBlockL0','bool'], ['bSnr','snrOn','bool'], ['bZone','snrZone','pct'],
  ['bLr','lrOn','bool'], ['bLrN','lrN','int'], ['bLrK','lrK','num'],
  ['bTrail','trailOn','bool'], ['bTrailPct','trailPct','pct'],
  ['bMaker','makerOn','bool'], ['bMakerThr','makerThr','pct'], ['bEqOn','eqSizeOn','bool'], ['bEqRef','eqRef','num'], ['bEqMin','eqSizeMin','num'], ['bEqMax','eqSizeMax','num'],
  ['bFee','fee','pct'], ['bSlip','slipOn','bool'],
];
function btEsc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function btDate(ms, t) { const d = new Date(ms); const p = n => String(n).padStart(2, '0');
  return p(d.getDate()) + '/' + p(d.getMonth() + 1) + '/' + String(d.getFullYear()).slice(2) + (t ? ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) : ''); }
function btIso(ms) { const d = new Date(ms); const p = n => String(n).padStart(2, '0'); return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()); }
function btModes() {
  const fx = $('bSizeMode').value === 'fixed', geoT = $('bTpMode').value === 'geo', geoS = $('bSlMode').value === 'geo';
  $('bBaseL').hidden = fx; $('bMultL').hidden = fx; $('bFixedL').hidden = !fx;
  $('bTpRatioL').hidden = !geoT; $('bTpAddL').hidden = geoT; $('bSlRatioL').hidden = !geoS; $('bSlAddL').hidden = geoS;
  const rg = $('bDays').value === 'range'; $('bFromL').hidden = !rg; $('bToL').hidden = !rg;
  if (rg && !$('bFrom').value) { const now = Date.now(); $('bFrom').value = btIso(now - 60 * 864e5); $('bTo').value = btIso(now); }
}
function tfpOpts(sel, d, cur, withCustom) {
  const ps = (d && d.tfPresets) || {}, k = Object.keys(ps).join(',') + '|' + cur;
  if (sel.dataset.k === k) return; sel.dataset.k = k;
  sel.innerHTML = (withCustom ? '<option value="custom">Custom</option>' : '<option value="">(isi manual)</option>') +
    Object.keys(ps).map(n => '<option value="' + n + '">' + ps[n].label + '</option>').join('');
  if (document.activeElement !== sel) sel.value = cur;
}
document.addEventListener('change', e => {
  if (e.target && e.target.id === 'bTfp') { const p = ((D && D.tfPresets) || {})[e.target.value]; if (p) btFill(p); }
});
document.addEventListener('click', async e => {
  if (!e.target || e.target.id !== 'dTfpApply') return;
  const n = $('dTfp').value, p = ((D && D.tfPresets) || {})[n];
  if (!p) { toast('Pilih preset dulu'); return; }
  if (!confirm('Terapkan preset ' + p.label + '? Jarak/TP/SL/trailing/filter berubah; posisi terbuka tidak ditutup, TP/SL-nya ikut berubah.')) return;
  try { const r = await api('/api/tfpreset', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({preset:n})}); toast(r.msg || 'OK'); $('dTfp').dataset.k = ''; await tick(); }
  catch (err) { toast(err.message || 'Gagal'); }
});
function btFill(p) {
  BT_F.forEach(([id, k, t]) => {
    if (p[k] == null) return;
    const el = $(id), v = p[k];
    el.value = t === 'bool' ? (v ? '1' : '0') : t === 'pct' ? String(+(v * 100).toFixed(4)) : String(v);
  });
  if (p.days != null) $('bDays').value = ['30', '90', '180'].includes(String(p.days)) ? String(p.days) : '180';
  btModes();
}
function btCollect() {
  const p = {};
  BT_F.forEach(([id, k, t]) => {
    const v = $(id).value;
    p[k] = t === 'bool' ? v === '1' : t === 'pct' ? Number(v) / 100 : (t === 'int' || t === 'num') ? Number(v) : v.trim();
  });
  if ($('bDays').value === 'range') { p.dateFrom = $('bFrom').value; p.dateTo = $('bTo').value; p.days = 180; }
  else { p.days = Number($('bDays').value); p.dateFrom = ''; p.dateTo = ''; }
  return p;
}
async function btOpen() {
  if (!BT.loaded) {
    try {
      const d = await api('/api/bt/defaults');
      BT.live = d.live || {};
      btFill(Object.assign({}, d.defaults || {}, BT.live));
      BT.loaded = true;
      if (!d.available) { $('btState').textContent = 'Modul tidak ada'; toast(d.msg || 'okx_backtest.py tidak ditemukan di folder bot'); }
    } catch (e) { toast(e.message || 'Gagal memuat setting'); }
  }
  await btPoll(true);
  btRuns();
}
function btProg(j) {
  const run = j && j.state === 'running';
  $('btProgP').hidden = !run; $('btnBtRun').disabled = run;
  $('btState').textContent = !j ? 'Siap' : run ? 'Berjalan' : j.state === 'done' ? 'Selesai' : j.state === 'cancelled' ? 'Dibatalkan' : j.state === 'error' ? 'Error' : j.state;
  if (j) { const pc = Math.max(0, Math.min(100, Number(j.pct) || 0));
    $('btBar').style.width = pc.toFixed(0) + '%'; $('btPct').textContent = pc.toFixed(0) + '%'; $('btMsg').textContent = j.msg || '-'; }
}
async function btPoll(first) {
  clearTimeout(BT.timer);
  let j = null;
  try { j = (await api('/api/bt/status')).job; } catch (e) { BT.timer = setTimeout(btPoll, 4000); return; }
  const was = BT.cur && BT.cur.state === 'running' && BT.cur.id;
  BT.cur = j; btProg(j);
  if (j && j.state === 'running') { BT.timer = setTimeout(btPoll, 1500); return; }
  if (was && j && j.id === was) {
    if (j.state === 'done') { await btLoad(j.id, true); toast('Backtest selesai'); }
    else if (j.state === 'error') toast('Backtest gagal: ' + (j.msg || ''));
    else if (j.state === 'cancelled') toast('Backtest dibatalkan');
    btRuns();
  } else if (first && j && j.state === 'done' && !BT.res) await btLoad(j.id);
}
async function btRuns() {
  try { BT.runs = (await api('/api/bt/runs')).runs || []; } catch (e) { return; }
  $('btRunsN').textContent = BT.runs.length ? BT.runs.length + ' run' : '';
  if (!BT.runs.length) { $('btRuns').innerHTML = '<p class="btnote">Belum ada.</p>'; return; }
  $('btRuns').innerHTML = BT.runs.map(r => {
    const s = r.summary, id = r.id;
    const when = id.slice(6, 8) + '/' + id.slice(4, 6) + ' ' + id.slice(9, 11) + ':' + id.slice(11, 13);
    const per = r.dateFrom ? (r.dateFrom + ' → ' + (r.dateTo || 'kini')) : (r.days + ' hari');
    let right = '', sub = '';
    if (r.state === 'done' && s) {
      right = '<span class="' + cls(s.ret_usdt) + '">' + sign(s.ret_usdt, 2) + '</span>';
      sub = (s.IS != null ? 'IS ' + sign(s.IS, 2) + ' / OOS ' + sign(s.OOS, 2) + ' · ' : '') + 'DD ' + fmt(s.dd_usdt, 2) + (s.live != null ? ' · live ' + sign(s.live, 2) : '');
    } else { right = '<span class="mute">' + btEsc(r.state === 'running' ? 'berjalan' : r.state === 'cancelled' ? 'batal' : r.state) + '</span>'; sub = btEsc((r.msg || '').slice(0, 80)); }
    return '<button class="btrun' + (BT.res && BT.res.id === id ? ' on' : '') + '" type="button" data-btrun="' + id + '"' + (r.hasResult ? '' : ' disabled') + '>' +
      '<span>' + when + ' · ' + btEsc((r.inst || '').replace('-USDT-SWAP', '')) + ' · ' + btEsc(per) + '</span><span class="r num">' + right + '</span>' +
      '<small class="num" style="grid-column:1 / -1">' + sub + '</small></button>';
  }).join('');
}
async function btLoad(id, scroll) {
  try {
    const r = await api('/api/bt/result?id=' + encodeURIComponent(id));
    if (!r.main) { toast(r.msg || 'Hasil tidak ditemukan'); return; }
    btShow(r);
    if (scroll) window.scrollTo({top: $('btRes').getBoundingClientRect().top + window.scrollY - 96, behavior: 'smooth'});
    document.querySelectorAll('.btrun').forEach(b => b.classList.toggle('on', b.dataset.btrun === id));
  } catch (e) { toast(e.message || 'Gagal memuat hasil'); }
}
function btSum(p) {
  const sz = p.sizeMode === 'fixed' ? fmt(p.fixedSize, 2) + ' kontrak/level' : fmt(p.baseUsdt, 0) + ' USDT' + (p.mult !== 1 ? ' ×' + fmt(p.mult, 2) : '') + '/level';
  return sz + ', ' + p.so + ' safety, jarak ' + fmt(p.step * 100, 2) + '% ×' + fmt(p.scale, 2) +
    ', TP ' + fmt(p.tp * 100, 2) + '% ' + (p.tpMode === 'geo' ? '×' + fmt(p.tpRatio, 2) : '+' + fmt(p.tpAdd * 100, 2) + '%') +
    ', SL ' + fmt(p.sl * 100, 2) + '% ' + (p.slMode === 'geo' ? '×' + fmt(p.slRatio, 2) : '+' + fmt(p.slAdd * 100, 2) + '%') +
    ', max rugi ' + fmt(p.maxLoss, 2) + ', cooldown ' + Math.round(p.cooldown / 60) + 'm, lev ' + fmt(p.leverage, 0) + 'x' +
    ', tren ' + (p.trendOn ? p.trendTf + ' ' + p.trendFast + '/' + p.trendSlow + ' ' + fmt(p.trendTh * 100, 2) + '%' + (p.trendBlockL0 ? ' (L0)' : '') : 'mati') +
    ', SnR ' + (p.snrOn ? fmt(p.snrZone * 100, 2) + '%' : 'mati')
      + ', LR ' + (p.lrOn ? ('N' + p.lrN + ' k' + fmt(p.lrK, 2)) : 'mati')
      + ', fee ' + fmt(p.fee * 100, 3) + '%' + (p.slipOn ? ' + slip/funding' : '');
}
function btRow(name, s, eq0, hl) {
  return '<tr' + (hl ? ' class="hl"' : '') + '><td>' + name + '</td><td class="' + cls(s.ret_usdt) + '">' + sign(s.ret_usdt, 2) + ' <small>' + sign(s.ret, 1) + '%</small></td>' +
    '<td>' + fmt(s.dd_usdt, 2) + '</td><td>' + sign(s.worst_day_usdt, 2) + '</td><td>' + s.n + '</td><td>' + fmt(s.win, 0) + '%</td><td>' + s.n_sl + '</td></tr>';
}
function btShow(r) {
  BT.res = r;
  const m = r.main, P = r.params, eq0 = P.equity, A = m.ALL, st = m.stats || {};
  const rej = Object.values(st.rejections || {}).reduce((a, b) => a + b, 0);
  const cards = [
    ['Return', sign(A.ret_usdt, 2) + ' USDT', sign(A.ret, 2) + '% dari ' + fmt(eq0, 0), cls(A.ret_usdt)],
    ['Max drawdown', fmt(A.dd_usdt, 2) + ' USDT', fmt(A.dd, 1) + '%', A.dd_usdt > 0 ? 'down' : ''],
    ['Hari terburuk', sign(A.worst_day_usdt, 2) + ' USDT', sign(A.worst_day, 1) + '%', cls(A.worst_day_usdt)],
    ['Trade', String(A.n), 'TP ' + A.n_tp + (A.n_trail ? ' · TP trailing ' + A.n_trail : '') + (st.maker_fills != null ? ' · isi maker ' + st.maker_fills : '') + ' · fee ' + fmt(A.fees, 2), ''],
    ['Win rate', fmt(A.win, 1) + '%', 'per entry', ''],
    ['SL kena', String(A.n_sl), 'max rugi ' + A.n_maxloss + 'x', A.n_sl ? 'down' : ''],
    ['Margin puncak', fmt(st.peak_used, 2) + ' USDT', fmt(st.peak_used / eq0 * 100, 0) + '% equity · tolak ' + rej, ''],
    ['Jarak likuidasi', st.min_liq_dist == null ? '-' : fmt(st.min_liq_dist, 1) + '%', 'minimum' + (P.slipOn ? ' · funding ' + sign(-st.funding, 2) : ''), ''],
  ];
  let h = '<div class="panel"><div class="ptitle"><b>Hasil ' + btEsc(P.inst) + '</b><span>' + btDate(r.data.from) + ' – ' + btDate(r.data.to - 1) + '</span></div>';
  h += '<div class="btmeta num">' + fmt(r.data.candles, 0) + ' candle 5m · harga ' + fmt(r.data.first_px, pxDec(r.data.first_px)) + ' → ' + fmt(r.data.last_px, pxDec(r.data.last_px)) +
       ' · L0 ≈ ' + fmt(m.size_first, 2) + ' kontrak · ' + fmt(r.runtime_s, 1) + ' dtk<br/>' + btEsc(btSum(P)) + '</div>';
  h += '<div class="btcards num">' + cards.map(c => '<div><small>' + c[0] + '</small><span class="' + c[3] + '">' + c[1] + '</span><em>' + c[2] + '</em></div>').join('') + '</div></div>';
  // grafik equity + drawdown
  h += '<div class="panel"><div class="ptitle"><b>Equity</b><span>' + (m.IS ? 'garis = batas IS / OOS' : '') + '</span></div>' +
       '<div class="btleg"><span><i style="border-color:var(--up)"></i>Setting uji</span>' + (r.live ? '<span><i style="border-color:var(--mute);border-top-style:dashed"></i>Setting live</span>' : '') +
       '<span><i style="border-color:var(--line)"></i>Equity awal</span></div><div class="btch" id="btEq"></div>' +
       '<div class="ptitle" style="margin:10px 0 4px"><b style="font-size:13px">Drawdown</b><span id="btDdMax"></span></div><div class="btch" id="btDd"></div>' +
       '<div class="xax num" id="btX"></div></div>';
  // tabel IS/OOS + blok
  h += '<div class="panel"><div class="ptitle"><b>Per periode</b><span>USDT</span></div><div class="bttw"><table class="bttbl num"><tr><th>Periode</th><th>Return</th><th>DD</th><th>Hari terburuk</th><th>Trade</th><th>Win</th><th>SL</th></tr>';
  h += btRow('Semua', A, eq0, true);
  if (m.IS) { h += btRow('IS (blok 1-4)', m.IS, eq0, true); h += btRow('OOS (blok 5-6)', m.OOS, eq0, true); }
  (m.blocks || []).forEach((b, i) => { h += btRow('Blok ' + (i + 1) + ' <small>' + btDate(b.from) + '</small>', b, eq0, false); });
  h += '</table></div></div>';
  if (r.live) {
    const L = r.live, rows = [['Semua', 'ALL']].concat(m.IS ? [['IS', 'IS'], ['OOS', 'OOS']] : []);
    h += '<div class="panel"><div class="ptitle"><b>Bandingkan dengan live</b><span>USDT</span></div><div class="bttw"><table class="bttbl num"><tr><th></th><th>Uji</th><th>Live</th><th>Selisih</th><th>DD uji</th><th>DD live</th></tr>';
    rows.forEach(([n, k]) => { const a = m[k], b = L[k], df = a.ret_usdt - b.ret_usdt;
      h += '<tr><td>' + n + '</td><td class="' + cls(a.ret_usdt) + '">' + sign(a.ret_usdt, 2) + '</td><td class="' + cls(b.ret_usdt) + '">' + sign(b.ret_usdt, 2) + '</td><td class="' + cls(df) + '">' + sign(df, 2) +
           '</td><td>' + fmt(a.dd_usdt, 2) + '</td><td>' + fmt(b.dd_usdt, 2) + '</td></tr>'; });
    const lst = L.stats || {};
    h += '<tr><td>Trade / SL</td><td>' + m.ALL.n + ' / ' + m.ALL.n_sl + '</td><td>' + L.ALL.n + ' / ' + L.ALL.n_sl + '</td><td></td><td colspan="2"><small>margin ' + fmt(st.peak_used, 1) + ' vs ' + fmt(lst.peak_used, 1) + '</small></td></tr>';
    h += '</table></div><p class="pnote">Live: ' + btEsc(btSum(r.live_params)) + '</p></div>';
  }
  $('btRes').innerHTML = h;
  btChart();
}
function btChart() {
  const r = BT.res; if (!r || !$('btEq')) return;
  const m = r.main, cv = m.curve || [], lv = r.live ? (r.live.curve || []) : [], eq0 = r.params.equity;
  if (cv.length < 2) { $('btEq').innerHTML = '<p class="btnote">Data terlalu sedikit.</p>'; return; }
  const W = Math.max(280, Math.round($('btEq').clientWidth || 520)), H = 180, HD = 64, pad = 4;
  const t0 = cv[0][0], t1 = cv[cv.length - 1][0];
  let lo = eq0, hi = eq0; cv.concat(lv).forEach(p => { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); });
  const sp = (hi - lo) || 1; lo -= sp * .06; hi += sp * .06;
  const X = t => pad + (W - 2 * pad) * (t - t0) / ((t1 - t0) || 1), Y = v => pad + (H - 2 * pad) * (hi - v) / (hi - lo);
  const line = a => a.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1)).join('');
  let s = '<svg viewBox="0 0 ' + W + ' ' + H + '" height="' + H + '">';
  const bl = m.blocks || [];
  if (m.IS && bl.length === 6) { const xo = X(bl[4].from);
    s += '<rect x="' + xo.toFixed(1) + '" y="0" width="' + (W - xo).toFixed(1) + '" height="' + H + '" fill="rgba(240,185,11,.05)"/>' +
         '<line x1="' + xo.toFixed(1) + '" x2="' + xo.toFixed(1) + '" y1="0" y2="' + H + '" stroke="var(--warn)" stroke-dasharray="3 3" stroke-width="1"/>' +
         '<text x="' + (xo - 4).toFixed(1) + '" y="12" fill="var(--mute)" font-size="10" text-anchor="end">IS</text><text x="' + (xo + 4).toFixed(1) + '" y="12" fill="var(--warn)" font-size="10">OOS</text>'; }
  s += '<line x1="0" x2="' + W + '" y1="' + Y(eq0).toFixed(1) + '" y2="' + Y(eq0).toFixed(1) + '" stroke="var(--line)" stroke-width="1"/>';
  if (lv.length > 1) s += '<path d="' + line(lv) + '" fill="none" stroke="var(--mute)" stroke-width="1.4" stroke-dasharray="4 3"/>';
  const end = cv[cv.length - 1][1];
  s += '<path d="' + line(cv) + '" fill="none" stroke="' + (end >= eq0 ? 'var(--up)' : 'var(--down)') + '" stroke-width="1.8" stroke-linejoin="round"/></svg>';
  s += '<span class="yl num" style="top:2px">' + fmt(hi, 2) + '</span><span class="yl num" style="bottom:2px">' + fmt(lo, 2) + '</span>';
  $('btEq').innerHTML = s;
  let mdd = 0; cv.forEach(p => { mdd = Math.max(mdd, p[2]); }); const dmax = mdd || 1;
  const YD = v => pad + (HD - 2 * pad) * v / dmax;
  let d = 'M' + X(cv[0][0]).toFixed(1) + ',' + pad;
  cv.forEach(p => { d += 'L' + X(p[0]).toFixed(1) + ',' + YD(p[2]).toFixed(1); });
  d += 'L' + X(t1).toFixed(1) + ',' + pad + 'Z';
  $('btDd').innerHTML = '<svg viewBox="0 0 ' + W + ' ' + HD + '" height="' + HD + '"><line x1="0" x2="' + W + '" y1="' + pad + '" y2="' + pad + '" stroke="var(--line)"/>' +
    '<path d="' + d + '" fill="var(--downbg)" stroke="var(--down)" stroke-width="1.2" stroke-linejoin="round"/></svg><span class="yl num" style="bottom:2px">-' + fmt(mdd, 2) + '</span>';
  $('btDdMax').textContent = 'maks -' + fmt(mdd, 2) + ' USDT';
  const mid = t0 + (t1 - t0) / 2;
  $('btX').innerHTML = '<span>' + btDate(t0) + '</span><span>' + btDate(mid) + '</span><span>' + btDate(t1) + '</span>';
}
['bSizeMode', 'bTpMode', 'bSlMode', 'bDays'].forEach(id => $(id).addEventListener('change', btModes));
$('btnBtLive').addEventListener('click', () => { if (BT.live) { btFill(BT.live); toast('Form diisi dari setting live'); } });
$('btnBtRun').addEventListener('click', async () => {
  const params = btCollect();
  $('btnBtRun').disabled = true;
  try {
    const r = await api('/api/bt/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({params, compare:$('bCmp').value === '1'})});
    if (!r.ok) { toast(r.msg || 'Gagal memulai'); $('btnBtRun').disabled = false; return; }
    BT.cur = {id:r.id, state:'running', pct:0, msg:'Mulai'}; btProg(BT.cur); btRuns();
    BT.timer = setTimeout(btPoll, 800);
  } catch (e) { toast(e.message || 'Gagal memulai'); $('btnBtRun').disabled = false; }
});
$('btnBtCancel').addEventListener('click', async () => {
  if (!confirm('Batalkan backtest yang sedang berjalan?')) return;
  try { const r = await api('/api/bt/cancel', {method:'POST'}); toast(r.msg || 'Dibatalkan'); } catch (e) { toast(e.message || 'Gagal'); }
  btPoll();
});
$('btRuns').addEventListener('click', e => { const b = e.target.closest('[data-btrun]'); if (b) btLoad(b.dataset.btrun, true); });
window.addEventListener('resize', () => { if ($('tab-bt').classList.contains('on')) btChart(); });

tick(); setInterval(tick, 5000);
</script>
</body>
</html>
"""

app = Flask(__name__)
BOT = None


@app.after_request
def persist_after_change(resp):
    from flask import request
    if (BOT is not None and request.method == "POST" and request.path.startswith("/api/")
            and not request.path.startswith("/api/bt/") and not request.path.startswith("/api/ethlive")
            and resp.status_code < 300):   # backtest tidak mengubah setting
        try:
            BOT.save_state()
        except Exception as e:
            log.warning("Simpan setting gagal: %s", e)
    return resp


@app.before_request
def check_token():
    if not DASH_TOKEN:
        return None
    from flask import request
    got = request.args.get("token") or request.headers.get("X-Token") or ""
    if got != DASH_TOKEN:
        return jsonify({"ok": False, "msg": "unauthorized"}), 401
    return None


def snapshot():
    pos = BOT.get_positions() if BOT else {}
    tick = BOT.get_ticker() if BOT else None
    tail = ""
    if LOG_PATH.exists():
        with LOG_PATH.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            chunk = fh.read().decode("utf-8", errors="replace")
        lines = [
            ln for ln in chunk.splitlines()
            if "HTTP Request" not in ln and "/api/snapshot" not in ln and "GET / HTTP" not in ln
        ]
        tail = "\n".join(lines[-40:])
    return {
        "ts": datetime.now().strftime("%H:%M:%S"),
        "inst": BOT.inst if BOT else INST_ID,
        "markets": list(MARKETS) if (not BOT or BOT.inst in MARKETS) else [BOT.inst, *MARKETS],
        "demo": (BOT.flag if BOT else FLAG) == "1",
        "hasDemo": bool(keys_for("1")),
        "hasLive": bool(keys_for("0")),
        "posMode": BOT.pos_mode if BOT else "",
        "last": None if not tick else tick["last"],
        "usdt": BOT.get_balance_usdt() if BOT else None,
        "long": pos.get("long", 0),
        "short": pos.get("short", 0),
        "long_avg": pos.get("long_avg", 0),
        "short_avg": pos.get("short_avg", 0),
        "upl": pos.get("upl", 0),
        "long_upl": pos.get("long_upl", 0),
        "short_upl": pos.get("short_upl", 0),
        "mgn": pos.get("mgn", TD_MODE),
        "paused": bool(BOT and BOT.paused),
        "entry": BOT.entry if BOT else ENTRY,
        "autoClose": bool(BOT and BOT.auto_close),
        "tpPct": f"{(BOT.tp_pct * 100):.2f}%" if BOT else "",
        "slPct": f"{(BOT.sl_pct * 100):.2f}%" if BOT else "",
        "closeOnWin": bool(BOT and BOT.close_on_win),
        "winUsdt": f"{BOT.win_usdt:.2f}" if BOT else "",
        "orderSize": BOT.order_size if BOT else ORDER_SIZE,
        "maxLong": BOT.max_long if BOT else MAX_LONG,
        "maxShort": BOT.max_short if BOT else MAX_SHORT,
        "maxLoss": BOT.max_loss if BOT else MAX_LOSS_USDT,
        "cooldown": BOT.cooldown_sec if BOT else COOLDOWN_SEC,
        "grid": BOT.grid if BOT else GRID,
        "gridStep": BOT.grid_step_cfg if BOT else GRID_STEP,
        "gridLow": f"{BOT.grid_low:.1f}" if BOT else "",
        "gridHigh": f"{BOT.grid_high:.1f}" if BOT else "",
        "equity": (BOT.equity[-240:] if BOT else []),
        "lev": BOT.leverage if BOT else LEVERAGE,
        "levChoices": LEV_CHOICES,
        "tdMode": TD_MODE,
        "preset": BOT.preset if BOT else "",
        "tfPresets": TF_PRESETS,
        "tfPreset": (BOT.tf_preset_now() if BOT else "custom"),
        "presets": PRESETS,
        "emSl": BOT.emergency_sl if BOT else EMERGENCY_SL_PCT,
        "dca": ({"base": BOT.dca_base, "mult": BOT.dca_mult, "step": BOT.dca_step,
                 "scale": BOT.dca_scale, "max": BOT.dca_max} if BOT else {}),
        "split": BOT.split if BOT else False,
        "trend": (BOT.trend_info() if BOT else {}),
        "snr": (BOT.snr_info() if BOT else {}),
        "lr": (BOT.lr_info() if BOT else {}),
        "trail": ({"on": bool(BOT.trail_on), "pct": BOT.trail_pct} if BOT else {}),
        "maker": (BOT.maker_info() if BOT else {}),
        "eqSize": (BOT.eq_info() if BOT else {}),
        "tg": {"configured": TG.enabled, "chat": _mask(TG.chat_id) if TG.enabled else "",
               "on": bool(BOT.tg_on) if BOT else True, "hours": BOT.tg_status_hours if BOT else TG_STATUS_HOURS,
               "trades": bool(BOT.tg_trades) if BOT else True, "sent": TG.sent, "failed": TG.failed,
               "lastErr": TG.last_err},
        "tpMode": BOT.tp_mode if BOT else TP_MODE,
        "slMode": BOT.sl_mode if BOT else SL_MODE,
        "tpAdd": BOT.tp_add if BOT else TP_ADD,
        "tpRatio": BOT.tp_ratio if BOT else TP_RATIO,
        "slAdd": BOT.sl_add if BOT else SL_ADD,
        "slRatio": BOT.sl_ratio if BOT else SL_RATIO,
        "splitInfo": (BOT.split_info(pos, tick["last"] if tick else 0) if BOT and pos else {}),
        "dcaState": (BOT.dca_info(pos, tick["last"] if tick else 0) if BOT and pos else {}),
        "ctVal": BOT.ct_val if BOT else 0.01,
        "tp": BOT.tp_pct if BOT else 0,
        "sl": BOT.sl_pct if BOT else 0,
        "log": tail,
    }


# ---------- Backtest (tab dashboard) ----------
# Backtest berjalan sebagai PROSES TERPISAH (okx_backtest.py, prioritas rendah, tanpa jendela) supaya loop trading tidak
# pernah terganggu: tidak ada lock bersama dan tidak ada state bersama. Dashboard hanya menulis bt_runs/<id>/params.json lalu
# membaca status.json / result.json. Proses backtest hanya memakai endpoint PUBLIK OKX dan mendapat environment yang sudah
# dibersihkan (tanpa API key). Tidak ada yang mengubah setting live dari sini.
BT_MOD = BASE_DIR / "okx_backtest.py"
BT_RUNS = BASE_DIR / "bt_runs"
BT_CACHE = BASE_DIR / "bt_cache"
BT_KEEP = 30
BT_STALE_SEC = 45
BT_ID_RE = re.compile(r"^\d{8}_\d{6}$")
BT_ENV_KEEP = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE",
               "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "COMPUTERNAME", "NUMBER_OF_PROCESSORS",
               "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE"}
_BT = {"proc": None, "id": None, "mod": None, "mtime": 0.0}
_BT_LOCK = threading.Lock()   # hanya untuk state backtest, terpisah dari BOT.lock


def _bt_module():
    """Muat okx_backtest.py (hanya definisi fungsi, tanpa efek samping) untuk validasi parameter & nilai default."""
    import importlib.util
    mt = BT_MOD.stat().st_mtime
    if _BT["mod"] is None or mt != _BT["mtime"]:
        spec = importlib.util.spec_from_file_location("okx_backtest_dash", str(BT_MOD))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _BT["mod"], _BT["mtime"] = mod, mt
    return _BT["mod"]


def bt_live_params():
    """Setting live sekarang dalam format okx_backtest. Hanya membaca atribut BOT (tanpa panggilan API, tanpa lock)."""
    b = BOT
    if b is None:
        return {}
    try:
        eq = float(b.equity[-1].get("eq") or 0) if b.equity else 0.0
    except Exception:
        eq = 0.0
    try:
        lev = float(b.leverage)
    except (TypeError, ValueError):
        lev = 5.0
    return {"inst": b.inst, "sizeMode": "dcaBase", "baseUsdt": float(b.dca_base), "mult": float(b.dca_mult),
            "fixedSize": float(b.order_size), "so": int(b.dca_max), "step": float(b.dca_step), "scale": float(b.dca_scale),
            "tp": float(b.base_tp), "tpMode": b.tp_mode, "tpAdd": float(b.tp_add), "tpRatio": float(b.tp_ratio),
            "sl": float(b.base_sl), "slMode": b.sl_mode, "slAdd": float(b.sl_add), "slRatio": float(b.sl_ratio),
            "emSl": float(b.emergency_sl), "maxLoss": float(b.max_loss), "cooldown": int(b.cooldown_sec),
            "trendOn": bool(b.trend_on), "trendTf": b.trend_tf, "trendFast": int(b.trend_fast), "trendSlow": int(b.trend_slow),
            "trendTh": float(b.trend_th), "trendBlockL0": bool(b.trend_block_l0), "snrOn": bool(b.snr_on),
            "snrZone": float(b.snr_zone), "snrSrc": b.snr_src,
            "lrOn": bool(b.lr_on), "lrN": int(b.lr_n), "lrK": float(b.lr_k), "leverage": lev,
            "trailOn": bool(getattr(b, "trail_on", False)), "trailPct": float(getattr(b, "trail_pct", 0.002)),
            "makerOn": bool(getattr(b, "maker_on", False)), "makerTick": float(b.tick_sz) if getattr(b, "tick_sz", 0) else 0.1,
            "eqSizeOn": bool(getattr(b, "eq_size_on", False)), "eqRef": float(getattr(b, "eq_ref", 58.0)),
            "eqSizeMin": float(getattr(b, "eq_min", 0.5)), "eqSizeMax": float(getattr(b, "eq_max", 2.0)),
            "equity": round(eq, 2) if eq > 0 else 58.0, "liveSplit": bool(b.split), "ctVal": float(b.ct_val)}


def _bt_read(rid, name):
    try:
        return json.loads((BT_RUNS / rid / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _bt_write_status(rid, st):
    try:
        p = BT_RUNS / rid / "status.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        log.warning("Backtest: status gagal ditulis: %s", e)


def _bt_ids():
    try:
        return sorted((p.name for p in BT_RUNS.iterdir() if p.is_dir() and BT_ID_RE.match(p.name)), reverse=True)
    except OSError:
        return []


def bt_current():
    """Status job terakhir. Job yang prosesnya sudah mati / heartbeat basi ditandai error (supaya tidak 'macet')."""
    rid, proc = _BT["id"], _BT["proc"]
    if not rid:
        ids = _bt_ids()
        rid = ids[0] if ids else None
        proc = None
    if not rid:
        return None
    st = _bt_read(rid, "status.json") or {"state": "running", "pct": 0, "msg": "Menyiapkan"}
    if st.get("state") == "running":
        mine = proc is not None and _BT["id"] == rid
        dead = mine and proc.poll() is not None
        stale = time.time() - float(st.get("hb") or st.get("started") or 0) > BT_STALE_SEC
        if dead or (not mine and stale) or (mine and stale and st.get("hb")):
            err = ""
            try:
                err = (BT_RUNS / rid / "stderr.txt").read_text(encoding="utf-8", errors="replace").strip()[-300:]
            except OSError:
                pass
            if mine and not dead:
                try:
                    proc.kill()
                except OSError:
                    pass
            st.update(state="error", msg="Proses backtest berhenti tanpa hasil" + (f": {err}" if err else ""))
            _bt_write_status(rid, st)
    st["id"] = rid
    return st


def _bt_prune(keep_id):
    import shutil
    for rid in _bt_ids()[BT_KEEP:]:
        if rid != keep_id:
            shutil.rmtree(BT_RUNS / rid, ignore_errors=True)


def _bt_reap(proc, rid, grace=10.0):
    """Setelah tombol batal: tunggu proses berhenti sendiri; kalau tidak (mis. tertahan jaringan), hentikan paksa."""
    try:
        proc.wait(timeout=grace)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    st = _bt_read(rid, "status.json") or {}
    if st.get("state") in (None, "running"):
        st.update(state="cancelled", msg="Dibatalkan")
        _bt_write_status(rid, st)


@app.get("/api/bt/defaults")
def api_bt_defaults():
    out = {"ok": True, "live": bt_live_params(), "available": BT_MOD.exists(), "defaults": {}}
    if out["available"]:
        try:
            out["defaults"] = dict(_bt_module().DEFAULTS)
        except Exception as e:
            out["available"], out["msg"] = False, f"okx_backtest.py gagal dimuat: {e}"
    return jsonify(out)


@app.post("/api/bt/start")
def api_bt_start():
    from flask import request
    import subprocess
    body = request.get_json(silent=True) or {}
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    with _BT_LOCK:
        cur = bt_current()
        if cur and cur.get("state") == "running":
            return jsonify({"ok": False, "msg": "Masih ada backtest yang berjalan. Tunggu atau batalkan dulu."}), 409
        if not BT_MOD.exists():
            return jsonify({"ok": False, "msg": "okx_backtest.py tidak ada di folder bot"}), 503
        try:
            mod = _bt_module()
            P = mod.norm(params)
            mod.period(P)
        except Exception as e:
            return jsonify({"ok": False, "msg": f"Parameter tidak valid: {e}"}), 400
        rid = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = BT_RUNS / rid
        if folder.exists():
            return jsonify({"ok": False, "msg": "Tunggu sebentar lalu coba lagi"}), 429
        try:
            folder.mkdir(parents=True)
            req = {"params": params, "live": bt_live_params(), "compare": bool(body.get("compare")),
                   "cacheDir": str(BT_CACHE), "offline": os.getenv("OKX_BT_OFFLINE", "") == "1"}
            (folder / "params.json").write_text(json.dumps(req), encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k in BT_ENV_KEEP}
            flags = (0x00004000 | 0x08000000) if os.name == "nt" else 0   # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW
            with open(folder / "stderr.txt", "wb") as err:
                proc = subprocess.Popen([sys.executable, str(BT_MOD), "--job", str(folder)], cwd=str(BASE_DIR), env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                        close_fds=True, creationflags=flags)
        except Exception as e:
            log.warning("Backtest gagal dimulai: %s", e)
            return jsonify({"ok": False, "msg": f"Backtest gagal dimulai: {e}"}), 500
        _BT.update(proc=proc, id=rid)
        _bt_prune(rid)
    log.info("Backtest %s dimulai: %s %s, pid %s", rid, P["inst"],
             f"{P['dateFrom']}..{P['dateTo'] or 'kini'}" if P.get("dateFrom") else f"{P['days']} hari", proc.pid)
    return jsonify({"ok": True, "id": rid})


@app.get("/api/bt/status")
def api_bt_status():
    with _BT_LOCK:
        return jsonify({"ok": True, "job": bt_current()})


@app.post("/api/bt/cancel")
def api_bt_cancel():
    with _BT_LOCK:
        cur = bt_current()
        if not cur or cur.get("state") != "running":
            return jsonify({"ok": False, "msg": "Tidak ada backtest yang berjalan"}), 400
        rid = cur["id"]
        try:
            (BT_RUNS / rid / "cancel").write_text("1", encoding="utf-8")
        except OSError as e:
            return jsonify({"ok": False, "msg": f"Gagal membatalkan: {e}"}), 500
        proc = _BT["proc"] if _BT["id"] == rid else None
    if proc is not None:
        threading.Thread(target=_bt_reap, args=(proc, rid), name="bt-reap", daemon=True).start()
    log.info("Backtest %s dibatalkan dari dashboard", rid)
    return jsonify({"ok": True, "msg": "Backtest dibatalkan"})


@app.get("/api/bt/runs")
def api_bt_runs():
    out = []
    for rid in _bt_ids()[:BT_KEEP]:
        st = _bt_read(rid, "status.json") or {}
        rq = _bt_read(rid, "params.json") or {}
        p = rq.get("params") or {}
        out.append({"id": rid, "state": st.get("state", "?"), "msg": st.get("msg", ""), "summary": st.get("summary"),
                    "inst": p.get("inst"), "days": p.get("days"), "dateFrom": p.get("dateFrom"), "dateTo": p.get("dateTo"),
                    "hasResult": (BT_RUNS / rid / "result.json").exists()})
    return jsonify({"ok": True, "runs": out})


@app.get("/api/bt/result")
def api_bt_result():
    from flask import request
    rid = request.args.get("id", "")
    if not BT_ID_RE.match(rid):
        return jsonify({"ok": False, "msg": "id tidak valid"}), 400
    p = BT_RUNS / rid / "result.json"
    if not p.exists():
        return jsonify({"ok": False, "msg": "hasil tidak ditemukan"}), 404
    return Response(p.read_text(encoding="utf-8"), mimetype="application/json")


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.post("/api/action")
def api_action():
    from flask import request
    name = (request.get_json(silent=True) or {}).get("action")
    if BOT is None:
        return jsonify({"ok": False, "msg": "bot belum siap"}), 503
    with BOT.lock:
        if name in ("open_long", "open_short"):
            sd = "long" if name == "open_long" else "short"
            pos = BOT.get_positions()
            cap = BOT.max_long if sd == "long" else BOT.max_short
            if not pos["ok"]:
                return jsonify({"ok": False, "msg": "Posisi gagal dibaca dari OKX, order tidak dikirim"}), 400
            if pos[sd] + BOT.order_size > cap + BOT.lot_sz / 2:
                return jsonify({"ok": False, "msg": (f"Ditolak: {sd} {pos[sd]:g} + {BOT.order_size:g} "
                                                     f"melebihi batas {cap:g} kontrak")}), 400
            (BOT.open_long if sd == "long" else BOT.open_short)()
            msg = f"Perintah buka {sd} dikirim"
        elif name in ("close_long", "close_short", "close_all"):
            sides = {"close_long": ("long",), "close_short": ("short",), "close_all": ("long", "short")}[name]
            parts, failed = [], False
            for sd in sides:
                r = BOT.close_long() if sd == "long" else BOT.close_short()
                if r is None:
                    parts.append(f"{sd}: tidak ada posisi / posisi gagal dibaca")
                elif r.get("code") == "0":
                    parts.append(f"{sd}: ditutup ({r.get('_via', 'close-position')})")
                else:
                    failed = True
                    d = (r.get("data") or [{}])[0] if r.get("data") else {}
                    parts.append(f"{sd}: GAGAL {r.get('code')} {d.get('sCode', '')} {d.get('sMsg') or r.get('msg')}")
                if BOT.pos_mode != "long_short_mode":
                    break  # net mode: satu kali tutup sudah cukup
            msg = "; ".join(parts)
            if failed:
                return jsonify({"ok": False, "msg": msg + " | Cek posisi di OKX."}), 502
        elif name in ("close_win", "close_win_long", "close_win_short"):
            sides = {"close_win": ("long", "short"), "close_win_long": ("long",), "close_win_short": ("short",)}[name]
            ok, msg = BOT.close_winners(sides)
        elif name == "toggle_pause":
            msg = BOT.set_paused(not BOT.paused, "dashboard")
        elif name == "toggle_split":
            BOT.split = not BOT.split
            save_setting("split", BOT.split)
            pos = BOT.get_positions()
            for sd in ("long", "short"):
                if BOT.split:
                    BOT.lots[sd] = {"anchor": 0.0, "lots": []}
                    BOT.reconcile_lots(sd, pos)   # posisi yang ada jadi satu entry
                    if pos[sd] > 0:
                        BOT.refresh_side_sl(sd, pos)
                else:
                    BOT.lots[sd] = {"anchor": 0.0, "lots": []}
            BOT.save_lots()
            msg = "Entry terpisah " + ("hidup: TP per entry, level diisi ulang" if BOT.split else "mati: TP dari harga rata-rata")
            log.info(msg)
        elif name == "toggle_entry":
            BOT.entry = "off" if BOT.entry == "both" else "both"
            msg = "Entry: " + BOT.entry
            log.info(msg)
        elif name == "toggle_tpsl":
            BOT.auto_close = not BOT.auto_close
            msg = "TP/SL " + ("hidup" if BOT.auto_close else "mati")
            log.info(msg)
        elif name == "toggle_win":
            BOT.close_on_win = not BOT.close_on_win
            msg = f"Tutup jika win {'hidup' if BOT.close_on_win else 'mati'} (>= {BOT.win_usdt} USDT)"
            log.info(msg)
        elif name == "toggle_grid":
            cycle = ["off", "auto", "arith", "up", "down", "dca"]
            BOT.grid = cycle[(cycle.index(BOT.grid) + 1) % len(cycle)] if BOT.grid in cycle else "auto"
            BOT.grid_low = 0.0
            BOT.grid_high = 0.0
            names = {"off": "mati", "auto": "auto", "arith": "arithmetic", "up": "trailing up", "down": "trailing down", "dca": "DCA"}
            msg = "Grid " + names.get(BOT.grid, BOT.grid)
            log.info(msg)
        else:
            return jsonify({"ok": False, "msg": "aksi tidak dikenal"}), 400
    return jsonify({"ok": True, "msg": msg})


@app.post("/api/settings")
def api_settings():
    from flask import request
    body = request.get_json(silent=True) or {}
    if BOT is None:
        return jsonify({"ok": False, "msg": "bot belum siap"}), 503
    try:
        new = {
            "order_size": max(0.01, float(body.get("orderSize"))),
            "max_long": max(0.01, float(body.get("maxLong"))),
            "max_short": max(0.01, float(body.get("maxShort"))),
            "win_usdt": max(0.0, float(body.get("winUsdt"))),
            "max_loss": max(0.0, float(body.get("maxLoss"))),
            "cooldown_sec": max(0, int(float(body.get("cooldown")))),
        }
        # "Step harga" tidak lagi ada di dashboard: field kosong/tidak dikirim = pakai nilai tersimpan
        _gs = body.get("gridStep")
        step = BOT.grid_step_cfg if _gs is None or str(_gs).strip() == "" else max(0.0, float(_gs))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "angka tidak valid"}), 400
    if new["order_size"] > min(new["max_long"], new["max_short"]):
        return jsonify({"ok": False, "msg": "Size order tidak boleh lebih besar dari batas long/short"}), 400
    try:
        _t = BOT.get_ticker()
        _bal = BOT.get_balance_usdt()
        if _t and _bal:
            need = (new["max_long"] + new["max_short"]) * BOT.ct_val * _t["last"] / max(1.0, float(BOT.leverage))
            if need > _bal:
                return jsonify({"ok": False, "msg": (
                    f"Batas L/S {new['max_long']:g}/{new['max_short']:g} kontrak butuh margin ~{need:.2f} USDT "
                    f"di {BOT.leverage}x, saldo tersedia {_bal:.2f} USDT. Kecilkan batasnya.")}), 400
    except Exception as e:
        log.warning("Cek margin gagal: %s", e)
    inst = str(body.get("inst") or "").strip().upper()
    with BOT.lock:
        changed = (
            any(abs(float(getattr(BOT, k)) - float(v)) > 1e-9 for k, v in new.items())
            or abs(step - BOT.grid_step_cfg) > 1e-9
        )
        if inst and inst != BOT.inst:
            ok, switched = BOT.set_market(inst)
            if not ok:
                return jsonify({"ok": False, "msg": switched}), 400
            if not changed and BOT.preset:
                ok, msg = BOT.apply_preset(BOT.preset)
                return jsonify({"ok": True, "msg": f"Market {BOT.inst}. " + msg})
        if not changed:
            return jsonify({"ok": True, "msg": "Tidak ada perubahan"})
        for k, v in new.items():
            setattr(BOT, k, v)
        if abs(step - BOT.grid_step_cfg) > 1e-9:
            BOT.grid_low = 0.0
            BOT.grid_high = 0.0
        BOT.grid_step_cfg = step
    msg = (
        f"Parameter: {BOT.inst} size {BOT.order_size:g} max L/S {BOT.max_long:g}/{BOT.max_short:g} "
        f"win {BOT.win_usdt:g} loss {BOT.max_loss:g} cooldown {BOT.cooldown_sec}s step {BOT.grid_step_cfg:g}"
    )
    BOT.clear_preset()
    log.info(msg)
    return jsonify({"ok": True, "msg": msg})


@app.post("/api/account")
def api_account():
    from flask import request
    if BOT is None:
        return jsonify({"ok": False, "msg": "bot belum siap"}), 503
    b = request.get_json(silent=True) or {}
    target = str(b.get("target") or "")
    if target not in ("demo", "live"):
        return jsonify({"ok": False, "msg": "target harus demo atau live"}), 400
    if target == "live" and str(b.get("confirm") or "") != "LIVE":
        return jsonify({"ok": False, "msg": "Ketik LIVE untuk konfirmasi pindah ke akun sungguhan"}), 400
    if target == "live" and not DASH_TOKEN:
        return jsonify({"ok": False, "msg": "Isi OKX_DASH_TOKEN di .env dulu sebelum memakai akun LIVE"}), 400
    with BOT.lock:
        ok, msg = BOT.switch_account(target)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.post("/api/leverage")
def api_leverage():
    from flask import request
    if BOT is None:
        return jsonify({"ok": False, "msg": "bot belum siap"}), 503
    try:
        lev = int(float((request.get_json(silent=True) or {}).get("lev")))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "leverage tidak valid"}), 400
    if lev not in LEV_CHOICES:
        return jsonify({"ok": False, "msg": "pilihan leverage: " + ", ".join(f"{x}x" for x in LEV_CHOICES)}), 400
    with BOT.lock:
        ok, msg = BOT.change_leverage(lev)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.post("/api/dca")
def api_dca():
    from flask import request
    if BOT is None:
        return jsonify({"ok": False, "msg": "bot belum siap"}), 503
    b = request.get_json(silent=True) or {}
    try:
        base = float(b.get("base"))
        mult = float(b.get("mult"))
        step = float(b.get("step")) / 100
        scale = float(b.get("scale"))
        mx = int(float(b.get("max")))
        tp_mode = str(b.get("tpMode", BOT.tp_mode))
        sl_mode = str(b.get("slMode", BOT.sl_mode))
        tp_add = float(b.get("tpAdd", BOT.tp_add * 100)) / 100
        tp_ratio = float(b.get("tpRatio", BOT.tp_ratio))
        sl_add = float(b.get("slAdd", BOT.sl_add * 100)) / 100
        sl_ratio = float(b.get("slRatio", BOT.sl_ratio))
        tr_on = str(b.get("trendOn", "1" if BOT.trend_on else "0")).lower() in ("1", "true", "on", "yes")
        tr_tf = str(b.get("trendTf", BOT.trend_tf))
        tr_fast = int(float(b.get("trendFast", BOT.trend_fast)))
        tr_slow = int(float(b.get("trendSlow", BOT.trend_slow)))
        tr_th = float(b.get("trendTh", BOT.trend_th * 100)) / 100
        tr_l0 = str(b.get("trendBlockL0", "1" if BOT.trend_block_l0 else "0")).lower() in ("1", "true", "on", "yes")
        sn_on = str(b.get("snrOn", "1" if BOT.snr_on else "0")).lower() in ("1", "true", "on", "yes")
        sn_zone = float(b.get("snrZone", BOT.snr_zone * 100)) / 100
        lr_on = str(b.get("lrOn", "1" if BOT.lr_on else "0")).lower() in ("1", "true", "on", "yes")
        lr_n = int(float(b.get("lrN", BOT.lr_n)))
        lr_k = float(b.get("lrK", BOT.lr_k))
        tg_on = str(b.get("tgOn", "1" if BOT.tg_on else "0")).lower() in ("1", "true", "on", "yes")
        tg_hours = float(b.get("tgStatusHours", BOT.tg_status_hours) or 0)
        tg_trades = str(b.get("tgTrades", "1" if BOT.tg_trades else "0")).lower() in ("1", "true", "on", "yes")
        trl_on = str(b.get("trailOn", "1" if BOT.trail_on else "0")).lower() in ("1", "true", "on", "yes")
        trl_pct = float(b.get("trailPct", BOT.trail_pct * 100)) / 100
        mk_on = str(b.get("makerOn", "1" if BOT.maker_on else "0")).lower() in ("1", "true", "on", "yes")
        mk_fb = str(b.get("makerFallbackTaker", "1" if BOT.maker_fb else "0")).lower() in ("1", "true", "on", "yes")
        eq_on = str(b.get("eqSizeOn", "1" if BOT.eq_size_on else "0")).lower() in ("1", "true", "on", "yes")
        eq_ref = float(b.get("eqRef", BOT.eq_ref))
        eq_min = float(b.get("eqSizeMin", BOT.eq_min))
        eq_max = float(b.get("eqSizeMax", BOT.eq_max))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "angka tidak valid"}), 400
    if tr_tf not in TREND_TFS or not (2 <= tr_fast < tr_slow <= 500) or not (0 <= tr_th <= 0.05):
        return jsonify({"ok": False, "msg": "filter tren di luar batas: TF 15m/1H, 2 <= EMA cepat < EMA lambat <= 500, ambang 0-5%"}), 400
    if base <= 0 or not (1 <= mult <= 3) or not (0.0005 <= step <= 0.1) or not (1 <= scale <= 3) or not (0 <= mx <= 10):
        return jsonify({"ok": False, "msg": "di luar batas: pengali 1-3, jarak 0,05-10%, skala 1-3, safety 0-10"}), 400
    if not (0.0005 <= sn_zone <= 0.03):
        return jsonify({"ok": False, "msg": "zona SnR di luar batas: 0,05-3%"}), 400
    if not (50 <= lr_n <= 300 and 0.5 <= lr_k <= 5.0):
        return jsonify({"ok": False, "msg": "filter LR di luar batas: N 50-300, k 0,5-5"}), 400
    if not (0 <= tg_hours <= 168):
        return jsonify({"ok": False, "msg": "status Telegram: 0-168 jam (0 = mati)"}), 400
    if not (1 <= eq_ref <= 1e6 and 0.1 <= eq_min <= 1.0 <= eq_max <= 5.0):
        return jsonify({"ok": False, "msg": "ukuran equity: eqRef 1+, min 0,1-1, maks 1-5"}), 400
    if not (0.0005 <= trl_pct <= 0.02):
        return jsonify({"ok": False, "msg": "jarak TP trailing di luar batas: 0,05-2%"}), 400
    if tp_mode not in ("arith", "geo") or sl_mode not in ("arith", "geo"):
        return jsonify({"ok": False, "msg": "mode TP/SL harus arith atau geo"}), 400
    if not (0 <= tp_add <= 0.02 and 0 <= sl_add <= 0.05 and 1 <= tp_ratio <= 3 and 1 <= sl_ratio <= 3):
        return jsonify({"ok": False, "msg": "di luar batas: tambahan TP 0-2%, tambahan SL 0-5%, rasio 1-3"}), 400
    with BOT.lock:
        BOT.dca_base, BOT.dca_mult, BOT.dca_step, BOT.dca_scale, BOT.dca_max = base, mult, step, scale, mx
        BOT.tp_mode, BOT.sl_mode = tp_mode, sl_mode
        BOT.tp_add, BOT.tp_ratio, BOT.sl_add, BOT.sl_ratio = tp_add, tp_ratio, sl_add, sl_ratio
        BOT.trend_on, BOT.trend_tf, BOT.trend_fast, BOT.trend_slow = tr_on, tr_tf, tr_fast, tr_slow
        BOT.trend_th, BOT.trend_block_l0 = tr_th, tr_l0
        BOT.snr_on, BOT.snr_zone = sn_on, sn_zone
        BOT.lr_on, BOT.lr_n, BOT.lr_k = lr_on, lr_n, lr_k
        BOT.sanitize_lr()
        if bool(BOT.trail_on) != trl_on or abs(BOT.trail_pct - trl_pct) > 1e-12:
            log.info("TP trailing %s, jarak %.2f%%", "HIDUP" if trl_on else "MATI", trl_pct * 100)
        BOT.trail_on, BOT.trail_pct = trl_on, trl_pct
        if bool(BOT.maker_on) != mk_on or bool(BOT.maker_fb) != mk_fb:
            log.info("Maker safety %s (fallback taker %s)", "HIDUP" if mk_on else "MATI", "ya" if mk_fb else "tidak")
        was_mk = bool(BOT.maker_on)
        BOT.maker_on, BOT.maker_fb = mk_on, mk_fb
        if was_mk and not mk_on:
            try:
                BOT.maker_cancel_all("maker dimatikan")
            except Exception as e:
                log.warning("Maker: batal gagal: %s", e)
        if bool(BOT.eq_size_on) != eq_on:
            log.info("Ukuran proporsional equity %s (ref %.0f, %.2fx-%.2fx)", "HIDUP" if eq_on else "MATI", eq_ref, eq_min, eq_max)
        BOT.eq_size_on, BOT.eq_ref, BOT.eq_min, BOT.eq_max = eq_on, eq_ref, eq_min, eq_max
        if abs(tg_hours - float(BOT.tg_status_hours or 0)) > 1e-9:
            BOT._tg_status_at = time.time()  # hitung ulang dari sekarang
        BOT.tg_on, BOT.tg_status_hours, BOT.tg_trades = tg_on, tg_hours, tg_trades
        BOT.refresh_trend(force=True)
        BOT.refresh_snr(force=True)
        BOT.refresh_lr(force=True)
        for k, v in (("tpMode", tp_mode), ("slMode", sl_mode), ("tpAdd", tp_add), ("tpRatio", tp_ratio),
                     ("slAdd", sl_add), ("slRatio", sl_ratio)):
            save_setting(k, v)
        if BOT.grid == "dca":
            BOT.sync_dca_sizes()  # hanya di mode DCA; di mode lain size/batas manual tidak boleh tertimpa
        BOT.clear_preset()
    tp_txt = f"TP +{tp_add*100:.2f}%/level" if tp_mode == "arith" else f"TP x{tp_ratio:g}/level"
    sl_txt = f"SL +{sl_add*100:.2f}%/level" if sl_mode == "arith" else f"SL x{sl_ratio:g}/level"
    sn_txt = f"filter SnR zona {sn_zone*100:.2f}%" if sn_on else "filter SnR mati"
    lr_txt = f"filter LR N={lr_n} k={lr_k:g}" if lr_on else "filter LR mati"
    lr_txt += f", TP trailing {trl_pct*100:.2f}%" if trl_on else ", TP trailing mati"
    lr_txt += (", maker safety" + (" +fallback" if mk_fb else "")) if mk_on else ", maker mati"
    lr_txt += f", ukuran equity {eq_min:g}-{eq_max:g}x ref {eq_ref:g}" if eq_on else ""
    tr_txt = (f"filter tren EMA{tr_fast}/{tr_slow} {tr_tf} {tr_th*100:.2f}%" + (" +L0" if tr_l0 else "")) if tr_on else "filter tren mati"
    if not TG.enabled:
        tg_txt = "Telegram belum diatur"
    elif not tg_on:
        tg_txt = "Telegram mati"
    else:
        tg_txt = "Telegram hidup" + (f", status tiap {tg_hours:g} jam" if tg_hours else "") + ("" if tg_trades else ", tanpa notif order")
    msg = (f"DCA: awal {base:g} USDT, {mx} safety x{mult:g}, jarak {step*100:.2f}% x{scale:g}, {tp_txt}, {sl_txt}, {tr_txt}, "
           f"{sn_txt}, {lr_txt}, {tg_txt}")
    log.info(msg)
    return jsonify({"ok": True, "msg": msg})


@app.post("/api/preset")
def api_preset():
    from flask import request
    if BOT is None:
        return jsonify({"ok": False, "msg": "bot belum siap"}), 503
    name = str((request.get_json(silent=True) or {}).get("preset") or "")
    with BOT.lock:
        ok, msg = BOT.apply_preset(name)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.post("/api/tfpreset")
def api_tfpreset():
    from flask import request
    if BOT is None:
        return jsonify({"ok": False, "msg": "bot belum siap"}), 503
    name = str((request.get_json(silent=True) or {}).get("preset") or "")
    with BOT.lock:
        ok, msg = BOT.apply_tf_preset(name)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.post("/api/tgtest")
def api_tgtest():
    if not TG.enabled:
        return jsonify({"ok": False, "msg": "Telegram belum diatur: isi OKX_TG_TOKEN dan OKX_TG_CHAT_ID di .env lalu restart bot"}), 400
    TG.send(f"\U0001F514 Tes Telegram dari bot OKX ({BOT.inst if BOT else INST_ID}) jam {datetime.now():%H:%M:%S}", force=True)
    return jsonify({"ok": True, "msg": "Pesan tes masuk antrean, cek Telegram Anda" + (f" (error terakhir: {TG.last_err})" if TG.last_err else "")})


@app.get("/api/snapshot")
def api_snapshot():
    return jsonify(snapshot())


# ---------- grafik harga (candle publik OKX, di-cache; tidak menyentuh loop trading) ----------
CHART_BARS = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600}
CHART_LIMIT = 200
_CHART = {"cache": {}, "fail": {}, "mkt": {}, "marks": (0.0, []), "warned": 0.0, "lock": threading.Lock()}
_MARK_OPEN = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \| INFO \| ENTRY (long|short) L(\d+) sz=\S+ level=\S+ px=([\d.]+)")
_MARK_CLOSE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \| INFO \| (TP|SL)(?: trailing)? ENTRY (long|short) L(\S+) entry=\S+ px=([\d.]+)")
# baris log thread ETH diberi awalan "[ETH] " oleh _EthLogTag; regex BTC di atas tidak pernah cocok dengan baris itu
_MARK_OPEN_ETH = re.compile(_MARK_OPEN.pattern.replace(r"\| INFO \| ", r"\| INFO \| \[ETH\] ", 1))
_MARK_CLOSE_ETH = re.compile(_MARK_CLOSE.pattern.replace(r"\| INFO \| ", r"\| INFO \| \[ETH\] ", 1))


def _chart_warn(msg):
    if time.time() - _CHART["warned"] > 600:
        _CHART["warned"] = time.time()
        log.warning("Grafik harga: %s (trading tidak terpengaruh)", msg)


def chart_candles(inst, bar, flag):
    """(rows naik [ts_ms, o, h, l, c, selesai], error, basi). Candle publik OKX, cache 10-20 detik.
    Instance MarketAPI sendiri (tanpa API key), terpisah dari yang dipakai loop trading."""
    key = (inst, bar)
    ttl = 10 if bar == "1m" else 20
    c = _CHART["cache"].get(key)
    now = time.time()
    if c and now - c["t"] < ttl:
        return c["rows"], "", False
    if now - _CHART["fail"].get(key, 0) < 10:  # baru gagal: jangan langsung coba lagi
        return (c["rows"] if c else []), "OKX belum merespons, memakai data terakhir", True
    if not _CHART["lock"].acquire(timeout=8):
        return (c["rows"] if c else []), "sedang memuat", True
    try:
        c = _CHART["cache"].get(key)
        if c and time.time() - c["t"] < ttl:
            return c["rows"], "", False
        err = ""
        for attempt in range(2):
            try:
                mkt = _CHART["mkt"].get(flag)
                if mkt is None:
                    mkt = _CHART["mkt"][flag] = MarketData.MarketAPI(flag=flag)
                r = mkt.get_candlesticks(instId=inst, bar=bar, limit=str(CHART_LIMIT))
                if r.get("code") == "0" and r.get("data"):
                    rows = []
                    for x in r["data"]:
                        try:
                            rows.append([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]),
                                         0 if (len(x) > 8 and str(x[8]) == "0") else 1])
                        except (TypeError, ValueError, IndexError):
                            continue
                    rows.sort()
                    if rows:
                        _CHART["cache"][key] = {"t": time.time(), "rows": rows}
                        return rows, "", False
                err = str(r.get("msg") or r.get("code") or "data kosong")[:200]
                break
            except Exception as e:  # mis. WinError 10035 (socket sedang sibuk): coba sekali lagi
                err = f"{type(e).__name__}: {e}"[:200]
                if attempt == 0:
                    time.sleep(0.3)
        _CHART["fail"][key] = time.time()
        _chart_warn("candle gagal: " + err)
        return (c["rows"] if c else []), err, True
    finally:
        _CHART["lock"].release()


def chart_marks(eth=False):
    """Isi/tutup entry terakhir dari log bot (cache 20 detik). Gagal baca log = daftar kosong. eth=True: baris [ETH]."""
    ck = "marks_eth" if eth else "marks"
    m_open, m_close = (_MARK_OPEN_ETH, _MARK_CLOSE_ETH) if eth else (_MARK_OPEN, _MARK_CLOSE)
    t, cached = _CHART.get(ck, (0.0, []))
    if time.time() - t < 20:
        return cached
    marks = []
    try:
        if LOG_PATH.exists():
            with LOG_PATH.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 524288))
                chunk = fh.read().decode("utf-8", errors="replace")
            for ln in chunk.splitlines():
                m = m_open.match(ln)
                if m:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                    marks.append({"t": int(ts * 1000), "kind": "open", "side": m.group(2), "lvl": m.group(3),
                                  "px": float(m.group(4))})
                    continue
                m = m_close.match(ln)
                if m:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                    marks.append({"t": int(ts * 1000), "kind": m.group(2).lower(), "side": m.group(3),
                                  "lvl": m.group(4), "px": float(m.group(5))})
    except Exception as e:
        _chart_warn("baca log gagal: " + str(e)[:200])
        marks = []
    marks = marks[-300:]
    _CHART[ck] = (time.time(), marks)
    return marks


@app.get("/api/ethlive")
def api_ethlive():
    """Status ETH LIVE dari memori (tanpa request OKX, tanpa lock)."""
    try:
        e = ETH
        cfg = eth_cfg(min_sz=e.min_sz if e is not None else 0.01)
        out = {"ok": True, "available": e is not None, "on": cfg["on"], "cfg": cfg, "inst": ETH_INST, "lev": ETH_LEV}
        if e is None:
            return jsonify(out)
        p = dict(e._last_pos or {})
        last = e.last_px or 0.0
        out.update(ready=e.ready, err=e.err, block=e.block, avail=e.avail, last=last, managing=e.has_pos(),
                   levOk=e.lev_ok, posTs=p.get("ts"), autoClose=bool(e.auto_close), tp=e.tp_pct, sl=e.sl_pct,
                   ctVal=e.ct_val, mgn="isolated",
                   pos={k: p.get(k) for k in ("long", "short", "long_avg", "short_avg", "long_upl", "short_upl", "upl")})
        if e.spec_ok:
            out["spec"] = {"ctVal": e.ct_val, "minSz": e.min_sz, "lotSz": e.lot_sz}
            out["lvlUsdt"] = e.min_sz * e.ct_val * last
        try:
            out["lots"] = e.split_info(p, last) if e.ready else {}
        except Exception:
            out["lots"] = {}
        try:
            out["maker"] = e.maker_info() if e.ready else {}
        except Exception:
            out["maker"] = {}
        try:
            out["filters"] = " | ".join((e.trend_text(), e.snr_text(), e.lr_text())) if e.ready else ""
        except Exception:
            out["filters"] = ""
        return jsonify(out)
    except Exception as ex:
        return jsonify({"ok": False, "available": False, "msg": str(ex)[:200]})


ETH_ACTIONS = {"open_long": ("open", "long"), "open_short": ("open", "short"),
               "close_long": ("close", "long"), "close_short": ("close", "short")}


@app.post("/api/ethlive/action")
def api_ethlive_action():
    """Tombol manual ETH. Whitelist aksi, hanya objek ETH (client & instId ETH); tidak ada jalur ke BOT (BTC)."""
    from flask import request
    name = str((request.get_json(silent=True) or {}).get("action") or "")
    if name not in ETH_ACTIONS:
        return jsonify({"ok": False, "msg": "aksi ETH tidak dikenal"}), 400
    e = ETH
    if e is None or e.inst != ETH_INST:
        return jsonify({"ok": False, "msg": "ETH live belum siap"}), 503
    kind, side = ETH_ACTIONS[name]
    if not e.lock.acquire(timeout=30):
        return jsonify({"ok": False, "msg": "Bot ETH sedang sibuk, coba lagi"}), 503
    try:
        with eth_ctx():
            log.info("Dashboard: tombol ETH %s", name)
            ok, msg = e.manual_open(side) if kind == "open" else e.manual_close(side)
    except Exception as ex:
        ok, msg = False, f"ETH {name} error: {ex}"[:200]
    finally:
        e.lock.release()
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.post("/api/ethlive/settings")
def api_ethlive_settings():
    """Hanya kunci ethLiveOn / ethMaxLoss / ethMaxContracts / ethMinFreeUsdt. Tidak menyentuh setting BTC."""
    from flask import request
    b = request.get_json(silent=True) or {}
    changes = {}
    if "on" in b:
        changes["ethLiveOn"] = _on(b.get("on"))
    if "maker" in b:
        changes["ethMakerOn"] = _on(b.get("maker"))
    for key, fld, lo, hi in (("ethMaxLoss", "maxLoss", 0.1, 1000.0), ("ethMaxContracts", "maxContracts", 0.0, 1e6),
                             ("ethMinFreeUsdt", "minFree", 0.0, 1e9)):
        if fld in b and b.get(fld) is not None and str(b.get(fld)).strip() != "":
            try:
                v = float(b.get(fld))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "msg": f"{fld}: angka tidak valid"}), 400
            if not _math.isfinite(v) or v < lo or v > hi:
                return jsonify({"ok": False, "msg": f"{fld} harus {lo:g}..{hi:g}"}), 400
            changes[key] = v
    if not changes:
        return jsonify({"ok": False, "msg": "tidak ada perubahan"}), 400
    for k, v in changes.items():
        save_setting(k, v)
    with eth_ctx():
        log.info("Setting ETH live lewat dashboard: %s", changes)
    parts = []
    if "ethLiveOn" in changes:
        parts.append("ETH live " + ("HIDUP" if changes["ethLiveOn"] else "mati"))
    if "ethMakerOn" in changes:
        parts.append("maker ETH " + ("HIDUP" if changes["ethMakerOn"] else "mati"))
    parts += [f"{k} {v:g}" for k, v in changes.items() if k not in ("ethLiveOn", "ethMakerOn")]
    return jsonify({"ok": True, "msg": ", ".join(parts) + " (berlaku di putaran ETH berikutnya)"})


@app.get("/api/chart")
def api_chart():
    """Data grafik harga. Hanya baca: candle publik OKX (di-cache) + garis dari catatan bot. Tidak pernah 500."""
    from flask import request
    bar = "5m"
    try:
        bar = request.args.get("bar", "5m")
        if bar not in CHART_BARS:
            bar = "5m"
        inst = BOT.inst if BOT else INST_ID
        flag = BOT.flag if BOT else FLAG
        want = str(request.args.get("instId") or "").strip().upper()
        eth = want == ETH_INST and inst != ETH_INST   # whitelist: hanya ETH live; nilai lain = market BTC (perilaku lama)
        src = ETH if eth else BOT
        if eth:
            inst = ETH_INST
        rows, err, stale = chart_candles(inst, bar, flag)
        try:
            levels = src.chart_levels() if src is not None and (not eth or src.ready) else []
        except Exception as e:
            levels, err = [], (err + " | " if err else "") + "garis gagal: " + str(e)[:100]
        last = (src.last_px if src is not None and src.last_px else None) or (rows[-1][4] if rows else None)
        return jsonify({"ok": bool(rows), "inst": inst, "bar": bar, "barSec": CHART_BARS[bar], "candles": rows,
                        "last": last, "levels": levels, "marks": chart_marks(eth), "err": err, "stale": stale,
                        "split": bool(src is not None and src.split_active()), "ts": int(time.time() * 1000)})
    except Exception as e:
        _chart_warn(f"endpoint error {type(e).__name__}: {e}")
        return jsonify({"ok": False, "bar": bar, "barSec": CHART_BARS.get(bar, 300), "candles": [], "levels": [],
                        "marks": [], "err": f"{type(e).__name__}: {e}"[:200], "stale": True})


def tg_command(cmd):
    """Perintah Telegram (hanya dari chat yang dikonfigurasi). Tidak ada perintah buka/tutup posisi."""
    b = BOT
    if b is None:
        return "Bot belum siap."
    if cmd in ("/start", "/help"):
        return ("Perintah bot OKX:\n/status - equity, posisi, uPnL, tren, SnR, LR\n"
                "/pause - jeda (tidak buka posisi baru; TP/SL & batas rugi tetap jalan)\n"
                "/resume - lanjutkan\n/help - bantuan ini")
    if cmd == "/status":
        return "\U0001F4CA Status\n" + b.status_text() + eth_status_suffix()
    if cmd in ("/pause", "/resume"):
        want = cmd == "/pause"
        if not b.lock.acquire(timeout=60):
            return "Bot sedang sibuk, coba lagi sebentar."
        try:
            if bool(b.paused) == want:
                return "Bot memang sudah " + ("dijeda." if want else "berjalan.")
            b.set_paused(want, "Telegram", notify=False)
            b.save_state()  # sama seperti dashboard: tersimpan di okx_settings.json
        finally:
            b.lock.release()
        return ("\u23F8 Bot dijeda. Posisi baru tidak dibuka; TP/SL dan batas rugi tetap jalan." if want
                else "\u25B6 Bot dilanjutkan.")
    return "Perintah tidak dikenal. Ketik /help"


_SHUTDOWN = threading.Event()


def shutdown_notice(reason):
    """Kirim pesan berhenti (sekali) dan tunggu antrean Telegram terkirim."""
    if _SHUTDOWN.is_set():
        return
    _SHUTDOWN.set()
    log.info("Bot berhenti: %s", reason)
    try:
        if TG.send(f"\u23F9 Bot berhenti ({reason})."):
            TG.flush(8)
    except Exception:
        pass


def watch_stop_flag(bot, interval=2.0, exit_fn=None):
    """stop_bot.ps1 membuat okx_stop.flag: tunggu putaran trading selesai (lock), kabari, lalu keluar dengan rapi."""
    exit_fn = exit_fn or os._exit
    while True:
        time.sleep(interval)
        try:
            hit = STOP_FLAG.exists()
        except OSError:
            hit = False
        if not hit:
            continue
        try:
            STOP_FLAG.unlink()
        except OSError:
            pass
        got = bot.lock.acquire(timeout=60)  # jangan berhenti di tengah kirim order
        if ETH is not None:
            got = ETH.lock.acquire(timeout=60) and got
        for b_ in (bot, ETH):
            try:
                if b_ is not None and (b_.lots["long"].get("mk") or b_.lots["short"].get("mk")):
                    if b_ is ETH:
                        _CTX.eth = True
                    b_.maker_cancel_all("bot berhenti")
            except Exception as e:
                log.warning("Maker: batal saat berhenti gagal: %s", e)
            finally:
                _CTX.eth = False
        shutdown_notice("diminta lewat stop_bot" + ("" if got else ", putaran trading tidak selesai 60 dtk"))
        logging.shutdown()
        exit_fn(0)
        return


def wait_for_okx(bot, tries=40, gap=15):
    """Saat Windows baru menyala, jaringan sering belum siap: tunggu API publik OKX bisa dihubungi."""
    for i in range(1, tries + 1):
        try:
            r = bot.market.get_ticker(instId=bot.inst)
            if r.get("code") == "0":
                return True
            why = r.get("msg") or r
        except Exception as e:
            why = e
        log.warning("Menunggu koneksi OKX (%d/%d): %s", i, tries, why)
        time.sleep(gap)
    return False


# ---------- SIMULASI ETH (paper trading, modul terpisah okx_paper_eth.py) ----------
# Modul itu tidak meng-import SDK okx dan tidak menerima BOT / klien OKX: hanya fungsi baca setting dan fungsi kirim Telegram.
# Semua akses jaringannya lewat endpoint publik (market/public). Tidak pernah mengirim order, tidak menyentuh posisi BTC.
PAPER = None
PAPER_MOD = BASE_DIR / "okx_paper_eth.py"


def _paper_notify(text):
    try:
        TG.send(text)
    except Exception:
        pass


def start_paper():
    global PAPER
    if not PAPER_MOD.exists():
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("okx_paper_eth", str(PAPER_MOD))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        PAPER = mod.PaperETH(str(BASE_DIR), load_settings, notify=_paper_notify)
        threading.Thread(target=PAPER.run_forever, args=(_SHUTDOWN,), name="paper-eth", daemon=True).start()
        st = load_settings()
        log.info("Simulasi ETH siap (%s): data publik saja, TANPA order sungguhan",
                 "hidup" if st.get("ethPaperOn") in (True, 1, "1", "true", "on") else "mati, ethPaperOn tidak aktif")
    except Exception as e:
        PAPER = None
        log.warning("Simulasi ETH tidak bisa dimuat: %s", e)
    return PAPER


def start_eth_live(btc):
    global ETH
    try:
        ETH = EthLiveBot(btc)
        threading.Thread(target=eth_loop, args=(ETH, _SHUTDOWN), name="eth-live", daemon=True).start()
        cfg = eth_cfg()
        log.info("[ETH] ETH live %s: %s", ETH_INST, "HIDUP (order sungguhan, ukuran minimum)" if cfg["on"]
                 else "mati (ethLiveOn tidak aktif)")
    except Exception as e:
        ETH = None
        log.warning("[ETH] ETH live tidak bisa dimuat: %s", e)
    return ETH


def main():
    global BOT
    if not acquire_single_instance():
        log.warning("Bot lain sudah berjalan (port kunci 127.0.0.1:%s terpakai). Salinan ini keluar tanpa trading.",
                    LOCK_PORT)
        raise SystemExit(0)
    try:
        if STOP_FLAG.exists():
            STOP_FLAG.unlink()  # sisa perintah stop lama
    except OSError:
        pass
    log.info("=== Bot mulai | PID %s | %s ===", os.getpid(), "konsol" if HAS_CONSOLE else "latar belakang (tanpa konsol)")
    BOT = OKXHedgeBot()
    bot = BOT
    TG.gate = lambda: bool(getattr(BOT, "tg_on", True))
    TG.handler = tg_command
    if not wait_for_okx(bot):
        raise SystemExit("OKX tidak bisa dihubungi, cek internet")
    bot.refresh_config()
    saved_inst = bot.load_state()
    if saved_inst and saved_inst != bot.inst:
        log.info("Market tersimpan: %s (menimpa .env %s)", saved_inst, bot.inst)
        bot.inst = saved_inst
    if not bot.load_spec():
        raise SystemExit("Spec market gagal dibaca, cek OKX_INST_ID")
    _st = load_settings()
    for key, attr, cast in (("tpMode", "tp_mode", str), ("slMode", "sl_mode", str), ("tpAdd", "tp_add", float),
                            ("tpRatio", "tp_ratio", float), ("slAdd", "sl_add", float), ("slRatio", "sl_ratio", float)):
        if key in _st:
            setattr(bot, attr, cast(_st[key]))
    if "split" in _st:
        bot.split = bool(_st["split"])
        log.info("Entry terpisah: %s (tersimpan)", "hidup" if bot.split else "mati")
    bot.load_lots()
    bot.load_cooldown()
    if bot.split and bot.grid != "dca":
        log.warning("Entry terpisah hidup tapi grid=%s (bukan dca): entry terpisah & okx_lots.json TIDAK dipakai. "
                    "TP/SL dihitung dari harga rata-rata posisi (tpBase/slBase).", bot.grid)
    if SETTINGS_PATH.exists():
        try:
            saved_lev = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("leverage")
            if saved_lev:
                bot.leverage = str(saved_lev)
                log.info("Memakai leverage tersimpan %sx (menimpa .env)", bot.leverage)
        except (OSError, ValueError) as e:
            log.warning("Baca leverage tersimpan gagal: %s", e)
    bot.set_hedge_mode()
    if bot.pos_mode == "long_short_mode" and TD_MODE == "isolated":
        bot.set_leverage("long")
        bot.set_leverage("short")
    else:
        bot.set_leverage()
    if PRESET_PATH.exists():
        try:
            saved = json.loads(PRESET_PATH.read_text(encoding="utf-8")).get("preset", "")
            if saved in PRESETS:
                log.info("Memuat preset tersimpan '%s' (menimpa nilai .env)", saved)
                bot.apply_preset(saved, save=False)
        except (OSError, ValueError) as e:
            log.warning("Baca preset gagal: %s", e)
    log.info("USDT avail: %s", bot.get_balance_usdt())
    log.info("Posisi awal: %s", bot.get_positions())
    TG.start()
    try:
        TG.send("\U0001F680 Bot start\n" + bot.status_text())
    except Exception as e:
        log.warning("Pesan start Telegram gagal: %s", e)
    threading.Thread(target=bot.run, name="trading", daemon=True).start()
    threading.Thread(target=watch_stop_flag, args=(bot,), name="stop-flag", daemon=True).start()
    start_eth_live(bot)   # simulasi ETH (okx_paper_eth.py) dimatikan: thread tidak dijalankan
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        lan = probe.getsockname()[0]
        probe.close()
    except OSError:
        lan = None
    if BOT.flag == "0":
        log.warning("################  AKUN LIVE: order memakai uang sungguhan  ################")
    if DASH_TOKEN:
        host = "0.0.0.0"
        if lan:
            log.info("HP (Wi-Fi sama): http://%s:%s/?token=<OKX_DASH_TOKEN>", lan, PORT)
        log.info("Komputer ini: http://127.0.0.1:%s/?token=<OKX_DASH_TOKEN>", PORT)
    else:
        host = "127.0.0.1"
        log.warning("OKX_DASH_TOKEN kosong: dashboard hanya bisa dibuka dari komputer ini.")
        log.info("Komputer ini: http://127.0.0.1:%s", PORT)
    try:
        app.run(host=host, port=PORT, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_notice("proses dihentikan")


if __name__ == "__main__":
    main()
