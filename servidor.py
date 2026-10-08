# 01V96 StageMix: servidor que liga a página StageMix (celular/tablet) à Yamaha 01V96 pelo USB-MIDI.
# A página é a mesma do REAPER: este servidor responde na "língua" da API web do REAPER + ponte igreja_remote
# e traduz tudo para SysEx da mesa (parameter change). Sem instalar nada além do Python.
#
#   python servidor.py              -> procura a 01V96 nas portas MIDI
#   python servidor.py --simular    -> mesa de mentira (pra testar sem a mesa)
#   python servidor.py --listar     -> mostra as portas MIDI
#   python servidor.py --porta 8096 --midi "01V96"
import http.server, socketserver, urllib.parse, threading, time, math, random, json, os, sys, socket, argparse
from collections import deque
import yamaha01v96 as Y

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = open(os.path.join(HERE, "servidor.log"), "a", encoding="utf-8", buffering=1)


def log(*a):
    s = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(s, flush=True)
    LOG.write(s + "\n")


NCH, NAUX = 32, 8
AUX0 = NCH          # track da página: canal n = n, AUX a = 32 + a, Stereo = 0
AUX_COLORS = [0xE8A33A, 0x3FB27F, 0x9B6BD6, 0xD9534F, 0x3A9AD9, 0xC9C93A, 0xD97AB3, 0x7A8A99]

DEFAULTS = {
    "on": 1, "fader": 0, "pan": 0, "phase": 0, "pair": 0, "solo": 0, "att": 0,
    "gate.on": 0, "gate.type": 0, "gate.attack": 0, "gate.range": -56, "gate.hold": 60, "gate.decay": 44, "gate.thr": -260,
    "comp.loc": 1, "comp.on": 0, "comp.type": 0, "comp.attack": 60, "comp.release": 37, "comp.ratio": 6, "comp.gain": 0, "comp.knee": 2, "comp.thr": -80,
    "eq.mode": 0, "eq.lowQ": 41, "eq.lowF": 36, "eq.lowG": 0, "eq.hpfOn": 1, "eq.lmQ": 23, "eq.lmF": 72, "eq.lmG": 0,
    "eq.hmQ": 23, "eq.hmF": 96, "eq.hmG": 0, "eq.hiQ": 42, "eq.hiF": 112, "eq.hiG": 0, "eq.lpfOn": 1, "eq.on": 1,
    "aux.on": 1, "aux.fader": 1023, "st.on": 1, "st.bal": 0, "st.fader": 823,
}
for _a in range(1, 9):
    DEFAULTS.update({"send%d.on" % _a: 1, "send%d.pre" % _a: 0, "send%d.level" % _a: 0})


