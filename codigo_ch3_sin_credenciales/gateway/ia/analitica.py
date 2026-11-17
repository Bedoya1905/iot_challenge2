"""
Une la predicción y el balance hídrico y los agrega a cada mensaje procesado.
Lo usan tanto el servicio en vivo (servicio_gateway.py) como la validación
fuera de línea (validacion/validar_ia.py), para que ambos ejecuten el mismo código.
"""
from __future__ import annotations

from config import Config
from ia.anomalias import DetectorAnomalias, balance_integrado
from ia.prediccion import ajustar_tendencia, resumen_prediccion
from procesamiento import ProcesadorLocal


class Analitica:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.detector = DetectorAnomalias(cfg)

    def enriquecer(self, agregado: dict, proc: ProcesadorLocal) -> dict:
        cfg = self.cfg
        s = proc.series_historial()

        # 1. Predicción (ventana larga)
        tend = ajustar_tendencia(s["t"], s["nivel"], cfg.ventana_tendencia_s)
        agregado.update(resumen_prediccion(tend, cfg.nivel_alerta_pct, cfg.nivel_critico_pct,
                                           cfg.horizonte_prediccion_s))

        # 2. Balance hídrico integrado (ventana corta)
        bal = balance_integrado(s["t"], s["nivel"], s["evap"], s["caudal"], cfg.ventana_anomalia_s, cfg)
        res = self.detector.evaluar(*(bal if bal else (None, None, None)))
        agregado.update(res.a_dict())
        return agregado
