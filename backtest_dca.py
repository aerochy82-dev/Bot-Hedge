"""
Backtest DCA dua arah (long + short) untuk okx_hedge_bot.py
============================================================
Meniru logika mode DCA di bot:
  - order awal (USDT) di tiap sisi, safety order makin besar (mult) dan makin jauh (step x scale)
  - TP dan SL dihitung dari harga rata-rata, Max rugi dalam USDT
  - cooldown setelah posisi ditutup, fee taker di setiap order

Contoh:
  python backtest_dca.py                         # 30 hari, 5m, BTC-USDT-SWAP, semua preset
  python backtest_dca.py --days 90 --bar 15m
  python backtest_dca.py --inst ETH-USDT-SWAP --preset seimbang
  python backtest_dca.py --csv data.csv          # pakai file candle sendiri (ts,open,high,low,close)
  python backtest_dca.py --compare               # gabung vs terpisah (TP geo+SL arith) vs terpisah (TP arith+SL geo)
  python backtest_dca.py --mode gabung           # mode lama: TP/SL dari harga rata-rata
  python backtest_dca.py --tp-mode geo --tp-ratio 1.3 --sl-mode arith --sl-add 0.4

Default meniru bot: entry terpisah, mode TP/SL dibaca dari okx_settings.json (kalau ada).

Data diambil dari API publik OKX (tanpa API key) dan disimpan di folder bt_cache/.
Hasil: ringkasan di layar + bt_trades_<preset>.csv + bt_equity_<preset>.csv
"""
import argparse
import csv
import math
import time
from pathlib import Path

PRESETS = {
    "konservatif": {"label": "Konservatif", "baseUsdt": 10, "mult": 1.5, "step": 0.008, "scale": 1.3, "so": 3,
                    "tp": 0.008, "sl": 0.025, "maxLoss": 2.5, "cooldown": 300},
    "seimbang":    {"label": "Seimbang", "baseUsdt": 15, "mult": 1.4, "step": 0.006, "scale": 1.25, "so": 4,
                    "tp": 0.006, "sl": 0.025, "maxLoss": 4.5, "cooldown": 180},
    "agresif":     {"label": "Agresif", "baseUsdt": 20, "mult": 1.5, "step": 0.005, "scale": 1.2, "so": 5,
                    "tp": 0.005, "sl": 0.02, "maxLoss": 9.0, "cooldown": 120},
}
try:  # pakai preset dari bot kalau file-nya ada di folder yang sama
    from okx_hedge_bot import PRESETS as _BOT_PRESETS  # noqa: E402
    PRESETS = {k: dict(v) for k, v in _BOT_PRESETS.items()}
except (Exception, SystemExit):
    pass

BAR_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1H": 3600, "4H": 14400}


# ---------------------------------------------------------------- data
def fetch_candles(inst, bar, days):
    from okx import MarketData
    cache = Path("bt_cache")
    cache.mkdir(exist_ok=True)
    path = cache / f"{inst}_{bar}_{days}d.csv"
    if path.exists() and time.time() - path.stat().st_mtime < 3600:
        print(f"Memakai cache {path}")
        return load_csv(path)
    api = MarketData.MarketAPI(flag="0")
    need = int(days * 86400 / BAR_SEC[bar])
    rows, after = {}, ""
    print(f"Mengambil ~{need} candle {inst} {bar} dari OKX ...")
    while len(rows) < need:
        r = api.get_history_candlesticks(instId=inst, bar=bar, after=after, limit="100")
        data = r.get("data") or []
        if r.get("code") != "0" or not data:
            break
        for d in data:
            rows[int(d[0])] = (int(d[0]), float(d[1]), float(d[2]), float(d[3]), float(d[4]))
        after = data[-1][0]
        print(f"\r  {len(rows)}/{need}", end="", flush=True)
        time.sleep(0.12)
    print()
    out = sorted(rows.values())[-need:]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close"])
        w.writerows(out)
    return out


