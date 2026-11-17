"""
Módulo de IA - Componente 3: RECOMENDACIÓN para las autoridades con un modelo
de IA generativa consumido por API (Gemini por defecto; Claude como alternativa).

Arquitectura "el cálculo es local, la interpretación es del LLM":
  - La predicción (prediccion.py) y el balance hídrico (anomalias.py) se
    calculan en el Gateway con métodos deterministas y verificables.
  - El LLM recibe esos resultados ya calculados como un JSON compacto y los
    traduce a: nivel de riesgo, un resumen en lenguaje claro para un
    funcionario no técnico y hasta 3 acciones priorizadas, tomadas de un
    catálogo de acciones de gestión del riesgo.

Salvaguardas (por qué se puede confiar en la salida):
  1. Respuesta JSON validada contra un esquema; si no es válida, se usa la
     recomendación por reglas (el sistema nunca se queda sin recomendación).
  2. Piso de riesgo: el LLM puede SUBIR el riesgo respecto al estado del
     sistema (p. ej. NORMAL con anomalía), pero nunca bajarlo. Si lo baja,
     se corrige y se registra el problema.
  3. Verificación de cifras: todo número que aparezca en el texto debe estar
     en el contexto enviado; los que no, se registran como "cifras no
     soportadas" (indicador de alucinación usado en la validación).
  4. Límite de frecuencia: solo se consulta al cambiar el estado o la
     anomalía, o cada IA_INTERVALO_MIN_S, para respetar la cuota gratuita.
  5. Si no hay internet o API key, se usa el respaldo por reglas: las alertas
     y la alarma física NUNCA dependen del LLM.

Challenge #3 - IoT - Universidad de La Sabana 2026-2
Capa: procesamiento local + IA (Rafael Alejandro Rincón)
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from config import Config

NIVELES_RIESGO = ["bajo", "moderado", "alto", "critico"]
CONFIANZAS = ["baja", "media", "alta"]
ESTADOS = ["NORMAL", "VIGILANCIA", "ALERTA", "CRITICO"]   # escala del contrato

# Piso de riesgo según el estado de la lógica de fusión del ESP32.
# Con fallas de sensor el piso sube al menos a "moderado" (sistema parcialmente ciego).
PISO_RIESGO = {"NORMAL": "bajo", "VIGILANCIA": "moderado", "ALERTA": "alto", "CRITICO": "critico"}

COLOMBIA = timezone(timedelta(hours=-5))

PROMPT_SISTEMA = """Eres un asistente de apoyo a la decisión para autoridades locales (alcaldía, \
Consejo Municipal de Gestión del Riesgo de Desastres - CMGRD, operador del acueducto y CAR) en \
Sabana Centro, Cundinamarca, durante el Fenómeno de El Niño 2026. Recibes un JSON con mediciones \
y cálculos que YA hizo un sistema IoT de monitoreo de una fuente hídrica. Tu tarea es interpretar \
y recomendar, no recalcular.

Reglas obligatorias:
1. Usa solo los datos del JSON. No inventes cifras. Si citas un número, cópialo tal como aparece.
2. Si un dato es null o hay fallas de sensores, dilo y baja la confianza.
3. El riesgo nunca puede ser menor que el piso indicado en "piso_riesgo".
4. Si balance_hidrico.anomalia_confirmada es true, el nivel baja más rápido de lo que explican la \
evaporación y el consumo medido: prioriza la inspección en campo (fugas, captaciones no \
autorizadas) antes que el racionamiento.
5. Si el descenso se explica por evaporación o consumo y el nivel se acerca a los umbrales, \
prioriza medidas de ahorro y racionamiento escalonado.
6. Máximo 3 acciones, ordenadas por prioridad, en modo imperativo, cada una de máximo 140 \
caracteres, adaptadas del catálogo.
7. Escribe en español claro para un funcionario no técnico. Respeta la nota de "escala".