class Mixer:
    """Estado da mesa, como a ponte enxerga (valores crus da 01V96)."""

    def __init__(self, dev=0):
        self.dev = dev
        self.low_res = False   # "Fader Resolution" LOW na mesa: faders/envios vão de 0 a 255 em vez de 0 a 1023
        self.lock = threading.RLock()
        self.v = {}            # (nome, canal 0..) -> valor
        self.got = set()       # o que já veio da mesa
        self.names = {}        # ("ch"/"aux"/"st", canal) -> lista de 20 letras
        self.title = [" "] * 16
        self.scene = None
        self.meters = {}       # (grupo, ponto, canal) -> (dB, hora)
        self.meter_hist = {}   # (grupo, ponto, canal) -> deque[(hora, dB)] últimos 300 ms
        self.meter_raw = {}    # pra tela de diagnóstico
        self.meter_regs = {}   # (grupo, ponto) -> (canal, quantidade, hora do pedido)
        self.focus = {}        # canal (1..) -> hora do último pedido da página
        self.last_rx = 0
        self.rx_count = 0
        self.recent = deque(maxlen=60)
        self.link = None
        self.sync_busy = False

    # ----- valores -----
    def get(self, name, ch):
        with self.lock:
            return self.v.get((name, ch), DEFAULTS.get(name, 0))

    def set(self, name, ch, value, send=True):
        lo, hi = Y.P[name][4:6]
        value = max(lo, min(hi, int(round(value))))
        with self.lock:
            self.v[(name, ch)] = value
        if send and self.link:
            self.link.send(Y.change(self.dev, name, ch, self._to_dev(name, value)))

    def _is_fader(self, name):
        return Y.P[name][5] == 1023

    def _to_dev(self, name, value):
        return int(round(value * 255 / 1023)) if self.low_res and self._is_fader(name) else value

    def _from_dev(self, name, value):
        return int(round(value * 1023 / 255)) if self.low_res and self._is_fader(name) else value

    def name_of(self, kind, ch):
        with self.lock:
            n = self.names.get((kind, ch))
        if not n:
            return ""
        long_, short = "".join(n[4:20]).strip(), "".join(n[0:4]).strip()
        return long_ or short

    # ----- mensagens da mesa -----
    def on_midi(self, msg):
        self.last_rx = time.time(); self.rx_count += 1
        r = Y.parse(msg)
        if r is None:
            if msg[:1] == b"\xF0" or (len(msg) and msg[0] & 0xF0 != 0xF0):
                self.recent.append("?? " + msg.hex(" "))
            return
        if r[0] == "pc":
            self.scene = r[1] + 1
            log("troca de cena (program change %d): sincronizando" % r[1])
            threading.Timer(0.4, self.sync, kwargs={"full": True}).start()
            return
        if r[0] == "meter":
            _, g, p, ch0, vals = r
            reg = self.meter_regs.get((g, p))
            if reg and len(vals) != reg[1]:
                return   # resposta curta / eco do pedido
            now = time.time()
            with self.lock:
                for k, raw in enumerate(vals):
                    key = (g, p, ch0 + k)
                    db = Y.meter_db(raw)
                    self.meters[key] = (db, now)
                    self.meter_raw[key] = raw
                    h = self.meter_hist.setdefault(key, deque())
                    h.append((now, db))
                    while h and now - h[0][0] > 0.3:
                        h.popleft()
            return
        _, name, ch, value = r
        self.recent.append("%s ch%d = %d" % (name, ch + 1, value))
        with self.lock:
            if name.startswith("scene.title"):
                self.title[int(name[11:])] = chr(value) if 32 <= value < 127 else " "
            elif ".name" in name:
                kind, idx = name.split(".name")
                self.names.setdefault((kind, ch), [" "] * 20)[int(idx)] = chr(value) if 32 <= value < 127 else " "
            else:
                self.v[(name, ch)] = self._from_dev(name, value)
            self.got.add((name, ch))

    # ----- pedidos -----
    def _burst(self, msgs):
        n = 0
        for m in msgs:
            self.link.send(m)
            n += 1
            if n % 24 == 0:
                time.sleep(0.03)   # muita coisa de uma vez e a mesa perde respostas

    def sync(self, full=True, channels=None):
        if not self.link or self.sync_busy:
            return
        self.sync_busy = True
        try:
            d = self.dev
            chs = channels if channels is not None else range(NCH)
            msgs = []
            for c in chs:
                msgs += [Y.request(d, n, c) for n in Y.CH_PARAMS]
            for a in range(NAUX):
                msgs += [Y.request(d, "aux.on", a), Y.request(d, "aux.fader", a)]
            msgs += [Y.request(d, "st.on", 0), Y.request(d, "st.fader", 0), Y.request(d, "st.bal", 0)]
            self._burst(msgs)
            if full:
                msgs = [Y.request(d, "scene.title%d" % i, 0) for i in range(16)]
                for c in chs:
                    msgs += [Y.request(d, "ch.name%d" % i, c) for i in range(20)]
                for a in range(NAUX):
                    msgs += [Y.request(d, "aux.name%d" % i, a) for i in range(20)]
                msgs += [Y.request(d, "st.name%d" % i, 0) for i in range(20)]
                self._burst(msgs)
                msgs = []
                for c in chs:
                    msgs += [Y.request(d, n, c) for n in Y.DYN_PARAMS]
                self._burst(msgs)
        finally:
            self.sync_busy = False

    def sync_channel_dyn(self, n):
        if self.link:
            self._burst([Y.request(self.dev, k, n - 1) for k in Y.DYN_PARAMS])

    def meter_loop(self):
        """Pede os medidores de novo antes dos 10 s; e os pontos internos (gate/EQ/comp) do canal aberto."""
        last_focus = None
        while True:
            now = time.time()
            foc = [n for n, t in self.focus.items() if now - t < 6]
            cur = max(foc, key=lambda n: self.focus[n]) if foc else None
            want = {(0, 0): (0, NCH), (2, 0): (0, NAUX), (4, 0): (0, 2)}
            if cur:
                c0 = cur - 1 if cur < NCH else cur - 2   # dois canais (com um só a resposta parece eco do pedido)
                for p in (1, 2, 3, 4, 5):
                    want[(0, p)] = (c0, 2)
            for (g, p), (c, cnt) in want.items():
                reg = self.meter_regs.get((g, p))
                if not reg or reg[0] != c or reg[1] != cnt or now - reg[2] > 8:
                    self.meter_regs[(g, p)] = (c, cnt, now)
                    if self.link:
                        self.link.send(Y.meter_request(self.dev, g, p, c, cnt))
            if cur != last_focus and cur:
                threading.Thread(target=self.sync_channel_dyn, args=(cur,), daemon=True).start()
            last_focus = cur
            time.sleep(0.25)

    def meter(self, g, p, ch, peak=False):
        with self.lock:
            m = self.meters.get((g, p, ch))
            if not m or time.time() - m[1] > 0.6:
                return -150.0
            if peak:
                h = self.meter_hist.get((g, p, ch))
                return max(x[1] for x in h) if h else m[0]
            return m[0]