def load_csv(path):
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out.append((int(float(r["ts"])), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    out.sort()
    return out


def fetch_spec(inst):
    try:
        from okx import PublicData
        r = PublicData.PublicAPI(flag="0").get_instruments(instType="SWAP", instId=inst)
        d = r["data"][0]
        return float(d["ctVal"]), float(d["lotSz"]), float(d["minSz"])
    except Exception as e:
        print(f"Spec gagal dibaca ({e}), memakai default BTC")
        return 0.01, 0.01, 0.01


# ---------------------------------------------------------------- engine
class Side:
    """Satu sisi DCA (long atau short)."""

    def __init__(self, name, p, ct, lot, min_sz, fee):
        self.name, self.p, self.ct, self.lot, self.min_sz, self.fee = name, p, ct, lot, min_sz, fee
        self.sign = 1 if name == "long" else -1
        self.reset()
        self.cool_until = 0
        self.trades = []

    def reset(self):
        self.qty = 0.0      # dalam koin (kontrak x ctVal)
        self.cost = 0.0     # sum(qty x harga)
        self.n = 0          # jumlah order terisi di siklus ini
        self.last = 0.0     # harga isi terakhir
        self.fees = 0.0
        self.start_ts = 0
        self.peak_notional = 0.0

    @property
    def avg(self):
        return self.cost / self.qty if self.qty else 0.0

    def contracts(self, k, px):
        usdt = self.p["baseUsdt"] * self.p["mult"] ** k
        steps = max(1, round(usdt / (self.ct * px) / self.lot))
        return max(self.min_sz, steps * self.lot)

    def upl(self, px):
        return self.sign * (px - self.avg) * self.qty

    def fill(self, px, ts):
        c = self.contracts(self.n, px if self.n == 0 else self.last)
        q = c * self.ct
        if self.n == 0:
            self.start_ts = ts
        self.qty += q
        self.cost += q * px
        self.fees += q * px * self.fee
        self.n += 1
        self.last = px
        self.peak_notional = max(self.peak_notional, self.qty * px)

    # harga-harga pemicu
    def next_safety(self):
        if self.qty == 0 or self.n > self.p["so"]:
            return None
        gap = self.p["step"] * self.p["scale"] ** (self.n - 1)
        return self.last * (1 - self.sign * gap)

    def tp_px(self):
        return self.avg * (1 + self.sign * self.p["tp"])

    def sl_px(self):
        return self.avg * (1 - self.sign * self.p["sl"])

    def loss_px(self):
        if self.p["maxLoss"] <= 0:
            return None
        return self.avg - self.sign * self.p["maxLoss"] / self.qty

    def close(self, px, ts, reason):
        exit_fee = self.qty * px * self.fee
        gross = self.upl(px)
        net = gross - self.fees - exit_fee
        self.trades.append({
            "side": self.name, "open_ts": self.start_ts, "close_ts": ts, "reason": reason,
            "orders": self.n, "safety_used": self.n - 1, "avg": round(self.avg, 2), "exit": round(px, 2),
            "qty_coin": round(self.qty, 6), "peak_notional": round(self.peak_notional, 2),
            "gross": round(gross, 4), "fees": round(self.fees + exit_fee, 4), "net": round(net, 4),
        })
        self.reset()
        self.cool_until = ts + self.p["cooldown"] * 1000
        return net

    def move(self, a, b, ts):
        """Harga bergerak monoton dari a ke b. Proses semua pemicu yang dilewati, berurutan."""
        realized = 0.0
        down = b < a
        for _ in range(50):
            if self.qty == 0:
                return realized
            cands = []
            against = (down and self.sign == 1) or (not down and self.sign == -1)
            if against:
                s = self.next_safety()
                if s is not None:
                    cands.append((s, "safety"))
                cands.append((self.sl_px(), "SL"))
                lp = self.loss_px()
                if lp is not None:
                    cands.append((lp, "MAXLOSS"))
            else:
                cands.append((self.tp_px(), "TP"))
            # yang dilewati di segmen ini, urut sesuai arah gerak
            hit = [(px, k) for px, k in cands if (b <= px <= a if down else a <= px <= b)]
            if not hit:
                return realized
            hit.sort(key=lambda x: -x[0] if down else x[0])
            # kalau beberapa di harga yang sama, keluar (SL/MAXLOSS) didahulukan, lalu safety
            px, kind = hit[0]
            same = [k for p_, k in hit if abs(p_ - px) < 1e-9]
            for pref in ("MAXLOSS", "SL", "TP", "safety"):
                if pref in same:
                    kind = pref
                    break
            if kind == "safety":
                self.fill(px, ts)
            else:
                realized += self.close(px, ts, kind)
            a = px
        return realized


# ---------------------------------------------------------------- entry terpisah (sama dengan bot)
DEFAULT_TPSL = {"tpMode": "geo", "slMode": "arith", "tpAdd": 0.002, "tpRatio": 1.25, "slAdd": 0.005, "slRatio": 1.3}


def load_bot_settings():
    """Ambil mode TP/SL dari okx_settings.json (disimpan oleh dashboard), kalau ada."""
    out = dict(DEFAULT_TPSL)
    try:
        import json
        st = json.loads(Path("okx_settings.json").read_text(encoding="utf-8"))
        for k in out:
            if k in st:
                out[k] = st[k]
    except Exception:
        pass
    return out


class SplitSide:
    """Satu sisi DCA dengan entry terpisah: TP/SL per entry, level diisi ulang, level dikunci setelah SL."""

    def __init__(self, name, p, ct, lot, min_sz, fee, tpsl):
        self.name, self.p, self.ct, self.lot, self.min_sz, self.fee, self.t = name, p, ct, lot, min_sz, fee, tpsl
        self.sign = 1 if name == "long" else -1
        self.lots = []          # {lvl, entry, qty, fee}
        self.anchor = 0.0
        self.locked = set()
        self.cool_until = 0
        self.trades = []
        self.peak_notional = 0.0
        self.refills = 0
        self.seen = set()

    # --- rumus sama dengan bot
    def gap(self, k):
        return self.p["step"] * self.p["scale"] ** max(0, k - 1)

    def level_px(self, k):
        px = self.anchor
        for i in range(1, k + 1):
            px *= (1 - self.sign * self.gap(i))
        return px

    def contracts(self, k, px):
        usdt = self.p["baseUsdt"] * self.p["mult"] ** k
        steps = max(1, round(usdt / (self.ct * px) / self.lot))
        return max(self.min_sz, steps * self.lot)

    def tp_pct(self, k):
        if self.t["tpMode"] == "arith":
            return self.p["tp"] + k * self.t["tpAdd"]
        return self.p["tp"] * self.t["tpRatio"] ** k

    def sl_pct(self, k):
        if self.t["slMode"] == "arith":
            return self.p["sl"] + k * self.t["slAdd"]
        return self.p["sl"] * self.t["slRatio"] ** k

    def tp_px(self, l):
        return l["entry"] * (1 + self.sign * self.tp_pct(l["lvl"]))

    def sl_px(self, l):
        return l["entry"] * (1 - self.sign * self.sl_pct(l["lvl"]))

    # --- posisi
    @property
    def qty(self):
        return sum(l["qty"] for l in self.lots)

    @property
    def fees(self):
        return sum(l["fee"] for l in self.lots)

    @property
    def avg(self):
        q = self.qty
        return sum(l["qty"] * l["entry"] for l in self.lots) / q if q else 0.0

    def upl(self, px):
        return sum(self.sign * (px - l["entry"]) * l["qty"] for l in self.lots)

    def open_level(self, k, px, ts):
        q = self.contracts(k, self.level_px(k)) * self.ct
        if (k, round(self.anchor, 6)) in self.seen:
            self.refills += 1
        self.seen.add((k, round(self.anchor, 6)))
        self.lots.append({"lvl": k, "entry": px, "qty": q, "fee": q * px * self.fee, "ts": ts})
        self.peak_notional = max(self.peak_notional, self.qty * px)

    def close_lot(self, l, px, ts, reason):
        exit_fee = l["qty"] * px * self.fee
        gross = self.sign * (px - l["entry"]) * l["qty"]
        net = gross - l["fee"] - exit_fee
        self.trades.append({
            "side": self.name, "open_ts": l["ts"], "close_ts": ts, "reason": reason, "orders": 1,
            "safety_used": l["lvl"], "avg": round(l["entry"], 2), "exit": round(px, 2), "qty_coin": round(l["qty"], 6),
            "peak_notional": round(l["qty"] * l["entry"], 2), "gross": round(gross, 4),
            "fees": round(l["fee"] + exit_fee, 4), "net": round(net, 4),
        })
        self.lots.remove(l)
        if reason == "SL" and self.lots:
            self.locked.add(l["lvl"])
        if not self.lots:
            self.anchor, self.locked = 0.0, set()
            self.cool_until = ts + self.p["cooldown"] * 1000
        return net

    def close_all(self, px, ts, reason):
        total = 0.0
        for l in list(self.lots):
            total += self.close_lot(l, px, ts, reason)
        return total

    def start(self, px, ts):
        """Sisi kosong dan cooldown selesai: jangkar baru, buka L0."""
        if not self.lots and ts >= self.cool_until:
            self.anchor, self.locked = px, set()
            self.open_level(0, px, ts)

    def move(self, a, b, ts):
        realized = 0.0
        down = b < a
        against = (down and self.sign == 1) or (not down and self.sign == -1)
        for _ in range(200):
            if not self.anchor:
                return realized
            c = []  # (harga, prioritas, jenis, data)  prioritas kecil = didahulukan di harga yang sama
            if against:
                for l in self.lots:
                    c.append((self.sl_px(l), 1, "SL", l))
                if self.lots and self.p.get("maxLoss", 0) > 0:
                    q = self.qty
                    c.append((self.avg - self.sign * self.p["maxLoss"] / q, 0, "MAXLOSS", None))
                filled = {l["lvl"] for l in self.lots}
                for k in range(self.p["so"] + 1):
                    if k not in filled and k not in self.locked:
                        c.append((self.level_px(k), 2, "ENTRY", k))
            else:
                for l in self.lots:
                    c.append((self.tp_px(l), 1, "TP", l))
                for k in list(self.locked):
                    c.append((self.level_px(k), 2, "UNLOCK", k))
            lo, hi = (b, a) if down else (a, b)
            hit = [x for x in c if lo - 1e-9 <= x[0] <= hi + 1e-9]
            # entry yang levelnya sudah terlewat (harga sudah di bawah level) juga diisi, di harga sekarang
            if not hit:
                return realized
            hit.sort(key=lambda x: ((-x[0] if down else x[0]), x[1]))
            px, _, kind, data = hit[0]
            if kind == "ENTRY":
                self.open_level(data, px, ts)
            elif kind == "UNLOCK":
                self.locked.discard(data)
            elif kind == "MAXLOSS":
                realized += self.close_all(px, ts, "MAXLOSS")
                return realized
            else:
                realized += self.close_lot(data, px, ts, kind)
                if not self.lots:
                    return realized
            a = px
        return realized


def run_split(candles, p, ct, lot, min_sz, fee, start_equity, tpsl):
    sides = [SplitSide("long", p, ct, lot, min_sz, fee, tpsl), SplitSide("short", p, ct, lot, min_sz, fee, tpsl)]
    realized, equity_rows = 0.0, []
    peak_eq, max_dd, max_notional = start_equity, 0.0, 0.0
    for ts, o, h, l, c in candles:
        for s in sides:
            s.start(o, ts)
        path = [o, l, h, c] if c >= o else [o, h, l, c]
        for a, b in zip(path, path[1:]):
            if a == b:
                continue
            for s in sides:
                realized += s.move(a, b, ts)
        upl = sum(s.upl(c) - s.fees for s in sides)
        eq = start_equity + realized + upl
        peak_eq = max(peak_eq, eq)
        max_dd = max(max_dd, peak_eq - eq)
        max_notional = max(max_notional, sum(s.qty * c for s in sides))
        equity_rows.append((ts, round(eq, 4)))
    last_c = candles[-1][4]
    open_upl = sum(s.upl(last_c) - s.fees - s.qty * last_c * fee for s in sides)
    trades = sorted(sides[0].trades + sides[1].trades, key=lambda t: t["close_ts"])
    return {
        "trades": trades, "equity": equity_rows, "realized": realized, "open_upl": open_upl,
        "max_dd": max_dd, "max_notional": max_notional,
        "open": {s.name: [f"L{l['lvl']}" for l in s.lots] for s in sides},
        "refills": sum(s.refills for s in sides),
    }


def run(candles, p, ct, lot, min_sz, fee, start_equity):
    sides = [Side("long", p, ct, lot, min_sz, fee), Side("short", p, ct, lot, min_sz, fee)]
    realized = 0.0
    equity_rows = []
    peak_eq, max_dd = start_equity, 0.0
    max_notional = 0.0
    for ts, o, h, l, c in candles:
        # order awal di open candle kalau sisi kosong dan cooldown selesai
        for s in sides:
            if s.qty == 0 and ts >= s.cool_until:
                s.fill(o, ts)
        # jalur harga di dalam candle (asumsi umum): naik dulu kalau candle merah, turun dulu kalau hijau
        path = [o, l, h, c] if c >= o else [o, h, l, c]
        for a, b in zip(path, path[1:]):
            if a == b:
                continue
            for s in sides:
                realized += s.move(a, b, ts)
                # sisi yang baru tutup bisa buka lagi di candle berikutnya (cooldown >= 1 bar praktisnya)
        upl = sum(s.upl(c) - s.fees for s in sides if s.qty)
        eq = start_equity + realized + upl
        peak_eq = max(peak_eq, eq)
        max_dd = max(max_dd, peak_eq - eq)
        max_notional = max(max_notional, sum(s.qty * c for s in sides))
        equity_rows.append((ts, round(eq, 4)))
    last_c = candles[-1][4]
    open_upl = sum(s.upl(last_c) - s.fees - s.qty * last_c * fee for s in sides if s.qty)
    trades = sides[0].trades + sides[1].trades
    trades.sort(key=lambda t: t["close_ts"])
    return {
        "trades": trades, "equity": equity_rows, "realized": realized, "open_upl": open_upl,
        "max_dd": max_dd, "max_notional": max_notional,
        "open": {s.name: (s.n - 1 if s.qty else None) for s in sides},
    }


def summarize(name, p, res, days):
    t = res["trades"]
    by = lambda r: [x for x in t if x["reason"] == r]
    tp, sl, ml = by("TP"), by("SL"), by("MAXLOSS")
    fees = sum(x["fees"] for x in t)
    wins = len(tp)
    losses = len(sl) + len(ml)
    total = res["realized"] + res["open_upl"]
    su = [x["safety_used"] for x in t]
    print(f"\n=== {p['label']} | {res.get('variant', 'gabung')} ===")
    unit = "Entry ditutup" if res.get("split") else "Siklus selesai"
    print(f"  {unit:<19}: {len(t)}  (TP {wins}, SL {len(sl)}, Max rugi {len(ml)})")
    if res.get("split"):
        print(f"  Level diisi ulang  : {res.get('refills', 0)} kali")
    if t:
        print(f"  Win rate           : {wins / len(t) * 100:.1f}%")
        print(f"  Rata-rata TP       : {sum(x['net'] for x in tp) / max(1, wins):+.4f} USDT")
        if losses:
            print(f"  Rata-rata rugi     : {sum(x['net'] for x in sl + ml) / losses:+.4f} USDT")
        print(f"  {'Level rata-rata' if res.get('split') else 'Safety terpakai':<19}: rata-rata {sum(su) / len(su):.2f}, maks {max(su)}")
    print(f"  Fee dibayar        : {fees:.4f} USDT")
    print(f"  PnL terealisasi    : {res['realized']:+.4f} USDT")
    print(f"  PnL posisi terbuka : {res['open_upl']:+.4f} USDT  (terbuka: {res['open']})")
    print(f"  TOTAL              : {total:+.4f} USDT  (~{total / max(days, 1e-9):+.4f}/hari)")
    print(f"  Drawdown maks      : {res['max_dd']:.4f} USDT")
    print(f"  Notional maks      : {res['max_notional']:.2f} USDT")
    return {"total": total, "wins": wins, "losses": losses, "n": len(t), "fees": fees}


def variant_label(v):
    if v["mode"] == "gabung":
        return "gabung (TP/SL dari rata-rata)"
    t = v["tpsl"]
    tp = f"TP arith +{t['tpAdd']*100:.2f}%" if t["tpMode"] == "arith" else f"TP geo x{t['tpRatio']:g}"
    sl = f"SL arith +{t['slAdd']*100:.2f}%" if t["slMode"] == "arith" else f"SL geo x{t['slRatio']:g}"
    return f"terpisah, {tp}, {sl}"


def main():
    ap = argparse.ArgumentParser(description="Backtest DCA dua arah")
    ap.add_argument("--inst", default="BTC-USDT-SWAP")
    ap.add_argument("--bar", default="5m", choices=list(BAR_SEC))
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--preset", default="all", help="konservatif / seimbang / agresif / all")
    ap.add_argument("--csv", help="file candle sendiri (ts,open,high,low,close)")
    ap.add_argument("--fee", type=float, default=0.0005, help="fee taker per sisi, default 0.05%%")
    ap.add_argument("--equity", type=float, default=4000, help="saldo awal untuk hitung drawdown")
    ap.add_argument("--ct", type=float, help="ctVal manual (lewati baca spec)")
    ap.add_argument("--lot", type=float, help="lotSz manual")
    ap.add_argument("--mode", default="terpisah", choices=["gabung", "terpisah"],
                    help="gabung = TP/SL dari harga rata-rata, terpisah = TP/SL per entry (default, sama dengan bot)")
    ap.add_argument("--compare", action="store_true",
                    help="bandingkan: gabung vs terpisah (TP geo + SL arith) vs terpisah (TP arith + SL geo)")
    ap.add_argument("--tp-mode", choices=["arith", "geo"])
    ap.add_argument("--sl-mode", choices=["arith", "geo"])
    ap.add_argument("--tp-add", type=float, help="tambahan TP per level, persen (arith)")
    ap.add_argument("--tp-ratio", type=float, help="rasio TP per level (geo)")
    ap.add_argument("--sl-add", type=float, help="tambahan SL per level, persen (arith)")
    ap.add_argument("--sl-ratio", type=float, help="rasio SL per level (geo)")
    a = ap.parse_args()

    if a.csv:
        candles = load_csv(a.csv)
    else:
        candles = fetch_candles(a.inst, a.bar, a.days)
    if len(candles) < 10:
        raise SystemExit("Data candle terlalu sedikit")
    if a.ct and a.lot:
        ct, lot, min_sz = a.ct, a.lot, a.lot
    else:
        ct, lot, min_sz = fetch_spec(a.inst)
    days = (candles[-1][0] - candles[0][0]) / 86400000 + BAR_SEC.get(a.bar, 300) / 86400
    lo = min(c[3] for c in candles)
    hi = max(c[2] for c in candles)
    chg = (candles[-1][4] / candles[0][1] - 1) * 100
    print(f"{a.inst} | {len(candles)} candle | {days:.1f} hari | harga {candles[0][1]:.1f} -> {candles[-1][4]:.1f} "
          f"({chg:+.2f}%) | rentang {lo:.1f}-{hi:.1f} ({(hi / lo - 1) * 100:.2f}%)")
    print(f"ctVal {ct:g}, lotSz {lot:g}, fee {a.fee * 100:.3f}% per order")

    tpsl = load_bot_settings()
    for arg, key, scale in (("tp_mode", "tpMode", None), ("sl_mode", "slMode", None), ("tp_add", "tpAdd", 100),
                            ("tp_ratio", "tpRatio", None), ("sl_add", "slAdd", 100), ("sl_ratio", "slRatio", None)):
        v = getattr(a, arg)
        if v is not None:
            tpsl[key] = v / scale if scale else v
    if a.compare:
        variants = [
            {"mode": "gabung", "tpsl": tpsl},
            {"mode": "terpisah", "tpsl": dict(tpsl, tpMode="geo", slMode="arith")},
            {"mode": "terpisah", "tpsl": dict(tpsl, tpMode="arith", slMode="geo")},
        ]
    else:
        variants = [{"mode": a.mode, "tpsl": tpsl}]

    names = list(PRESETS) if a.preset == "all" else [a.preset]
    table = []
    for n in names:
        p = PRESETS[n]
        for vi, v in enumerate(variants):
            if v["mode"] == "gabung":
                res = run(candles, p, ct, lot, min_sz, a.fee, a.equity)
            else:
                res = run_split(candles, p, ct, lot, min_sz, a.fee, a.equity, v["tpsl"])
                res["split"] = True
            res["variant"] = variant_label(v)
            st = summarize(n, p, res, days)
            table.append((p["label"], res["variant"], st, res["max_dd"], res["max_notional"]))
            tag = n if len(variants) == 1 else f"{n}_{['gabung', 'tpgeo_slarith', 'tparith_slgeo'][vi]}"
            with open(f"bt_trades_{tag}.csv", "w", newline="") as f:
                if res["trades"]:
                    w = csv.DictWriter(f, fieldnames=list(res["trades"][0]))
                    w.writeheader()
                    w.writerows(res["trades"])
            with open(f"bt_equity_{tag}.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["ts", "equity"])
                w.writerows(res["equity"])
    if len(table) > 1:
        print("\n" + "=" * 96)
        print(f"{'Preset':<12} {'Varian':<46} {'Total':>9} {'Win%':>6} {'DD maks':>8} {'Notional':>9}")
        print("-" * 96)
        for lbl, var, st, dd, notion in table:
            wr = st["wins"] / st["n"] * 100 if st["n"] else 0
            print(f"{lbl:<12} {var:<46} {st['total']:>+9.3f} {wr:>5.1f}% {dd:>8.3f} {notion:>9.1f}")
        best = max(table, key=lambda x: x[2]["total"] / max(x[3], 0.01))
        print("-" * 96)
        print(f"Terbaik (untung dibanding drawdown): {best[0]} | {best[1]}")
    print("\nCatatan: funding fee, slippage, dan urutan harga di dalam candle tidak dihitung persis.")
    print("Hasil masa lalu tidak menjamin hasil ke depan. Uji beberapa periode berbeda.")


if __name__ == "__main__":
    main()
