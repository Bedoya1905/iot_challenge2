"""
Mide la latencia del camino de BAJADA (Gateway -> ESP32) con el comando "ping",
que el firmware acusa sin tocar la alarma (corrección del Ch2: "medir latencia").

Envía N comandos {"accion":"ping","cmd_id":...} a neusa/estacion01/comando y espera el
acuse en neusa/estacion01/comando/ack. Mide el tiempo de ida y vuelta (RTT) con el reloj
de ESTE equipo, así que no depende de que los relojes estén sincronizados.
La latencia de un sentido se estima como RTT/2.

Uso (en la Raspberry Pi o en un PC de la misma WLAN):
  python herramientas/medir_latencia_comando.py --n 100 --intervalo 1 --csv latencia_bajada.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import threading
import time
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import cargar_config  # noqa: E402


def main():
    cfg = cargar_config()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--intervalo", type=float, default=1.0, help="segundos entre comandos")
    ap.add_argument("--timeout", type=float, default=5.0, help="segundos para dar un acuse por perdido")
    ap.add_argument("--qos", type=int, default=1, choices=[0, 1])
    ap.add_argument("--csv", help="guarda cada medición")
    args = ap.parse_args()

    enviados: dict[str, float] = {}
    rtt: dict[str, float] = {}
    lock = threading.Lock()

    def on_message(c, u, msg):
        t = time.perf_counter()
        try:
            ack = json.loads(msg.payload)
        except json.JSONDecodeError:
            return
        cid = ack.get("cmd_id", "")
        with lock:
            if cid in enviados and cid not in rtt:
                rtt[cid] = (t - enviados[cid]) * 1000.0

    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"latencia-{uuid.uuid4().hex[:6]}")
    if cfg.mqtt_usuario:
        cli.username_pw_set(cfg.mqtt_usuario, cfg.mqtt_clave)
    cli.on_message = on_message
    cli.connect(cfg.mqtt_host, cfg.mqtt_puerto, 30)
    cli.subscribe(cfg.t_ack, qos=1)
    cli.loop_start()
    time.sleep(1.0)

    print(f"Enviando {args.n} ping a {cfg.t_comando} (QoS {args.qos})...")
    for i in range(args.n):
        cid = f"ping-{i:04d}-{uuid.uuid4().hex[:4]}"
        with lock:
            enviados[cid] = time.perf_counter()
        cli.publish(cfg.t_comando, json.dumps({"accion": "ping", "cmd_id": cid}), qos=args.qos)
        time.sleep(args.intervalo)
    time.sleep(args.timeout)
    cli.loop_stop()

    valores = sorted(rtt.values())
    perdidos = args.n - len(valores)
    if not valores:
        sys.exit("No llegó ningún acuse: ¿el ESP32 está conectado y suscrito a neusa/estacion01/comando?")
    p = lambda q: valores[min(len(valores) - 1, int(q * len(valores)))]  # noqa: E731
    print(f"\nAcuses recibidos: {len(valores)}/{args.n} (perdidos: {perdidos}, {100 * perdidos / args.n:.1f} %)")
    print(f"RTT  mín {valores[0]:.1f} · p50 {p(0.5):.1f} · p95 {p(0.95):.1f} · máx {valores[-1]:.1f} ms "
          f"· media {statistics.fmean(valores):.1f} ± {statistics.pstdev(valores):.1f} ms")
    print(f"Latencia de un sentido estimada (RTT/2): p50 {p(0.5) / 2:.1f} ms · p95 {p(0.95) / 2:.1f} ms")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["cmd_id", "rtt_ms"])
            for cid in enviados:
                w.writerow([cid, round(rtt[cid], 2) if cid in rtt else ""])
        print(f"Detalle en {args.csv}")


if __name__ == "__main__":
    main()
