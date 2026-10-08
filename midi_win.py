# MIDI no Windows direto pela winmm.dll (sem instalar nada além do Python).
# Entrada: SysEx (MIM_LONGDATA) e mensagens curtas (MIM_DATA, ex.: program change da troca de cena).
import ctypes, threading, queue, time
from ctypes import wintypes as wt

winmm = ctypes.WinDLL("winmm")

class MIDIINCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wt.WORD), ("wPid", wt.WORD), ("vDriverVersion", wt.UINT), ("szPname", wt.WCHAR * 32), ("dwSupport", wt.DWORD)]

class MIDIOUTCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wt.WORD), ("wPid", wt.WORD), ("vDriverVersion", wt.UINT), ("szPname", wt.WCHAR * 32),
                ("wTechnology", wt.WORD), ("wVoices", wt.WORD), ("wNotes", wt.WORD), ("wChannelMask", wt.WORD), ("dwSupport", wt.DWORD)]

class MIDIHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wt.DWORD), ("dwBytesRecorded", wt.DWORD), ("dwUser", ctypes.c_size_t),
                ("dwFlags", wt.DWORD), ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t), ("dwOffset", wt.DWORD),
                ("dwReserved", ctypes.c_size_t * 8)]

MIM_DATA, MIM_LONGDATA = 0x3C3, 0x3C4
CALLBACK_FUNCTION = 0x30000
MHDR_DONE = 1
HDR = ctypes.sizeof(MIDIHDR)
MidiInProc = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wt.UINT, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t)

winmm.midiInOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), wt.UINT, ctypes.c_size_t, ctypes.c_size_t, wt.DWORD]
winmm.midiOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), wt.UINT, ctypes.c_size_t, ctypes.c_size_t, wt.DWORD]
for fn in ("midiInPrepareHeader", "midiInUnprepareHeader", "midiInAddBuffer"):
    getattr(winmm, fn).argtypes = [ctypes.c_void_p, ctypes.POINTER(MIDIHDR), wt.UINT]
for fn in ("midiOutPrepareHeader", "midiOutUnprepareHeader", "midiOutLongMsg"):
    getattr(winmm, fn).argtypes = [ctypes.c_void_p, ctypes.POINTER(MIDIHDR), wt.UINT]
for fn in ("midiInStart", "midiInStop", "midiInReset", "midiInClose", "midiOutReset", "midiOutClose"):
    getattr(winmm, fn).argtypes = [ctypes.c_void_p]
winmm.midiOutShortMsg.argtypes = [ctypes.c_void_p, wt.DWORD]


def input_names():
    out = []
    for i in range(winmm.midiInGetNumDevs()):
        c = MIDIINCAPSW()
        winmm.midiInGetDevCapsW(i, ctypes.byref(c), ctypes.sizeof(c))
        out.append(c.szPname)
    return out


def output_names():
    out = []
    for i in range(winmm.midiOutGetNumDevs()):
        c = MIDIOUTCAPSW()
        winmm.midiOutGetDevCapsW(i, ctypes.byref(c), ctypes.sizeof(c))
        out.append(c.szPname)
    return out


def find_port(names, wanted):
    """Acha a porta da mesa. 'wanted' = número da porta ou parte do nome; sem nada, procura '01V96' (a porta 1)."""
    if isinstance(wanted, int) or (isinstance(wanted, str) and wanted.isdigit()):
        k = int(wanted)
        return k if 0 <= k < len(names) else None
    key = (wanted or "01V96").lower()
    hits = [i for i, n in enumerate(names) if key in n.lower()]
    if not hits:
        return None
    # o driver da Yamaha cria várias portas ("... Port1", "... Port2" ou "-1", "-2"): a 1 é a que fala com a mesa
    for i in hits:
        n = names[i].lower().replace(" ", "")
        if n.endswith("port1") or n.endswith("-1") or n.endswith("1)"):
            return i
    return hits[0]


