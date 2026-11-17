"""
Tablas SQLite del procesamiento local y del módulo de IA.

Aquí se guardan los RESULTADOS del procesamiento: agregados, eventos y
recomendaciones de IA. La tabla 'lecturas_crudas' guarda además cada lectura
del ESP32 cuando GUARDAR_CRUDO=1, que es como opera en la Raspberry Pi.
"""
from __future__ import annotations

import json
import sqlite3
import threading

ESQUEMA = """
CREATE TABLE IF NOT EXISTS agregados (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    estacion      TEXT    NOT NULL,
    t_inicio      REAL    NOT NULL,
    t_fin         REAL    NOT NULL,
    n             INTEGER,
    nivel         REAL,
    tasa_desc     REAL,
    horas_a_critico REAL,
    residual_pct_h  REAL,
    anomalia      INTEGER,
    estado        INTEGER,
    estado_max    INTEGER,
    calidad_enlace_pct REAL,
    json          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_agregados_t ON agregados (estacion, t_fin);

CREATE TABLE IF NOT EXISTS eventos (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    estacion  TEXT NOT NULL,
    t         REAL NOT NULL,
    tipo      TEXT NOT NULL,
    json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_eventos_t ON eventos (estacion, t);

CREATE TABLE IF NOT EXISTS ia_recomendaciones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    estacion    TEXT NOT NULL,
    t           REAL NOT NULL,
    fuente      TEXT,
    modelo      TEXT,
    riesgo      TEXT,
    estado_sistema TEXT,
    latencia_s  REAL,
    valida      INTEGER,
    problemas   TEXT,
    contexto    TEXT,
    json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS historico_esp32 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    estacion    TEXT NOT NULL,
    t           REAL NOT NULL,      -- inicio del agregado de 5 min (epoch s)
    altura_cm   REAL,
    estado_max  INTEGER,
    json        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_historico_t ON historico_esp32 (estacion, t);

CREATE TABLE IF NOT EXISTS lecturas_crudas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    estacion    TEXT,
    t_recepcion REAL NOT NULL,
    seq         INTEGER,
    json        TEXT NOT NULL
);
"""


class Almacen:
    def __init__(self, ruta: str):
        self.con = sqlite3.connect(ruta, check_same_thread=False)
        self.con.execute("PRAGMA journal_mode=WAL")   # permite leer la base mientras el servicio escribe
        self.con.executescript(ESQUEMA)
        self._lock = threading.Lock()

    def _exec(self, sql: str, args: tuple) -> None:
        with self._lock:
            self.con.execute(sql, args)
            self.con.commit()

    def guardar_agregado(self, ag: dict) -> None:
        self._exec(
            "INSERT INTO agregados (estacion,t_inicio,t_fin,n,nivel,tasa_desc,horas_a_critico,"
            "residual_pct_h,anomalia,estado,estado_max,calidad_enlace_pct,json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ag["estacion"], ag["t_inicio"], ag["t_fin"], ag["n"], ag.get("nivel"), ag.get("tasa_desc"),
             ag.get("horas_a_critico"), ag.get("residual_pct_h"), ag.get("anomalia"), ag.get("estado_num"),
             ag.get("estado_max"), ag.get("calidad_enlace_pct"), json.dumps(ag, ensure_ascii=False)))

    def guardar_evento(self, ev: dict) -> None:
        self._exec("INSERT INTO eventos (estacion,t,tipo,json) VALUES (?,?,?,?)",
                   (ev["estacion"], ev["t"], ev["tipo"], json.dumps(ev, ensure_ascii=False)))

    def guardar_recomendacion(self, estacion: str, rec: dict, contexto: dict) -> None:
        self._exec(
            "INSERT INTO ia_recomendaciones (estacion,t,fuente,modelo,riesgo,estado_sistema,latencia_s,"
            "valida,problemas,contexto,json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (estacion, rec["t"], rec["fuente"], rec["modelo"], rec["riesgo"], rec.get("estado_sistema"),
             rec["latencia_s"], int(rec["valida"]), json.dumps(rec["problemas"], ensure_ascii=False),
             json.dumps(contexto, ensure_ascii=False), json.dumps(rec, ensure_ascii=False)))

    def guardar_historico(self, estacion: str, a: dict) -> None:
        self._exec("INSERT OR IGNORE INTO historico_esp32 (estacion,t,altura_cm,estado_max,json) "
                   "VALUES (?,?,?,?,?)",
                   (estacion, a["t"], a.get("altura_cm"), a.get("estado_max"), json.dumps(a)))

    def guardar_crudo(self, estacion: str, t: float, seq: int | None, payload: dict) -> None:
        self._exec("INSERT INTO lecturas_crudas (estacion,t_recepcion,seq,json) VALUES (?,?,?,?)",
                   (estacion, t, seq, json.dumps(payload)))