# ---------- simulador (mesa de mentira, fala o mesmo SysEx) ----------
class Sim01V96:
    NAMES = ["BUMBO", "CAIXA", "HH", "TOM 1", "TOM 2", "SURDO", "OH L", "OH R", "BAIXO", "GUIT 1", "GUIT 2", "VIOLAO",
             "TECLADO L", "TECLADO R", "PIANO", "VOZ 1", "VOZ 2", "VOZ 3", "VOZ 4", "PASTOR", "MIC SEM FIO", "", "", ""]

    def __init__(self, on_message):
        self.out = on_message
        self.v = {}
        for c in range(NCH):
            self.v[("fader", c)] = random.choice([600, 700, 780, 823]) if c < 21 else 0
            self.v[("pan", c)] = [0, -20, 20, -40, 40][c % 5]
            self.v[("send1.level", c)] = 600 if c >= 15 else 0
            self.v[("send5.level", c)] = 500 if c in (15, 16, 17, 18) else 0
            self.v[("gate.on", c)] = 1 if c < 6 else 0
            self.v[("comp.on", c)] = 1 if c >= 8 else 0
            self.v[("eq.lowQ", c)] = 44 if c >= 15 else 41
            self.v[("eq.lmG", c)] = -35 if c == 0 else 0
            self.v[("eq.hmG", c)] = 40 if c in (1, 15) else 0
            self.v[("on", c)] = 0 if c >= 21 else 1
            self._name("ch", c, (self.NAMES[c] if c < len(self.NAMES) else "") or "ch%d" % (c + 1))
        for a, nm in enumerate(["MON 1", "MON 2", "MON 3", "IN EAR", "REVERB", "DELAY", "AUX7", "AUX8"]):
            self._name("aux", a, nm)
            self.v[("aux.fader", a)] = 823
        for i, ch_ in enumerate("CULTO DOMINGO".ljust(16)):
            self.v[("scene.title%d" % i, 0)] = ord(ch_)
        self.regs = {}
        self.q = deque()
        threading.Thread(target=self._loop, daemon=True).start()

    def _name(self, kind, c, nm):
        for i, letter in enumerate(nm[:4].ljust(4) + nm[:16].ljust(16)):   # 4 letras (curto) + 16 (longo)
            self.v[("%s.name%d" % (kind, i), c)] = ord(letter)

    def get(self, name, ch):
        return self.v.get((name, ch), DEFAULTS.get(name, 32 if ".name" in name or "title" in name else 0))

    def send(self, msg):
        msg = bytes(msg)
        if msg[4] == Y.D and msg[5] == 0x21:   # pedido de medidor
            self.regs[(msg[6], msg[7])] = (msg[8], (msg[9] << 7) | msg[10], time.time())
            return
        name = Y.BY_ADDR.get(tuple(msg[4:8]))
        if not name:
            return
        if (msg[2] & 0xF0) == 0x30:   # pedido -> responde com o valor
            self.q.append(Y.change(0, name, msg[8], self.get(name, msg[8])))
        elif len(msg) == 14:
            self.v[(name, msg[8])] = Y.dec(msg[9:13])

    def level(self, c, t):
        if c >= 21 or not self.get("on", c):
            return -150.0
        base = -20 + 6 * math.sin(t * 2.1 + c) + 4 * math.sin(t * 6.3 + 2 * c) * (0.5 + 0.5 * math.sin(t * 0.7)) + random.uniform(-2, 2)
        if c in (0, 1, 3, 4, 5) and math.sin(t * 7 + c) < 0.2:
            base -= 40   # bateria: batidas com silêncio no meio
        return base

    def _loop(self):
        nxt = time.time()
        while True:
            while self.q:
                self.out(self.q.popleft())
            now = time.time()
            if now >= nxt:
                nxt = now + 0.05
                for (g, p), (c0, cnt, t0) in list(self.regs.items()):
                    if now - t0 > 10:
                        continue
                    vals = []
                    for c in range(c0, c0 + cnt):
                        lv = self.level(c, now) if g == 0 else -18 + 4 * math.sin(now * 2 + c) if g in (2, 4) else -150
                        if g == 0 and p in (3, 5):   # redução de ganho (comp / gate), como ganho: 0 dB = sem redução
                            thr = self.get("comp.thr" if p == 3 else "gate.thr", c) / 10
                            if p == 3:
                                ratio = Y.RATIO[self.get("comp.ratio", c)]
                                gr = max(0, (lv - thr) * (1 - 1 / ratio)) if self.get("comp.on", c) else 0
                            else:
                                gr = -self.get("gate.range", c) if self.get("gate.on", c) and lv < thr else 0
                            lv = -gr
                        vals.append(Y.meter_value(lv))
                    body = []
                    for v in vals:
                        body += [(v >> 7) & 0x7F, v & 0x7F]
                    self.out(bytes([0xF0, 0x43, 0x10, 0x3E, Y.D, 0x21, g, p, c0] + body + [0xF7]))
            time.sleep(0.005)


