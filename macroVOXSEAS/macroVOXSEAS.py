import ctypes
import json
import math
import os
import sys
import threading
import time
import tkinter as tk
from collections import deque

import customtkinter as ctk
import cv2
import keyboard
import mss
import numpy as np
import pydirectinput

ctypes.windll.user32.SetProcessDPIAware()
pydirectinput.PAUSE = 0
try:
    # Sem isso o Windows faz time.sleep() dormir ~15 ms no mínimo, o que deixa o macro lento para reagir
    ctypes.windll.winmm.timeBeginPeriod(1)
except Exception:
    pass

# Pasta onde ficam zona.json e config.json: ao lado do .exe (ou do script, se rodar pelo Python).
# Dentro de um .exe "onefile", __file__ aponta para uma pasta temporária que some ao fechar.
if getattr(sys, "frozen", False):
    PASTA = os.path.dirname(sys.executable)
else:
    PASTA = os.path.dirname(os.path.abspath(__file__))

ARQ_ZONA = os.path.join(PASTA, "zona.json")
ARQ_CFG = os.path.join(PASTA, "config.json")
PADRAO = {"left": 991, "top": 395, "width": 60, "height": 400}
CFG_PADRAO = {
    "antecip_ms": 280.0,  # amortecimento (ms)
    "intervalo": 6.0,     # intervalo entre cliques (s)
    "pausa": 3.0,         # espera após pescar (s)
}

# Faixas de cor em HSV (H vai de 0 a 180 no OpenCV)
VERDE_MIN = np.array([40, 100, 100])
VERDE_MAX = np.array([85, 255, 255])
LARANJA_MIN = np.array([5, 100, 140])
LARANJA_MAX = np.array([30, 255, 255])
MIN_AREA = 25  # área mínima (em pixels) para considerar que achou algo

# ---------------- controle da barra ----------------
# Tudo em "alturas de barra" (a altura da barra verde vira a unidade), então não depende do tamanho da tela.
# Valores estimados a partir de vídeos do minigame; só mexa se souber o que está fazendo.
LATENCIA_S = 0.10     # atraso entre o clique e a barra reagir
ACEL_SOBE = 7.0       # aceleração para cima com o clique segurado (alturas de barra / s²)
ACEL_CAI = 5.2        # aceleração para baixo com o clique solto
KP = 12.0             # força com que a barra é puxada para o peixe
KD_BASE = 7.0         # freio (valor com amortecimento de AMORT_REF_MS)
AMORT_REF_MS = 280.0  # o slider "Amortecimento" escala o freio em relação a este valor
PERIODO_PWM = 0.10    # o clique é ligado/desligado neste ritmo para a barra "ficar parada" (s)
GRACA_S = 0.25        # se perder barra/peixe por menos que isso, mantém o último comando (s)

COR_ON = "#2ecc71"
COR_OFF = "#5dade2"


def carregar_zona():
    try:
        with open(ARQ_ZONA) as f:
            z = json.load(f)
        return {k: int(z[k]) for k in PADRAO}
    except Exception:
        return dict(PADRAO)


def salvar_zona(zona):
    try:
        with open(ARQ_ZONA, "w") as f:
            json.dump(zona, f)
    except Exception:
        pass


def carregar_cfg():
    cfg = dict(CFG_PADRAO)
    try:
        with open(ARQ_CFG) as f:
            lido = json.load(f)
        for k in cfg:
            cfg[k] = float(lido.get(k, cfg[k]))
    except Exception:
        pass
    return cfg


# ---------------- detecção ----------------
def maior_regiao(mask):
    """Retorna (x, y, w, h) da maior mancha da máscara, ou None."""
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contornos, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contornos:
        return None
    c = max(contornos, key=cv2.contourArea)
    if cv2.contourArea(c) < MIN_AREA:
        return None
    return cv2.boundingRect(c)


