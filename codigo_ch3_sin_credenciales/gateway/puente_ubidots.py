"""Puente Gateway -> Ubidots por MQTT (Challenge #3).

Va en gateway/ (al lado de config.py) y usa el mismo gateway/.env.

Qué hace:
  1. Subida: cada mensaje de neusa/estacion01/procesado se reenvía TAL CUAL
     a /v1.6/devices/<UBIDOTS_DEVICE> en el broker MQTT de Ubidots.
  2. Bajada (switch alarma_activa en el tablero de Ubidots):
       0.0 -> publica {"accion":"silenciar_alarma","hasta": ahora+3600} en neusa/estacion01/comando
       1.0 -> publica {"accion":"reactivar_alarma"}
  3. Sincronización: cuando la telemetría pasa de silenciada=true a false
     (p. ej. venció la hora), escribe alarma_activa=1.0 en Ubidots. Solo en la
     transición y no justo después de un comando (espera un ciclo).

Variables nuevas en .env:
  UBIDOTS_TOKEN=BBUS-...            (obligatorio)
  UBIDOTS_DEVICE=estacion-neusa     (opcional)
  UBIDOTS_HOST=industrial.api.ubidots.com   (opcional)
  UBIDOTS_PUERTO=1883               (opcional)

Uso:  ../venv/bin/python puente_ubidots.py
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time

import paho.mqtt.client as mqtt

from config import cargar_config

log = logging.getLogger("puente")

SILENCIO_S = 3600          # contrato: silenciar por 1 hora
ESPERA_TRAS_COMANDO_S = 5  # "esperar un ciclo" (telemetría cada 2 s)
VENTANA_ECO_S = 15         # ignorar el eco de lo que el propio puente escribe


class Puente:
    def __init__(self):
        self.cfg = cfg = cargar_config()
        self.token = os.environ.get("UBIDOTS_TOKEN", "").strip()
        if not self.token:
            raise SystemExit("Falta UBIDOTS_TOKEN en gateway/.env")
        self.device = os.environ.get("UBIDOTS_DEVICE", "estacion-neusa").strip()
        self.ubi_host = os.environ.get("UBIDOTS_HOST", "industrial.api.ubidots.com").strip()
        self.ubi_puerto = int(os.environ.get("UBIDOTS_PUERTO", "1883"))
        self.t_ubi_pub = f"/v1.6/devices/{self.device}"
        self.t_ubi_switch = f"/v1.6/devices/{self.device}/alarma_activa/lv"

        self._silenciada_previa: bool | None = None
        self._t_ultimo_comando = 0.0
        self._eco_esperado: tuple[float, float] | None = None   # (valor, hasta_t)
        self._lock = threading.Lock()

        # Cliente local (Mosquitto de la Raspberry)
        self.local = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id=f"gw-puente-ubidots-{cfg.estacion_id}")
        if cfg.mqtt_usuario:
            self.local.username_pw_set(cfg.mqtt_usuario, cfg.mqtt_clave)
        self.local.on_connect = self._local_conectado
        self.local.on_message = self._local_mensaje
        self.local.reconnect_delay_set(min_delay=1, max_delay=30)

        # Cliente Ubidots (usuario = token, clave cualquiera)
        self.ubi = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                               client_id=f"gw-{cfg.estacion_id}-{int(time.time())}")
        self.ubi.username_pw_set(self.token, "x")
        self.ubi.on_connect = self._ubi_conectado
        self.ubi.on_message = self._ubi_mensaje
        self.ubi.reconnect_delay_set(min_delay=2, max_delay=60)

    # ------------------------------------------------------------ local
    def _local_conectado(self, c, userdata, flags, rc, props):
        if rc.is_failure:
            log.error("MQTT local: conexión rechazada (%s) - revisar MQTT_USUARIO/MQTT_CLAVE", rc)
            return
        log.info("MQTT local: conectado a %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_puerto)
        c.subscribe([(self.cfg.t_procesado, 1), (self.cfg.t_telemetria, 0)])

    def _local_mensaje(self, c, userdata, msg):
        texto = msg.payload.decode("utf-8", errors="replace")
        if msg.topic == self.cfg.t_procesado:
            self.ubi.publish(self.t_ubi_pub, texto, qos=1)
            log.info("-> Ubidots %s: %s", self.t_ubi_pub, texto[:200])
        elif msg.topic == self.cfg.t_telemetria:
            try:
                silenciada = bool(json.loads(texto).get("silenciada", False))
            except (json.JSONDecodeError, AttributeError):
                return
            with self._lock:
                previa, self._silenciada_previa = self._silenciada_previa, silenciada
                reciente = time.time() - self._t_ultimo_comando < ESPERA_TRAS_COMANDO_S
            if previa is True and silenciada is False and not reciente:
                log.info("La alarma dejó de estar silenciada en el ESP32 -> alarma_activa=1.0")
                self._escribir_switch(1.0)

    # ------------------------------------------------------------ Ubidots
    def _ubi_conectado(self, c, userdata, flags, rc, props):
        if rc.is_failure:
            log.error("Ubidots: conexión rechazada (%s) - revisar UBIDOTS_TOKEN / UBIDOTS_HOST", rc)
            return
        log.info("Ubidots: conectado a %s:%s (dispositivo %s)", self.ubi_host, self.ubi_puerto, self.device)
        c.subscribe(self.t_ubi_switch, qos=1)

    def _ubi_mensaje(self, c, userdata, msg):
        try:
            valor = float(msg.payload.decode().strip())
        except ValueError:
            log.warning("Valor raro en alarma_activa: %r", msg.payload)
            return
        with self._lock:
            eco = self._eco_esperado
            if eco and eco[0] == valor and time.time() < eco[1]:
                self._eco_esperado = None
                log.info("alarma_activa=%s (eco del propio puente, se ignora)", valor)
                return
        if valor == 0.0:
            cmd = {"accion": "silenciar_alarma", "hasta": int(time.time()) + SILENCIO_S}
        else:
            cmd = {"accion": "reactivar_alarma"}
        with self._lock:
            self._t_ultimo_comando = time.time()
        self.local.publish(self.cfg.t_comando, json.dumps(cmd), qos=1)
        log.info("alarma_activa=%s -> comando %s", valor, cmd)

    def _escribir_switch(self, valor: float):
        with self._lock:
            self._eco_esperado = (valor, time.time() + VENTANA_ECO_S)
        self.ubi.publish(self.t_ubi_pub, json.dumps({"alarma_activa": valor}), qos=1)

    # ------------------------------------------------------------ ciclo
    def correr(self):
        self.ubi.connect_async(self.ubi_host, self.ubi_puerto, 60)
        self.local.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_puerto, 30)
        self.ubi.loop_start()
        self.local.loop_start()
        log.info("Puente Ubidots iniciado")
        detener = threading.Event()
        signal.signal(signal.SIGTERM, lambda *a: detener.set())
        signal.signal(signal.SIGINT, lambda *a: detener.set())
        detener.wait()
        self.local.loop_stop()
        self.ubi.loop_stop()
        log.info("Puente detenido")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Puente().correr()