# ---------- emulação da API web do REAPER + ponte igreja_remote ----------
MIX = Mixer()
acks, ack_order = {}, []
seq = [0]


def clean(s):
    return str(s).replace("|", " ").replace("^", " ").replace("~", " ").replace("\t", " ")


def ch_name(n):
    return MIX.name_of("ch", n - 1) or "ch%d" % n


def aux_name(a):
    return MIX.name_of("aux", a - 1) or "AUX%d" % a


def track_line(num):
    g = MIX.get
    if num == 0:
        vol, on, pan, name, color = Y.fader_to_lin(g("st.fader", 0), True), g("st.on", 0), g("st.bal", 0), "MASTER", 0
        pk = max(MIX.meter(4, 0, 0), MIX.meter(4, 0, 1))
        flags, sendcnt = 0, 0
    elif num <= NCH:
        c = num - 1
        vol, on, pan, name, color = Y.fader_to_lin(g("fader", c)), g("on", c), g("pan", c), "%d - %s" % (num, ch_name(num)), 0
        pk = MIX.meter(0, 0, c)
        flags, sendcnt = (16 if g("solo", c) else 0), NAUX
    else:
        a = num - AUX0 - 1
        vol, on, pan, name, color = Y.fader_to_lin(g("aux.fader", a), True), g("aux.on", a), 0, aux_name(a + 1), AUX_COLORS[a] | 0x1000000
        pk = MIX.meter(2, 0, a)
        flags, sendcnt = 0, 0
    if not on:
        flags |= 8
    pk = int(round(max(-150, pk) * 10))
    return "\t".join(map(str, ["TRACK", num, clean(name), flags, "%.6f" % vol, "%.6f" % (pan / 63), pk, pk, "1.000000", 0, sendcnt, 0, 0, color]))


# ----- "plugins" do canal: GATE, EQ e COMP da própria 01V96 -----
def fmt_g(v):
    return ("%.2f" % v).rstrip("0").rstrip(".") if v < 10 else "%d" % round(v)


