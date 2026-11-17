"""
Procesamiento local en el Gateway, ANTES de transferir a la plataforma IoT.

Entrada: telemetría del ESP32 según el CONTRATO MQTT del equipo
(neusa/estacion01/telemetria; sección 2.2 de la wiki).

Qué hace con cada mensaje:
  1. Normaliza y valida contra el contrato: si un campo falta, no es numérico o
     cae fuera de su rango válido, queda en NULL y su nombre se agrega a la
     lista "falla". Nunca se reemplaza por cero ni por el último valor.
  2. Controla la secuencia (extensión "seq"): mensajes perdidos, duplicados y
     reinicios del ESP32.
  3. Filtra atípicos del nivel con un filtro de Hampel (mediana ± k·MAD).
  4. Detecta eventos (cambio de estado, falla de sensor, reinicio, conexión) y
     los DEPURA antes de notificar: una subida de estado se confirma con 2
     lecturas seguidas (4 s) y una bajada debe sostenerse 30 s.
  5. Agrega por ventanas internas de 60 s (análisis de IA y SQLite) y arma el
     mensaje para Ubidots cada 5 min con las variables del contrato
     (payload_ubidots), respetando el límite de la cuenta.

Las ventanas se cierran con el tiempo de los DATOS (no con el reloj de pared),
de modo que el mismo código sirve en vivo y para reprocesar datos guardados.

Challenge #3 - IoT - Universidad de La Sabana 2026-2
Capa: procesamiento local + IA (Rafael Alejandro Rincón)
"""
from __future__ import annotations

import math
import statistics
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from config import Config

# Escala de estado del contrato
ESTADOS = ["NORMAL", "VIGILANCIA", "ALERTA", "CRITICO"]

# Campos del contrato y su rango válido (en las unidades del contrato).
RANGOS_CONTRATO: dict[str, tuple[float, float]] = {
    "nivel_cm": (2.0, 400.0),
    "caudal_lmin": (0.0, 30.0),
    "radiacion_wm2": (0.0, 1200.0),
    "temp_c": (-5.0, 40.0),
    "hum_pct": (0.0, 100.0),
    "presion_hpa": (650.0, 780.0),
    "evap_tasa_mmdia": (0.0, 30.0),   # TASA instantánea expresada en mm/día (no el acumulado)
}
TS_MINIMO = 1_760_000_000          # regla del contrato para "ts"
FALLAS_SENSOR = {"nivel", "caudal", "ldr", "dht", "bmp"}

# Desfase máximo aceptado entre la hora del ESP32 (NTP) y la del Gateway.
MAX_DESFASE_HORA_S = 120.0


@dataclass
class Evento:
    tipo: str
    t: float
    detalle: dict[str, Any] = field(default_factory=dict)

    def a_dict(self, estacion: str) -> dict[str, Any]:
        return {"estacion": estacion, "tipo": self.tipo, "t": round(self.t, 3), **self.detalle}


@dataclass
class Muestra:
    """Lectura validada y filtrada, con su instante en segundos epoch.
    'datos' usa nombres internos: nivel (%), altura (cm de agua), dist (cm, distancia
    cruda del sensor), tasa_cmh (cm/h, - = baja), tend24h (cm/h), tasa (%/h, + = baja),
    caudal (L/min), temp (°C), hum (%), pres (kPa), rad (W/m²), evap (mm/día)."""
    t: float
    seq: int | None
    datos: dict[str, float | None]
    nivel_filtrado: float | None
    atipico: bool
    estado: int
    silenciada: bool
    fallas: frozenset[str]
    latencia_ms: float | None = None   # recepción en el Gateway − hora NTP del ESP32


