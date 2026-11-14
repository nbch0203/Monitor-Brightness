"""
Control de Brillo - ajusta el brillo de los monitores externos por DDC/CI
y de la pantalla integrada (portátiles) por WMI.
"""
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import customtkinter as ctk
from monitorcontrol import get_monitors

APP_NAME = "Control de Brillo"
CONFIG_PATH = Path(os.environ.get("APPDATA", Path.home())) / "ControlBrillo" / "config.json"
PRESETS_DEFAULT = {"Noche": 15, "Trabajo": 50, "Día": 85}


# ---------------------------------------------------------------- backend ---
@dataclass
class Pantalla:
    nombre: str
    tipo: str                     # "ddc" | "wmi"
    handle: object = None         # monitorcontrol.Monitor o métodos WMI
    lectura: object = None        # objeto WMI de lectura (solo tipo wmi)
    brillo: int | None = None
    ok: bool = True
    lock: threading.Lock = field(default_factory=threading.Lock)

    def leer(self) -> int | None:
        with self.lock:
            try:
                if self.tipo == "ddc":
                    with self.handle:
                        self.brillo = int(self.handle.get_luminance())
                else:
                    self.brillo = int(self.lectura.CurrentBrightness)
                self.ok = True
            except Exception:
                self.ok = False
            return self.brillo

    def escribir(self, valor: int) -> bool:
        valor = max(0, min(100, int(valor)))
        with self.lock:
            for _ in range(3):
                try:
                    if self.tipo == "ddc":
                        with self.handle:
                            self.handle.set_luminance(valor)
                    else:
                        self.handle.WmiSetBrightness(0, valor)
                    self.brillo = valor
                    self.ok = True
                    return True
                except Exception:
                    time.sleep(0.15)
            self.ok = False
            return False


def _nombres_wmi() -> list[str]:
    """Nombres amigables de los monitores según Windows, en orden de detección."""
    try:
        import wmi
        w = wmi.WMI(namespace="root\\wmi")
        nombres = []
        for m in w.WmiMonitorID():
            n = "".join(chr(c) for c in (m.UserFriendlyName or []) if c).strip()
            nombres.append(n or "Monitor")
        return nombres
    except Exception:
        return []


def detectar_pantallas() -> list[Pantalla]:
    pantallas: list[Pantalla] = []

    # Pantalla integrada (portátiles) vía WMI
    try:
        import wmi
        w = wmi.WMI(namespace="root\\wmi")
        metodos = w.WmiMonitorBrightnessMethods()
        for i, b in enumerate(w.WmiMonitorBrightness()):
            p = Pantalla(nombre="Pantalla integrada", tipo="wmi",
                         handle=metodos[i], lectura=b)
            p.brillo = int(b.CurrentBrightness)
            pantallas.append(p)
    except Exception:
        pass

    # Monitores externos vía DDC/CI
    nombres = _nombres_wmi()
    for i, m in enumerate(get_monitors()):
        # El nombre sale de Windows (WMI); pedirlo por DDC/CI tarda varios segundos.
        nombre = nombres[i] if i < len(nombres) else f"Monitor {i + 1}"
        p = Pantalla(nombre=nombre, tipo="ddc", handle=m)
        p.leer()
        pantallas.append(p)

    return pantallas


# ----------------------------------------------------------------- config ---
def cargar_config() -> dict:
    try:
        cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
        cfg.setdefault("presets", dict(PRESETS_DEFAULT))
        cfg.setdefault("tema", "dark")
        return cfg
    except Exception:
        return {"presets": dict(PRESETS_DEFAULT), "tema": "dark"}