def candidatos_laranja(mask):
    """Todas as manchas laranja da máscara: lista de dicts com x, y, w, h, area, cy."""
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(mask)
    lista = []
    for k in range(1, n):
        x, y, w, h, area = (int(v) for v in st[k])
        if area >= MIN_AREA:
            lista.append({"x": x, "y": y, "w": w, "h": h, "area": area, "cy": y + h / 2})
    return lista


def escolher_peixe(cands, altura_img, ultimo_y=None, limite_salto=None):
    """Escolhe qual mancha laranja é o peixe.

    Os enfeites laranja do topo e da base do tubo aparecem "cortados" pela borda da zona, e o peixe
    quase nunca encosta nelas. Por isso:
      1) se já estávamos seguindo o peixe, vale a mancha mais perto da última posição;
      2) senão, vale a maior mancha que NÃO encosta na borda de cima nem na de baixo da zona.
    Se só existirem manchas encostadas na borda, devolve None (melhor "perder" o peixe do que seguir um enfeite).
    """
    if not cands:
        return None
    if ultimo_y is not None and limite_salto is not None:
        perto = min(cands, key=lambda c: abs(c["cy"] - ultimo_y))
        if abs(perto["cy"] - ultimo_y) <= limite_salto:
            return perto
    livres = [c for c in cands if c["y"] > 0 and c["y"] + c["h"] < altura_img]
    if livres:
        return max(livres, key=lambda c: c["area"])
    return None


def analisar(img_bgr, ultimo_y=None, limite_salto=None):
    """Devolve (caixa_verde, peixe, candidatos). A caixa verde é (x, y, w, h); o peixe é um dict ou None."""
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    caixa_verde = maior_regiao(cv2.inRange(hsv, VERDE_MIN, VERDE_MAX))
    cands = candidatos_laranja(cv2.inRange(hsv, LARANJA_MIN, LARANJA_MAX))
    peixe = escolher_peixe(cands, img_bgr.shape[0], ultimo_y, limite_salto)
    return caixa_verde, peixe, cands


class Piloto:
    """Decide, a cada instante, se o clique deve estar segurado ou solto.

    Em vez de ligar/desligar o clique só quando a barra cruza o peixe (o que faz ela balançar sem parar),
    calcula a aceleração que a barra precisa e a "traduz" em cliques rápidos (PWM): segurando uma fração do
    tempo ela fica parada, mais que isso sobe, menos que isso desce. Também compensa o atraso do jogo
    usando os últimos comandos enviados.
    """

    def __init__(self):
        self.alturas = deque(maxlen=90)
        self.reset()

    def reset(self):
        self.t_ant = None
        self.y_ant = None
        self.f_ant = None
        self.vel = 0.0       # velocidade da barra (alturas de barra / s, positivo = descendo)
        self.vf = 0.0        # velocidade do peixe
        self.hist = deque()  # (instante, aceleração comandada) dos últimos LATENCIA_S
        self.alturas.clear()

    def altura_barra(self, h_px):
        self.alturas.append(max(float(h_px), 1.0))
        return max(float(np.percentile(self.alturas, 80)), 10.0)

    def decidir(self, agora, verde_y_px, peixe_y_px, h_barra_px, amort_ms):
        y = verde_y_px / h_barra_px
        f = peixe_y_px / h_barra_px
        if self.t_ant is not None:
            dt = max(agora - self.t_ant, 1e-3)
            self.vel += (1 - math.exp(-dt / 0.05)) * ((y - self.y_ant) / dt - self.vel)
            self.vf += (1 - math.exp(-dt / 0.08)) * ((f - self.f_ant) / dt - self.vf)
        self.t_ant, self.y_ant, self.f_ant = agora, y, f

        # aceleração média que já foi comandada e ainda não apareceu na barra (atraso do jogo)
        while self.hist and agora - self.hist[0][0] > LATENCIA_S:
            self.hist.popleft()
        a_med = sum(a for _, a in self.hist) / len(self.hist) if self.hist else 0.0

        # onde a barra e o peixe estarão daqui a LATENCIA_S
        vp = self.vel + a_med * LATENCIA_S
        yp = y + self.vel * LATENCIA_S + 0.5 * a_med * LATENCIA_S ** 2
        fp = f + self.vf * LATENCIA_S

        kd = KD_BASE * max(amort_ms, 0.0) / AMORT_REF_MS
        a_des = -KP * (yp - fp) - kd * (vp - self.vf)  # negativo = quer subir

        # fração do tempo com o clique segurado: 0 = solto sempre, 1 = segurado sempre
        p = (ACEL_CAI - a_des) / (ACEL_SOBE + ACEL_CAI)
        p = min(1.0, max(0.0, p))
        segurar = ((agora % PERIODO_PWM) / PERIODO_PWM) < p

        self.hist.append((agora, -ACEL_SOBE if segurar else ACEL_CAI))
        return segurar