def _num(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _stats(valores: list[float]) -> dict[str, float | None]:
    if not valores:
        return {"prom": None, "min": None, "max": None}
    return {"prom": round(statistics.fmean(valores), 3),
            "min": round(min(valores), 3), "max": round(max(valores), 3)}


def normalizar_contrato(payload: dict, cfg: Config) -> tuple[dict, list[str]]:
    """Convierte un mensaje del contrato a la representación interna.

    Devuelve (interno, campos_rechazados). 'campos_rechazados' son los campos
    del contrato que faltaban, no eran numéricos o estaban fuera de rango
    (quedan en None y se agregan a la lista de fallas, como manda el contrato).
    """
    rechazados: list[str] = []
    v: dict[str, float | None] = {}
    for campo, (lo, hi) in RANGOS_CONTRATO.items():
        x = _num(payload.get(campo))
        if x is None or not (lo <= x <= hi):
            rechazados.append(campo)
            x = None
        v[campo] = x

    altura = cfg.distancia_vacio_cm - cfg.distancia_lleno_cm
    nivel_pct = _num(payload.get("nivel_pct"))                      # extensión
    if v["nivel_cm"] is None:
        nivel_pct = None
    elif nivel_pct is None or not (0.0 <= nivel_pct <= 100.0):
        nivel_pct = min(100.0, max(0.0, (cfg.distancia_vacio_cm - v["nivel_cm"]) / altura * 100.0))

    altura_cm = _num(payload.get("altura_cm"))                      # extensión
    if v["nivel_cm"] is None:
        altura_cm = None
    elif altura_cm is None or not (0.0 <= altura_cm <= 400.0):
        altura_cm = min(altura, max(0.0, cfg.distancia_vacio_cm - v["nivel_cm"]))

    tasa_cmh = _num(payload.get("tasa_cmh"))                        # extensión, - = baja
    tasa_pct_h = None if tasa_cmh is None else -tasa_cmh / altura * 100.0
    tend24h = _num(payload.get("tendencia_24h_cmh"))                # extensión, - = baja

    ts_ms = _num(payload.get("ts_ms"))                              # extensión
    ts = _num(payload.get("ts"))
    if ts_ms and ts_ms / 1000.0 > TS_MINIMO:
        t_disp = ts_ms / 1000.0
    elif ts and ts > TS_MINIMO:
        t_disp = ts
    else:
        t_disp = None                                               # sin NTP: hora de recepción

    falla_esp = payload.get("falla") or []
    if not isinstance(falla_esp, list):
        falla_esp = [str(falla_esp)]

    interno = {
        "seq": int(payload["seq"]) if _num(payload.get("seq")) is not None else None,
        "t_disp": t_disp,
        "nivel": nivel_pct,
        "altura": altura_cm,
        "dist": v["nivel_cm"],
        "tasa": tasa_pct_h,
        "tasa_cmh": tasa_cmh,
        "tend24h": tend24h,
        "caudal": v["caudal_lmin"],
        "temp": v["temp_c"],
        "hum": v["hum_pct"],
        "pres": None if v["presion_hpa"] is None else v["presion_hpa"] / 10.0,   # hPa -> kPa
        "rad": v["radiacion_wm2"],
        "evap": v["evap_tasa_mmdia"],
        "estado": int(payload["estado"]),
        "falla": [str(f) for f in falla_esp],
        "silenciada": bool(payload.get("silenciada", False)),
    }
    return interno, rechazados


class FiltroHampel:
    """Filtro de Hampel causal sobre una ventana deslizante.

    Una muestra es atípica si |x - mediana| > k · 1.4826 · MAD y además supera
    una desviación mínima absoluta (para no marcar ruido cuando la ventana es
    casi constante y la MAD ≈ 0). La muestra atípica se reemplaza por la mediana.
    """

    def __init__(self, ventana: int, k: float, minimo: float):
        self.buf: deque[float] = deque(maxlen=ventana)
        self.k = k
        self.minimo = minimo
        self._rechazos = 0

    def filtrar(self, x: float) -> tuple[float, bool]:
        if len(self.buf) < 3:
            self.buf.append(x)
            return x, False
        med = statistics.median(self.buf)
        mad = statistics.median(abs(v - med) for v in self.buf)
        umbral = max(self.k * 1.4826 * mad, self.minimo)
        if abs(x - med) > umbral:
            # El atípico no entra a la ventana. Si el cambio es real, las
            # muestras siguientes lo confirman (se acepta tras media ventana).
            self._rechazos += 1
            if self._rechazos > len(self.buf) // 2:
                self.buf.clear()
                self.buf.append(x)
                self._rechazos = 0
                return x, False
            return med, True
        self._rechazos = 0
        self.buf.append(x)
        return x, False


class ProcesadorLocal:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self.hampel = FiltroHampel(cfg.hampel_ventana, cfg.hampel_k, cfg.hampel_min_pct)

        self.ultimo_seq: int | None = None
        self.estado_notificado: int | None = None
        self._candidato: int | None = None
        self._candidato_desde = 0.0
        self._candidato_n = 0
        self.ultimas_fallas: frozenset[str] = frozenset()
        self._fuera_previos: frozenset[str] = frozenset()
        self.ultimo_t: float | None = None

        self.ventana_inicio: float | None = None
        self.ventana: list[Muestra] = []
        self.perdidos_ventana = 0
        self.duplicados_ventana = 0

        max_hist = int(max(cfg.ventana_tendencia_s, cfg.ventana_anomalia_s, cfg.periodo_nube_s) / 2.0 * 1.5) + 50
        self.historial: deque[Muestra] = deque(maxlen=max_hist)

        self.total_recibidos = 0
        self.total_perdidos = 0
        self.total_invalidos = 0

    # ------------------------------------------------------------------ #
    def ingresar(self, payload: dict, t_recepcion: float) -> tuple[list[Evento], dict | None]:
        """Procesa un mensaje de telemetría del contrato.
        Devuelve (eventos, agregado); 'agregado' no es None cuando se cerró una ventana."""
        with self._lock:
            return self._ingresar(payload, t_recepcion)

    def _ingresar(self, payload: dict, t_recepcion: float) -> tuple[list[Evento], dict | None]:
        eventos: list[Evento] = []
        self.total_recibidos += 1

        if (not isinstance(payload, dict) or _num(payload.get("estado")) is None
                or not 0 <= int(payload["estado"]) <= 3):
            self.total_invalidos += 1
            eventos.append(Evento("mensaje_invalido", t_recepcion, {"motivo": "esquema"}))
            return eventos, None

        d, rechazados = normalizar_contrato(payload, self.cfg)
        t = t_recepcion
        if d["t_disp"] is not None and abs(d["t_disp"] - t_recepcion) <= MAX_DESFASE_HORA_S:
            t = d["t_disp"]
        # Latencia de subida: válida solo con ambos relojes sincronizados por NTP.
        latencia_ms = round((t_recepcion - t) * 1000.0, 1) if t != t_recepcion else None
        seq = d["seq"]

        # --- 2. control de secuencia (si el ESP32 envía la extensión "seq") ---
        if seq is not None and self.ultimo_seq is not None:
            if seq == self.ultimo_seq:
                self.duplicados_ventana += 1
                return eventos, None
            if seq < self.ultimo_seq:
                eventos.append(Evento("reinicio_dispositivo", t, {"seq": seq, "seq_previo": self.ultimo_seq}))
            elif seq > self.ultimo_seq + 1:
                perdidos = seq - self.ultimo_seq - 1
                self.perdidos_ventana += perdidos
                self.total_perdidos += perdidos
        if seq is not None:
            self.ultimo_seq = seq

        # --- 1. campos rechazados (los null de sensores caídos ya vienen en "falla") ---
        # Se notifica solo cuando aparece un campo nuevo fuera de rango (no en cada mensaje).
        fuera = frozenset(c for c in rechazados if payload.get(c) is not None)
        if fuera - self._fuera_previos:
            eventos.append(Evento("dato_fuera_de_rango", t, {"campos": sorted(fuera), "seq": seq}))
        self._fuera_previos = fuera
        fallas = frozenset(d["falla"]) | frozenset(rechazados)

        # --- 3. filtro de atípicos sobre el nivel ---
        nivel_f, atipico = (None, False)
        if d["nivel"] is not None:
            nivel_f, atipico = self.hampel.filtrar(d["nivel"])

        # --- 4. eventos depurados ---
        estado = d["estado"]
        ev = self._depurar_estado(estado, t)
        if ev is not None:
            previo, espera = ev
            eventos.append(Evento("cambio_estado", t, {
                "estado": estado, "nombre": ESTADOS[estado], "previo": previo,
                "nivel_pct": d["nivel"], "seq": seq, "confirmado_en_s": espera,
            }))
        sens_ahora = fallas & FALLAS_SENSOR
        sens_antes = self.ultimas_fallas & FALLAS_SENSOR
        if sens_ahora - sens_antes:
            eventos.append(Evento("falla_sensor", t, {"nuevas": sorted(sens_ahora - sens_antes)}))
        if sens_antes - sens_ahora:
            eventos.append(Evento("falla_resuelta", t, {"resueltas": sorted(sens_antes - sens_ahora)}))
        self.ultimas_fallas = fallas

        datos = {k: d[k] for k in ("nivel", "altura", "dist", "tasa", "tasa_cmh", "tend24h",
                                   "caudal", "temp", "hum", "pres", "rad", "evap")}
        muestra = Muestra(t, seq, datos, nivel_f, atipico, estado, d["silenciada"], fallas, latencia_ms)

        # --- 5. agregación por ventana (cierra con el tiempo de los datos) ---
        agregado = None
        if self.ventana_inicio is None:
            self.ventana_inicio = t
        elif t >= self.ventana_inicio + self.cfg.periodo_agregado_s:
            agregado = self._cerrar_ventana(t)
            self.ventana_inicio = t

        self.ventana.append(muestra)
        self.historial.append(muestra)
        self.ultimo_t = t
        return eventos, agregado

    def _depurar_estado(self, estado: int, t: float) -> tuple[int | None, float] | None:
        """Devuelve (estado_previo, segundos_de_confirmación) si hay que notificar.
        Subir de estado: 2 lecturas seguidas. Bajar: sostenido debounce_bajada_s."""
        if self.estado_notificado is None:
            self.estado_notificado = estado
            return None, 0.0
        if estado == self.estado_notificado:
            self._candidato = None
            return None
        if estado != self._candidato:
            self._candidato, self._candidato_desde, self._candidato_n = estado, t, 0
        self._candidato_n += 1
        subida = estado > self.estado_notificado
        confirmada = (self._candidato_n >= self.cfg.confirmar_subida_muestras if subida
                      else t - self._candidato_desde >= self.cfg.debounce_bajada_s)
        if not confirmada:
            return None
        previo = self.estado_notificado
        self.estado_notificado, self._candidato = estado, None
        return previo, round(t - self._candidato_desde, 1)

    def _cerrar_ventana(self, t_fin: float) -> dict | None:
        if not self.ventana:
            return None
        v = self.ventana
        ag: dict[str, Any] = {
            "estacion": self.cfg.estacion_id,
            "t_inicio": round(self.ventana_inicio, 3),
            "t_fin": round(t_fin, 3),
            "n": len(v),
            "perdidos": self.perdidos_ventana,
            "duplicados": self.duplicados_ventana,
            "atipicos_nivel": sum(1 for m in v if m.atipico),
        }
        lats = sorted(m.latencia_ms for m in v if m.latencia_ms is not None)
        if lats:
            ag["latencia_p50_ms"] = lats[len(lats) // 2]
            ag["latencia_p95_ms"] = lats[min(len(lats) - 1, int(0.95 * len(lats)))]
            ag["latencia_max_ms"] = lats[-1]
        esperados = len(v) + self.perdidos_ventana
        ag["calidad_enlace_pct"] = round(100.0 * len(v) / esperados, 1) if esperados else None

        niveles = [m.nivel_filtrado for m in v if m.nivel_filtrado is not None]
        s = _stats(niveles)
        ag["nivel"], ag["nivel_min"], ag["nivel_max"] = s["prom"], s["min"], s["max"]
        ag["nivel_ultimo"] = round(niveles[-1], 2) if niveles else None

        for campo in ("altura", "dist", "caudal", "temp", "hum", "pres", "rad", "evap"):
            s = _stats([m.datos[campo] for m in v if m.datos[campo] is not None])
            ag[campo], ag[f"{campo}_min"], ag[f"{campo}_max"] = s["prom"], s["min"], s["max"]

        tasas = [m.datos["tasa"] for m in v if m.datos["tasa"] is not None]
        ag["tasa_esp32"] = round(tasas[-1], 2) if tasas else None              # %/h, + = baja
        tcmh = [m.datos["tasa_cmh"] for m in v if m.datos["tasa_cmh"] is not None]
        ag["tasa_cmh_esp32"] = round(tcmh[-1], 3) if tcmh else None           # cm/h, - = baja
        tend = [m.datos["tend24h"] for m in v if m.datos["tend24h"] is not None]
        ag["tendencia_24h_cmh"] = round(tend[-1], 4) if tend else None

        ag["estado_max"] = max(m.estado for m in v)       # peor estado de la ventana
        ag["estado_num"] = v[-1].estado
        ag["estado"] = ESTADOS[ag["estado_num"]]
        ag["falla"] = sorted(set().union(*(m.fallas for m in v)))
        ag["silenciada"] = v[-1].silenciada

        self.ventana = []
        self.perdidos_ventana = 0
        self.duplicados_ventana = 0
        return ag

    def forzar_cierre(self, t: float) -> dict | None:
        """Cierra la ventana en curso (p. ej. si la estación deja de enviar)."""
        with self._lock:
            ag = self._cerrar_ventana(t)
            self.ventana_inicio = None
            return ag

    def series_historial(self, desde_t: float | None = None) -> dict[str, list]:
        """Series para el módulo de IA, solo con nivel válido."""
        with self._lock:
            ms = [m for m in self.historial if m.nivel_filtrado is not None
                  and (desde_t is None or m.t >= desde_t)]
            return {
                "t": [m.t for m in ms],
                "nivel": [m.nivel_filtrado for m in ms],
                "caudal": [m.datos["caudal"] for m in ms],
                "evap": [m.datos["evap"] for m in ms],
                "temp": [m.datos["temp"] for m in ms],
            }


# ---------------------------------------------------------------------------
#  Salto 2 del contrato: mensaje para Ubidots (cada 5 min, un solo mensaje)
# ---------------------------------------------------------------------------
def _media_ponderada(ags: list[dict], campo: str) -> float | None:
    pares = [(a[campo], a["n"]) for a in ags if a.get(campo) is not None]
    if not pares:
        return None
    return sum(v * n for v, n in pares) / sum(n for _, n in pares)


def payload_ubidots(ags: list[dict], tasa_cmh: float | None, recomendacion: dict | None,
                    cfg: Config) -> dict:
    """Arma el mensaje del contrato para /v1.6/devices/<dispositivo> a partir de
    las ventanas internas de los últimos 5 min.

    - nivel: ALTURA del agua en cm (sube cuando sube el agua), acordado por el equipo.
      La distancia cruda del sensor (nivel_cm del salto 1) queda en SQLite.
    - tasa_descenso: cm/h, negativa = baja; es la derivada de 'nivel'.
    - evaporacion: tasa instantánea de Priestley-Taylor en mm/día (no acumulado).
    - estado_alerta: peor estado de los 5 min.
    - presion NO se envía (acordado): Ubidots STEM permite 10 variables por
      dispositivo y la 10.ª es alarma_activa (el switch). La presión sigue en el
      salto 1 y en SQLite, y el ESP32 la usa en Priestley-Taylor.
    - riesgo_ia (extensión): 0 bajo … 3 crítico, con el resumen y la tendencia
      de 24 h en el 'context' (no ocupan variables).
    Total: 9 variables por envío = 9 dots.
    Las variables sin dato se omiten: Ubidots no admite null.
    """
    if not ags:
        return {}
    r = lambda x, d: None if x is None else round(x, d)  # noqa: E731
    nivel = {"altura_cm": _media_ponderada(ags, "altura"), "distancia_cm": _media_ponderada(ags, "dist"),
             "pct": _media_ponderada(ags, "nivel")}[cfg.nivel_nube]
    p = {
        "nivel": r(nivel, 2),
        "caudal": r(_media_ponderada(ags, "caudal"), 2),
        "radiacion": r(_media_ponderada(ags, "rad"), 0),
        "temperatura": r(_media_ponderada(ags, "temp"), 1),
        "humedad": r(_media_ponderada(ags, "hum"), 0),
        "evaporacion": r(_media_ponderada(ags, "evap"), 2),
        "tasa_descenso": r(tasa_cmh, 3),
        "estado_alerta": max(a["estado_max"] for a in ags),   # peor estado de los 5 min
    }
    salida: dict[str, Any] = {k: v for k, v in p.items() if v is not None}
    if recomendacion:
        contexto = {"resumen": recomendacion["resumen"][:200], "fuente": recomendacion["fuente"]}
        tend = next((a["tendencia_24h_cmh"] for a in reversed(ags) if a.get("tendencia_24h_cmh") is not None), None)
        if tend is not None:
            contexto["tendencia_24h_cmh"] = tend
        salida["riesgo_ia"] = {"value": recomendacion["riesgo_num"], "context": contexto}
    return salida
