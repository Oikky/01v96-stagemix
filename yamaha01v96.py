# Protocolo SysEx da Yamaha 01V96 (V2), tirado da planilha oficial "01V96 V2.0 Parameter Change Format List"
# (cópia em referencia\). Mudança:  F0 43 1n 3E mm tt ee ii cc d1 d2 d3 d4 F7
#                       Pedido:   F0 43 3n 3E mm tt ee ii cc F7
# mm = 7F (universal) ou 0D (específico da 01V96); tt = tipo; ee = elemento; ii = índice; cc = canal (0 = canal 1)
# d1..d4 = valor de 28 bits em grupos de 7 (negativo em complemento de 2).
import json, math, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
U, D = 0x7F, 0x0D

# nome -> (modelo, tipo, elemento, índice, mínimo, máximo)
P = {
    "phase": (U, 1, 0x17, 0, 0, 1),
    "pair": (U, 1, 0x18, 0, 0, 1),
    "on": (U, 1, 0x1A, 0, 0, 1),
    "pan": (U, 1, 0x1B, 0, -63, 63),
    "fader": (U, 1, 0x1C, 0, 0, 1023),
    "att": (U, 1, 0x1D, 0, -960, 120),
    "gate.on": (U, 1, 0x1E, 0, 0, 1),
    "gate.type": (U, 1, 0x1E, 5, 0, 1),
    "gate.attack": (U, 1, 0x1E, 6, 0, 120),
    "gate.range": (U, 1, 0x1E, 7, -70, 0),
    "gate.hold": (U, 1, 0x1E, 8, 0, 215),
    "gate.decay": (U, 1, 0x1E, 9, 0, 159),
    "gate.thr": (U, 1, 0x1E, 10, -540, 0),
    "comp.loc": (U, 1, 0x1F, 0, 0, 2),
    "comp.on": (U, 1, 0x1F, 1, 0, 1),
    "comp.type": (U, 1, 0x1F, 3, 0, 3),
    "comp.attack": (U, 1, 0x1F, 4, 0, 120),
    "comp.release": (U, 1, 0x1F, 5, 0, 159),
    "comp.ratio": (U, 1, 0x1F, 6, 0, 15),
    "comp.gain": (U, 1, 0x1F, 7, 0, 180),
    "comp.knee": (U, 1, 0x1F, 8, 0, 89),
    "comp.thr": (U, 1, 0x1F, 9, -540, 0),
    "eq.mode": (U, 1, 0x20, 0, 0, 1),
    "eq.lowQ": (U, 1, 0x20, 1, 0, 44), "eq.lowF": (U, 1, 0x20, 2, 5, 124), "eq.lowG": (U, 1, 0x20, 3, -180, 180),
    "eq.hpfOn": (U, 1, 0x20, 4, 0, 1),
    "eq.lmQ": (U, 1, 0x20, 5, 0, 40), "eq.lmF": (U, 1, 0x20, 6, 5, 124), "eq.lmG": (U, 1, 0x20, 7, -180, 180),
    "eq.hmQ": (U, 1, 0x20, 8, 0, 40), "eq.hmF": (U, 1, 0x20, 9, 5, 124), "eq.hmG": (U, 1, 0x20, 10, -180, 180),
    "eq.hiQ": (U, 1, 0x20, 11, 0, 43), "eq.hiF": (U, 1, 0x20, 12, 5, 124), "eq.hiG": (U, 1, 0x20, 13, -180, 180),
    "eq.lpfOn": (U, 1, 0x20, 14, 0, 1),
    "eq.on": (U, 1, 0x20, 15, 0, 1),
    "solo": (D, 3, 0x2E, 0, 0, 1),
    # AUX (masters) e Stereo out
    "aux.on": (U, 1, 0x36, 0, 0, 1),
    "aux.fader": (U, 1, 0x39, 0, 0, 1023),
    "st.on": (U, 1, 0x4D, 0, 0, 1),
    "st.bal": (U, 1, 0x4E, 0, -63, 63),
    "st.fader": (U, 1, 0x4F, 0, 0, 1023),
}
for a in range(1, 9):   # envios dos canais pros AUX 1-8
    P["send%d.on" % a] = (U, 1, 0x23, 3 * (a - 1), 0, 1)
    P["send%d.pre" % a] = (U, 1, 0x23, 3 * (a - 1) + 1, 0, 1)
    P["send%d.level" % a] = (U, 1, 0x23, 3 * (a - 1) + 2, 0, 1023)
for i in range(16):   # título da cena atual (edit buffer)
    P["scene.title%d" % i] = (U, 1, 0x01, i, 32, 122)
NAME_EL = {"ch": 0x04, "aux": 0x10, "st": 0x12}   # nomes: modelo 0D, tipo 02, índice 0-3 curto, 4-19 longo
for k, el in NAME_EL.items():
    for i in range(20):
        P["%s.name%d" % (k, i)] = (D, 2, el, i, 32, 122)

BY_ADDR = {v[:4]: k for k, v in P.items()}

CH_PARAMS = ["on", "fader", "pan", "phase", "pair", "solo"] + ["send%d.%s" % (a, x) for a in range(1, 9) for x in ("on", "pre", "level")]
DYN_PARAMS = [k for k in P if k.split(".")[0] in ("gate", "comp", "eq")] + ["att"]


def enc(v):
    v = int(v)
    if v < 0:
        v += 1 << 28
    return [(v >> 21) & 0x7F, (v >> 14) & 0x7F, (v >> 7) & 0x7F, v & 0x7F]