class MidiPort:
    """Abre entrada e saída. on_message(bytes) recebe cada SysEx completo e cada mensagem curta."""

    def __init__(self, in_id, out_id, on_message, nbuf=32, bufsize=4096):
        self.on_message = on_message
        self.hin, self.hout = ctypes.c_void_p(), ctypes.c_void_p()
        self.q = queue.Queue()
        self._cb = MidiInProc(self._callback)  # guardar a referência: senão o Python apaga e o Windows chama memória solta
        r = winmm.midiInOpen(ctypes.byref(self.hin), in_id, ctypes.cast(self._cb, ctypes.c_void_p).value, 0, CALLBACK_FUNCTION)
        if r:
            raise OSError("midiInOpen falhou (%d): a porta está em uso por outro programa?" % r)
        r = winmm.midiOutOpen(ctypes.byref(self.hout), out_id, 0, 0, 0)
        if r:
            winmm.midiInClose(self.hin)
            raise OSError("midiOutOpen falhou (%d): a porta está em uso por outro programa?" % r)
        self.bufs, self.hdrs, self.by_addr = [], [], {}
        for _ in range(nbuf):
            b = ctypes.create_string_buffer(bufsize)
            h = MIDIHDR()
            h.lpData, h.dwBufferLength = ctypes.cast(b, ctypes.c_void_p), bufsize
            winmm.midiInPrepareHeader(self.hin, ctypes.byref(h), HDR)
            winmm.midiInAddBuffer(self.hin, ctypes.byref(h), HDR)
            self.bufs.append(b); self.hdrs.append(h)
            self.by_addr[ctypes.addressof(h)] = h
        self.acc = bytearray()
        self.closed = False
        self.out_lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()
        winmm.midiInStart(self.hin)

    def _callback(self, h, msg, inst, p1, p2):
        # roda numa thread do Windows: só enfileira, o trabalho fica com _worker
        if msg in (MIM_DATA, MIM_LONGDATA):
            self.q.put((msg, p1))

    def _worker(self):
        while not self.closed:
            try:
                msg, p1 = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if msg == MIM_DATA:
                st = p1 & 0xFF
                if st >= 0xF8:   # clock / active sensing
                    continue
                n = 2 if (st & 0xF0) in (0xC0, 0xD0) else 3
                self._deliver(bytes([(p1 >> (8 * k)) & 0xFF for k in range(n)]))
                continue
            h = self.by_addr.get(p1)
            if h is None:
                continue
            data = ctypes.string_at(h.lpData, h.dwBytesRecorded)
            if not self.closed:
                winmm.midiInAddBuffer(self.hin, ctypes.byref(h), HDR)  # devolve o buffer pro driver
            for byte in data:
                if byte == 0xF0:
                    self.acc = bytearray([0xF0])
                elif self.acc:
                    self.acc.append(byte)
                    if byte == 0xF7:
                        msg_bytes, self.acc = bytes(self.acc), bytearray()
                        self._deliver(msg_bytes)

    def _deliver(self, m):
        try:
            self.on_message(m)
        except Exception as e:
            print("[midi] erro tratando mensagem:", e, flush=True)

    def send(self, data):
        data = bytes(data)
        with self.out_lock:
            if self.closed:
                return
            if data[0] != 0xF0 and len(data) <= 3:
                v = 0
                for k, b in enumerate(data):
                    v |= b << (8 * k)
                winmm.midiOutShortMsg(self.hout, v)
                return
            b = ctypes.create_string_buffer(data, len(data))
            h = MIDIHDR()
            h.lpData, h.dwBufferLength, h.dwBytesRecorded = ctypes.cast(b, ctypes.c_void_p), len(data), len(data)
            winmm.midiOutPrepareHeader(self.hout, ctypes.byref(h), HDR)
            winmm.midiOutLongMsg(self.hout, ctypes.byref(h), HDR)
            t0 = time.time()
            while not (h.dwFlags & MHDR_DONE) and time.time() - t0 < 1.0:
                time.sleep(0.0005)
            winmm.midiOutUnprepareHeader(self.hout, ctypes.byref(h), HDR)

    def close(self):
        self.closed = True
        winmm.midiInStop(self.hin); winmm.midiInReset(self.hin)
        for h in self.hdrs:
            winmm.midiInUnprepareHeader(self.hin, ctypes.byref(h), HDR)
        winmm.midiInClose(self.hin)
        winmm.midiOutReset(self.hout); winmm.midiOutClose(self.hout)
