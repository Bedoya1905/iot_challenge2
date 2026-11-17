"""
Configuración del Gateway (Raspberry Pi) para el procesamiento local y el
módulo de IA. Todo se lee de variables de entorno o de un archivo .env en la
carpeta del gateway, para no subir credenciales al repositorio.

Challenge #3 - IoT - Universidad de La Sabana 2026-2
Capa: procesamiento local + IA (Rafael Alejandro Rincón)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _cargar_env(ruta: Path) -> None:
    """Carga un archivo .env sencillo (CLAVE=valor). No pisa variables ya definidas."""
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        valor = valor.split(" #", 1)[0]          # admite comentarios al final de la línea
        os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


def _f(nombre: str, defecto: float) -> float:
    return float(os.environ.get(nombre, defecto))


def _i(nombre: str, defecto: int) -> int:
    return int(os.environ.get(nombre, defecto))


def _b(nombre: str, defecto: bool) -> bool:
    return os.environ.get(nombre, str(defecto)).strip().lower() in ("1", "true", "si", "sí", "yes")


@dataclass
class Config:
    # --- MQTT (broker Mosquitto local en la Raspberry Pi) ---
    mqtt_host: str = "localhost"
    mqtt_puerto: int = 1883
    mqtt_usuario: str = ""
    mqtt_clave: str = ""
    mqtt_base: str = "neusa/estacion01"   # contrato del equipo
    estacion_id: str = "estacion01"

    # --- Almacenamiento ---
    db_path: str = "gateway.db"
    guardar_crudo: bool = False   # True: guarda también las lecturas crudas (así opera en la Raspberry Pi)

    # --- Procesamiento local ---
    periodo_agregado_s: float = 60.0      # ventana interna (IA, SQLite)
    periodo_nube_s: float = 300.0         # contrato: 1 mensaje a Ubidots cada 5 min
    nivel_nube: str = "altura_cm"         # "altura_cm" (acordado), "distancia_cm" o "pct"
    nivel_alerta_pct: float = 30.0        # deben coincidir con el firmware
    nivel_critico_pct: float = 15.0
    hampel_ventana: int = 7               # filtro de atípicos sobre el nivel
    hampel_k: float = 3.0
    hampel_min_pct: float = 4.0           # desviación mínima para declarar atípico
    timeout_estacion_s: float = 30.0      # sin datos por más de esto -> evento
    confirmar_subida_muestras: int = 2    # lecturas seguidas para notificar una subida de alerta
    debounce_bajada_s: float = 30.0       # tiempo sostenido para notificar una bajada

    # --- Geometría del tanque del prototipo (cambiar si cambia el tanque) ---
    distancia_vacio_cm: float = 12.0      # deben coincidir con el firmware
    distancia_lleno_cm: float = 3.8
    area_tanque_cm2: float = 600.0        # área de la sección transversal del tanque
    caudal_es_salida: bool = True         # el caudalímetro mide la salida (consumo)

    # --- IA: analítica local ---
    ventana_tendencia_s: float = 60.0     # ventana de la regresión (elegida con validar_ia.py)
    ventana_anomalia_s: float = 180.0     # ventana del balance hídrico
    umbral_residual_pct_h: float = 40.0   # descenso no explicado que se considera anómalo
    anomalia_confirmaciones: int = 2      # ventanas consecutivas para confirmar
    horizonte_prediccion_s: float = 120.0 # horizonte del nivel previsto que se reporta

    # --- IA: recomendación con modelo generativo ---
    ia_proveedor: str = "gemini"          # gemini | anthropic | reglas
    gemini_api_key: str = ""
    gemini_modelo: str = "gemini-3.5-flash-lite"
    anthropic_api_key: str = ""
    anthropic_modelo: str = "claude-haiku-5-5"
    ia_intervalo_min_s: float = 600.0     # entre consultas si nada cambia (cuida la cuota)
    ia_timeout_s: float = 25.0
    ia_horizonte_urgente_h: float = 0.25  # cruce a crítico antes de esto -> riesgo alto (reglas)
    sitio_nombre: str = "Embalse del Neusa (Cogua/Tausa) - prototipo a escala"

    extra: dict = field(default_factory=dict)

    @property
    def t_telemetria(self) -> str:
        return f"{self.mqtt_base}/telemetria"

    @property
    def altura_util_cm(self) -> float:
        return self.distancia_vacio_cm - self.distancia_lleno_cm

    @property
    def t_estado(self) -> str:          # contrato: LWT "online"/"offline", retenido
        return f"{self.mqtt_base}/estado"

    @property
    def t_comando(self) -> str:         # contrato: Gateway -> ESP32
        return f"{self.mqtt_base}/comando"

    @property
    def t_ack(self) -> str:             # extensión: acuse del ESP32
        return f"{self.mqtt_base}/comando/ack"

    @property
    def t_procesado(self) -> str:
        return f"{self.mqtt_base}/procesado"

    @property
    def t_eventos(self) -> str:
        return f"{self.mqtt_base}/eventos"

    @property
    def t_ia(self) -> str:              # extensión: recomendación compacta para el ESP32
        return f"{self.mqtt_base}/ia"

    @property
    def t_ia_detalle(self) -> str:      # interno del Gateway
        return f"{self.mqtt_base}/ia/detalle"

    @property
    def t_historico(self) -> str:       # extensión: agregados del periodo sin conexión
        return f"{self.mqtt_base}/historico"

    @property
    def t_procesado_ventana(self) -> str:   # interno: ventana de 60 s completa
        return f"{self.mqtt_base}/procesado/ventana"


def cargar_config(ruta_env: str | Path | None = None) -> Config:
    ruta = Path(ruta_env) if ruta_env else Path(__file__).with_name(".env")
    _cargar_env(ruta)
    return Config(
        mqtt_host=os.environ.get("MQTT_HOST", "localhost"),
        mqtt_puerto=_i("MQTT_PUERTO", 1883),
        mqtt_usuario=os.environ.get("MQTT_USUARIO", ""),
        mqtt_clave=os.environ.get("MQTT_CLAVE", ""),
        mqtt_base=os.environ.get("MQTT_BASE", "neusa/estacion01"),
        estacion_id=os.environ.get("ESTACION_ID", "estacion01"),
        db_path=os.environ.get("DB_PATH", "gateway.db"),
        guardar_crudo=_b("GUARDAR_CRUDO", False),
        periodo_agregado_s=_f("PERIODO_AGREGADO_S", 60.0),
        periodo_nube_s=_f("PERIODO_NUBE_S", 300.0),
        nivel_nube=os.environ.get("NIVEL_NUBE", "altura_cm"),
        nivel_alerta_pct=_f("NIVEL_ALERTA_PCT", 30.0),
        nivel_critico_pct=_f("NIVEL_CRITICO_PCT", 15.0),
        hampel_ventana=_i("HAMPEL_VENTANA", 7),
        hampel_k=_f("HAMPEL_K", 3.0),
        hampel_min_pct=_f("HAMPEL_MIN_PCT", 4.0),
        timeout_estacion_s=_f("TIMEOUT_ESTACION_S", 30.0),
        confirmar_subida_muestras=_i("CONFIRMAR_SUBIDA_MUESTRAS", 2),
        debounce_bajada_s=_f("DEBOUNCE_BAJADA_S", 30.0),
        distancia_vacio_cm=_f("DISTANCIA_VACIO_CM", 12.0),
        distancia_lleno_cm=_f("DISTANCIA_LLENO_CM", 3.8),
        area_tanque_cm2=_f("AREA_TANQUE_CM2", 600.0),
        caudal_es_salida=_b("CAUDAL_ES_SALIDA", True),
        ventana_tendencia_s=_f("VENTANA_TENDENCIA_S", 60.0),
        ventana_anomalia_s=_f("VENTANA_ANOMALIA_S", 180.0),
        umbral_residual_pct_h=_f("UMBRAL_RESIDUAL_PCT_H", 40.0),
        anomalia_confirmaciones=_i("ANOMALIA_CONFIRMACIONES", 2),
        horizonte_prediccion_s=_f("HORIZONTE_PREDICCION_S", 120.0),
        ia_proveedor=os.environ.get("IA_PROVEEDOR", "gemini").lower(),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        gemini_modelo=os.environ.get("GEMINI_MODELO", "gemini-3.5-flash-lite"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        anthropic_modelo=os.environ.get("ANTHROPIC_MODELO", "claude-haiku-5-5"),
        ia_intervalo_min_s=_f("IA_INTERVALO_MIN_S", 600.0),
        ia_timeout_s=_f("IA_TIMEOUT_S", 25.0),
        ia_horizonte_urgente_h=_f("IA_HORIZONTE_URGENTE_H", 0.25),
        sitio_nombre=os.environ.get("SITIO_NOMBRE", "Embalse del Neusa (Cogua/Tausa) - prototipo a escala"),
    )