def lin_spec(name, lo, hi, scale=1, fmt="%.1f"):
    return dict(key=name, fmt=lambda r: fmt % (r / scale), norm=lambda r: (r - lo) / (hi - lo),
                from_norm=lambda p: lo + p * (hi - lo), from_val=lambda v: v * scale)


def tab_spec(name, table):
    keys = sorted(table)
    return dict(key=name, fmt=lambda r: fmt_g(table.get(r, 0)), norm=lambda r: keys.index(r) / (len(keys) - 1) if r in keys else 0,
                from_norm=lambda p: keys[int(round(p * (len(keys) - 1)))], from_val=lambda v: Y.nearest(table, max(v, 0.01), log=True))


Q_LISTS = {   # ordem do arrasto: tipo especial primeiro, depois Q de largo (0.10) a estreito (10.0)
    "eq.lowQ": [44, 41] + list(range(40, -1, -1)),
    "eq.lmQ": list(range(40, -1, -1)),
    "eq.hmQ": list(range(40, -1, -1)),
    "eq.hiQ": [43, 42] + list(range(40, -1, -1)),
}


def q_spec(name):
    lst = Q_LISTS[name]
    return dict(key=name, fmt=lambda r: Y.EQ_Q.get(r, "1.0"), norm=lambda r: lst.index(r) / (len(lst) - 1) if r in lst else 0.5,
                from_norm=lambda p: lst[int(round(p * (len(lst) - 1)))],
                from_val=lambda v: Y.nearest({i: float(Y.EQ_Q[i]) for i in range(41)}, v, log=True))


F_TAB = {i: Y.EQ_F[i] for i in range(5, 125)}
GATE = [(10, "Threshold", lin_spec("gate.thr", -540, 0, 10)), (7, "Range", lin_spec("gate.range", -70, 0, 1, "%d")),
        (6, "Attack", lin_spec("gate.attack", 0, 120, 1, "%d")), (8, "Hold", tab_spec("gate.hold", Y.HOLD_MS)),
        (9, "Release", tab_spec("gate.decay", Y.DECAY_MS))]
COMP = [(9, "Threshold", lin_spec("comp.thr", -540, 0, 10)),
        (6, "Ratio", dict(key="comp.ratio", fmt=lambda r: "inf" if Y.RATIO[r] == math.inf else fmt_g(Y.RATIO[r]), norm=lambda r: r / 15,
                          from_norm=lambda p: round(p * 15), from_val=lambda v: Y.nearest(Y.RATIO, v))),
        (4, "Attack", lin_spec("comp.attack", 0, 120, 1, "%d")), (5, "Release", tab_spec("comp.release", Y.DECAY_MS)),
        (7, "Wet", lin_spec("comp.gain", 0, 180, 10, "%+.1f")), (8, "Knee", lin_spec("comp.knee", 0, 5, 1, "%d"))]
EQ = []
for _lab, _q, _f, _g in (("Low", "eq.lowQ", "eq.lowF", "eq.lowG"), ("LowMid", "eq.lmQ", "eq.lmF", "eq.lmG"),
                         ("HiMid", "eq.hmQ", "eq.hmF", "eq.hmG"), ("High", "eq.hiQ", "eq.hiF", "eq.hiG")):
    EQ += [(Y.P[_f][3], "Freq-" + _lab, tab_spec(_f, F_TAB)), (Y.P[_g][3], "Gain-" + _lab, lin_spec(_g, -180, 180, 10)),
           (Y.P[_q][3], "Q-" + _lab, q_spec(_q))]


def chain(n):
    """[(nome do 'plugin', parâmetro de liga/desliga, lista de parâmetros)] na ordem do sinal na 01V96"""
    gate, eq, comp = ("GATE (01V96)", "gate.on", GATE), ("EQ (01V96)", "eq.on", EQ), ("COMP (01V96)", "comp.on", COMP)
    return [gate, comp, eq] if MIX.get("comp.loc", n - 1) == 0 else [gate, eq, comp]


def eq_types(c):
    g = MIX.get
    lq, hq = g("eq.lowQ", c), g("eq.hiQ", c)
    low = "0" if lq == 41 else ("4" if g("eq.hpfOn", c) else "5") if lq == 44 else "8"
    high = "1" if hq == 42 else ("3" if g("eq.lpfOn", c) else "5") if hq == 43 else "8"
    return [low, "8", "8", high]


