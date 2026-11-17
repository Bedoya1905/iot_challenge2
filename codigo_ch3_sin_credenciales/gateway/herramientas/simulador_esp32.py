"""
Simulador del ESP32 para probar el Gateway sin hardware y generar datos de
escenarios controlados (con su etiqueta de verdad) para validar el módulo de IA.

Habla el CONTRATO MQTT del equipo, igual que el firmware:
  publica  neusa/estacion01/telemetria   (QoS 1)
           neusa/estacion01/estado       "online" / LWT "offline" (retenido)
           neusa/estacion01/comando/ack  acuse de cada comando (extensión)
  escucha  neusa/estacion01/comando      silenciar_alarma / reactivar_alarma / ping
           neusa/estacion01/ia           recomendación de IA (extensión)
y reproduce la lógica del dispositivo: mediana de nivel con ruido, tasa de
eventos en cm/h por mínimos cuadrados (ventana de 5 min), Priestley-Taylor
(tasa instantánea) y la fusión de 4 estados con histéresis por condición.

Modelo físico del tanque del prototipo:
    dN/dt [%/h] = -(evaporación + salida_medida + fuga_no_medida - entrada) / altura_útil

Uso:
  python herramientas/simulador_esp32.py --mqtt --host localhost
  python herramientas/simulador_esp32.py --csv datos_sinteticos.csv --semilla 7 --repeticiones 5

ADVERTENCIA: los datos de este simulador son SINTÉTICOS. Sirven para probar
el software; no reemplazan la validación con datos medidos del prototipo.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --- Parámetros copiados del firmware (sketch_challenge3.ino) ---
NIVEL_ALERTA, NIVEL_CRITICO = 30.0, 15.0
CAUDAL_ANOMALO, EVAP_ALTA = 15.0, 5.0
TASA_EVENTO_CMH, TEMP_CALOR = 2.0, 24.0          # descenso de 2 cm/h = evento
M_NIVEL, M_EVAP, M_CAUDAL, M_TASA, M_TEMP = 3.0, 0.5, 2.0, 0.5, 1.0
VENTANA_TASA, MIN_TASA = 150, 150                # 5 min a 2 s (prototipo)
ALFA_EMA_RAD = 0.1
ALFA_PT, ALBEDO = 1.26, 0.06
DIST_VACIO, DIST_LLENO = 12.0, 3.8
ALTURA = DIST_VACIO - DIST_LLENO
DT = 2.0
NORMAL, VIGILANCIA, ALERTA, CRITICO = 0, 1, 2, 3


@dataclass
class Segmento:
    nombre: str
    duracion_s: float
    salida_lmin: float = 0.0      # pasa por el caudalímetro (consumo medido)
    fuga_lmin: float = 0.0        # NO pasa por el caudalímetro
    entrada_lmin: float = 0.0     # llenado / lluvia
    temp: float = 14.0            # día nublado típico de la Sabana
    hum: float = 75.0
    rad: float = 150.0
    anomalia: int = 0             # etiqueta de verdad para la validación
    falla_dht: bool = False


ESCENARIO_VALIDACION = [
    Segmento("estable", 480),
    Segmento("consumo_medido", 120, salida_lmin=1.0),
    Segmento("reposo", 180),
    Segmento("fuga_no_medida", 300, fuga_lmin=0.2, anomalia=1),
    Segmento("reposo", 120),
    Segmento("llenado", 240, entrada_lmin=0.5),
    Segmento("calor_y_fuga_lenta", 360, fuga_lmin=0.08, temp=26.0, hum=40.0, rad=300.0, anomalia=1),
    Segmento("reposo", 120, temp=18.0, rad=250.0),
    Segmento("consumo_hasta_critico", 150, salida_lmin=1.0),
    Segmento("reposo", 120),
]

ESCENARIOS_DEMO = {
    "validacion": ESCENARIO_VALIDACION,
    "fuga": [Segmento("estable", 240), Segmento("fuga_no_medida", 600, fuga_lmin=0.1, anomalia=1)],
    "consumo": [Segmento("estable", 240), Segmento("consumo_medido", 300, salida_lmin=1.0)],
    "falla": [Segmento("estable", 120), Segmento("falla_dht", 120, falla_dht=True), Segmento("estable", 120)],
}


def evaporacion_pt(temp: float, rad: float, pres_kpa: float) -> float:
    es = 0.6108 * math.exp(17.27 * temp / (temp + 237.3))
    delta = 4098.0 * es / (temp + 237.3) ** 2
    gamma = 0.665e-3 * pres_kpa
    lam = 2.501 - 0.002361 * temp
    rn = (1.0 - ALBEDO) * rad * 0.0864
    return max(0.0, ALFA_PT * delta / (delta + gamma) * rn / lam)


def _sobre(prev, v, u, m):
    if v is None:
        return False
    return v > u - m if prev else v > u


def _bajo(prev, v, u, m):
    if v is None:
        return False
    return v < u + m if prev else v <= u


def evaluar_estado(nivel, evap, caudal, tasa_cmh, temp, f: dict) -> int:
    """Port de evaluarEstado() del firmware (4 estados, histéresis por condición).
    tasa_cmh: cambio de la altura en cm/h (negativo = baja)."""
    f["crit"] = _bajo(f.get("crit"), nivel, NIVEL_CRITICO, M_NIVEL)
    f["bajo"] = _bajo(f.get("bajo"), nivel, NIVEL_ALERTA, M_NIVEL)
    f["evap"] = _sobre(f.get("evap"), evap, EVAP_ALTA, M_EVAP)
    f["cons"] = _sobre(f.get("cons"), caudal, CAUDAL_ANOMALO, M_CAUDAL)
    f["desc"] = _sobre(f.get("desc"), None if tasa_cmh is None else -tasa_cmh, TASA_EVENTO_CMH, M_TASA)
    f["temp"] = _sobre(f.get("temp"), temp, TEMP_CALOR, M_TEMP)
    calor = f["evap"] or f["temp"]
    if f["crit"] or (f["bajo"] and (f["evap"] or f["cons"] or f["desc"])) or (f["desc"] and calor):
        return CRITICO
    if f["bajo"]:
        return ALERTA
    if f["evap"] or f["cons"] or f["desc"]:
        return VIGILANCIA
    return NORMAL


class ESP32Simulado:
    def __init__(self, segmentos, area_cm2=600.0, nivel0=85.0, ruido_pct=1.0,
                 p_atipico=0.004, semilla=None):
        self.seg = segmentos
        self.h = ALTURA
        self.area = area_cm2
        self.nivel = nivel0
        self.ruido = ruido_pct
        self.p_atipico = p_atipico
        self.rng = random.Random(semilla)
        self.seq = 0
        self.t_rel = 0.0
        self.buf_tasa: list[tuple[float, float]] = []
        self.fusion: dict = {}
        self.fusion_ideal: dict = {}
        self.estado_esperado = NORMAL
        self.silencio_hasta = 0.0       # t_rel en que vence el silencio
        self.silencio_estado = NORMAL
        self.estado = NORMAL
        self.rad_f = None

    def _pct_h(self, lmin: float) -> float:
        return lmin * 60_000.0 / self.area / self.h * 100.0

    def _tasa_cmh(self):
        """Pendiente por mínimos cuadrados de la altura (cm/h, negativa = baja)."""
        if len(self.buf_tasa) < MIN_TASA:
            return None
        ts = [t for t, _ in self.buf_tasa]
        hs = [h for _, h in self.buf_tasa]
        tm, hm = sum(ts) / len(ts), sum(hs) / len(hs)
        sxx = sum((t - tm) ** 2 for t in ts)
        sxy = sum((t - tm) * (h - hm) for t, h in zip(ts, hs))
        return sxy / sxx * 3600.0 if sxx > 0 else None

    def silenciar(self, duracion_s: float):
        self.silencio_hasta = self.t_rel + duracion_s
        self.silencio_estado = self.estado

    def silenciada(self) -> bool:
        if self.estado > self.silencio_estado or self.t_rel >= self.silencio_hasta:
            self.silencio_hasta = 0.0
            return False
        return True

    def pasos(self, t0_epoch: float):
        """Genera (payload_del_contrato, segmento) cada DT segundos de tiempo simulado."""
        for s in self.seg:
            for _ in range(int(s.duracion_s / DT)):
                self.t_rel += DT
                pres = 70.1 + self.rng.gauss(0, 0.03)
                temp = s.temp + self.rng.gauss(0, 0.2)
                hum = s.hum + self.rng.gauss(0, 1.0)
                rad_cruda = max(0.0, s.rad + self.rng.gauss(0, 15))
                self.rad_f = rad_cruda if self.rad_f is None else self.rad_f + ALFA_EMA_RAD * (rad_cruda - self.rad_f)
                rad = self.rad_f
                evap = evaporacion_pt(temp, rad, pres)

                # Dinámica del tanque
                d_pct_h = (evap / 24.0) / (self.h * 10.0) * 100.0 \
                    + self._pct_h(s.salida_lmin) + self._pct_h(s.fuga_lmin) - self._pct_h(s.entrada_lmin)
                salida_real = s.salida_lmin if self.nivel > 0 else 0.0
                self.nivel = min(100.0, max(0.0, self.nivel - d_pct_h * DT / 3600.0))

                medido = self.nivel + self.rng.gauss(0, self.ruido)
                if self.rng.random() < self.p_atipico:
                    medido += self.rng.choice([-1, 1]) * self.rng.uniform(15, 40)
                medido = min(100.0, max(0.0, medido))
                altura = medido / 100.0 * ALTURA
                dist = DIST_VACIO - altura
                caudal = max(0.0, salida_real + (self.rng.gauss(0, 0.03) if salida_real else 0.0))

                self.buf_tasa.append((self.t_rel, altura))
                if len(self.buf_tasa) > VENTANA_TASA:
                    self.buf_tasa.pop(0)
                tasa = self._tasa_cmh()

                falla_dht = s.falla_dht
                self.estado = evaluar_estado(medido, None if falla_dht else evap, caudal, tasa,
                                             None if falla_dht else temp, self.fusion)
                self.seq += 1
                t_ms = int((t0_epoch + self.t_rel) * 1000)
                payload = {
                    "ts": t_ms // 1000,
                    "nivel_cm": round(dist, 2),
                    "caudal_lmin": round(caudal, 2),
                    "radiacion_wm2": round(rad),
                    "temp_c": None if falla_dht else round(temp, 1),
                    "hum_pct": None if falla_dht else round(hum, 1),
                    "presion_hpa": round(pres * 10.0, 1),
                    "evap_tasa_mmdia": None if falla_dht else round(evap, 2),
                    "estado": self.estado,
                    "falla": ["dht"] if falla_dht else [],
                    # extensiones
                    "seq": self.seq, "ts_ms": t_ms, "nivel_pct": round(medido, 1),
                    "altura_cm": round(altura, 2),
                    "tasa_cmh": None if tasa is None else round(tasa, 3),
                    "tendencia_24h_cmh": None,      # requiere >= 6 h de datos
                    "silenciada": self.silenciada(),
                }

                # "Verdad" para medir falsos positivos/negativos: el estado que
                # daría la misma lógica con sensores ideales (sin ruido) y la
                # tasa de descenso real instantánea.
                self.estado_esperado = evaluar_estado(
                    self.nivel, evaporacion_pt(s.temp, s.rad, 70.1), salida_real,
                    -d_pct_h * ALTURA / 100.0, s.temp, self.fusion_ideal)
                yield payload, s


def generar_csv(ruta: str, segmentos, semilla: int, t0: float, repeticiones: int = 1, **kw) -> int:
    """Escribe el CSV de validación con los campos del contrato. Con
    repeticiones > 1 se repite el ensayo completo con otra semilla de ruido."""
    campos = ["ts", "ts_ms", "seq", "nivel_cm", "nivel_pct", "altura_cm", "tasa_cmh", "tendencia_24h_cmh",
              "caudal_lmin", "radiacion_wm2", "temp_c", "hum_pct", "presion_hpa", "evap_tasa_mmdia",
              "estado", "falla", "silenciada",
              "escenario", "repeticion", "etiqueta_anomalia", "estado_esperado", "falla_esperada", "nivel_real"]
    n = 0
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        t_ini = t0
        for rep in range(1, repeticiones + 1):
            sim = ESP32Simulado(segmentos, semilla=semilla + rep - 1, **kw)
            for p, s in sim.pasos(t_ini):
                fila = dict(p)
                fila["falla"] = json.dumps(p["falla"])
                fila["escenario"] = s.nombre
                fila["repeticion"] = rep
                fila["etiqueta_anomalia"] = s.anomalia
                fila["estado_esperado"] = sim.estado_esperado
                fila["falla_esperada"] = int(s.falla_dht)
                fila["nivel_real"] = round(sim.nivel, 3)
                w.writerow(fila)
                n += 1
            t_ini += sim.t_rel + 600   # 10 min entre repeticiones (se reinicia el ESP32)
    return n


def correr_mqtt(args, segmentos):
    import paho.mqtt.client as mqtt

    base = args.base
    sim = ESP32Simulado(segmentos, semilla=args.semilla, area_cm2=args.area)
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sim-esp32-estacion01")
    if args.usuario:
        cli.username_pw_set(args.usuario, args.clave)
    cli.will_set(f"{base}/estado", "offline", qos=1, retain=True)

    def on_connect(c, u, f, rc, p):
        c.publish(f"{base}/estado", "online", qos=1, retain=True)
        c.subscribe([(f"{base}/comando", 1), (f"{base}/ia", 1)])
        print("simulador conectado; publicando en", f"{base}/telemetria")

    def on_message(c, u, msg):
        texto = msg.payload.decode()
        if msg.topic.endswith("/comando"):
            try:
                cmd = json.loads(texto)
            except json.JSONDecodeError:
                cmd = {}
            accion, ahora = cmd.get("accion", ""), int(time.time())
            ok, nota, hasta = True, "ok", 0
            if accion == "silenciar_alarma":
                hasta = int(cmd.get("hasta") or ahora + 1800)
                if hasta <= ahora:
                    ok, nota = False, "hasta_vencido"
                else:
                    sim.silenciar(min(hasta - ahora, 86400) * args.acelerar)
            elif accion == "reactivar_alarma":
                sim.silencio_hasta = 0.0
            elif accion != "ping":
                ok, nota = False, "accion_desconocida"
            c.publish(f"{base}/comando/ack", json.dumps({
                "accion": accion, "ok": ok, "cmd_id": cmd.get("cmd_id", ""), "hasta": hasta,
                "ts": ahora, "nota": nota}), qos=1)
            print("comando:", accion, "->", nota)
        else:
            print("IA ->", texto[:160])

    cli.on_connect, cli.on_message = on_connect, on_message
    cli.connect(args.host, args.puerto, 30)
    cli.loop_start()
    periodo = DT / args.acelerar
    try:
        for p, s in sim.pasos(time.time()):
            if args.acelerar == 1:
                p["ts_ms"] = int(time.time() * 1000)
                p["ts"] = p["ts_ms"] // 1000
            else:
                p["ts"], p["ts_ms"] = 0, 0   # tiempo acelerado: el Gateway usa su hora de recepción
            cli.publish(f"{base}/telemetria", json.dumps(p), qos=1)
            print(f"[{s.nombre:22s}] seq={p['seq']:5d} altura={p['altura_cm']:5.2f} cm "
                  f"tasa={p['tasa_cmh']} cm/h estado={p['estado']}")
            time.sleep(periodo)
    except KeyboardInterrupt:
        pass
    finally:
        cli.publish(f"{base}/estado", "offline", qos=1, retain=True)
        time.sleep(0.5)
        cli.loop_stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--escenario", default="validacion", choices=list(ESCENARIOS_DEMO))
    ap.add_argument("--semilla", type=int, default=7)
    ap.add_argument("--area", type=float, default=600.0, help="área del tanque en cm²")
    ap.add_argument("--csv", help="genera un CSV en vez de publicar por MQTT")
    ap.add_argument("--repeticiones", type=int, default=1, help="repeticiones del ensayo en modo --csv")
    ap.add_argument("--mqtt", action="store_true")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--puerto", type=int, default=1883)
    ap.add_argument("--usuario", default="")
    ap.add_argument("--clave", default="")
    ap.add_argument("--base", default="neusa/estacion01")
    ap.add_argument("--acelerar", type=float, default=1.0,
                    help="factor de aceleración en modo MQTT (las tasas se distorsionan si >1)")
    args = ap.parse_args()
    seg = ESCENARIOS_DEMO[args.escenario]
    if args.csv:
        n = generar_csv(args.csv, seg, args.semilla, t0=1_762_700_000.0,
                        repeticiones=args.repeticiones, area_cm2=args.area)
        print(f"{n} lecturas sintéticas escritas en {args.csv}")
    elif args.mqtt:
        correr_mqtt(args, seg)
    else:
        ap.error("use --csv RUTA o --mqtt")


if __name__ == "__main__":
    main()
