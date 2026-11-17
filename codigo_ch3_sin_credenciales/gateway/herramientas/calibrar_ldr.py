"""
Calibración del LDR contra un instrumento de referencia (corrección del Ch2:
"calibrar la irradiancia contra un instrumento, no contra valores de literatura").

Entrada: CSV con lecturas SIMULTÁNEAS del LDR y de la referencia. Cada fila es
el promedio de unos 30 s en una condición de luz distinta (sombra, nublado,
sol parcial, sol directo, distintas horas). Se recomiendan al menos 10 filas.

  Columnas: adc, ref_wm2                  (solarímetro / piranómetro en W/m²)
       o:   adc, ref_isc_ma  + --isc-stc-ma X
            (celda fotovoltaica de referencia: G = 1000 W/m² · Isc / Isc_STC,
             Isc medida con el multímetro en modo corriente, en cortocircuito)

Se ajustan tres modelos y se elige el de menor error en validación cruzada
dejando uno fuera (LOO), para no premiar al modelo que solo memoriza los puntos:
  - lineal:   G = a + b·ADC                    (lo que hacía el Ch2 con 2 puntos)
  - potencia: G = a·ADC^b
  - fisico:   G = a·R_ldr^b,  R_ldr = R_fijo·(4095/ADC − 1)
              (LDR entre 3.3 V y el pin, resistencia fija a GND; R_ldr ∝ G^−γ)

Salida: la tabla CAL_LDR_ADC / CAL_LDR_WM2 lista para pegar en el firmware, el
error del ajuste (R², RMSE, LOO-RMSE) y una gráfica.

Uso:
  python herramientas/calibrar_ldr.py --csv calibracion_ldr.csv --puntos 8
  python herramientas/calibrar_ldr.py --csv cal.csv --isc-stc-ma 120 --grafica cal.png
"""
from __future__ import annotations

import argparse
import csv
import math
import sys

import numpy as np