def chan_lines(n, with_meters):
    c = n - 1
    seq[0] += 1
    out = ["Z|ok|48000|%d" % seq[0], "C|%d" % n]
    meters = []
    grs = {"GATE (01V96)": 5, "COMP (01V96)": 3}
    for f, (fname, onkey, params) in enumerate(chain(n)):
        out.append("|".join(map(str, ["FX", n, f, "", fname, MIX.get(onkey, c), -1, 0, ""])))
        for i, label, sp in params:
            r = MIX.get(sp["key"], c)
            out.append("|".join(map(str, ["P", n, f, i, label, "%.5f" % max(0, min(1, sp["norm"](r))), sp["fmt"](r), ""])))
        if fname.startswith("EQ"):
            out.append("E|%d|%d|%s|%s" % (n, f, ",".join(eq_types(c)), "1,1,1,1"))
        if fname in grs:
            gr = MIX.meter(0, grs[fname], c)
            gr = 0.0 if gr <= -149 else max(0.0, min(70.0, -gr))   # GR vem como ganho: 0 dB = sem redução (conferir na mesa: /diag)
            meters.append("G|%d|%d|%.2f" % (n, f, gr if gr >= 0.1 else 0.0))
    if with_meters:
        last = chain(n)[-1][0]
        post_p = 2 if last.startswith("COMP") else 1
        pre, post = MIX.meter(0, 0, c), MIX.meter(0, post_p, c)
        prex, postx = MIX.meter(0, 0, c, True), MIX.meter(0, post_p, c, True)
        meters.append("M|%d|%d|%d|%d|%d" % (n, round(pre * 10), round(post * 10), round(prex * 10), round(postx * 10)))
    return out, meters


def find_param(n, f, i):
    ch = chain(n)
    if not (0 <= f < len(ch)):
        return None, None
    for pi, _, sp in ch[f][2]:
        if pi == i:
            return ch[f], sp
    return ch[f], None


def apply_bridge(cmds):
    for cmd in filter(None, cmds.split(",")):
        kind, _, rest = cmd.partition(".")
        addr, _, val = rest.partition("=")
        try:
            nums = [int(x) for x in addr.split(".")]
        except ValueError:
            continue
        if kind == "f":
            MIX.focus[nums[0]] = time.time()
            continue
        if not nums or not (1 <= nums[0] <= NCH) or len(nums) < 2:
            continue
        n, c = nums[0], nums[0] - 1
        fx, sp = find_param(n, nums[1], nums[2] if len(nums) > 2 else -1)
        if kind == "b" and fx:
            MIX.set(fx[1], c, 1 if val == "1" else 0)
        elif kind == "p" and sp:
            MIX.set(sp["key"], c, sp["from_norm"](max(0.0, min(1.0, float(val)))))
        elif kind == "v" and sp:
            v = math.inf if val == "inf" else -math.inf if val == "-inf" else float(val)
            if v == -math.inf:
                v = -1e6
            MIX.set(sp["key"], c, sp["from_val"](v))


def send_line(num, y):
    a = y + 1
    if not (1 <= num <= NCH) or not (1 <= a <= NAUX):
        return "SEND\t%d\t%d\t0\t0.000000\t0.000000\t-1" % (num, y)
    c = num - 1
    flags = 0 if MIX.get("send%d.on" % a, c) else 8
    return "SEND\t%d\t%d\t%d\t%.6f\t0.000000\t%d" % (num, y, flags, Y.fader_to_lin(MIX.get("send%d.level" % a, c)), AUX0 + a)


def toggle(name, ch):
    MIX.set(name, ch, 0 if MIX.get(name, ch) else 1)


