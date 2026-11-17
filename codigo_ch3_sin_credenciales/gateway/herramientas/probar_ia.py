"""Prueba rápida del proveedor de IA configurado con un contexto de ejemplo.

Uso:  python herramientas/probar_ia.py            (usa gateway/.env)
Muestra la respuesta validada, la latencia y los problemas detectados.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import cargar_config  # noqa: E402
from ia.recomendador import Recomendador, construir_contexto  # noqa: E402

cfg = cargar_config()
agregado = {
    "t_fin": time.time(), "estado": "VIGILANCIA", "nivel": 42.3, "nivel_min": 41.0,
    "tendencia_valida": True, "tasa_desc": 85.4, "tasa_desc_p10": 70.0, "tasa_desc_p90": 99.0,
    "horas_a_alerta": 0.14, "horas_a_critico": 0.32, "horas_a_critico_pesimista": 0.27,
    "nivel_pred": 39.5, "horizonte_pred_min": 2.0, "temp": 21.0, "hum": 55.0, "rad": 600,
    "evap": 4.1, "caudal": 0.0, "descenso_obs_pct_h": 85.4, "explicado_evap_pct_h": 0.2,
    "explicado_caudal_pct_h": 0.0, "residual_pct_h": 85.2, "tipo_balance": "perdida_no_explicada",
    "anomalia": 1, "silenciada": False, "calidad_enlace_pct": 100.0,
}
ctx = construir_contexto(agregado, cfg)
print("Contexto enviado:\n", json.dumps(ctx, ensure_ascii=False, indent=1))
r = Recomendador(cfg).recomendar(ctx)
print("\nRespuesta:\n", json.dumps(r, ensure_ascii=False, indent=1))