def dec(d):
    v = (d[0] << 21) | (d[1] << 14) | (d[2] << 7) | d[3]
    return v - (1 << 28) if v & (1 << 27) else v


def change(dev, name, ch, value):
    m, t, e, i, lo, hi = P[name]
    value = max(lo, min(hi, int(round(value))))
    return bytes([0xF0, 0x43, 0x10 | dev, 0x3E, m, t, e, i, ch] + enc(value) + [0xF7])


def request(dev, name, ch):
    m, t, e, i = P[name][:4]
    return bytes([0xF0, 0x43, 0x30 | dev, 0x3E, m, t, e, i, ch, 0xF7])


def parse(msg):
    """('param', nome, canal, valor) | ('meter', grupo, ponto, canal inicial, [dados 14 bits]) | ('pc', programa) | None"""
    if len(msg) == 2 and (msg[0] & 0xF0) == 0xC0:
        return ("pc", msg[1])
    if len(msg) < 10 or msg[0] != 0xF0 or msg[1] != 0x43 or msg[3] != 0x3E or (msg[2] & 0xF0) != 0x10:
        return None
    if msg[4] == D and msg[5] == 0x21:
        body = msg[9:-1]
        if len(body) % 2:
            return None
        return ("meter", msg[6], msg[7], msg[8], [(body[k] << 7) | body[k + 1] for k in range(0, len(body), 2)])
    if len(msg) == 14:
        name = BY_ADDR.get(tuple(msg[4:8]))
        if name:
            return ("param", name, msg[8], dec(msg[9:13]))
    return None


def meter_request(dev, group, point, ch, count):
    """Medidores remotos: a mesa manda ~20x/s por 10 s; tem que pedir de novo antes de acabar."""
    return bytes([0xF0, 0x43, 0x30 | dev, 0x3E, D, 0x21, group, point, ch, (count >> 7) & 0x7F, count & 0x7F, 0xF7])


def meter_db(v):
    """Valor do medidor (14 bits) -> dB. Formato da tabela "METER DATA" da planilha: 0x0FFF = 0 dB, 0x1FFF = clip;
    byte alto = oitava (6 dB), byte baixo = mantissa linear (confere com a tabela: -3 dB = 0x0F6A, -48 dB = 0x0804)."""
    if v >= 0x1000:
        return 0.5
    e, m = v >> 8, v & 0xFF
    if e == 0:
        return -150.0 if m == 0 else -96.0
    return 20 * math.log10(2.0 ** (e - 16) * (1 + m / 256.0))


def meter_value(db):
    """dB -> valor do medidor (o contrário de meter_db; usado pelo simulador)"""
    if db > 0.2:
        return 0x1FFF
    if db <= -96:
        return 0
    lin = 10 ** (db / 20)
    e = int(math.floor(math.log2(lin))) + 16
    m = int(round((lin / 2.0 ** (e - 16) - 1) * 256))
    if m > 255:
        e, m = e + 1, 0
    return max(0, min(0x0FFF, (e << 8) | m))


# ---------- tabelas de valores ----------
_T = json.load(open(os.path.join(HERE, "tabelas_01v96.json"), encoding="utf-8"))
TAB = {k: {int(a): b for a, b in t.items()} for k, t in _T.items()}


def _num(s):
    s = s.replace("m s", "ms").replace(" ", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    v = float(m.group(0))
    if s.endswith("kHz"):
        v *= 1000
    if s.endswith("s") and not s.endswith("ms"):
        v *= 1000   # segundos -> ms
    return v


def _fader_db(tab):
    out = []
    for i in range(1024):
        s = TAB[tab][i]
        out.append(-math.inf if "Inf" in s else float(s.replace("dB", "")))
    return out


FADER_DB = _fader_db("#05-2")      # canais e envios: -inf .. +10 dB
BUS_FADER_DB = _fader_db("#15-2")  # AUX e Stereo: -inf .. 0 dB
HOLD_MS = {i: _num(s) for i, s in TAB["#06"].items()}   # índice -> ms (a tabela pula um índice)
DECAY_MS = {i: _num(s) for i, s in TAB["#07"].items()}
RATIO = [math.inf if "∞" in TAB["#08"][i] else float(TAB["#08"][i].split(":")[0]) for i in range(16)]
EQ_F = {i: _num(TAB["#12"][i]) for i in range(128)}
EQ_Q = {i: TAB["#11"][i] for i in range(45)}


def fader_to_lin(idx, bus=False):
    db = (BUS_FADER_DB if bus else FADER_DB)[max(0, min(1023, idx))]
    return 0.0 if db == -math.inf else 10 ** (db / 20)


def lin_to_fader(lin, bus=False):
    tab = BUS_FADER_DB if bus else FADER_DB
    if lin <= 1e-7:
        return 0
    db = 20 * math.log10(lin)
    if db >= tab[-1]:
        return 1023
    # tabela crescente: busca o passo mais próximo
    lo, hi = 1, 1023
    while lo < hi:
        mid = (lo + hi) // 2
        if tab[mid] < db:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo == 1 or abs(tab[lo] - db) <= abs(tab[lo - 1] - db) else lo - 1


def nearest(values, target, log=False):
    """índice do valor mais perto (values = lista ou dict índice->valor)"""
    items = values.items() if isinstance(values, dict) else enumerate(values)
    best, bd = None, None
    for i, v in items:
        if v is None:
            continue
        if log:
            d = abs(math.log(max(v, 1e-6)) - math.log(max(target, 1e-6)))
        else:
            d = abs((1e9 if v == math.inf else v) - (1e9 if target == math.inf else target))
        if bd is None or d < bd:
            best, bd = i, d
    return best