def handle(cmd):
    parts = cmd.split("/")
    if cmd == "TRACK":
        return "\n".join(track_line(n) for n in range(0, NCH + NAUX + 1))
    if cmd == "TRANSPORT":
        t = "".join(MIX.title).strip()
        lbl = ("%02d " % MIX.scene if MIX.scene else "") + (t or "--")
        return "TRANSPORT\t0\t0.000000\t0\t%s\t%s" % (clean(lbl), clean(lbl))
    if parts[0] == "GET" and len(parts) == 5 and parts[1] == "TRACK" and parts[3] == "SEND":
        return send_line(int(parts[2]), int(parts[4]))
    if parts[0] == "GET" and len(parts) == 4 and parts[1] == "TRACK" and parts[3] == "B_PHASE":
        n = int(parts[2])
        return "GET/TRACK/%d/B_PHASE\t%d" % (n, MIX.get("phase", n - 1) if 1 <= n <= NCH else 0)
    if cmd.startswith("GET/EXTSTATE/IGREJA/"):
        key = parts[3]
        if key == "state":
            return "EXTSTATE\tIGREJA\tstate\t" + "^".join("A|%s|%s" % (c, acks[c]) for c in ack_order)
        if key[:1] in "cm" and key[1:].isdigit():
            n = int(key[1:])
            if not (1 <= n <= NCH) or time.time() - MIX.focus.get(n, 0) > 6:
                return "EXTSTATE\tIGREJA\t%s\t" % key
            lines, meters = chan_lines(n, key[0] == "m")
            body = lines if key[0] == "c" else [lines[0]] + meters
            return "EXTSTATE\tIGREJA\t%s\t" % key + "^".join(body)
        return "EXTSTATE\tIGREJA\t%s\t" % key
    if cmd.startswith("SET/EXTSTATE/IGREJA/in/"):
        msg = urllib.parse.unquote(cmd[len("SET/EXTSTATE/IGREJA/in/"):])
        head, _, cmds = msg.partition("|")
        client, _, s = head.partition(":")
        apply_bridge(cmds)
        if client not in acks:
            ack_order.append(client)
            if len(ack_order) > 8:
                acks.pop(ack_order.pop(0), None)
        acks[client] = s
        return ""
    if parts[0] == "SET" and parts[1] == "TRACK" and len(parts) >= 4:
        n = int(parts[2])
        what = parts[3]
        val = parts[4].rstrip("eg") if len(parts) > 4 else ""
        if n == 0:
            if what == "VOL": MIX.set("st.fader", 0, Y.lin_to_fader(float(val), True))
            elif what == "MUTE": toggle("st.on", 0)
            elif what == "PAN": MIX.set("st.bal", 0, float(val) * 63)
        elif n <= NCH:
            c = n - 1
            if what == "VOL": MIX.set("fader", c, Y.lin_to_fader(float(val)))
            elif what == "MUTE": toggle("on", c)
            elif what == "SOLO": toggle("solo", c)
            elif what == "PAN": MIX.set("pan", c, float(val) * 63)
            elif what == "B_PHASE": MIX.set("phase", c, int(float(val)))
            elif what == "SEND" and len(parts) >= 7:
                a = int(parts[4]) + 1
                if parts[5] == "VOL": MIX.set("send%d.level" % a, c, Y.lin_to_fader(float(parts[6].rstrip("eg"))))
                elif parts[5] == "MUTE": toggle("send%d.on" % a, c)
        elif n <= NCH + NAUX:
            a = n - AUX0 - 1
            if what == "VOL": MIX.set("aux.fader", a, Y.lin_to_fader(float(val), True))
            elif what == "MUTE": toggle("aux.on", a)
        return ""
    return ""