Catálogo de acciones:
- MONITOREO: aumentar la frecuencia de revisión o verificar la medición en sitio.
- INSPECCION: inspeccionar fugas, válvulas abiertas o captaciones no autorizadas aguas arriba.
- RACIONAMIENTO: activar racionamiento por sectores u horarios, priorizando el consumo humano \
doméstico sobre riego e industria (orden de prioridad de usos del agua en Colombia).
- COMUNICACION: informar a la comunidad el estado y las medidas de ahorro.
- COORDINACION: notificar al CMGRD, a la CAR y al operador del acueducto.
- MANTENIMIENTO: revisar en sitio el sensor que reporta falla.
- FUENTES_ALTERNAS: alistar abastecimiento alterno (carrotanques, pozos autorizados).

Responde SOLO con un objeto JSON, sin texto adicional, con exactamente estas claves:
{"riesgo": "bajo|moderado|alto|critico",
 "resumen": "máximo 240 caracteres",
 "acciones": ["acción 1", "acción 2"],
 "horizonte_horas": número o null (en cuántas horas se espera el nivel crítico, copiado del JSON),
 "confianza": "baja|media|alta",
 "justificacion": "máximo 300 caracteres: qué datos del JSON sustentan el riesgo"}"""


# ---------------------------------------------------------------------------
#  Contexto que se envía al modelo
# ---------------------------------------------------------------------------
def construir_contexto(agregado: dict, cfg: Config, fallas: list[str] | None = None) -> dict:
    """Arma el JSON compacto que recibe el LLM a partir del mensaje procesado.

    'agregado' es el diccionario que publica el Gateway en <base>/procesado,
    ya enriquecido con la predicción y el balance hídrico.
    """
    def r(x, d=1):
        return None if x is None else round(float(x), d)

    def minutos(h):
        return None if h is None else round(h * 60.0, 1)

    estado = agregado.get("estado", "NORMAL")
    t_fin = agregado.get("t_fin", time.time())
    return {
        "sitio": cfg.sitio_nombre,
        "hora_local": datetime.fromtimestamp(t_fin, COLOMBIA).strftime("%Y-%m-%d %H:%M"),
        "escala": ("Prototipo de laboratorio: el tanque se vacía en minutos, por eso los tiempos "
                   "se dan también en minutos. En un embalse real serían días."),
        "estado_sistema": estado,
        "piso_riesgo": (PISO_RIESGO.get(estado, "moderado") if not fallas or estado != "NORMAL"
                        else "moderado"),
        "nivel_pct": r(agregado.get("nivel")),
        "nivel_min_ultimo_minuto_pct": r(agregado.get("nivel_min")),
        "umbral_alerta_pct": cfg.nivel_alerta_pct,
        "umbral_critico_pct": cfg.nivel_critico_pct,
        "prediccion": {
            "tendencia_valida": bool(agregado.get("tendencia_valida")),
            "tasa_descenso_pct_h": r(agregado.get("tasa_desc")),
            "rango_tasa_pct_h": [r(agregado.get("tasa_desc_p10")), r(agregado.get("tasa_desc_p90"))],
            "horas_a_alerta": r(agregado.get("horas_a_alerta"), 2),
            "minutos_a_alerta": minutos(agregado.get("horas_a_alerta")),
            "horas_a_critico": r(agregado.get("horas_a_critico"), 2),
            "minutos_a_critico": minutos(agregado.get("horas_a_critico")),
            "minutos_a_critico_pesimista": minutos(agregado.get("horas_a_critico_pesimista")),
            "nivel_previsto_pct": r(agregado.get("nivel_pred")),
            "horizonte_prevision_min": agregado.get("horizonte_pred_min"),
        },
        "clima": {
            "temperatura_c": r(agregado.get("temp")),
            "humedad_pct": r(agregado.get("hum")),
            "radiacion_w_m2": r(agregado.get("rad"), 0),
            "evaporacion_mm_dia": r(agregado.get("evap"), 2),
        },
        "caudal_salida_l_min": r(agregado.get("caudal"), 2),
        "balance_hidrico": {
            "descenso_observado_pct_h": r(agregado.get("descenso_obs_pct_h")),
            "explicado_por_evaporacion_pct_h": r(agregado.get("explicado_evap_pct_h"), 2),
            "explicado_por_consumo_pct_h": r(agregado.get("explicado_caudal_pct_h")),
            "no_explicado_pct_h": r(agregado.get("residual_pct_h")),
            "clasificacion": agregado.get("tipo_balance"),
            "anomalia_confirmada": bool(agregado.get("anomalia")),
        },
        "fallas_sensores": fallas or [],
        "alarma_fisica_silenciada": bool(agregado.get("silenciada")),
        "calidad_enlace_pct": agregado.get("calidad_enlace_pct"),
    }


# ---------------------------------------------------------------------------
#  Validación de la respuesta del LLM
# ---------------------------------------------------------------------------
_RE_NUM = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?")


def _numeros_del_contexto(obj: Any, acc: set[float] | None = None) -> set[float]:
    acc = set() if acc is None else acc
    if isinstance(obj, bool) or obj is None:
        return acc
    if isinstance(obj, (int, float)):
        acc.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numeros_del_contexto(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numeros_del_contexto(v, acc)
    elif isinstance(obj, str):
        for m in _RE_NUM.findall(obj):
            acc.add(float(m.replace(",", ".")))
    return acc


def cifras_no_soportadas(texto: str, contexto: dict) -> list[str]:
    """Números citados en el texto que no corresponden a ningún dato del contexto.

    Se aceptan redondeos (tolerancia 2 % o 0.15 absoluto), enteros pequeños
    (conteos como "3 sectores") y el año en curso.
    """
    permitidos = _numeros_del_contexto(contexto)
    malos = []
    for m in _RE_NUM.findall(texto):
        x = float(m.replace(",", "."))
        if x.is_integer() and (0 <= x <= 10 or x in (24, 48, 72) or 2000 <= x <= 2100):
            continue
        if any(abs(x - p) <= max(0.15, 0.02 * abs(p)) for p in permitidos):
            continue
        malos.append(m)
    return malos


def extraer_json(texto: str) -> dict:
    """Extrae el primer objeto JSON del texto (tolera bloques ```json ...```)."""
    texto = texto.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto, flags=re.S)
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        ini, fin = texto.find("{"), texto.rfind("}")
        if ini >= 0 and fin > ini:
            return json.loads(texto[ini:fin + 1])
        raise


def validar_respuesta(obj: Any, contexto: dict) -> tuple[dict | None, list[str]]:
    """Valida y normaliza la respuesta. Devuelve (respuesta | None, problemas).

    None significa que la respuesta es inutilizable y debe usarse el respaldo.
    Los problemas no fatales (riesgo corregido, cifras no soportadas) se
    registran pero la respuesta se conserva.
    """
    problemas: list[str] = []
    if not isinstance(obj, dict):
        return None, ["no_es_objeto"]

    riesgo = str(obj.get("riesgo", "")).strip().lower().replace("í", "i")
    if riesgo not in NIVELES_RIESGO:
        return None, [f"riesgo_invalido:{riesgo!r}"]

    resumen = obj.get("resumen")
    if not isinstance(resumen, str) or not resumen.strip():
        return None, ["resumen_faltante"]
    if len(resumen) > 240:
        problemas.append("resumen_largo")
        resumen = resumen[:237].rstrip() + "..."

    acciones = obj.get("acciones")
    if not isinstance(acciones, list) or not acciones or not all(isinstance(a, str) for a in acciones):
        return None, ["acciones_invalidas"]
    if len(acciones) > 3:
        problemas.append("demasiadas_acciones")
        acciones = acciones[:3]
    acciones = [a.strip()[:140] for a in acciones if a.strip()]

    confianza = str(obj.get("confianza", "media")).strip().lower()
    if confianza not in CONFIANZAS:
        problemas.append("confianza_invalida")
        confianza = "media"

    horizonte = obj.get("horizonte_horas")
    if horizonte is not None and not isinstance(horizonte, (int, float)):
        problemas.append("horizonte_invalido")
        horizonte = None

    justificacion = str(obj.get("justificacion", ""))[:300]

    piso = contexto.get("piso_riesgo", "bajo")
    if NIVELES_RIESGO.index(riesgo) < NIVELES_RIESGO.index(piso):
        problemas.append(f"riesgo_bajo_corregido:{riesgo}->{piso}")
        riesgo = piso

    malas = cifras_no_soportadas(" ".join([resumen, justificacion, *acciones]), contexto)
    if malas:
        problemas.append("cifras_no_soportadas:" + ",".join(malas))

    return {
        "riesgo": riesgo,
        "resumen": resumen.strip(),
        "acciones": acciones,
        "horizonte_horas": horizonte,
        "confianza": confianza,
        "justificacion": justificacion.strip(),
    }, problemas


# ---------------------------------------------------------------------------
#  Respaldo determinista (sin internet o respuesta inválida)
# ---------------------------------------------------------------------------
def recomendacion_por_reglas(ctx: dict, horizonte_urgente_h: float = 1.0) -> dict:
    estado = ctx.get("estado_sistema", "NORMAL")
    riesgo = ctx.get("piso_riesgo") or PISO_RIESGO.get(estado, "moderado")
    bal = ctx.get("balance_hidrico", {})
    pred = ctx.get("prediccion", {})
    h_crit = pred.get("horas_a_critico")
    min_crit = pred.get("minutos_a_critico")
    nivel = ctx.get("nivel_pct")
    acciones: list[str] = []

    def subir(a: str):
        nonlocal riesgo
        if NIVELES_RIESGO.index(a) > NIVELES_RIESGO.index(riesgo):
            riesgo = a

    if ctx.get("fallas_sensores"):
        acciones.append("Revisar en sitio los sensores con falla: " + ", ".join(ctx["fallas_sensores"]) + ".")
    if bal.get("anomalia_confirmada"):
        subir("alto")
        acciones.append("Inspeccionar en campo fugas, válvulas abiertas o captaciones no autorizadas.")
    if h_crit is not None and 0 < h_crit <= horizonte_urgente_h:
        subir("alto")
    if estado in ("CRITICO", "ALERTA"):
        acciones.append("Activar racionamiento por sectores priorizando el consumo humano doméstico.")
        acciones.append("Notificar al CMGRD, a la CAR y al operador del acueducto.")
    elif estado == "VIGILANCIA" or riesgo == "alto":
        acciones.append("Informar a la comunidad el estado de la fuente y las medidas de ahorro.")
        acciones.append("Aumentar la frecuencia de revisión del tablero y verificar el nivel en sitio.")
    if not acciones:
        acciones.append("Mantener el monitoreo de rutina; no se requieren medidas adicionales.")

    partes = [f"Estado {estado}"]
    if nivel is not None:
        partes.append(f"nivel {nivel} %")
    if min_crit is not None and min_crit > 0:
        partes.append(f"nivel crítico previsto en {min_crit} min si la tendencia se mantiene")
    if bal.get("anomalia_confirmada"):
        partes.append("descenso no explicado por evaporación ni consumo")
    resumen = ", ".join(partes) + "."

    return {
        "riesgo": riesgo,
        "resumen": resumen[:240],
        "acciones": acciones[:3],
        "horizonte_horas": h_crit,
        "confianza": "baja" if ctx.get("fallas_sensores") else "media",
        "justificacion": "Recomendación por reglas deterministas (respaldo sin modelo generativo).",
    }


# ---------------------------------------------------------------------------
#  Clientes de los proveedores (HTTP directo, sin SDK)
# ---------------------------------------------------------------------------
class ErrorProveedor(RuntimeError):
    pass


def llamar_gemini(prompt_usuario: str, cfg: Config, http=requests) -> str:
    """API de Gemini (Google AI Studio), endpoint generateContent."""
    if not cfg.gemini_api_key:
        raise ErrorProveedor("falta GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{cfg.gemini_modelo}:generateContent"
    cuerpo = {
        "systemInstruction": {"parts": [{"text": PROMPT_SISTEMA}]},
        "contents": [{"role": "user", "parts": [{"text": prompt_usuario}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
        },
    }
    r = http.post(url, json=cuerpo, timeout=cfg.ia_timeout_s,
                  headers={"x-goog-api-key": cfg.gemini_api_key, "Content-Type": "application/json"})
    if r.status_code != 200:
        raise ErrorProveedor(f"gemini HTTP {r.status_code}: {r.text[:200]}")
    datos = r.json()
    try:
        partes = datos["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError) as e:
        raise ErrorProveedor(f"gemini respuesta sin contenido: {str(datos)[:200]}") from e
    # Los modelos con razonamiento pueden devolver partes de "pensamiento"; se omiten.
    return "".join(p.get("text", "") for p in partes if not p.get("thought"))


def llamar_anthropic(prompt_usuario: str, cfg: Config, http=requests) -> str:
    """API de Claude (Anthropic), endpoint Messages."""
    if not cfg.anthropic_api_key:
        raise ErrorProveedor("falta ANTHROPIC_API_KEY")
    cuerpo = {
        "model": cfg.anthropic_modelo,
        "max_tokens": 1024,
        "temperature": 0.2,
        "system": PROMPT_SISTEMA,
        "messages": [{"role": "user", "content": prompt_usuario}],
    }
    r = http.post("https://api.anthropic.com/v1/messages", json=cuerpo, timeout=cfg.ia_timeout_s,
                  headers={"x-api-key": cfg.anthropic_api_key, "anthropic-version": "2023-06-01",
                           "content-type": "application/json"})
    if r.status_code != 200:
        raise ErrorProveedor(f"anthropic HTTP {r.status_code}: {r.text[:200]}")
    datos = r.json()
    return "".join(b.get("text", "") for b in datos.get("content", []) if b.get("type") == "text")


PROVEEDORES = {"gemini": llamar_gemini, "anthropic": llamar_anthropic}


# ---------------------------------------------------------------------------
#  Orquestador
# ---------------------------------------------------------------------------
class Recomendador:
    def __init__(self, cfg: Config, http=requests, reloj=time.time):
        self.cfg = cfg
        self.http = http
        self.reloj = reloj
        self._ultima_consulta: float | None = None
        self._ultima_firma: tuple | None = None

    @staticmethod
    def _firma(ctx: dict) -> tuple:
        """Lo que, si cambia, justifica una consulta inmediata."""
        bal = ctx.get("balance_hidrico", {})
        return (ctx.get("estado_sistema"), bool(bal.get("anomalia_confirmada")),
                tuple(ctx.get("fallas_sensores", [])))

    def debe_consultar(self, ctx: dict, ahora: float | None = None) -> bool:
        ahora = self.reloj() if ahora is None else ahora
        if self._ultima_consulta is None or self._firma(ctx) != self._ultima_firma:
            return True
        return ahora - self._ultima_consulta >= self.cfg.ia_intervalo_min_s

    def recomendar(self, ctx: dict, ahora: float | None = None) -> dict:
        ahora = self.reloj() if ahora is None else ahora
        self._ultima_consulta = ahora
        self._ultima_firma = self._firma(ctx)

        proveedor = self.cfg.ia_proveedor
        modelo = {"gemini": self.cfg.gemini_modelo, "anthropic": self.cfg.anthropic_modelo}.get(proveedor, "-")
        problemas: list[str] = []
        t0 = time.monotonic()
        respuesta = None

        if proveedor in PROVEEDORES:
            prompt = ("Datos actuales del sistema IoT (JSON):\n"
                      + json.dumps(ctx, ensure_ascii=False, indent=1))
            try:
                texto = PROVEEDORES[proveedor](prompt, self.cfg, self.http)
                respuesta, problemas = validar_respuesta(extraer_json(texto), ctx)
                if respuesta is None:
                    problemas = ["respuesta_invalida"] + problemas
            except (ErrorProveedor, requests.RequestException, ValueError) as e:
                problemas = [f"error_proveedor:{type(e).__name__}:{str(e)[:120]}"]
        latencia = time.monotonic() - t0

        fuente = proveedor
        if respuesta is None:
            respuesta = recomendacion_por_reglas(ctx, self.cfg.ia_horizonte_urgente_h)
            fuente = "reglas"
            modelo = "-"

        return {
            **respuesta,
            "riesgo_num": NIVELES_RIESGO.index(respuesta["riesgo"]),
            "fuente": fuente,
            "modelo": modelo,
            "latencia_s": round(latencia, 3),
            "problemas": problemas,
            "valida": fuente != "reglas" and not any(p.startswith("riesgo_bajo") for p in problemas),
            "estado_sistema": ctx.get("estado_sistema"),
            "t": round(ahora, 3),
        }


def payload_para_esp32(rec: dict) -> dict:
    """Versión compacta para el tablero local del ESP32 (buffer MQTT de 1.5 kB)."""
    return {
        "riesgo": rec["riesgo"],
        "resumen": rec["resumen"][:200],
        "fuente": rec["fuente"],
        "t": int(rec["t"] * 1000),
    }
