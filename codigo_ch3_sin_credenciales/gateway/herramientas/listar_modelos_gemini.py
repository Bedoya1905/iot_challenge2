"""Lista los modelos de Gemini disponibles para la API key (para elegir GEMINI_MODELO).

Uso:  python herramientas/listar_modelos_gemini.py
"""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import cargar_config  # noqa: E402

cfg = cargar_config()
if not cfg.gemini_api_key:
    sys.exit("Defina GEMINI_API_KEY en gateway/.env")
r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                 headers={"x-goog-api-key": cfg.gemini_api_key}, timeout=20)
r.raise_for_status()
for m in r.json().get("models", []):
    if "generateContent" in m.get("supportedGenerationMethods", []):
        print(m["name"].removeprefix("models/"))