class Overlay(tk.Toplevel):
    """Quadro verde translúcido: arraste para mover, alça branca redimensiona."""

    MIN_W, MIN_H, ALCA = 20, 40, 22

    def __init__(self, master, zona):
        super().__init__(master)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.35)
        self.configure(bg="#00ff00")
        self.geometry(f"{zona['width']}x{zona['height']}+{zona['left']}+{zona['top']}")

        self.alca = tk.Frame(self, bg="white")
        self.alca.place(relx=1, rely=1, anchor="se", width=self.ALCA, height=self.ALCA)

        self._e = {}
        for w in (self, self.alca):
            w.bind("<ButtonPress-1>", self._apertar)
            w.bind("<B1-Motion>", self._arrastar)

    def _geo(self, x, y, w, h):
        self.geometry(f"{max(w, self.MIN_W)}x{max(h, self.MIN_H)}+{x}+{y}")

    def _apertar(self, e):
        self._e = {
            "mx": e.x_root, "my": e.y_root,
            "x": self.winfo_x(), "y": self.winfo_y(),
            "w": self.winfo_width(), "h": self.winfo_height(),
            "redim": e.widget is self.alca,
        }

    def _arrastar(self, e):
        s = self._e
        dx, dy = e.x_root - s["mx"], e.y_root - s["my"]
        if s["redim"]:
            self._geo(s["x"], s["y"], s["w"] + dx, s["h"] + dy)
        else:
            self._geo(s["x"] + dx, s["y"] + dy, s["w"], s["h"])

    def zona(self):
        self.update_idletasks()
        return {
            "left": self.winfo_x(), "top": self.winfo_y(),
            "width": self.winfo_width(), "height": self.winfo_height(),
        }


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Pesca Macro")
        self.geometry("360x630")
        self.resizable(False, False)

        # Sempre no topo (o after garante, pois o CustomTkinter às vezes perde o atributo na abertura)
        self.attributes("-topmost", True)
        self.after(200, lambda: self.attributes("-topmost", True))

        cfg = carregar_cfg()
        self.zona = carregar_zona()
        self.overlay = None
        self.ligado = False
        self.segurando = False
        self.antecip_ms = cfg["antecip_ms"]  # amortecimento (ms): quanto o macro "freia" a barra
        self.intervalo = cfg["intervalo"]
        self.pausa = cfg["pausa"]  # segundos de espera após a barra sumir, antes de lançar de novo
        self.auto_clique = True
        self.status = "Parado"
        self.thread = None
        self.pedido_toggle = False
        self.pedido_captura = False
        self.foto_ate = 0.0

        # ---------- cabeçalho ----------
        ctk.CTkLabel(self, text="Pesca Macro", font=ctk.CTkFont(size=22, weight="bold")).pack(pady=(16, 8))

        self.lbl_loop = ctk.CTkLabel(self, text="Main Loop: OFF", text_color=COR_OFF)
        self.lbl_loop.pack()
        self.lbl_overlay = ctk.CTkLabel(self, text="Overlay: OFF", text_color=COR_OFF)
        self.lbl_overlay.pack(pady=(2, 0))
        self.lbl_status = ctk.CTkLabel(self, text="Status: Parado", text_color="gray70")
        self.lbl_status.pack(pady=(2, 8))

        # ---------- abas ----------
        self.abas = ctk.CTkTabview(self, width=320, height=330)
        self.abas.pack(padx=20)
        aba_macro = self.abas.add("Macro")
        aba_det = self.abas.add("Detecção")

        # --- aba Macro ---
        self.sw_auto = ctk.CTkSwitch(aba_macro, text="Clicar automaticamente (lançar)", command=self.alternar_auto)
        self.sw_auto.select()
        self.sw_auto.pack(pady=(14, 0))

        self.lbl_intervalo = ctk.CTkLabel(aba_macro, text=f"Intervalo entre cliques: {self.intervalo:.0f} s")
        self.lbl_intervalo.pack(pady=(12, 0))
        s = ctk.CTkSlider(aba_macro, from_=3, to=30, number_of_steps=27, command=self.mudar_intervalo)
        s.set(self.intervalo)
        s.pack(padx=20, pady=(4, 0), fill="x")

        self.lbl_pausa = ctk.CTkLabel(aba_macro, text=f"Espera após pescar: {self.pausa:.1f} s")
        self.lbl_pausa.pack(pady=(16, 0))
        s = ctk.CTkSlider(aba_macro, from_=0, to=10, number_of_steps=20, command=self.mudar_pausa)
        s.set(self.pausa)
        s.pack(padx=20, pady=(4, 0), fill="x")

        self.lbl_amort = ctk.CTkLabel(aba_macro, text=f"Amortecimento: {self.antecip_ms:.0f} ms")
        self.lbl_amort.pack(pady=(16, 0))
        s = ctk.CTkSlider(aba_macro, from_=0, to=500, number_of_steps=50, command=self.mudar_amort)
        s.set(self.antecip_ms)
        s.pack(padx=20, pady=(4, 0), fill="x")

        self.sw_topo = ctk.CTkSwitch(aba_macro, text="Sempre no topo", command=self.alternar_topo)
        self.sw_topo.select()
        self.sw_topo.pack(pady=(18, 0))

        # --- aba Detecção ---
        self.btn_zona = ctk.CTkButton(aba_det, text="Zona de pesca", height=34, command=self.alternar_overlay)
        self.btn_zona.pack(padx=15, pady=(10, 2), fill="x")
        self.lbl_zona = ctk.CTkLabel(aba_det, text="", text_color="gray60", font=ctk.CTkFont(size=11))
        self.lbl_zona.pack()
        self.atualizar_texto_zona()

        # ---------- iniciar ----------
        self.btn_start = ctk.CTkButton(
            self, text="Iniciar (F6)", height=40, fg_color="#27ae60", hover_color="#1e8449",
            command=self.alternar_macro,
        )
        self.btn_start.pack(padx=25, pady=(12, 4), fill="x")

        ctk.CTkLabel(
            self, text="F6 liga/desliga  •  F8 salva uma foto do que o macro enxerga",
            text_color="gray60", font=ctk.CTkFont(size=11),
        ).pack()

        keyboard.add_hotkey("F6", lambda: setattr(self, "pedido_toggle", True))
        keyboard.add_hotkey("F8", lambda: setattr(self, "pedido_captura", True))
        self.after(100, self.checar_atalhos)
        self.protocol("WM_DELETE_WINDOW", self.fechar)

    # ---------- interface: ações ----------
    def checar_atalhos(self):
        if self.pedido_toggle:
            self.pedido_toggle = False
            self.alternar_macro()
        self.lbl_status.configure(text=f"Status: {self.status}")
        self.after(100, self.checar_atalhos)

    def salvar_cfg(self):
        try:
            with open(ARQ_CFG, "w") as f:
                json.dump(
                    {"antecip_ms": self.antecip_ms, "intervalo": self.intervalo, "pausa": self.pausa},
                    f,
                )
        except Exception:
            pass

    def atualizar_texto_zona(self):
        z = self.zona
        self.lbl_zona.configure(text=f"x={z['left']}  y={z['top']}  {z['width']}x{z['height']}")

    def mudar_amort(self, valor):
        self.antecip_ms = float(valor)
        self.lbl_amort.configure(text=f"Amortecimento: {self.antecip_ms:.0f} ms")
        self.salvar_cfg()

    def mudar_intervalo(self, valor):
        self.intervalo = float(valor)
        self.lbl_intervalo.configure(text=f"Intervalo entre cliques: {self.intervalo:.0f} s")
        self.salvar_cfg()

    def mudar_pausa(self, valor):
        self.pausa = float(valor)
        self.lbl_pausa.configure(text=f"Espera após pescar: {self.pausa:.1f} s")
        self.salvar_cfg()

    def alternar_auto(self):
        self.auto_clique = bool(self.sw_auto.get())

    def alternar_topo(self):
        self.attributes("-topmost", bool(self.sw_topo.get()))

    def alternar_overlay(self):
        if self.overlay is None:
            if self.ligado:
                self.parar_macro()
            self.overlay = Overlay(self, self.zona)
            self.btn_zona.configure(text="Salvar zona")
            self.lbl_overlay.configure(text="Overlay: ON", text_color=COR_ON)
        else:
            self.fechar_overlay()

    def fechar_overlay(self):
        if self.overlay is not None:
            self.zona = self.overlay.zona()
            salvar_zona(self.zona)
            self.overlay.destroy()
            self.overlay = None
            self.btn_zona.configure(text="Zona de pesca")
            self.lbl_overlay.configure(text="Overlay: OFF", text_color=COR_OFF)
            self.atualizar_texto_zona()

    def alternar_macro(self):
        if self.ligado:
            self.parar_macro()
        else:
            self.iniciar_macro()

    def iniciar_macro(self):
        self.fechar_overlay()  # o quadro verde atrapalharia a detecção
        self.ligado = True
        self.lbl_loop.configure(text="Main Loop: ON", text_color=COR_ON)
        self.btn_start.configure(text="Parar (F6)", fg_color="#c0392b", hover_color="#922b21")
        self.thread = threading.Thread(target=self.loop_macro, daemon=True)
        self.thread.start()

    def parar_macro(self):
        self.ligado = False
        self.status = "Parado"
        self.lbl_loop.configure(text="Main Loop: OFF", text_color=COR_OFF)
        self.btn_start.configure(text="Iniciar (F6)", fg_color="#27ae60", hover_color="#1e8449")

    def fechar(self):
        self.ligado = False
        if self.thread is not None:
            self.thread.join(timeout=1)
        self.fechar_overlay()
        self.destroy()

    # ---------- macro (roda em outra thread) ----------
    def clicar(self, segurar):
        if segurar and not self.segurando:
            pydirectinput.mouseDown()
            self.segurando = True
        elif not segurar and self.segurando:
            pydirectinput.mouseUp()
            self.segurando = False

    def lancar(self):
        """Um clique rápido para lançar a vara."""
        self.clicar(False)
        pydirectinput.mouseDown()
        time.sleep(0.08)
        pydirectinput.mouseUp()

    def salvar_captura(self, img, caixa_verde, peixe, cands):
        """F8: salva uma foto da zona mostrando o que o macro enxergou (para diagnóstico)."""
        try:
            vis = img.copy()
            for c in cands:  # todas as manchas laranja: amarelo
                cv2.rectangle(vis, (c["x"], c["y"]), (c["x"] + c["w"], c["y"] + c["h"]), (0, 255, 255), 1)
            if caixa_verde is not None:  # barra verde: vermelho
                x, y, w, h = caixa_verde
                cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 0, 255), 2)
            if peixe is not None:  # a mancha escolhida como peixe: rosa
                cv2.rectangle(
                    vis, (peixe["x"], peixe["y"]), (peixe["x"] + peixe["w"], peixe["y"] + peixe["h"]), (255, 0, 255), 2
                )
            lado = np.concatenate([img, vis], axis=1)
            lado = cv2.resize(lado, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
            ok, buf = cv2.imencode(".png", lado)
            if ok:
                with open(os.path.join(PASTA, "captura_macro.png"), "wb") as f:
                    f.write(buf.tobytes())
                self.status = "Foto salva: captura_macro.png"
                self.foto_ate = time.time() + 2.0
        except Exception:
            self.status = "Não consegui salvar a foto"

    def loop_macro(self):
        zona = dict(self.zona)
        piloto = Piloto()
        ultimo_visto = -1e9        # última vez em que viu barra E peixe
        ultimo_peixe_y = None
        estava_pescando = False
        t_fps, n_fps, fps = time.time(), 0, 0.0
        proximo_clique = time.time()  # clica logo ao iniciar (se a barra não estiver na tela)

        with mss.mss() as sct:
            while self.ligado:
                img = np.array(sct.grab(zona))[:, :, :3]
                agora = time.perf_counter()
                relogio = time.time()

                # limite de "salto" do peixe entre leituras: cresce com o tempo sem vê-lo
                limite = None
                if ultimo_peixe_y is not None and piloto.alturas:
                    h_ref = float(np.percentile(piloto.alturas, 80))
                    limite = h_ref * (0.5 + 4.0 * (agora - ultimo_visto))
                caixa_verde, peixe, cands = analisar(img, ultimo_peixe_y, limite)

                if self.pedido_captura:
                    self.pedido_captura = False
                    self.salvar_captura(img, caixa_verde, peixe, cands)

                n_fps += 1
                if relogio - t_fps >= 1.0:
                    fps = n_fps / (relogio - t_fps)
                    n_fps, t_fps = 0, relogio

                if caixa_verde is not None and peixe is not None:
                    # ---- minigame na tela: a barra verde acompanha o peixe ----
                    estava_pescando = True
                    ultimo_visto = agora
                    ultimo_peixe_y = peixe["cy"]
                    if relogio >= self.foto_ate:
                        self.status = f"Pescando... ({fps:.0f} fps)"
                    proximo_clique = relogio + self.intervalo  # não lança enquanto pesca

                    verde_y = caixa_verde[1] + caixa_verde[3] / 2
                    h_barra = piloto.altura_barra(caixa_verde[3])
                    self.clicar(piloto.decidir(agora, verde_y, peixe["cy"], h_barra, self.antecip_ms))

                elif estava_pescando and agora - ultimo_visto < GRACA_S:
                    pass  # perdeu barra/peixe por um instante: mantém o último comando

                else:
                    # ---- barra não apareceu (ou sumiu de vez) ----
                    self.clicar(False)
                    piloto.reset()
                    ultimo_peixe_y = None

                    if estava_pescando:
                        estava_pescando = False
                        proximo_clique = relogio + self.pausa

                    extra = ""  # pista de diagnóstico: o que o macro está vendo pela metade
                    if caixa_verde is None and peixe is not None:
                        extra = " - só vejo o peixe"
                    elif caixa_verde is not None and peixe is None:
                        extra = " - só vejo a barra"

                    if self.auto_clique:
                        restante = proximo_clique - relogio
                        if restante <= 0:
                            self.status = "Lançando a vara"
                            self.lancar()
                            proximo_clique = time.time() + self.intervalo
                        else:
                            self.status = f"Esperando a barra ({restante:.0f}s){extra}"
                    else:
                        self.status = f"Esperando a barra{extra}"

                time.sleep(0.002)

        self.clicar(False)


if __name__ == "__main__":
    App().mainloop()