"""
Validación del módulo de IA con datos propios.

Reproduce las lecturas guardadas a través del MISMO código del Gateway
(ProcesadorLocal + Analitica + Recomendador) y mide:

  A. Predicción del nivel (Theil-Sen)
     - Backtest: en cada instante t se ajusta la tendencia con los datos
       anteriores a t y se predice el nivel en t+H. Se compara con lo medido.
     - MAE y RMSE contra dos referencias: persistencia (nivel(t+H)=nivel(t))
       y mínimos cuadrados ordinarios. "Skill" = 1 - MAE_modelo / MAE_persistencia.
     - Barrido del tamaño de ventana para justificar VENTANA_TENDENCIA_S.
     - Error del tiempo estimado hasta cruzar los umbrales de alerta y crítico.

  B. Detección de anomalías (balance hídrico)
     - Matriz de confusión por ventana contra la etiqueta de verdad del ensayo.
     - Precisión, sensibilidad (recall), F1 y retardo de detección por evento.

  C. Recomendación con LLM (opcional, consume cuota de la API)
     - % de respuestas JSON válidas, % con riesgo corregido por el piso,
       % con cifras no soportadas (alucinación numérica), latencia p50/p95 y
       consistencia entre repeticiones (misma entrada -> mismo riesgo).

Entradas:
  --csv datos.csv   columnas: t (epoch s) + campos del JSON de telemetría.
                    Opcionales: escenario, etiqueta_anomalia (0/1).
  --sqlite gw.db --consulta "SELECT ..."   alternativa al CSV.
  --etiquetas e.csv  columnas: t_inicio, t_fin, escenario, anomalia
                    (bitácora de los ensayos con el prototipo).

Uso:
  python validacion/validar_ia.py --csv datos.csv --salida resultados/
  python validacion/validar_ia.py --csv datos.csv --llm 6 --repeticiones 3 --salida resultados/

Challenge #3 - IoT - Universidad de La Sabana 2026-2
Capa: procesamiento local + IA (Rafael Alejandro Rincón)
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import cargar_config  # noqa: E402
from ia.analitica import Analitica  # noqa: E402
from ia.prediccion import ajustar_tendencia, horas_hasta, predecir_nivel  # noqa: E402
from ia.recomendador import Recomendador, construir_contexto  # noqa: E402
from procesamiento import FALLAS_SENSOR, FiltroHampel, ProcesadorLocal  # noqa: E402

# Campos del contrato (salto 1) + extensiones que se reenvían al procesador
CAMPOS_CONTRATO = ["ts", "nivel_cm", "caudal_lmin", "radiacion_wm2", "temp_c", "hum_pct",
                   "presion_hpa", "evap_tasa_mmdia", "estado", "falla",
                   "seq", "ts_ms", "nivel_pct", "altura_cm", "tasa_cmh", "tendencia_24h_cmh", "silenciada"]
ESTADOS = ["NORMAL", "VIGILANCIA", "ALERTA", "CRITICO"]


# --------------------------------------------------------------------------- datos
def _lista_falla(x) -> list:
    if isinstance(x, list):
        return x
    if x is None or (isinstance(x, float) and math.isnan(x)) or x == "":
        return []
    try:
        v = json.loads(x)
        return v if isinstance(v, list) else [str(v)]
    except (TypeError, json.JSONDecodeError):
        return [p.strip() for p in str(x).strip("[]").replace('"', "").split(",") if p.strip()]


def cargar_datos(args, cfg) -> pd.DataFrame:
    """Lee los datos (formato del contrato) y agrega columnas internas:
    t (epoch s), nivel (%), estado y falla (lista)."""
    if args.csv:
        df = pd.read_csv(args.csv)
    else:
        con = sqlite3.connect(args.sqlite)
        df = pd.read_sql_query(args.consulta, con)
        con.close()

    if "ts_ms" in df and df["ts_ms"].fillna(0).gt(1.76e12).any():
        df["t"] = np.where(df["ts_ms"].fillna(0) > 1.76e12, df["ts_ms"] / 1000.0, df.get("ts", np.nan))
    elif "ts" in df:
        df["t"] = df["ts"].astype(float)
    if "t_recepcion" in df:   # sin NTP en el ESP32: hora de llegada al Gateway
        df["t"] = df["t"].where(df["t"] > 1.76e9, df["t_recepcion"]) if "t" in df else df["t_recepcion"]
    if "t" not in df:
        raise SystemExit("los datos deben tener 'ts' (o 'ts_ms', o 't_recepcion') en epoch")
    df = df.sort_values("t").reset_index(drop=True)

    altura = cfg.distancia_vacio_cm - cfg.distancia_lleno_cm
    if "nivel_pct" in df and df["nivel_pct"].notna().any():
        df["nivel"] = df["nivel_pct"]
    else:
        df["nivel"] = ((cfg.distancia_vacio_cm - df["nivel_cm"]) / altura * 100.0).clip(0, 100)
    df["falla"] = df["falla"].apply(_lista_falla) if "falla" in df else [[] for _ in range(len(df))]

    if args.etiquetas:
        et = pd.read_csv(args.etiquetas)
        df["etiqueta_anomalia"] = 0
        df["escenario"] = "sin_etiqueta"
        extra = [c for c in ("estado_esperado", "falla_esperada", "repeticion") if c in et.columns]
        for col in extra:
            df[col] = np.nan
        for _, e in et.iterrows():
            m = (df["t"] >= e["t_inicio"]) & (df["t"] <= e["t_fin"])
            df.loc[m, "etiqueta_anomalia"] = int(e["anomalia"])
            df.loc[m, "escenario"] = e["escenario"]
            for col in extra:
                if not pd.isna(e[col]):
                    df.loc[m, col] = e[col]
    return df


def fila_a_payload(fila: pd.Series) -> dict:
    """Reconstruye el mensaje del contrato a partir de una fila guardada."""
    p = {}
    for c in CAMPOS_CONTRATO:
        if c not in fila:
            continue
        v = fila[c]
        if c == "falla":
            p[c] = list(v)
        elif not (isinstance(v, float) and math.isnan(v)):
            p[c] = v.item() if hasattr(v, "item") else v
    if "ts_ms" not in p and "ts" not in p:
        p["ts_ms"] = int(fila["t"] * 1000)
    p.setdefault("estado", 0)
    return p


# --------------------------------------------------------------- A. predicción
def serie_filtrada(df: pd.DataFrame, cfg) -> tuple[np.ndarray, np.ndarray]:
    h = FiltroHampel(cfg.hampel_ventana, cfg.hampel_k, cfg.hampel_min_pct)
    t, y = [], []
    reps = df["repeticion"] if "repeticion" in df else pd.Series(1, index=df.index)
    rep_previa = None
    for ti, ni, rep in zip(df["t"], df["nivel"], reps):
        if rep != rep_previa:     # cada repetición empieza con el filtro vacío
            h = FiltroHampel(cfg.hampel_ventana, cfg.hampel_k, cfg.hampel_min_pct)
            rep_previa = rep
        if pd.isna(ni):
            continue
        v, _ = h.filtrar(float(ni))
        t.append(float(ti))
        y.append(v)
    return np.array(t), np.array(y)


def valor_en(t: np.ndarray, y: np.ndarray, instante: float, tol: float = 6.0) -> float | None:
    m = np.abs(t - instante) <= tol
    return float(np.median(y[m])) if m.any() else None


def ols_pred(t: np.ndarray, y: np.ndarray, h: float) -> float:
    tr = t - t[-1]
    b, a = np.polyfit(tr, y, 1)
    return float(min(100, max(0, a + b * h)))


def regimen_en(df: pd.DataFrame):
    """Función t -> id del tramo (escenario) vigente, o None si no hay etiquetas."""
    if "escenario" not in df:
        return None
    ids = (df["escenario"] != df["escenario"].shift()).cumsum().to_numpy()
    tt = df["t"].to_numpy()
    return lambda x: int(ids[min(len(tt) - 1, max(0, np.searchsorted(tt, x) - 1))])


def backtest_prediccion(t, y, ventanas, horizontes, paso_s=10.0, regimen=None) -> pd.DataFrame:
    """'regimen' marca si dentro del horizonte cambió el tramo del ensayo (p. ej.
    se cerró la válvula): ningún predictor puede anticipar esa decisión externa,
    por eso las métricas se reportan también solo para horizontes sin cambio."""
    filas = []
    t_eval = np.arange(t[0] + max(ventanas), t[-1] - max(horizontes), paso_s)
    for te in t_eval:
        hasta = t <= te
        th, yh = t[hasta], y[hasta]
        for h in horizontes:
            real = valor_en(t, y, te + h)
            if real is None:
                continue
            persist = float(yh[-1])
            for w in ventanas:
                sel = th >= te - w
                tend = ajustar_tendencia(list(th[sel]), list(yh[sel]), w, min_puntos=10, min_duracion_s=w * 0.5)
                if tend is None:
                    continue
                filas.append({
                    "t": te, "ventana_s": w, "horizonte_s": h, "real": real,
                    "cambio_regimen": bool(regimen and regimen(te) != regimen(te + h)),
                    "theil_sen": predecir_nivel(tend, h),
                    "ols": ols_pred(th[sel], yh[sel], h),
                    "persistencia": persist,
                })
    return pd.DataFrame(filas)


def metricas_prediccion(bt: pd.DataFrame) -> pd.DataFrame:
    out = []
    if bt.empty:
        return pd.DataFrame(columns=["ventana_s", "horizonte_s", "n", "mae_theil_sen", "mae_ols",
                                     "mae_persistencia", "skill_vs_persistencia"])
    for (w, h), g in bt.groupby(["ventana_s", "horizonte_s"]):
        r = {"ventana_s": w, "horizonte_s": h, "n": len(g)}
        for m in ("theil_sen", "ols", "persistencia"):
            e = g[m] - g["real"]
            r[f"mae_{m}"] = float(np.mean(np.abs(e)))
            r[f"rmse_{m}"] = float(np.sqrt(np.mean(e ** 2)))
        r["skill_vs_persistencia"] = 1 - r["mae_theil_sen"] / r["mae_persistencia"] if r["mae_persistencia"] else None
        out.append(r)
    return pd.DataFrame(out)


def error_tiempo_a_umbral(t, y, umbral, ventana, paso_s=10.0, max_horizonte_s=900.0,
                          regimen=None) -> pd.DataFrame:
    """Para cada instante con cruce previsto, compara el tiempo previsto con el real.
    Con etiquetas de régimen, solo cuenta los cruces que ocurren en el mismo tramo."""
    filas = []
    for te in np.arange(t[0] + ventana, t[-1], paso_s):
        hasta = t <= te
        th, yh = t[hasta], y[hasta]
        if yh[-1] <= umbral:
            continue
        sel = th >= te - ventana
        tend = ajustar_tendencia(list(th[sel]), list(yh[sel]), ventana, min_puntos=10, min_duracion_s=ventana * 0.5)
        if tend is None:
            continue
        hrs = horas_hasta(tend, umbral)
        if hrs is None or hrs * 3600 > max_horizonte_s:
            continue
        futuro = (t > te) & (y <= umbral)
        if not futuro.any():
            continue
        real_s = float(t[futuro][0] - te)
        if real_s > max_horizonte_s * 1.5:
            continue
        if regimen and regimen(te) != regimen(te + real_s):
            continue
        filas.append({"t": te, "umbral": umbral, "previsto_s": hrs * 3600, "real_s": real_s,
                      "error_s": hrs * 3600 - real_s})
    return pd.DataFrame(filas)


# --------------------------------------------------------- B. reproducción + anomalías
def reproducir(df: pd.DataFrame, cfg) -> pd.DataFrame:
    proc = ProcesadorLocal(cfg)
    ana = Analitica(cfg)
    ags = []
    rep_previa = None
    for _, fila in df.iterrows():
        # Cada repetición del ensayo empieza con el Gateway "limpio"
        if "repeticion" in fila and fila["repeticion"] != rep_previa:
            proc, ana, rep_previa = ProcesadorLocal(cfg), Analitica(cfg), fila["repeticion"]
        # Si se guardó la hora de recepción, se usa: así se mide la latencia real
        t_rx = float(fila["t_recepcion"]) if "t_recepcion" in fila and not pd.isna(fila["t_recepcion"]) \
            else float(fila["t"])
        _, ag = proc.ingresar(fila_a_payload(fila), t_rx)
        if ag:
            ana.enriquecer(ag, proc)
            span = (df["t"] > ag["t_fin"] - cfg.ventana_anomalia_s) & (df["t"] <= ag["t_fin"])
            if "etiqueta_anomalia" in df:
                ag["etiqueta"] = int(df.loc[span, "etiqueta_anomalia"].mean() >= 0.5) if span.any() else 0
            if "escenario" in df:
                ag["escenario"] = fila["escenario"]   # tramo vigente al cerrar la ventana
            ags.append(ag)
    return pd.DataFrame(ags)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """Intervalo de confianza de Wilson (95 %) para una proporción k/n.
    Con pocas repeticiones el intervalo es ancho: eso es justamente lo que
    hay que reportar en lugar de un solo porcentaje."""
    if n == 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / den
    medio = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centro - medio), min(1.0, centro + medio)


def metricas_alertas(df: pd.DataFrame, gracia_s: float, umbral: int = 1) -> dict:
    """Falsos positivos y negativos de la ALERTA del dispositivo (observación del Ch2).

    Verdad: 'estado_esperado' (bitácora del ensayo o, en el simulador, el estado
    que daría la lógica con sensores ideales). Se evalúa la alerta binaria
    estado >= 'umbral' (por defecto 1: cualquier estado distinto de NORMAL) y la
    concordancia de severidad 4 × 4. Las fallas de sensor se evalúan aparte con
    'falla_esperada' contra la lista 'falla' reportada.

    - Por evento (lo que importa a la autoridad): cada tramo continuo con la
      misma verdad dentro de una repetición es un evento.
        tramo con alerta esperada  -> VP si el sistema alertó (con hasta
                                      'gracia_s' de retraso), si no FN; y su retardo.
        tramo sin alerta esperada  -> FP si hubo alerta pasada la gracia, si no VN;
                                      además, episodios de falsa alarma por hora.
    - Por muestra: matriz de confusión fuera de los 'gracia_s' posteriores a
      cada cambio de la verdad (la tasa de descenso necesita ~2.5 min de datos).
      Las muestras consecutivas están correlacionadas: su IC es optimista; el
      IC por evento es el honesto.
    """
    if "estado_esperado" not in df or df["estado_esperado"].isna().all():
        return {"disponible": False}
    d = df.dropna(subset=["estado_esperado"]).copy()
    d["rep"] = d["repeticion"] if "repeticion" in d else 1
    d["esp"] = d["estado_esperado"].astype(int)
    d["obs"] = d["estado"].astype(int)
    d["esp_b"] = (d["esp"] >= umbral).astype(int)
    d["obs_b"] = (d["obs"] >= umbral).astype(int)
    d["bloque"] = ((d["esp_b"] != d["esp_b"].shift()) | (d["rep"] != d["rep"].shift())).cumsum()

    eventos, episodios_fa, horas_normal = [], 0, 0.0
    for _, g in d.groupby("bloque"):
        ini, fin = float(g["t"].iloc[0]), float(g["t"].iloc[-1])
        rep = g["rep"].iloc[0]
        if g["esp_b"].iloc[0] == 1:
            ventana = d[(d["rep"] == rep) & (d["t"] >= ini) & (d["t"] <= fin + gracia_s)]
            alertas = ventana[ventana["obs_b"] == 1]
            ok = len(alertas) > 0
            eventos.append({"rep": rep, "tipo": "alerta", "inicio": ini, "duracion_s": fin - ini,
                            "esperado_max": int(g["esp"].max()), "resultado": "VP" if ok else "FN",
                            "retardo_s": float(alertas["t"].iloc[0] - ini) if ok else None,
                            "observado_max": int(ventana["obs"].max())})
        else:
            tramo = g[g["t"] >= ini + gracia_s]
            if tramo.empty:
                continue
            horas_normal += (float(tramo["t"].iloc[-1]) - float(tramo["t"].iloc[0])) / 3600.0
            subidas = int(((tramo["obs_b"] == 1) & (tramo["obs_b"].shift(fill_value=0) == 0)).sum())
            episodios_fa += subidas
            eventos.append({"rep": rep, "tipo": "normal", "inicio": ini, "duracion_s": fin - ini,
                            "resultado": "FP" if subidas or tramo["obs_b"].iloc[0] == 1 else "VN",
                            "pct_tiempo_en_alerta": round(100 * tramo["obs_b"].mean(), 1)})

    ev = pd.DataFrame(eventos)
    al = ev[ev["tipo"] == "alerta"] if len(ev) else ev
    no = ev[ev["tipo"] == "normal"] if len(ev) else ev
    vp, fn = int((al["resultado"] == "VP").sum()), int((al["resultado"] == "FN").sum())
    fp, vn = int((no["resultado"] == "FP").sum()), int((no["resultado"] == "VN").sum())

    # Por muestra, fuera de la gracia tras CUALQUIER cambio del estado esperado
    d["bloque_sev"] = ((d["esp"] != d["esp"].shift()) | (d["rep"] != d["rep"].shift())).cumsum()
    d["t_cambio"] = d.groupby("bloque_sev")["t"].transform("first")
    m = d[d["t"] >= d["t_cambio"] + gracia_s]
    mvp = int(((m["esp_b"] == 1) & (m["obs_b"] == 1)).sum())
    mfn = int(((m["esp_b"] == 1) & (m["obs_b"] == 0)).sum())
    mfp = int(((m["esp_b"] == 0) & (m["obs_b"] == 1)).sum())
    mvn = int(((m["esp_b"] == 0) & (m["obs_b"] == 0)).sum())
    conf = pd.crosstab(m["esp"], m["obs"]).reindex(index=[0, 1, 2, 3], columns=[0, 1, 2, 3], fill_value=0)

    # Fallas de sensor: tramos con falla esperada en los que la lista 'falla' la reportó
    fallas = {"detectadas": 0, "total": 0}
    if "falla_esperada" in d and d["falla_esperada"].fillna(0).astype(int).any():
        d["fe"] = d["falla_esperada"].fillna(0).astype(int)
        d["fo"] = d["falla"].apply(lambda f: bool(set(f) & FALLAS_SENSOR))
        bl = ((d["fe"] != d["fe"].shift()) | (d["rep"] != d["rep"].shift())).cumsum()
        for _, g in d[d["fe"] == 1].groupby(bl[d["fe"] == 1]):
            fallas["total"] += 1
            fallas["detectadas"] += int(g["fo"].any())

    retardos = al["retardo_s"].dropna() if len(al) else pd.Series(dtype=float)
    return {
        "disponible": True, "gracia_s": gracia_s, "umbral": umbral,
        "repeticiones": int(d["rep"].nunique()),
        "evento": {"vp": vp, "fn": fn, "fp": fp, "vn": vn,
                   "sensibilidad": vp / (vp + fn) if vp + fn else None, "ic_sensibilidad": wilson(vp, vp + fn),
                   "tasa_fp": fp / (fp + vn) if fp + vn else None, "ic_tasa_fp": wilson(fp, fp + vn),
                   "falsas_alarmas_por_hora": episodios_fa / horas_normal if horas_normal else None,
                   "horas_normales_evaluadas": round(horas_normal, 2),
                   "retardo_p50_s": float(retardos.median()) if len(retardos) else None,
                   "retardo_max_s": float(retardos.max()) if len(retardos) else None},
        "muestra": {"vp": mvp, "fn": mfn, "fp": mfp, "vn": mvn,
                    "tasa_fn": mfn / (mfn + mvp) if mfn + mvp else None, "ic_tasa_fn": wilson(mfn, mfn + mvp),
                    "tasa_fp": mfp / (mfp + mvn) if mfp + mvn else None, "ic_tasa_fp": wilson(mfp, mfp + mvn),
                    "confusion_4x4": conf.values.tolist()},
        "fallas": fallas,
        "eventos": eventos,
    }


def latencias(ag: pd.DataFrame) -> dict | None:
    if "latencia_p50_ms" not in ag or ag["latencia_p50_ms"].isna().all():
        return None
    a = ag.dropna(subset=["latencia_p50_ms"])
    return {"ventanas": len(a), "p50_mediana_ms": float(a["latencia_p50_ms"].median()),
            "p95_mediana_ms": float(a["latencia_p95_ms"].median()), "max_ms": float(a["latencia_max_ms"].max())}


def metricas_anomalias(ag: pd.DataFrame, df: pd.DataFrame) -> dict:
    if "etiqueta" not in ag:
        return {"disponible": False}
    v = ag[ag["balance_valido"] == True]  # noqa: E712
    y, p = v["etiqueta"].astype(int), v["anomalia"].astype(int)
    tp = int(((y == 1) & (p == 1)).sum())
    fp = int(((y == 0) & (p == 1)).sum())
    fn = int(((y == 1) & (p == 0)).sum())
    tn = int(((y == 0) & (p == 0)).sum())
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else None

    # Retardo por evento: desde el inicio del tramo anómalo hasta la 1.ª ventana con anomalía
    retardos = []
    df = df.copy()
    df["bloque"] = (df["etiqueta_anomalia"] != df["etiqueta_anomalia"].shift()).cumsum()
    for _, g in df[df["etiqueta_anomalia"] == 1].groupby("bloque"):
        ini, fin = g["t"].iloc[0], g["t"].iloc[-1]
        det = ag[(ag["anomalia"] == 1) & (ag["t_fin"] >= ini) & (ag["t_fin"] <= fin + 120)]
        retardos.append({"escenario": g["escenario"].iloc[0] if "escenario" in g else "",
                         "rep": g["repeticion"].iloc[0] if "repeticion" in g else 1,
                         "inicio": ini, "duracion_s": fin - ini,
                         "retardo_s": float(det["t_fin"].iloc[0] - ini) if len(det) else None})
    return {"disponible": True, "vp": tp, "fp": fp, "fn": fn, "vn": tn,
            "precision": prec, "ic_precision": wilson(tp, tp + fp),
            "sensibilidad": rec, "ic_sensibilidad": wilson(tp, tp + fn),
            "f1": f1, "eventos": retardos}


# ------------------------------------------------------------------ C. LLM
def validar_llm(ag: pd.DataFrame, cfg, n_contextos: int, repeticiones: int) -> dict:
    # Un contexto representativo por tramo del ensayo (la ventana central del tramo)
    if "escenario" in ag:
        bloques = (ag["escenario"] != ag["escenario"].shift()).cumsum()
        muestras = ag.groupby(bloques, group_keys=False).apply(lambda g: g.iloc[[len(g) // 2]])
    else:
        muestras = ag.iloc[np.linspace(0, len(ag) - 1, min(n_contextos, len(ag))).astype(int)]
    muestras = muestras.head(n_contextos)

    rec = Recomendador(cfg)
    resultados = []
    for _, fila in muestras.iterrows():
        a = {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in fila.to_dict().items()}
        fallas = [f for f in (a.get("falla") or []) if f in FALLAS_SENSOR]
        ctx = construir_contexto(a, cfg, fallas)
        for i in range(repeticiones):
            r = rec.recomendar(ctx)
            resultados.append({"escenario": a.get("escenario", ""), "estado": ctx["estado_sistema"],
                               "anomalia": ctx["balance_hidrico"]["anomalia_confirmada"], "rep": i, **r})
    df = pd.DataFrame(resultados)
    if df.empty:
        return {"n": 0}
    llm = df[df["fuente"] != "reglas"]
    lat = llm["latencia_s"] if len(llm) else pd.Series(dtype=float)
    consist = []
    for _, g in df.groupby(["escenario", "estado", "anomalia"]):
        if len(g) > 1:
            consist.append(Counter(g["riesgo"]).most_common(1)[0][1] / len(g))
    return {
        "n": len(df),
        "proveedor": cfg.ia_proveedor,
        "pct_respuestas_llm_validas": 100 * len(llm) / len(df),
        "pct_riesgo_corregido": 100 * df["problemas"].apply(lambda ps: any(p.startswith("riesgo_bajo") for p in ps)).mean(),
        "pct_cifras_no_soportadas": 100 * df["problemas"].apply(lambda ps: any(p.startswith("cifras") for p in ps)).mean(),
        "latencia_p50_s": float(lat.median()) if len(lat) else None,
        "latencia_p95_s": float(lat.quantile(0.95)) if len(lat) else None,
        "consistencia_media": float(np.mean(consist)) if consist else None,
        "detalle": df[["escenario", "estado", "anomalia", "rep", "fuente", "riesgo", "confianza",
                       "latencia_s", "problemas", "resumen", "acciones"]].to_dict("records"),
    }


# ------------------------------------------------------------------ gráficas
def graficar(df, ag, t, y, mp, salida: Path, cfg):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Corta las líneas en los huecos entre repeticiones (no unir puntos lejanos)
    def cortar(d: pd.DataFrame, col_t: str, hueco: float) -> pd.DataFrame:
        d = d.reset_index(drop=True)
        saltos = d.index[d[col_t].diff() > hueco]
        if len(saltos) == 0:
            return d
        vacios = pd.DataFrame({col_t: d.loc[saltos, col_t].values - 1e-3}, index=saltos - 0.5)
        return pd.concat([d, vacios]).sort_index().reset_index(drop=True)

    df = cortar(df, "t", 30.0)
    ag = cortar(ag, "t_fin", 3 * cfg.periodo_agregado_s)
    serie = cortar(pd.DataFrame({"t": t, "y": y}), "t", 30.0)
    t, y = serie["t"].to_numpy(), serie["y"].to_numpy()

    t0 = df["t"].iloc[0]
    fig, ax = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    m = lambda s: (np.asarray(s) - t0) / 60.0  # noqa: E731
    ax[0].plot(m(df["t"]), df["nivel"], ".", ms=2, color="#9aa4ad", label="nivel medido")
    ax[0].plot(m(t), y, "-", lw=1.2, color="#2d5f8a", label="nivel filtrado (Hampel)")
    if "nivel_real" in df:
        ax[0].plot(m(df["t"]), df["nivel_real"], "--", lw=1, color="#333", label="nivel real (simulado)")
    ax[0].axhline(cfg.nivel_alerta_pct, color="#c9a227", lw=0.8, ls=":")
    ax[0].axhline(cfg.nivel_critico_pct, color="#b83232", lw=0.8, ls=":")
    ax[0].set_ylabel("Nivel (%)")
    ax[0].legend(loc="upper right", fontsize=8)

    if "etiqueta_anomalia" in df:
        for a in ax:
            on = df["etiqueta_anomalia"].fillna(0).values == 1
            a.fill_between(m(df["t"]), 0, 1, where=on, transform=a.get_xaxis_transform(),
                           color="#e8a0a0", alpha=0.3, lw=0)

    ax[1].plot(m(ag["t_fin"]), ag["descenso_obs_pct_h"], "o-", ms=3, label="descenso observado")
    ax[1].plot(m(ag["t_fin"]), ag["explicado_caudal_pct_h"] + ag["explicado_evap_pct_h"], "s-", ms=3,
               label="explicado (evap. + consumo)")
    ax[1].plot(m(ag["t_fin"]), ag["residual_pct_h"], "^-", ms=3, label="residual (no explicado)")
    ax[1].axhline(cfg.umbral_residual_pct_h, color="#b83232", lw=0.8, ls="--", label="umbral residual")
    det = ag[ag["anomalia"] == 1]
    ax[1].set_xlim(left=-1)
    ax[1].plot(m(det["t_fin"]), det["residual_pct_h"], "x", color="#b83232", ms=9, mew=2, label="anomalía detectada")
    ax[1].set_ylabel("%/h")
    ax[1].legend(loc="upper right", fontsize=8)

    ax[2].step(m(df["t"]), df["estado"], where="post", color="#555")
    ax[2].set_yticks([0, 1, 2, 3], ["NORMAL", "VIGILANCIA", "ALERTA", "CRÍTICO"])
    ax[2].set_xlabel("Tiempo (min)")
    ax[2].set_ylabel("Estado ESP32")
    fig.suptitle("Validación del módulo de IA: nivel, balance hídrico y estado (franjas rojas = anomalía real)")
    fig.tight_layout()
    fig.savefig(salida / "validacion_series.png", dpi=130)
    plt.close(fig)

    if mp.empty:
        return
    fig, ax = plt.subplots(figsize=(7, 4))
    for h, g in mp.groupby("horizonte_s"):
        ax.plot(g["ventana_s"], g["mae_theil_sen"], "o-", label=f"Theil-Sen, H={int(h)} s")
        ax.plot(g["ventana_s"], g["mae_persistencia"], "--", color="gray", lw=0.8)
    ax.set_xlabel("Ventana de ajuste (s)")
    ax.set_ylabel("MAE del nivel previsto (%)")
    ax.set_title("Error de predicción vs. ventana (gris: persistencia)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(salida / "validacion_prediccion.png", dpi=130)
    plt.close(fig)


# ------------------------------------------------------------------ reporte
def fmt(x, d=2):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{d}f}"


def tabla_prediccion(mp) -> list[str]:
    if mp.empty:
        return ["Datos insuficientes para el backtest.", ""]
    L = ["| Ventana (s) | Horizonte (s) | n | MAE Theil-Sen (%) | MAE MCO (%) | MAE persistencia (%) | Skill |",
         "|---:|---:|---:|---:|---:|---:|---:|"]
    for _, r in mp.sort_values(["horizonte_s", "ventana_s"]).iterrows():
        L.append(f"| {r.ventana_s:.0f} | {r.horizonte_s:.0f} | {int(r.n)} | {fmt(r.mae_theil_sen)} | {fmt(r.mae_ols)} | "
                 f"{fmt(r.mae_persistencia)} | {fmt(r.skill_vs_persistencia)} |")
    mejor = mp.loc[mp.groupby("horizonte_s")["mae_theil_sen"].idxmin()]
    L += ["", "Mejor ventana por horizonte: " + ", ".join(
        f"H={r.horizonte_s:.0f} s → {r.ventana_s:.0f} s" for _, r in mejor.iterrows()), ""]
    return L


def ic(par, d=2):
    return "—" if not par or par[0] is None else f"[{par[0]:.{d}f}, {par[1]:.{d}f}]"


def seccion_alertas(al: dict, lat: dict | None) -> list[str]:
    L = ["## D. Alertas del dispositivo: falsos positivos y negativos", ""]
    if not al.get("disponible"):
        L += ["Sin columna `estado_esperado` (agréguela a la bitácora de ensayos).", ""]
    else:
        e, m = al["evento"], al["muestra"]
        L += [f"Repeticiones del ensayo: **{al['repeticiones']}** · gracia tras cada cambio de la verdad: "
              f"{al['gracia_s']:.0f} s · IC: Wilson al 95 %.", "",
              "**Por evento** (cada tramo de un ensayo):", "",
              "| | Alertó (estado ≥ umbral) | No alertó |", "|---|---:|---:|",
              f"| **Debía alertar** | {e['vp']} (VP) | {e['fn']} (FN) |",
              f"| **No debía alertar** | {e['fp']} (FP) | {e['vn']} (VN) |", "",
              f"- Sensibilidad: **{fmt(e['sensibilidad'])}** IC {ic(e['ic_sensibilidad'])}",
              f"- Tasa de falsos positivos por tramo: **{fmt(e['tasa_fp'])}** IC {ic(e['ic_tasa_fp'])}",
              f"- Falsas alarmas por hora de operación normal: **{fmt(e['falsas_alarmas_por_hora'])}** "
              f"({e['horas_normales_evaluadas']} h evaluadas)",
              f"- Retardo de la alerta: mediana {fmt(e['retardo_p50_s'], 0)} s · máximo {fmt(e['retardo_max_s'], 0)} s",
              f"- Fallas de sensor reportadas en la lista `falla`: {al['fallas']['detectadas']}/{al['fallas']['total']} tramos", "",
              "**Por muestra** (fuera de la gracia; muestras correlacionadas, IC optimista):", "",
              f"- Tasa de FN: {fmt(m['tasa_fn'], 3)} IC {ic(m['ic_tasa_fn'], 3)} · "
              f"tasa de FP: {fmt(m['tasa_fp'], 3)} IC {ic(m['ic_tasa_fp'], 3)}",
              "- Severidad (filas = esperado, columnas = observado; NORMAL/VIGILANCIA/ALERTA/CRÍTICO): "
              + " · ".join(str(f) for f in m["confusion_4x4"]), ""]
    L += ["## E. Latencia de subida (ESP32 → Gateway)", ""]
    if lat:
        L += [f"Mediana por ventana del p50: **{lat['p50_mediana_ms']:.0f} ms** · del p95: "
              f"{lat['p95_mediana_ms']:.0f} ms · máximo: {lat['max_ms']:.0f} ms ({lat['ventanas']} ventanas).", ""]
    else:
        L += ["No disponible: requiere la hora NTP del ESP32 (`ts_ms`) y la columna `t_recepcion`. "
              "La de bajada se mide con `herramientas/medir_latencia_comando.py`.", ""]
    return L


def escribir_reporte(salida: Path, origen: str, df, mp, mp_est, tu, an, llm, cfg, sintetico: bool,
                     al: dict | None = None, lat: dict | None = None):
    L = ["# Resultados de validación del módulo de IA", ""]
    if sintetico:
        L += ["> **Datos SINTÉTICOS** (simulador). Estos resultados prueban el software; "
              "la validación de la Wiki debe hacerse con datos medidos del prototipo.", ""]
    L += [f"- Origen de los datos: `{origen}`",
          f"- Lecturas: {len(df)} · duración: {(df['t'].iloc[-1] - df['t'].iloc[0]) / 60:.1f} min",
          f"- Ventana de agregación: {cfg.periodo_agregado_s:.0f} s · ventana de tendencia: "
          f"{cfg.ventana_tendencia_s:.0f} s · ventana del balance: {cfg.ventana_anomalia_s:.0f} s", ""]

    L += ["## A. Predicción del nivel", "",
          "Skill = 1 − MAE(Theil-Sen)/MAE(persistencia): > 0 significa que la tendencia aporta "
          "sobre suponer que el nivel no cambia.", ""]
    if mp_est is not None:
        L += ["**Horizontes sin cambio de régimen** (no se cierra/abre una válvula dentro del horizonte):", ""]
        L += tabla_prediccion(mp_est)
        L += ["**Todos los horizontes** (incluye cambios de régimen, imposibles de anticipar):", ""]
    L += tabla_prediccion(mp)
    if len(tu):
        L += ["**Tiempo hasta el umbral** (instantes con cruce previsto en ≤ 15 min que ocurre "
              "dentro del mismo tramo del ensayo):", "",
              "| Umbral (%) | n | Error medio absoluto (s) | Mediana del error (s) |", "|---:|---:|---:|---:|"]
        for u, g in tu.groupby("umbral"):
            L.append(f"| {u:.0f} | {len(g)} | {g['error_s'].abs().mean():.0f} | {g['error_s'].median():.0f} |")
        L.append("")
    L += ["![Predicción](validacion_prediccion.png)", ""]

    L += ["## B. Detección de anomalías (balance hídrico)", ""]
    if an.get("disponible"):
        L += ["| | Predicho anomalía | Predicho normal |", "|---|---:|---:|",
              f"| **Real anomalía** | {an['vp']} | {an['fn']} |",
              f"| **Real normal** | {an['fp']} | {an['vn']} |", "",
              f"Precisión: **{fmt(an['precision'])}** IC {ic(an['ic_precision'])} · "
              f"Sensibilidad: **{fmt(an['sensibilidad'])}** IC {ic(an['ic_sensibilidad'])} · "
              f"F1: **{fmt(an['f1'])}**", "",
              "| Rep. | Evento anómalo | Duración (s) | Retardo de detección (s) |", "|---:|---|---:|---:|"]
        for e in an["eventos"]:
            L.append(f"| {e.get('rep', 1)} | {e['escenario']} | {e['duracion_s']:.0f} | "
                     f"{fmt(e['retardo_s'], 0) if e['retardo_s'] is not None else 'no detectado'} |")
    else:
        L.append("Sin etiquetas de verdad: no se calcularon métricas de detección.")
    L += ["", "![Series](validacion_series.png)", ""]

    L += ["## C. Recomendación con modelo generativo", ""]
    if llm and llm.get("n"):
        L += [f"- Proveedor configurado: `{llm['proveedor']}` · llamadas: {llm['n']}",
              f"- Respuestas del LLM válidas (esquema JSON): **{fmt(llm['pct_respuestas_llm_validas'], 1)} %**",
              f"- Riesgo corregido por el piso de seguridad: {fmt(llm['pct_riesgo_corregido'], 1)} %",
              f"- Respuestas con cifras no soportadas por los datos: {fmt(llm['pct_cifras_no_soportadas'], 1)} %",
              f"- Latencia p50 / p95: {fmt(llm['latencia_p50_s'])} s / {fmt(llm['latencia_p95_s'])} s",
              f"- Consistencia entre repeticiones (mismo riesgo): {fmt(llm['consistencia_media'])}", "",
              "| Escenario | Estado | Anomalía | Fuente | Riesgo | Resumen |", "|---|---|---|---|---|---|"]
        for d in llm["detalle"]:
            if d["rep"] == 0:
                L.append(f"| {d['escenario']} | {d['estado']} | {'sí' if d['anomalia'] else 'no'} | "
                         f"{d['fuente']} | {d['riesgo']} | {d['resumen']} |")
    else:
        L.append("No se ejecutó (use `--llm N --repeticiones R`).")
    L += [""] + seccion_alertas(al or {}, lat)
    (salida / "reporte_validacion.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv")
    ap.add_argument("--sqlite")
    ap.add_argument("--consulta", default="SELECT * FROM lecturas")
    ap.add_argument("--etiquetas")
    ap.add_argument("--env")
    ap.add_argument("--salida", default="resultados_validacion")
    ap.add_argument("--ventanas", default="60,120,180,300,600")
    ap.add_argument("--horizontes", default="60,120,300")
    ap.add_argument("--llm", type=int, default=0, help="n.º de contextos a enviar al LLM (0 = no)")
    ap.add_argument("--repeticiones", type=int, default=3, help="repeticiones por contexto del LLM")
    ap.add_argument("--gracia", type=float, default=180.0,
                    help="segundos tras cada cambio de la verdad que no se cuentan como error (ventana de la tasa)")
    ap.add_argument("--umbral-alerta", type=int, default=1, choices=[1, 2, 3],
                    help="estado mínimo que cuenta como alerta (1 vigilancia, 2 alerta, 3 crítico)")
    ap.add_argument("--sintetico", action="store_true", help="marca el reporte como datos sintéticos")
    args = ap.parse_args()
    if not args.csv and not args.sqlite:
        ap.error("use --csv o --sqlite")

    cfg = cargar_config(args.env)
    salida = Path(args.salida)
    salida.mkdir(parents=True, exist_ok=True)

    df = cargar_datos(args, cfg)
    print(f"{len(df)} lecturas cargadas")

    t, y = serie_filtrada(df, cfg)
    ventanas = [float(v) for v in args.ventanas.split(",")]
    horizontes = [float(h) for h in args.horizontes.split(",")]
    bt = backtest_prediccion(t, y, ventanas, horizontes, regimen=regimen_en(df))
    mp = metricas_prediccion(bt)
    if bt.empty:
        print("AVISO: datos insuficientes para el backtest (se necesita más que la ventana + el horizonte máximos)")
    mp_est = (metricas_prediccion(bt[~bt["cambio_regimen"]])
              if len(bt) and bt["cambio_regimen"].any() else None)
    tu = pd.concat([error_tiempo_a_umbral(t, y, u, cfg.ventana_tendencia_s, regimen=regimen_en(df))
                    for u in (cfg.nivel_alerta_pct, cfg.nivel_critico_pct)], ignore_index=True)
    print("predicción (todos):\n", mp.round(2).to_string(index=False))
    if mp_est is not None:
        print("predicción (sin cambio de régimen):\n", mp_est.round(2).to_string(index=False))

    ag = reproducir(df, cfg)
    an = metricas_anomalias(ag, df)
    print("anomalías:", {k: v for k, v in an.items() if k != "eventos"})
    for e in an.get("eventos", []):
        print("  evento", e)

    al = metricas_alertas(df, args.gracia, args.umbral_alerta)
    if al.get("disponible"):
        print("alertas (evento):", al["evento"])
        print("alertas (muestra):", {k: v for k, v in al["muestra"].items() if k != "confusion_4x4"})
    lat = latencias(ag)

    llm = validar_llm(ag, cfg, args.llm, args.repeticiones) if args.llm > 0 else None
    if llm:
        print("LLM:", {k: v for k, v in llm.items() if k != "detalle"})

    graficar(df, ag, t, y, mp, salida, cfg)
    escribir_reporte(salida, args.csv or args.sqlite, df, mp, mp_est, tu, an, llm, cfg,
                     args.sintetico or ("nivel_real" in df.columns), al, lat)
    ag.to_csv(salida / "agregados_reprocesados.csv", index=False)
    bt.to_csv(salida / "backtest_prediccion.csv", index=False)
    metr = {"prediccion": mp.to_dict("records"), "tiempo_a_umbral": tu.to_dict("records"),
            "anomalias": an, "alertas": al, "latencia_subida": lat, "llm": llm}
    (salida / "metricas.json").write_text(json.dumps(metr, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"reporte en {salida / 'reporte_validacion.md'}")


if __name__ == "__main__":
    main()