def guardar_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------- UI ---
class FilaMonitor(ctk.CTkFrame):
    def __init__(self, master, pantalla: Pantalla, on_change):
        super().__init__(master, corner_radius=12)
        self.pantalla = pantalla
        self.on_change = on_change
        self._job = None
        self._enviando = False
        self._pendiente = None

        self.grid_columnconfigure(1, weight=1)

        icono = "💻" if pantalla.tipo == "wmi" else "🖥"
        self.lbl_nombre = ctk.CTkLabel(self, text=f"{icono}  {pantalla.nombre}",
                                       font=ctk.CTkFont(size=15, weight="bold"), anchor="w")
        self.lbl_nombre.grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(10, 0))

        self.lbl_valor = ctk.CTkLabel(self, text="--", width=48,
                                      font=ctk.CTkFont(size=18, weight="bold"))
        self.lbl_valor.grid(row=0, column=2, rowspan=2, padx=(0, 14))

        self.slider = ctk.CTkSlider(self, from_=0, to=100, number_of_steps=100,
                                    command=self._al_mover)
        self.slider.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14, pady=(6, 4))

        self.lbl_estado = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11),
                                       text_color="gray", anchor="w", justify="left")
        self.lbl_estado.grid(row=2, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 8))

        self.refrescar()

    def refrescar(self):
        p = self.pantalla
        if p.ok and p.brillo is not None:
            self.slider.configure(state="normal")
            self.slider.set(p.brillo)
            self.lbl_valor.configure(text=f"{p.brillo}%")
            self.lbl_estado.configure(
                text="DDC/CI" if p.tipo == "ddc" else "Pantalla integrada (WMI)",
                text_color="gray")
        else:
            self.slider.set(0)
            self.slider.configure(state="disabled")
            self.lbl_valor.configure(text="—")
            self.lbl_estado.configure(
                text=("No responde. Posibles causas:\n"
                      "• HDR activado en Windows (Win+Alt+B lo desactiva, luego pulsa ⟳)\n"
                      "• DDC/CI desactivado en el menú (OSD) del monitor\n"
                      "• Cable o adaptador que no pasa DDC/CI"),
                text_color="#e07a5f")

    def set_valor(self, valor: int):
        if not self.pantalla.ok:
            return
        self.slider.set(valor)
        self.lbl_valor.configure(text=f"{int(valor)}%")
        self._programar_envio(int(valor))

    def _al_mover(self, valor):
        self.lbl_valor.configure(text=f"{int(valor)}%")
        self._programar_envio(int(valor))
        self.on_change()

    def _programar_envio(self, valor: int):
        """Debounce: espera 120 ms sin cambios antes de enviar al monitor."""
        self._pendiente = valor
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(120, self._enviar)

    def _enviar(self):
        self._job = None
        if self._enviando:
            self._job = self.after(100, self._enviar)
            return
        valor, self._pendiente = self._pendiente, None
        if valor is None:
            return
        self._enviando = True

        def trabajo():
            ok = self.pantalla.escribir(valor)
            self.after(0, lambda: self._fin_envio(ok))
        threading.Thread(target=trabajo, daemon=True).start()

    def _fin_envio(self, ok):
        self._enviando = False
        if not ok:
            self.refrescar()
            self.on_change()
        elif self._pendiente is not None:
            self._enviar()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.cfg = cargar_config()
        ctk.set_appearance_mode(self.cfg.get("tema", "dark"))
        ctk.set_default_color_theme("blue")

        self.title(APP_NAME)
        self.geometry("460x540")
        self.minsize(400, 380)
        self.pantallas: list[Pantalla] = []
        self.filas: list[FilaMonitor] = []
        self._tray = None
        self._sincronizando = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Cabecera
        cab = ctk.CTkFrame(self, fg_color="transparent")
        cab.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        cab.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(cab, text="☀  Control de Brillo",
                     font=ctk.CTkFont(size=22, weight="bold")).grid(row=0, column=0, sticky="w")
        self.btn_tema = ctk.CTkButton(
            cab, text="🌙" if self.cfg["tema"] == "dark" else "☀", width=36,
            command=self._toggle_tema)
        self.btn_tema.grid(row=0, column=1, padx=(0, 6))
        self.btn_refrescar = ctk.CTkButton(cab, text="⟳", width=36, command=self.detectar)
        self.btn_refrescar.grid(row=0, column=2)

        # Control maestro
        maestro = ctk.CTkFrame(self, corner_radius=12)
        maestro.grid(row=1, column=0, sticky="ew", padx=16, pady=6)
        maestro.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(maestro, text="Todos los monitores",
                     font=ctk.CTkFont(size=14, weight="bold"), anchor="w"
                     ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 0))
        self.lbl_maestro = ctk.CTkLabel(maestro, text="--", width=48,
                                        font=ctk.CTkFont(size=18, weight="bold"))
        self.lbl_maestro.grid(row=0, column=1, rowspan=2, padx=(0, 14))
        self.slider_maestro = ctk.CTkSlider(maestro, from_=0, to=100, number_of_steps=100,
                                            command=self._maestro_movido)
        self.slider_maestro.grid(row=1, column=0, sticky="ew", padx=14, pady=(6, 8))

        presets = ctk.CTkFrame(maestro, fg_color="transparent")
        presets.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))
        self.botones_preset = []
        for i, (nombre, valor) in enumerate(self.cfg["presets"].items()):
            presets.grid_columnconfigure(i, weight=1)
            b = ctk.CTkButton(presets, text=f"{nombre}\n{valor}%", height=44,
                              fg_color=("gray80", "gray25"), text_color=("black", "white"),
                              hover_color=("gray70", "gray35"),
                              command=lambda v=valor: self.aplicar_a_todos(v))
            b.grid(row=0, column=i, sticky="ew", padx=4)
            b.bind("<Button-3>", lambda e, n=nombre: self._guardar_preset(n))
            self.botones_preset.append((nombre, b))
        ctk.CTkLabel(maestro, text="Clic derecho en un preset para guardar el brillo actual",
                     font=ctk.CTkFont(size=11), text_color="gray"
                     ).grid(row=3, column=0, columnspan=2, padx=14, pady=(0, 8), sticky="w")

        # Lista de monitores
        self.lista = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.lista.grid(row=2, column=0, sticky="nsew", padx=10, pady=4)
        self.lista.grid_columnconfigure(0, weight=1)

        self.lbl_pie = ctk.CTkLabel(self, text="Detectando monitores…",
                                    font=ctk.CTkFont(size=11), text_color="gray")
        self.lbl_pie.grid(row=3, column=0, sticky="w", padx=18, pady=(0, 10))

        self.protocol("WM_DELETE_WINDOW", self._ocultar_a_bandeja)
        self.bind("<Up>", lambda e: self._ajustar_maestro(+5))
        self.bind("<Down>", lambda e: self._ajustar_maestro(-5))
        self._iniciar_bandeja()
        self.detectar()

    # ----- detección
    def detectar(self):
        self.btn_refrescar.configure(state="disabled")
        self.lbl_pie.configure(text="Detectando monitores…")
        for f in self.filas:
            f.destroy()
        self.filas = []

        def trabajo():
            try:
                pantallas = detectar_pantallas()
            except Exception as e:
                err = f"Error al detectar monitores: {e}"
                self.after(0, lambda: (self.lbl_pie.configure(text=err, text_color="#e07a5f"),
                                       self.btn_refrescar.configure(state="normal")))
                return
            self.after(0, lambda: self._mostrar(pantallas))
        threading.Thread(target=trabajo, daemon=True).start()

    def _mostrar(self, pantallas):
        self.pantallas = pantallas
        for i, p in enumerate(pantallas):
            fila = FilaMonitor(self.lista, p, on_change=self._sincronizar_maestro)
            fila.grid(row=i, column=0, sticky="ew", pady=5, padx=4)
            self.filas.append(fila)
        activos = [p for p in pantallas if p.ok]
        if not pantallas:
            self.lbl_pie.configure(text="No se encontró ningún monitor compatible.")
        else:
            self.lbl_pie.configure(
                text=f"{len(activos)} de {len(pantallas)} monitores responden  ·  ↑/↓ ajustan todos")
        self._sincronizar_maestro()
        self.btn_refrescar.configure(state="normal")

    # ----- maestro
    def _sincronizar_maestro(self):
        activos = [f for f in self.filas if f.pantalla.ok]
        if not activos:
            self.lbl_maestro.configure(text="—")
            self.slider_maestro.configure(state="disabled")
            return
        self.slider_maestro.configure(state="normal")
        media = round(sum(f.slider.get() for f in activos) / len(activos))
        self._sincronizando = True
        self.slider_maestro.set(media)
        self._sincronizando = False
        self.lbl_maestro.configure(text=f"{media}%")

    def _maestro_movido(self, valor):
        if self._sincronizando:
            return
        valor = int(valor)
        self.lbl_maestro.configure(text=f"{valor}%")
        for f in self.filas:
            f.set_valor(valor)

    def aplicar_a_todos(self, valor: int):
        self._sincronizando = True
        self.slider_maestro.set(valor)
        self._sincronizando = False
        self._maestro_movido(valor)

    def _ajustar_maestro(self, delta: int):
        actual = int(self.slider_maestro.get())
        self.aplicar_a_todos(max(0, min(100, actual + delta)))

    def _guardar_preset(self, nombre: str):
        valor = int(self.slider_maestro.get())
        self.cfg["presets"][nombre] = valor
        guardar_config(self.cfg)
        for n, b in self.botones_preset:
            if n == nombre:
                b.configure(text=f"{nombre}\n{valor}%",
                            command=lambda v=valor: self.aplicar_a_todos(v))
        self.lbl_pie.configure(text=f"Preset «{nombre}» guardado en {valor}%")

    def _toggle_tema(self):
        nuevo = "light" if ctk.get_appearance_mode().lower() == "dark" else "dark"
        ctk.set_appearance_mode(nuevo)
        self.btn_tema.configure(text="🌙" if nuevo == "dark" else "☀")
        self.cfg["tema"] = nuevo
        guardar_config(self.cfg)

    # ----- bandeja del sistema
    def _iniciar_bandeja(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((16, 16, 48, 48), fill=(255, 200, 60, 255))
        for ang in range(0, 360, 45):
            r = math.radians(ang)
            d.line((32 + 22 * math.cos(r), 32 + 22 * math.sin(r),
                    32 + 30 * math.cos(r), 32 + 30 * math.sin(r)),
                   fill=(255, 200, 60, 255), width=5)

        def mostrar(icon=None, item=None):
            self.after(0, self._mostrar_ventana)

        def salir(icon=None, item=None):
            self.after(0, self._salir)

        def accion_preset(v):
            def _accion(icon, item):
                self.after(0, lambda: self.aplicar_a_todos(v))
            return _accion

        items = [pystray.MenuItem("Abrir", mostrar, default=True)]
        for nombre, valor in self.cfg["presets"].items():
            items.append(pystray.MenuItem(f"{nombre} ({valor}%)", accion_preset(valor)))
        items.append(pystray.MenuItem("Salir", salir))
        self._tray = pystray.Icon(APP_NAME, img, APP_NAME, pystray.Menu(*items))
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _mostrar_ventana(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _ocultar_a_bandeja(self):
        if self._tray:
            self.withdraw()
        else:
            self._salir()

    def _salir(self):
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
        self.destroy()
        sys.exit(0)


if __name__ == "__main__":
    App().mainloop()
