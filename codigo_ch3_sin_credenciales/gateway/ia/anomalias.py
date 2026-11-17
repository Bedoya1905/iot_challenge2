"""
Módulo de IA - Componente 2: DETECCIÓN DE ANOMALÍAS por balance hídrico.

Idea: el sistema ya mide las dos causas "legítimas" por las que el nivel baja:
  - evaporación potencial (Priestley-Taylor, mm/día, calculada en el ESP32), y
  - salida medida por el caudalímetro (L/min).
Si el nivel baja MÁS RÁPIDO de lo que esas dos causas explican, hay una
pérdida no explicada: fuga, captación no autorizada o falla del sensor.

    residual = descenso_observado - (descenso_por_evaporación + descenso_por_caudal)   [%/h]

Conversión de cada término a %/h del nivel útil del tanque:
    evaporación:  E[mm/día] / 24 / (altura_útil[mm]) · 100
    caudal:       Q[L/min] · 60 · 1000 [cm³/h] / área[cm²] / altura_útil[cm] · 100

Además del umbral físico absoluto, el residual se compara con su propia
historia mediante un z-score robusto (mediana y MAD), y la anomalía solo se
CONFIRMA tras N ventanas consecutivas, para no alertar por un solo dato ruidoso.

Esto convierte la tasa de descenso en información accionable: no es lo mismo
"baja porque hace calor" (racionamiento) que "baja y nada lo explica"
(inspección en campo). Esa distinción es la que pide el enunciado al hablar
de "tasas anómalas de descenso de nivel".

Challenge #3 - IoT - Universidad de La Sabana 2026-2
Capa: procesamiento local + IA (Rafael Alejandro Rincón)
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass

from config import Config


@dataclass
class ResultadoAnomalia:
    valido: bool
    descenso_obs_pct_h: float | None = None
    explicado_evap_pct_h: float | None = None
    explicado_caudal_pct_h: float | None = None
    residual_pct_h: float | None = None
    z_robusto: float | None = None
    sospecha: bool = False          # supera el umbral en esta ventana
    anomalia: bool = False          # confirmada (N ventanas seguidas)
    tipo: str = "sin_datos"

    def a_dict(self) -> dict:
        r = lambda x: None if x is None else round(x, 2)  # noqa: E731
        return {
            "balance_valido": self.valido,
            "descenso_obs_pct_h": r(self.descenso_obs_pct_h),
            "explicado_evap_pct_h": r(self.explicado_evap_pct_h),
            "explicado_caudal_pct_h": r(self.explicado_caudal_pct_h),
            "residual_pct_h": r(self.residual_pct_h),
            "z_robusto": r(self.z_robusto),
            "anomalia": int(self.anomalia),
            "tipo_balance": self.tipo,
        }


def descenso_por_evaporacion_pct_h(evap_mm_dia: float, altura_util_cm: float) -> float:
    return (evap_mm_dia / 24.0) / (altura_util_cm * 10.0) * 100.0


def descenso_por_caudal_pct_h(caudal_lmin: float, area_cm2: float, altura_util_cm: float) -> float:
    cm_por_h = caudal_lmin * 60.0 * 1000.0 / area_cm2
    return cm_por_h / altura_util_cm * 100.0


def balance_integrado(t: list[float], nivel: list[float], evap: list[float | None],
                      caudal: list[float | None], ventana_s: float, cfg: Config,
                      fraccion_extremos: float = 0.25) -> tuple[float, float, float] | None:
    """Balance de volumen integrado sobre la ventana [t_fin - ventana_s, t_fin].

    En vez de comparar una pendiente con un caudal promedio (que no coinciden
    cuando el consumo empieza o termina a mitad de la ventana), se compara lo
    que BAJÓ el nivel entre el primer y el último cuarto de la ventana con lo
    que la evaporación y la salida medida explican EN ESE MISMO INTERVALO:

        bajada_obs = mediana(nivel en el 1er cuarto) - mediana(nivel en el último cuarto)
        bajada_exp = ∫ (descenso_evap + descenso_caudal) dt   entre los centros de ambos cuartos

    Las medianas de cada cuarto hacen el balance robusto al ruido y a ecos.
    Devuelve (descenso_obs, explicado_evap, explicado_caudal) en %/h, o None
    si la ventana no tiene datos suficientes.
    """
    if len(t) < 20:
        return None
    t_fin = t[-1]
    idx = [i for i, ti in enumerate(t) if ti >= t_fin - ventana_s]
    if len(idx) < 20 or t[idx[-1]] - t[idx[0]] < 0.6 * ventana_s:
        return None
    t0, t1 = t[idx[0]], t[idx[-1]]
    q = fraccion_extremos * (t1 - t0)
    cabeza = [i for i in idx if t[i] <= t0 + q]
    cola = [i for i in idx if t[i] >= t1 - q]
    if len(cabeza) < 5 or len(cola) < 5:
        return None
    tc0 = statistics.median(t[i] for i in cabeza)
    tc1 = statistics.median(t[i] for i in cola)
    dur_h = (tc1 - tc0) / 3600.0
    if dur_h <= 0:
        return None
    bajada_obs = statistics.median(nivel[i] for i in cabeza) - statistics.median(nivel[i] for i in cola)

    int_e = int_q = 0.0
    for a, b in zip(idx, idx[1:]):
        if t[b] <= tc0 or t[b] > tc1:
            continue
        dt_h = (t[b] - max(t[a], tc0)) / 3600.0
        int_e += descenso_por_evaporacion_pct_h(evap[b] or 0.0, cfg.altura_util_cm) * dt_h
        if cfg.caudal_es_salida:
            int_q += descenso_por_caudal_pct_h(caudal[b] or 0.0, cfg.area_tanque_cm2, cfg.altura_util_cm) * dt_h
    return bajada_obs / dur_h, int_e / dur_h, int_q / dur_h


class DetectorAnomalias:
    def __init__(self, cfg: Config, historia: int = 30):
        self.cfg = cfg
        self.residuales: deque[float] = deque(maxlen=historia)
        self.consecutivas = 0

    def evaluar(self, descenso_obs_pct_h: float | None, e: float | None,
                q: float | None) -> ResultadoAnomalia:
        """Recibe el descenso observado y los explicados por evaporación (e) y
        por la salida medida (q), todos en %/h (ver balance_integrado)."""
        if descenso_obs_pct_h is None or e is None or q is None:
            self.consecutivas = 0
            return ResultadoAnomalia(valido=False)

        cfg = self.cfg
        residual = descenso_obs_pct_h - (e + q)

        # z-score robusto contra la historia reciente (antes de agregar el actual)
        z = None
        if len(self.residuales) >= 5:
            med = statistics.median(self.residuales)
            mad = statistics.median(abs(r - med) for r in self.residuales)
            escala = 1.4826 * mad if mad > 0 else None
            if escala:
                z = (residual - med) / escala
        self.residuales.append(residual)

        umbral = cfg.umbral_residual_pct_h
        sospecha = residual > umbral
        self.consecutivas = self.consecutivas + 1 if sospecha else 0
        anomalia = self.consecutivas >= cfg.anomalia_confirmaciones

        if anomalia:
            tipo = "perdida_no_explicada"
        elif sospecha:
            tipo = "sospecha_perdida"
        elif residual < -umbral:
            tipo = "aporte_no_medido"           # entra agua: lluvia, llenado
        elif descenso_obs_pct_h > umbral / 2:
            # Se nombra la causa que explica la mayor parte del descenso
            if q >= max(e, residual):
                tipo = "descenso_por_consumo"
            elif e >= max(q, residual):
                tipo = "descenso_por_evaporacion"
            else:
                tipo = "descenso_moderado_no_explicado"
        else:
            tipo = "estable"

        return ResultadoAnomalia(True, descenso_obs_pct_h, e, q, residual, z, sospecha, anomalia, tipo)
