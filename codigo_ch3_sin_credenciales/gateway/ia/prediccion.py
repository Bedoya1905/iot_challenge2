"""
Módulo de IA - Componente 1: PREDICCIÓN del nivel y del tiempo hasta los
umbrales de alerta y crítico.

Método: regresión robusta de Theil-Sen (pendiente = mediana de las pendientes
entre todos los pares de puntos) sobre la ventana reciente de nivel filtrado.
Se elige Theil-Sen en lugar de mínimos cuadrados porque tolera hasta ~29 % de
datos atípicos sin desviarse (punto de ruptura), y el HC-SR04 produce ecos
espurios. Los percentiles 10 y 90 de las pendientes por pares dan un rango de
incertidumbre de la tasa, que se propaga al tiempo estimado hasta el umbral.

Este cálculo NO se delega al modelo de lenguaje: los LLM no son fiables para
aritmética, y una predicción numérica debe ser reproducible y verificable.

Challenge #3 - IoT - Universidad de La Sabana 2026-2
Capa: procesamiento local + IA (Rafael Alejandro Rincón)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MAX_PUNTOS = 400   # Theil-Sen es O(n²): se submuestrea por encima de esto


@dataclass
class Tendencia:
    n: int
    duracion_s: float
    tasa_desc_pct_h: float          # positivo = el nivel baja
    tasa_desc_p10: float            # banda de incertidumbre (percentiles)
    tasa_desc_p90: float
    nivel_ajustado: float           # nivel estimado por la recta en el último instante
    t_ultimo: float
    r2: float

    def a_dict(self) -> dict:
        return {
            "n": self.n,
            "duracion_s": round(self.duracion_s, 1),
            "tasa_desc_pct_h": round(self.tasa_desc_pct_h, 2),
            "tasa_desc_p10": round(self.tasa_desc_p10, 2),
            "tasa_desc_p90": round(self.tasa_desc_p90, 2),
            "nivel_ajustado": round(self.nivel_ajustado, 2),
            "r2": round(self.r2, 3),
        }


def theil_sen(t: np.ndarray, y: np.ndarray) -> tuple[float, float, np.ndarray]:
    """Devuelve (pendiente, intercepto, pendientes_por_pares)."""
    i, j = np.triu_indices(len(t), k=1)
    dt = t[j] - t[i]
    ok = dt > 0
    pendientes = (y[j] - y[i])[ok] / dt[ok]
    if pendientes.size == 0:
        return 0.0, float(np.median(y)), pendientes
    b = float(np.median(pendientes))
    a = float(np.median(y - b * t))
    return b, a, pendientes


def ajustar_tendencia(t: list[float], nivel: list[float], ventana_s: float,
                      min_puntos: int = 20, min_duracion_s: float = 60.0) -> Tendencia | None:
    """Ajusta la tendencia del nivel sobre los últimos 'ventana_s' segundos."""
    if len(t) < min_puntos:
        return None
    ta = np.asarray(t, dtype=float)
    ya = np.asarray(nivel, dtype=float)
    sel = ta >= ta[-1] - ventana_s
    ta, ya = ta[sel], ya[sel]
    if len(ta) < min_puntos or ta[-1] - ta[0] < min_duracion_s:
        return None
    if len(ta) > MAX_PUNTOS:
        idx = np.linspace(0, len(ta) - 1, MAX_PUNTOS).round().astype(int)
        ta, ya = ta[idx], ya[idx]

    t0 = ta[-1]
    tr = ta - t0                       # tiempo relativo (s), mejor condicionado
    b, a, pend = theil_sen(tr, ya)
    pred = a + b * tr
    ss_res = float(np.sum((ya - pred) ** 2))
    ss_tot = float(np.sum((ya - ya.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    p10, p90 = (np.percentile(pend, [10, 90]) if pend.size else (b, b))
    # Pendiente en %/s -> descenso en %/h (signo invertido: positivo = baja)
    return Tendencia(
        n=len(ta),
        duracion_s=float(ta[-1] - ta[0]),
        tasa_desc_pct_h=-b * 3600.0,
        tasa_desc_p10=-float(p90) * 3600.0,   # la pendiente más alta = el descenso más bajo
        tasa_desc_p90=-float(p10) * 3600.0,
        nivel_ajustado=a,
        t_ultimo=float(t0),
        r2=r2,
    )


def horas_hasta(tend: Tendencia, umbral_pct: float, tasa_pct_h: float | None = None) -> float | None:
    """Horas hasta que el nivel cruce 'umbral_pct' si la tendencia se mantiene.

    0.0 si ya está por debajo; None si el nivel no está bajando (no hay cruce
    previsible) o si la tasa es despreciable (< 0.5 %/h).
    """
    tasa = tend.tasa_desc_pct_h if tasa_pct_h is None else tasa_pct_h
    if tend.nivel_ajustado <= umbral_pct:
        return 0.0
    if tasa < 0.5:
        return None
    return (tend.nivel_ajustado - umbral_pct) / tasa


def predecir_nivel(tend: Tendencia, horizonte_s: float) -> float:
    """Nivel previsto dentro de 'horizonte_s' segundos (acotado a 0-100 %)."""
    v = tend.nivel_ajustado - tend.tasa_desc_pct_h * horizonte_s / 3600.0
    return float(min(100.0, max(0.0, v)))


def resumen_prediccion(tend: Tendencia | None, nivel_alerta: float, nivel_critico: float,
                       horizonte_s: float) -> dict:
    """Bloque de predicción que se agrega al mensaje procesado y al contexto de la IA."""
    if tend is None:
        return {"tendencia_valida": False}
    h_alerta = horas_hasta(tend, nivel_alerta)
    h_crit = horas_hasta(tend, nivel_critico)
    # Rango del tiempo a crítico usando la banda de la tasa (p90 = escenario pesimista)
    h_crit_pes = horas_hasta(tend, nivel_critico, tend.tasa_desc_p90)
    h_crit_opt = horas_hasta(tend, nivel_critico, tend.tasa_desc_p10)
    r = lambda x, d=2: None if x is None else round(x, d)  # noqa: E731
    return {
        "tendencia_valida": True,
        **tend.a_dict(),
        "tasa_desc": round(tend.tasa_desc_pct_h, 2),
        "horas_a_alerta": r(h_alerta),
        "horas_a_critico": r(h_crit),
        "horas_a_critico_pesimista": r(h_crit_pes),
        "horas_a_critico_optimista": r(h_crit_opt),
        "nivel_pred": round(predecir_nivel(tend, horizonte_s), 2),
        "horizonte_pred_min": round(horizonte_s / 60.0, 1),
    }