def diag_text():
    now = time.time()
    out = ["01V96 StageMix - diagnóstico", "",
           "ligação: %s" % ("simulador" if isinstance(MIX.link, Sim01V96) else "MIDI" if MIX.link else "nenhuma"),
           "mensagens recebidas da mesa: %d (última há %.1f s)" % (MIX.rx_count, now - MIX.last_rx if MIX.last_rx else -1),
           "parâmetros já lidos: %d" % len(MIX.got), "cena: %s %s" % (MIX.scene, "".join(MIX.title).strip()), "",
           "medidores (grupo.ponto.canal = valor cru -> dB):"]
    with MIX.lock:
        for k in sorted(MIX.meter_raw)[:80]:
            if MIX.meter_raw[k]:
                out.append("  %d.%d.%d = 0x%04X -> %.1f dB" % (k[0], k[1], k[2] + 1, MIX.meter_raw[k], Y.meter_db(MIX.meter_raw[k])))
    out += ["", "últimas mensagens:"] + ["  " + x for x in list(MIX.recent)[::-1]]
    return "\n".join(out)


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=HERE, **k)

    def log_message(self, *a):
        pass

    def _text(self, body, ctype="text/plain; charset=utf-8"):
        b = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/_/"):
            out = []
            for c in self.path[3:].split(";"):
                if not c:
                    continue
                try:
                    r = handle(c if c.startswith("SET/EXTSTATE") else urllib.parse.unquote(c))
                except Exception as e:
                    log("erro no comando", c[:80], e)
                    r = ""
                if r:
                    out.append(r)
            return self._text("\n".join(out) + "\n")
        if self.path in ("/", "/index.html"):
            self.send_response(302); self.send_header("Location", "/stagemix.html"); self.end_headers(); return
        if self.path.startswith("/diag"):
            return self._text(diag_text())
        if self.path.split("?")[0] not in ("/stagemix.html", "/favicon.ico"):
            self.send_error(404); return
        return super().do_GET()


def lan_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1)); ips.add(s.getsockname()[0]); s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def main():
    ap = argparse.ArgumentParser(description="Ponte StageMix <-> Yamaha 01V96")
    ap.add_argument("--simular", action="store_true", help="usa uma mesa de mentira")
    ap.add_argument("--listar", action="store_true", help="lista as portas MIDI e sai")
    ap.add_argument("--porta", type=int, default=8096, help="porta web (padrão 8096)")
    ap.add_argument("--midi", default=None, help="porta MIDI: número ou parte do nome (padrão: procura '01V96')")
    ap.add_argument("--canal", type=int, default=1, help="canal MIDI (Tx/Rx da mesa), 1-16")
    ap.add_argument("--fader-low", action="store_true", help="mesa com Fader Resolution LOW (0-255)")
    a = ap.parse_args()
    MIX.dev = max(0, min(15, a.canal - 1))
    MIX.low_res = a.fader_low
    log("Fader Resolution esperado na mesa: %s" % ("LOW (0-255)" if MIX.low_res else "HIGH (0-1023)"))
    if a.listar or not a.simular:
        import midi_win as M
        ins, outs = M.input_names(), M.output_names()
        print("Entradas MIDI:", *["  %d: %s" % x for x in enumerate(ins)] or ["  (nenhuma)"], sep="\n")
        print("Saídas MIDI:", *["  %d: %s" % x for x in enumerate(outs)] or ["  (nenhuma)"], sep="\n")
        if a.listar:
            return
        i, o = M.find_port(ins, a.midi), M.find_port(outs, a.midi)
        if i is None or o is None:
            log("NÃO ACHEI A 01V96 nas portas MIDI. Confira: cabo USB ligado, mesa ligada, driver Yamaha USB-MIDI instalado.")
            log("Pra testar sem a mesa: INICIAR (simulador).bat  |  pra escolher a porta: --midi <número>")
            input("Enter pra sair...")
            return
        log("Mesa: entrada '%s' / saída '%s'" % (ins[i], outs[o]))
        MIX.link = M.MidiPort(i, o, MIX.on_midi)
    else:
        log("MODO SIMULADOR (sem mesa)")
        MIX.link = Sim01V96(MIX.on_midi)
    threading.Thread(target=MIX.meter_loop, daemon=True).start()
    threading.Thread(target=MIX.sync, daemon=True).start()

    def watchdog():   # sem resposta nenhuma da mesa: avisa e tenta sincronizar de novo
        time.sleep(4)
        while True:
            if not MIX.rx_count:
                log("A mesa ainda não respondeu. Confira na 01V96: DIO/SETUP > MIDI/HOST (porta USB) e MIDI > SETUP (Parameter Change Tx/Rx ON).")
                threading.Thread(target=MIX.sync, daemon=True).start()
            time.sleep(10)
    threading.Thread(target=watchdog, daemon=True).start()

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", a.porta), H) as srv:
        log("Pronto. Abra no celular/tablet (mesma rede Wi-Fi):")
        for ip in lan_ips():
            log("   http://%s:%d" % (ip, a.porta))
        log("Neste PC: http://127.0.0.1:%d   |   diagnóstico: /diag" % a.porta)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