def cargar(ruta: str, isc_stc_ma: float | None) -> tuple[np.ndarray, np.ndarray]:
    adc, ref = [], []
    with open(ruta, encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            a = float(fila["adc"])
            if "ref_wm2" in fila and fila["ref_wm2"] not in ("", None):
                g = float(fila["ref_wm2"])
            elif "ref_isc_ma" in fila:
                if not isc_stc_ma:
                    sys.exit("Con ref_isc_ma hay que indicar --isc-stc-ma (corriente de cortocircuito a 1000 W/m²)")
                g = 1000.0 * float(fila["ref_isc_ma"]) / isc_stc_ma
            else:
                sys.exit("El CSV debe tener la columna ref_wm2 o ref_isc_ma")
            adc.append(a)
            ref.append(g)
    x, y = np.array(adc), np.array(ref)
    orden = np.argsort(x)
    return x[orden], y[orden]


def r_ldr(adc: np.ndarray, r_fijo: float) -> np.ndarray:
    adc = np.clip(adc, 1, 4094)
    return r_fijo * (4095.0 / adc - 1.0)


class Modelo:
    def __init__(self, nombre: str, r_fijo: float):
        self.nombre, self.r_fijo, self.p = nombre, r_fijo, None

    def _x(self, adc):
        return r_ldr(adc, self.r_fijo) if self.nombre == "fisico" else adc

    def ajustar(self, adc, g):
        x = self._x(adc)
        if self.nombre == "lineal":
            self.p = np.polyfit(x, g, 1)
        else:   # potencia y físico: recta en escala log-log
            ok = (x > 0) & (g > 0)
            self.p = np.polyfit(np.log(x[ok]), np.log(g[ok]), 1)
        return self

    def predecir(self, adc):
        x = self._x(np.asarray(adc, dtype=float))
        if self.nombre == "lineal":
            return np.polyval(self.p, x)
        return np.exp(np.polyval(self.p, np.log(np.maximum(x, 1e-9))))


def metricas(modelo_nombre: str, adc, g, r_fijo: float) -> dict:
    m = Modelo(modelo_nombre, r_fijo).ajustar(adc, g)
    pred = m.predecir(adc)
    res = g - pred
    ss_tot = float(np.sum((g - g.mean()) ** 2))
    loo = []
    for i in range(len(adc)):
        mask = np.arange(len(adc)) != i
        mi = Modelo(modelo_nombre, r_fijo).ajustar(adc[mask], g[mask])
        loo.append(float(g[i] - mi.predecir([adc[i]])[0]))
    return {
        "modelo": m,
        "r2": 1 - float(np.sum(res ** 2)) / ss_tot if ss_tot else float("nan"),
        "rmse": float(np.sqrt(np.mean(res ** 2))),
        "loo_rmse": float(np.sqrt(np.mean(np.square(loo)))),
    }


def tabla(modelo: Modelo, adc_min: float, adc_max: float, n: int) -> tuple[np.ndarray, np.ndarray, float]:
    """Puntos de la tabla del firmware. Se eligen de forma voraz: se parte de los
    extremos y se agrega, una a una, la abscisa donde la interpolación lineal se
    aleja más del modelo continuo (más puntos donde la curva se dobla). Devuelve
    también el error máximo de interpolación que queda."""
    fino = np.linspace(adc_min, adc_max, 2000)
    modelo_fino = np.clip(modelo.predecir(fino), 0, 1400)
    idx = [0, len(fino) - 1]
    while len(idx) < n:
        idx.sort()
        err = np.abs(np.interp(fino, fino[idx], modelo_fino[idx]) - modelo_fino)
        idx.append(int(np.argmax(err)))
    idx = sorted(set(idx))
    xs, ys = np.round(fino[idx]), modelo_fino[idx]
    err = float(np.max(np.abs(np.interp(fino, xs, ys) - modelo_fino)))
    return xs, ys, err


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--isc-stc-ma", type=float, help="Isc de la celda de referencia a 1000 W/m² (mA)")
    ap.add_argument("--r-fijo", type=float, default=10_000.0, help="resistencia fija del divisor del LDR (Ω)")
    ap.add_argument("--puntos", type=int, default=8, help="puntos de la tabla del firmware")
    ap.add_argument("--modelo", choices=["auto", "lineal", "potencia", "fisico"], default="auto")
    ap.add_argument("--grafica", help="ruta PNG de la gráfica")
    args = ap.parse_args()

    adc, g = cargar(args.csv, args.isc_stc_ma)
    if len(adc) < 5:
        sys.exit(f"Solo hay {len(adc)} puntos; se necesitan al menos 5 (recomendado ≥ 10)")

    res = {n: metricas(n, adc, g, args.r_fijo) for n in ("lineal", "potencia", "fisico")}
    print(f"{len(adc)} puntos de calibración, ADC {adc.min():.0f}–{adc.max():.0f}, "
          f"referencia {g.min():.0f}–{g.max():.0f} W/m²\n")
    print(f"{'modelo':10s} {'R²':>7s} {'RMSE':>9s} {'LOO-RMSE':>10s}")
    for n, r in res.items():
        print(f"{n:10s} {r['r2']:7.3f} {r['rmse']:7.1f} W/m² {r['loo_rmse']:7.1f} W/m²")

    elegido = min(res, key=lambda n: res[n]["loo_rmse"]) if args.modelo == "auto" else args.modelo
    m = res[elegido]["modelo"]
    xs, ys, err_tabla = tabla(m, adc.min(), adc.max(), args.puntos)
    print(f"\nModelo elegido: {elegido} (menor error en validación cruzada)" if args.modelo == "auto"
          else f"\nModelo: {elegido}")
    print(f"Error máximo de la tabla de {len(xs)} puntos frente al modelo: {err_tabla:.1f} W/m²")
    print("Fuera del rango calibrado el firmware extrapola: calibre con días de sol pleno y con sombra.\n")
    print("// ---- Pegar en sketch_challenge3.ino (generado por calibrar_ldr.py) ----")
    print(f"// Modelo {elegido}: R² = {res[elegido]['r2']:.3f}, RMSE = {res[elegido]['rmse']:.1f} W/m², "
          f"LOO-RMSE = {res[elegido]['loo_rmse']:.1f} W/m², n = {len(adc)}")
    print(f"const uint8_t N_CAL_LDR = {len(xs)};")
    print("const float   CAL_LDR_ADC[N_CAL_LDR] = {" + ", ".join(f"{x:.0f}.0" for x in xs) + "};")
    print("const float   CAL_LDR_WM2[N_CAL_LDR] = {" + ", ".join(f"{y:.1f}" for y in ys) + "};")
    print("#define CALIBRACION_LDR_CON_INSTRUMENTO 1")

    if args.grafica:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7, 4.5))
        ax.plot(adc, g, "o", color="#333", label="mediciones (referencia)")
        fino = np.linspace(adc.min(), adc.max(), 400)
        for n, r in res.items():
            ax.plot(fino, r["modelo"].predecir(fino), lw=1.2,
                    label=f"{n} (LOO-RMSE {r['loo_rmse']:.0f} W/m²)")
        ax.plot(xs, ys, "s--", color="#b83232", ms=5, lw=0.8, label="tabla del firmware")
        ax.set_xlabel("ADC del LDR (0–4095)")
        ax.set_ylabel("Irradiancia de referencia (W/m²)")
        ax.set_title("Calibración del LDR contra instrumento")
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(args.grafica, dpi=130)
        print(f"\nGráfica: {args.grafica}")


if __name__ == "__main__":
    main()
