"""
Servicio de procesamiento local + IA del Gateway (Raspberry Pi).

Contrato MQTT del equipo (sección 2.2 de la wiki):
  Entradas (desde Mosquitto):
      neusa/estacion01/telemetria     ESP32 -> Gateway (QoS 1)
      neusa/estacion01/estado         LWT del ESP32: "online"/"offline"
      neusa/estacion01/comando/ack    acuse de comandos (extensión)
      neusa/estacion01/historico      agregados de 5 min del periodo sin conexión (extensión)
  Salidas (hacia Mosquitto):
      neusa/estacion01/procesado          cada 5 min: el mensaje del salto 2 YA ARMADO
                                          (8 variables del contrato + riesgo_ia). El puente
                                          lo reenvía tal cual a /v1.6/devices/estacion-neusa
      neusa/estacion01/eventos            inmediato y depurado: cambio de estado, fallas,
                                          anomalías, conexión (para las notificaciones)
      neusa/estacion01/ia                 recomendación compacta, retenida -> tablero del ESP32
      neusa/estacion01/ia/detalle         recomendación completa (registro)
      neusa/estacion01/procesado/ventana  ventana interna de 60 s con predicción y balance
  y guarda ventanas, eventos y recomendaciones en SQLite.

Flujo interno:
  1. ProcesadorLocal: validación contra el contrato, secuencia, Hampel, eventos, ventanas de 60 s
  2. Analitica: predicción (Theil-Sen) + balance hídrico (anomalías) en cada ventana
  3. Recomendador: LLM por API (o reglas si no hay internet), con límite de frecuencia
  4. Cada 5 min: payload_ubidots() con las ventanas acumuladas

Uso:
    python servicio_gateway.py                # lee gateway/.env
    python servicio_gateway.py --env otro.env

Challenge #3 - IoT - Universidad de La Sabana 2026-2
Capa: procesamiento local + IA (Rafael Alejandro Rincón)
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import paho.mqtt.client as mqtt

from almacen import Almacen
from config import Config, cargar_config
from ia.analitica import Analitica
from ia.prediccion import ajustar_tendencia
from ia.recomendador import Recomendador, construir_contexto, payload_para_esp32
from procesamiento import FALLAS_SENSOR, Evento, ProcesadorLocal, payload_ubidots

log = logging.getLogger("gateway")


class ServicioGateway:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.almacen = Almacen(cfg.db_path)
        self.proc = ProcesadorLocal(cfg)
        self.analitica = Analitica(cfg)
        self.recomendador = Recomendador(cfg)
        self.pool_ia = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ia")
        self._ia_en_curso = threading.Event()
        self._ultima_rx = time.time()
        self._sin_datos_notificado = False
        self._anomalia_previa = 0
        self._detener = threading.Event()
        self._lock_pub = threading.Lock()
        self._ventanas_nube: list[dict] = []          # ventanas desde el último envío a Ubidots
        self._ultimo_envio_nube: float | None = None
        self._ultima_recomendacion: dict | None = None

        self.cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=f"gw-procesamiento-{cfg.estacion_id}")
        if cfg.mqtt_usuario:
            self.cliente.username_pw_set(cfg.mqtt_usuario, cfg.mqtt_clave)
        self.cliente.will_set(f"{cfg.mqtt_base}/gateway/procesamiento", "offline", qos=1, retain=True)
        self.cliente.on_connect = self._on_connect
        self.cliente.on_message = self._on_message
        self.cliente.reconnect_delay_set(min_delay=1, max_delay=30)

    # ------------------------------------------------------------------ MQTT
    def _on_connect(self, cliente, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT: conexión rechazada (%s)", reason_code)
            return
        log.info("MQTT: conectado a %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_puerto)
        cliente.subscribe([(self.cfg.t_telemetria, 1), (self.cfg.t_estado, 1), (self.cfg.t_ack, 1),
                           (self.cfg.t_historico, 1)])
        cliente.publish(f"{self.cfg.mqtt_base}/gateway/procesamiento", "online", qos=1, retain=True)

    def _on_message(self, cliente, userdata, msg):
        t_rx = time.time()
        try:
            texto = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("payload no UTF-8 en %s", msg.topic)
            return

        if msg.topic == self.cfg.t_telemetria:
            self._telemetria(texto, t_rx)
        elif msg.topic == self.cfg.t_estado:
            self._emitir([Evento("conexion_dispositivo", t_rx, {"conexion": texto.strip()})])
        elif msg.topic == self.cfg.t_historico:
            self._historico(texto, t_rx)
        elif msg.topic == self.cfg.t_ack:
            try:
                ack = json.loads(texto)
            except json.JSONDecodeError:
                ack = {"crudo": texto}
            self._emitir([Evento("ack_comando", t_rx, ack)])

    def _historico(self, texto: str, t_rx: float):
        """Agregados de 5 min que el ESP32 cerró mientras no había conexión.
        Se guardan en SQLite con su hora (ts, o t_rx - edad_s si el ESP32 no
        tenía NTP). No se reenvían a Ubidots automáticamente (cuesta dots)."""
        try:
            datos = json.loads(texto)
            filas = datos["agregados"]
        except (json.JSONDecodeError, KeyError, TypeError):
            self._emitir([Evento("mensaje_invalido", t_rx, {"motivo": "historico"})])
            return
        for a in filas:
            ts = a.get("ts") or 0
            a["t"] = ts if ts > 1_760_000_000 else t_rx - float(a.get("edad_s") or 0)
            self.almacen.guardar_historico(self.cfg.estacion_id, a)
        if filas:
            self._emitir([Evento("historico_recibido", t_rx, {
                "agregados": len(filas), "desde": min(a["t"] for a in filas),
                "hasta": max(a["t"] for a in filas), "pendientes": datos.get("pendientes", 0)})])

    def _telemetria(self, texto: str, t_rx: float):
        try:
            payload = json.loads(texto)
        except json.JSONDecodeError:
            self._emitir([Evento("mensaje_invalido", t_rx, {"motivo": "json"})])
            return
        self._ultima_rx = t_rx
        if self._sin_datos_notificado:
            self._sin_datos_notificado = False
            self._emitir([Evento("estacion_reanudada", t_rx, {})])
        if self.cfg.guardar_crudo:
            self.almacen.guardar_crudo(self.cfg.estacion_id, t_rx, payload.get("seq"), payload)

        eventos, agregado = self.proc.ingresar(payload, t_rx)
        self._emitir(eventos)
        if agregado:
            self._publicar_agregado(agregado)

    # ------------------------------------------------------------- salidas
    def _emitir(self, eventos: list[Evento]):
        for ev in eventos:
            d = ev.a_dict(self.cfg.estacion_id)
            self.cliente.publish(self.cfg.t_eventos, json.dumps(d, ensure_ascii=False), qos=1)
            self.almacen.guardar_evento(d)
            log.info("evento %s %s", ev.tipo, {k: v for k, v in ev.detalle.items() if k != "seq"})

    def _publicar_agregado(self, ag: dict):
        # Se llama desde el hilo de red de paho y, si la estación calla, desde
        # el hilo principal: el detector de anomalías tiene estado, se serializa.
        with self._lock_pub:
            self._publicar_agregado_sin_lock(ag)

    def _publicar_agregado_sin_lock(self, ag: dict):
        self.analitica.enriquecer(ag, self.proc)
        ag["t_publicado"] = round(time.time(), 3)

        if ag.get("anomalia") and not self._anomalia_previa:
            self._emitir([Evento("anomalia_balance", ag["t_fin"], {
                "residual_pct_h": ag.get("residual_pct_h"), "tipo": ag.get("tipo_balance")})])
        elif self._anomalia_previa and not ag.get("anomalia"):
            self._emitir([Evento("anomalia_finalizada", ag["t_fin"], {})])
        self._anomalia_previa = ag.get("anomalia", 0)

        self.cliente.publish(self.cfg.t_procesado_ventana, json.dumps(ag, ensure_ascii=False), qos=0)
        self.almacen.guardar_agregado(ag)
        log.info("ventana: nivel=%s%% tasa=%s%%/h h_crit=%s residual=%s anomalia=%s estado=%s",
                 ag.get("nivel"), ag.get("tasa_desc"), ag.get("horas_a_critico"),
                 ag.get("residual_pct_h"), ag.get("anomalia"), ag.get("estado"))

        # Salto 2: un mensaje a Ubidots cada periodo_nube_s (5 min)
        self._ventanas_nube.append(ag)
        if self._ultimo_envio_nube is None:
            self._ultimo_envio_nube = ag["t_inicio"]
        if ag["t_fin"] - self._ultimo_envio_nube >= self.cfg.periodo_nube_s:
            self._publicar_nube(ag["t_fin"])

        fallas = [f for f in ag.get("falla", []) if f in FALLAS_SENSOR]
        ctx = construir_contexto(ag, self.cfg, fallas)
        if not self._ia_en_curso.is_set() and self.recomendador.debe_consultar(ctx):
            self._ia_en_curso.set()
            self.pool_ia.submit(self._tarea_ia, ctx)

    def _publicar_nube(self, t_fin: float):
        # tasa_descenso (cm/h, negativa = baja): la del ESP32 (la misma que
        # dispara la alarma). Si no llegó, se estima aquí con los últimos 5 min.
        tasa_cmh = next((a["tasa_cmh_esp32"] for a in reversed(self._ventanas_nube)
                         if a.get("tasa_cmh_esp32") is not None), None)
        if tasa_cmh is None:
            s = self.proc.series_historial(desde_t=t_fin - 300.0)
            tend = ajustar_tendencia(s["t"], s["nivel"], 300.0)
            tasa_cmh = None if tend is None else -tend.tasa_desc_pct_h * self.cfg.altura_util_cm / 100.0
        p = payload_ubidots(self._ventanas_nube, tasa_cmh, self._ultima_recomendacion, self.cfg)
        if p:
            self.cliente.publish(self.cfg.t_procesado, json.dumps(p, ensure_ascii=False), qos=1)
            log.info("procesado (salto 2, %d variables): %s", len(p), p)
        self._ventanas_nube = []
        self._ultimo_envio_nube = t_fin

    def _tarea_ia(self, ctx: dict):
        try:
            rec = self.recomendador.recomendar(ctx)
            self._ultima_recomendacion = rec
            self.cliente.publish(self.cfg.t_ia, json.dumps(payload_para_esp32(rec), ensure_ascii=False),
                                 qos=1, retain=True)
            self.cliente.publish(self.cfg.t_ia_detalle, json.dumps(rec, ensure_ascii=False), qos=1)
            self.almacen.guardar_recomendacion(self.cfg.estacion_id, rec, ctx)
            log.info("IA [%s] riesgo=%s latencia=%.2fs problemas=%s | %s", rec["fuente"], rec["riesgo"],
                     rec["latencia_s"], rec["problemas"], rec["resumen"])
        except Exception:  # noqa: BLE001 - la IA nunca debe tumbar el servicio
            log.exception("error en la tarea de IA")
        finally:
            self._ia_en_curso.clear()

    # ------------------------------------------------------------- ciclo
    def ejecutar(self):
        self.cliente.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_puerto, keepalive=30)
        self.cliente.loop_start()
        log.info("Servicio de procesamiento + IA iniciado (proveedor IA: %s)", self.cfg.ia_proveedor)
        try:
            while not self._detener.wait(1.0):
                if (not self._sin_datos_notificado
                        and time.time() - self._ultima_rx > self.cfg.timeout_estacion_s):
                    self._sin_datos_notificado = True
                    self._emitir([Evento("estacion_sin_datos", time.time(),
                                         {"segundos": round(time.time() - self._ultima_rx, 1)})])
                    ag = self.proc.forzar_cierre(time.time())
                    if ag:
                        self._publicar_agregado(ag)
        finally:
            self.cliente.publish(f"{self.cfg.mqtt_base}/gateway/procesamiento", "offline", qos=1, retain=True)
            self.cliente.loop_stop()
            self.pool_ia.shutdown(wait=False)

    def detener(self, *_):
        self._detener.set()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", help="archivo .env (por defecto gateway/.env)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    servicio = ServicioGateway(cargar_config(args.env))
    signal.signal(signal.SIGINT, servicio.detener)
    signal.signal(signal.SIGTERM, servicio.detener)
    servicio.ejecutar()


if __name__ == "__main__":
    main()
