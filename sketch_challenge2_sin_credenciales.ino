/*
  Challenge#2 - Internet de las Cosas
  Sistema IoT de monitoreo y alerta de escasez de agua - Sabana Centro (Cundinamarca)
  Universidad de La Sabana, 2026-2

  * VERSIÓN PARA EL PROTOTIPO FÍSICO - CHALLENGE #2 *

  Cambios respecto al Challenge #1:
  - La medición corre en una tarea FreeRTOS anclada al Core 0, separada del
    hilo principal (loop(), que en ESP32 corre en Core 1). El conteo de
    pulsos de caudal sigue en una ISR.
  - Servidor web embebido en el propio ESP32, conectado a la WLAN local.
    Sirve un tablero de control con valor actual, histórico reciente,
    notificación de eventualidades y botón para silenciar la alarma física.
  - Acceso al tablero protegido con autenticación HTTP Basic.
  - El estado compartido entre la tarea de medición y el servidor web se
    protege con un mutex de FreeRTOS.
  - Sin MQTT y sin dependencias externas: el HTML/CSS/JS del tablero va
    embebido en la memoria del ESP32, por lo que funciona en una WLAN
    sin salida a internet.
  - La evaporacion potencial se calcula con la ecuacion de Priestley-Taylor
    (1972), en mm/dia, en reemplazo del indice heuristico del Challenge #1.
  - La logica de fusion incorpora histeresis: para BAJAR de estado se exige
    superar el umbral por un margen, de modo que la alerta no oscila cuando
    una lectura queda justo sobre el limite.
  - Se agrega un cuarto estado, FALLA, para cuando un sensor no entrega
    lectura valida: el sistema deja de reportar "NORMAL" estando ciego.
  - Se integra el BMP280 para medir la presion atmosferica real, usada en la
    constante psicrometrica de Priestley-Taylor. Si el sensor no responde,
    se usa la presion estimada por altitud (FAO-56) y se indica como estimada.

  Componentes:
  - HC-SR04       : nivel de agua
  - ZJ-S201       : caudal por pulsos (ISR)
  - DHT22         : temperatura y humedad relativa
  - BMP280        : presión atmosférica (I2C, comparte bus con la LCD)
  - LDR           : proxy de radiación solar (divisor de voltaje con 10kΩ)
  - LCD1602 I2C   : visualización in situ, rota entre 3 pantallas
  - LEDs verde/amarillo/rojo + buzzer : alarma física in situ

  Librerías requeridas:
  - "DHT sensor library for ESPx" (beegee-tokyo)
  - "LiquidCrystal I2C" (Frank de Brabander / John Rickman)
  - "Adafruit BMP280 Library" (Adafruit) - instala también Adafruit Unified
    Sensor y Adafruit BusIO como dependencias
  (WiFi.h y WebServer.h vienen incluidas en el core de ESP32 para Arduino)
*/

#include <WiFi.h>
#include <WebServer.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <Adafruit_BMP280.h>
#include "DHTesp.h"

// ---------- Credenciales de la WLAN (proporcionada por las autoridades locales) ----------
// CAMBIAR antes de desplegar. No subir credenciales reales a un repositorio público.
const char* WIFI_SSID     = " ";
const char* WIFI_PASSWORD = " ";

// ---------- Credenciales de acceso al tablero de control ----------
// Restringe el tablero a dispositivos autorizados dentro de la WLAN.
const char* TABLERO_USUARIO = " ";
const char* TABLERO_CLAVE   = " ";

// ---------- Pines ----------
#define TRIG_PIN      5
#define ECHO_PIN      18
#define DHT_PIN       15
#define LDR_PIN       35
#define FLOW_PIN      32
#define LED_VERDE     25
#define LED_AMARILLO  26
#define LED_ROJO      27
#define BUZZER_PIN    33

// Bus I2C compartido: LCD (0x27) y BMP280 (0x76 o 0x77)
#define I2C_SDA_PIN   21
#define I2C_SCL_PIN   22

// ---------- Parámetros físicos del sistema ----------
const float DISTANCIA_VACIO_CM = 12.0;  // lectura con el tanque vacío  -> 0%
const float DISTANCIA_LLENO_CM = 3.8;   // lectura con el nivel máximo  -> 100%

const float NIVEL_ALERTA_PCT  = 30.0;
const float NIVEL_CRITICO_PCT = 15.0;

//const float FACTOR_PULSOS_POR_HZ = 7.5;
const float FACTOR_PULSOS_POR_HZ = 3.88;
const float CAUDAL_ANOMALO_LMIN  = 15.0;

// ---------- Márgenes de histéresis ----------
// Solo se aplican para BAJAR de estado. Entrar en alerta usa el umbral
// nominal; salir de ella exige superarlo por este margen.
const float MARGEN_NIVEL_PCT   = 3.0;   // puntos porcentuales de nivel
const float MARGEN_EVAP_MM_DIA = 0.5;   // mm/día
const float MARGEN_CAUDAL_LMIN = 2.0;   // L/min

// ---------- Parámetros de la ecuación de Priestley-Taylor ----------
// Altitud del sitio de despliegue (embalse del Neusa, Cogua/Tausa).
// Solo se usa como respaldo: si el BMP280 no responde, la presión se
// estima a partir de la altitud con la ecuación de FAO-56.
const float ALTITUD_SITIO_M = 3000.0;

// Albedo de la superficie monitoreada. 0.06 corresponde a agua abierta;
// NO usar el 0.23 de FAO-56, que es para pasto de referencia.
const float ALBEDO_AGUA = 0.06;

// Coeficiente empírico de Priestley-Taylor. 1.26 es el valor original de
// Priestley y Taylor (1972) para superficies saturadas. La literatura
// reporta valores más altos (hasta ~1.74) en climas áridos y semiáridos,
// donde 1.26 subestima la evaporación real. Parámetro a justificar o
// calibrar según las condiciones del sitio.
const float ALFA_PT = 1.26;

// Umbral de demanda evaporativa alta usado por la lógica de fusión (mm/día).
// Es un parámetro de diseño, del mismo tipo que los umbrales de nivel:
// debe calibrarse contra datos del sitio.
const float EVAPORACION_ALTA_MM_DIA = 5.0;

// ---------- Calibración de dos puntos del LDR -> radiación solar (W/m²) ----------
// AJUSTAR con mediciones propias: anotar la lectura de analogRead(LDR_PIN)
// en exterior con sombra/nublado y en exterior con sol directo al mediodía.
// Los valores de W/m² son de referencia estándar para esas condiciones,
// no mediciones hechas con un piranómetro calibrado.
const float ADC_SOMBRA = 170.0;
const float RS_SOMBRA  = 150.0;
const float ADC_SOL    = 3357.0;
const float RS_SOL     = 900.0;

// ---------- Parámetros de muestreo e histórico ----------
const uint32_t INTERVALO_LECTURA_MS   = 2000;  // periodo de la tarea de medición
const uint8_t  MUESTRAS_POR_HISTORICO = 5;     // 1 punto de histórico cada 5 lecturas (10 s)
const uint8_t  HISTORICO_MAX          = 60;    // 60 puntos x 10 s = 10 minutos

DHTesp dht;
LiquidCrystal_I2C lcd(0x27, 16, 2);
Adafruit_BMP280 bmp;
WebServer server(80);

bool bmpOk = false;   // true si el BMP280 respondió en el arranque

// FALLA: al menos un sensor no entrega lectura válida. No es un nivel de
// riesgo hídrico, sino un estado de diagnóstico del propio dispositivo.
enum NivelAlerta { NORMAL = 0, ADVERTENCIA = 1, CRITICO = 2, FALLA = 3 };

// ---------- Estado compartido entre la tarea de medición y el servidor web ----------
struct Lectura {
  float    nivelPct;
  float    tempC;
  float    humedadPct;
  float    presionKPa;
  bool     presionMedida;  // true: BMP280; false: estimada por altitud
  float    radiacionWm2;
  float    evaporacionMMDia;
  float    caudalLmin;
  float    volumenAcumuladoL;
  uint8_t  alerta;
  uint32_t tsSegundos;   // segundos desde el arranque del dispositivo
};

Lectura actual = {};   // todo en cero; alerta = 0 = NORMAL

Lectura historico[HISTORICO_MAX];
uint8_t historicoCantidad = 0;   // cuántas posiciones están llenas
uint8_t historicoInicio   = 0;   // posición más antigua (buffer circular)

bool alarmaSilenciada = false;   // la autoridad silenció la alarma desde el tablero

SemaphoreHandle_t mutexDatos;

// ---------- Conteo de pulsos de caudal (ISR) ----------
portMUX_TYPE muxCaudal = portMUX_INITIALIZER_UNLOCKED;
volatile uint32_t pulsosCaudal = 0;

void IRAM_ATTR isrPulsoCaudal() {
  portENTER_CRITICAL_ISR(&muxCaudal);
  pulsosCaudal++;
  portEXIT_CRITICAL_ISR(&muxCaudal);
}

// ============================================================================
//  PÁGINA DEL TABLERO DE CONTROL (embebida: no requiere internet)
// ============================================================================
const char PAGINA_HTML[] PROGMEM = R"HTMLPAGE(
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Monitoreo Hidrico - Embalse del Neusa</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; padding:16px; font-family: system-ui, Arial, sans-serif;
         background:#101418; color:#e6edf3; }
  h1 { font-size:1.15rem; margin:0 0 4px 0; }
  .sub { font-size:0.8rem; color:#8b949e; margin-bottom:16px; }
  #banner { padding:14px; border-radius:8px; font-weight:bold; text-align:center;
            margin-bottom:16px; font-size:1.05rem; }
  .normal      { background:#1a4d2e; color:#7ee2a8; }
  .advertencia { background:#5c4813; color:#ffd866; }
  .critico     { background:#5c1a1a; color:#ff8a8a; }
  .falla       { background:#3d2a5c; color:#c9a3ff; }
  .sinconexion { background:#30363d; color:#8b949e; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; }
  .card { background:#1a2027; border-radius:8px; padding:12px; }
  .etq { font-size:0.72rem; color:#8b949e; text-transform:uppercase; letter-spacing:0.5px; }
  .val { font-size:1.5rem; font-weight:bold; margin-top:4px; }
  .uni { font-size:0.85rem; color:#8b949e; font-weight:normal; }
  button { width:100%; margin-top:16px; padding:14px; font-size:0.95rem; font-weight:bold;
           border:none; border-radius:8px; background:#2d5f8a; color:#fff; cursor:pointer; }
  button:disabled { background:#30363d; color:#6e7681; cursor:default; }
  h2 { font-size:0.95rem; margin:24px 0 8px 0; }
  table { width:100%; border-collapse:collapse; font-size:0.8rem; }
  th, td { padding:6px 4px; text-align:right; border-bottom:1px solid #262c33; }
  th:first-child, td:first-child { text-align:left; }
  th { color:#8b949e; font-weight:normal; }
</style>
</head>
<body>
  <h1>Monitoreo hidrico - Embalse del Neusa</h1>
  <div class="sub">Tablero de control local &middot; Sabana Centro (Cundinamarca)</div>

  <div id="banner" class="sinconexion">Conectando con el dispositivo...</div>

  <div class="grid">
    <div class="card"><div class="etq">Nivel</div><div class="val"><span id="nivel">--</span><span class="uni"> %</span></div></div>
    <div class="card"><div class="etq">Caudal</div><div class="val"><span id="caudal">--</span><span class="uni"> L/min</span></div></div>
    <div class="card"><div class="etq">Volumen acumulado</div><div class="val"><span id="volumen">--</span><span class="uni"> L</span></div></div>
    <div class="card"><div class="etq">Temperatura</div><div class="val"><span id="temp">--</span><span class="uni"> &deg;C</span></div></div>
    <div class="card"><div class="etq">Humedad relativa</div><div class="val"><span id="humedad">--</span><span class="uni"> %</span></div></div>
    <div class="card"><div class="etq" id="etqPresion">Presion</div><div class="val"><span id="presion">--</span><span class="uni"> kPa</span></div></div>
    <div class="card"><div class="etq">Radiacion solar</div><div class="val"><span id="radiacion">--</span><span class="uni"> W/m2</span></div></div>
    <div class="card"><div class="etq">Evaporacion (P-T)</div><div class="val"><span id="evap">--</span><span class="uni"> mm/dia</span></div></div>
  </div>

  <button id="btnSilenciar" onclick="silenciar()">Silenciar alarma fisica</button>

  <h2>Historico reciente</h2>
  <table>
    <thead><tr><th>Hace</th><th>Nivel %</th><th>Caudal</th><th>Temp</th><th>Hum %</th><th>Estado</th></tr></thead>
    <tbody id="tbody"><tr><td colspan="6">Sin datos aun</td></tr></tbody>
  </table>

<script>
var ESTADOS = ["NORMAL", "ADVERTENCIA", "CRITICO", "FALLA"];
var CLASES  = ["normal", "advertencia", "critico", "falla"];
var ultimoEstado = -1;
var tsActual = 0;

function f(x, d) { return (x === null || x === undefined) ? "--" : x.toFixed(d); }

function hace(ts) {
  var s = tsActual - ts;
  if (s < 60) { return s + "s"; }
  return Math.floor(s / 60) + "m " + (s % 60) + "s";
}

function pintar(d) {
  tsActual = d.ts;
  document.getElementById("nivel").textContent     = f(d.nivel, 0);
  document.getElementById("caudal").textContent    = f(d.caudal, 1);
  document.getElementById("volumen").textContent   = f(d.volumen, 1);
  document.getElementById("temp").textContent      = f(d.temp, 1);
  document.getElementById("humedad").textContent   = f(d.humedad, 0);
  document.getElementById("presion").textContent   = f(d.presion, 1);
  document.getElementById("etqPresion").textContent =
    d.presionMedida ? "Presion" : "Presion (estimada)";
  document.getElementById("radiacion").textContent = f(d.radiacion, 0);
  document.getElementById("evap").textContent      = f(d.evap, 2);

  var b = document.getElementById("banner");
  b.className = CLASES[d.alerta];
  var txt = "Estado: " + ESTADOS[d.alerta];
  if (d.alerta === 3) {
    txt = "FALLA DE SENSOR - revisar el dispositivo en sitio";
  } else if (d.alerta > 0 && d.alerta !== ultimoEstado) {
    txt = "NUEVA ALERTA - " + ESTADOS[d.alerta];
  }
  if (d.silenciada && d.alerta > 0) { txt += " (alarma silenciada)"; }
  b.textContent = txt;
  ultimoEstado = d.alerta;

  var btn = document.getElementById("btnSilenciar");
  btn.disabled = (d.alerta === 0 || d.silenciada);
}

function pedirDatos() {
  fetch("/api/datos").then(function (r) { return r.json(); }).then(pintar)
    .catch(function () {
      var b = document.getElementById("banner");
      b.className = "sinconexion";
      b.textContent = "Sin conexion con el dispositivo";
    });
}

function pedirHistorico() {
  fetch("/api/historico").then(function (r) { return r.json(); }).then(function (h) {
    var tb = document.getElementById("tbody");
    if (!h.length) { tb.innerHTML = "<tr><td colspan='6'>Sin datos aun</td></tr>"; return; }
    var filas = "";
    for (var i = h.length - 1; i >= 0; i--) {
      var p = h[i];
      filas += "<tr><td>" + hace(p.ts) + "</td><td>" + f(p.nivel, 0) + "</td><td>" +
               f(p.caudal, 1) + "</td><td>" + f(p.temp, 1) + "</td><td>" +
               f(p.humedad, 0) + "</td><td>" + ESTADOS[p.alerta] + "</td></tr>";
    }
    tb.innerHTML = filas;
  }).catch(function () {});
}

function silenciar() {
  fetch("/api/silenciar", { method: "POST" }).then(pedirDatos).catch(function () {});
}

pedirDatos();
pedirHistorico();
setInterval(pedirDatos, 2000);
setInterval(pedirHistorico, 10000);
</script>
</body>
</html>
)HTMLPAGE";

// ============================================================================
//  LECTURA DE SENSORES
// ============================================================================
float medirDistanciaCM() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  long duracion = pulseIn(ECHO_PIN, HIGH, 30000); // timeout 30ms (~5 m)
  if (duracion == 0) return -1;
  return duracion * 0.0343 / 2.0;
}

float calcularNivelPct(float distanciaCM) {
  if (distanciaCM < 0) return -1;
  float pct = (DISTANCIA_VACIO_CM - distanciaCM) /
              (DISTANCIA_VACIO_CM - DISTANCIA_LLENO_CM) * 100.0;
  return constrain(pct, 0.0, 100.0);
}

// Convierte los pulsos acumulados desde la última lectura a L/min e integra
// el volumen. El acceso al contador se protege con el mismo spinlock de la ISR.
float leerCaudalLmin(uint32_t intervaloMs, float &volumenAcumuladoL) {
  portENTER_CRITICAL(&muxCaudal);
  uint32_t pulsos = pulsosCaudal;
  pulsosCaudal = 0;
  portEXIT_CRITICAL(&muxCaudal);

  float intervaloSeg = intervaloMs / 1000.0;
  float frecuenciaHz = pulsos / intervaloSeg;
  float caudalLmin   = frecuenciaHz / FACTOR_PULSOS_POR_HZ;

  volumenAcumuladoL += caudalLmin * (intervaloSeg / 60.0);
  return caudalLmin;
}

// Convierte la lectura del ADC a radiación solar incidente (W/m²) mediante
// una interpolación lineal entre los dos puntos de calibración del LDR.
float calcularRadiacionWm2() {
  int lectura = analogRead(LDR_PIN); // 0-4095 en ESP32
  float pendiente = (RS_SOL - RS_SOMBRA) / (ADC_SOL - ADC_SOMBRA);
  float rs = RS_SOMBRA + pendiente * (lectura - ADC_SOMBRA);
  Serial.printf("LDR crudo: %d\n", lectura);  // TEMPORAL: calibración del LDR
  return constrain(rs, 0.0, 1200.0);
}

// Presión atmosférica estimada a partir de la altitud (FAO-56, ec. 7).
// Respaldo para cuando el BMP280 no responde.
float presionEstimadaKPa() {
  return 101.3 * pow((293.0 - 0.0065 * ALTITUD_SITIO_M) / 293.0, 5.26);
}

// Presión atmosférica en kPa. Usa el BMP280 si responde con un valor
// físicamente plausible; si no, cae a la estimación por altitud.
// 'medida' indica cuál de las dos se usó.
float leerPresionKPa(bool &medida) {
  if (bmpOk) {
    float pa = bmp.readPressure();             // Pa
    if (!isnan(pa) && pa > 30000.0 && pa < 110000.0) {
      medida = true;
      return pa / 1000.0;                      // Pa -> kPa
    }
  }
  medida = false;
  return presionEstimadaKPa();
}

// ---------- Evaporación potencial: ecuación de Priestley-Taylor (1972) ----------
//   ET = alfa * [ delta / (delta + gamma) ] * (Rn - G) / lambda      [mm/día]
// Aproximaciones documentadas de esta implementación:
//   - Rn se estima como radiación neta de onda corta, (1 - albedo) * Rs;
//     se desprecia el balance de onda larga.
//   - Rs proviene de un LDR calibrado por dos puntos, no de un piranómetro.
//   - G (flujo de calor del suelo) se asume 0, convención de FAO-56 para
//     cálculos a escala diaria.
//   - Se usa temperatura del aire (DHT22), no de la superficie del agua.
//   - La presión (para gamma) proviene del BMP280, o de la altitud si falla.
float calcularEvaporacionMMDia(float tempC, float radiacionWm2, float presionKPa) {
  // Presión de vapor de saturación (kPa) - ecuación de Tetens
  float es = 0.6108 * exp((17.27 * tempC) / (tempC + 237.3));

  // Pendiente de la curva de presión de vapor de saturación (kPa/°C)
  float delta = (4098.0 * es) / ((tempC + 237.3) * (tempC + 237.3));

  // Constante psicrométrica (kPa/°C)
  float gamma = 0.665e-3 * presionKPa;

  // Calor latente de vaporización (MJ/kg)
  float lambda = 2.501 - 0.002361 * tempC;

  // Radiación: W/m² -> MJ/m²/día  (1 W/m² = 0.0864 MJ/m²/día)
  float rsMJ = radiacionWm2 * 0.0864;
  float rn   = (1.0 - ALBEDO_AGUA) * rsMJ;
  float g    = 0.0;

  float et = ALFA_PT * (delta / (delta + gamma)) * ((rn - g) / lambda);
  return (et < 0.0) ? 0.0 : et;
}

// Lógica de fusión con histéresis. La demanda evaporativa se compara contra
// un umbral en mm/día (Priestley-Taylor) en vez de contra un índice 0-100.
//
// Histéresis: si el estado anterior ya era de alerta, los umbrales se
// desplazan a favor de mantener la alerta. Así, con NIVEL_ALERTA_PCT = 30 y
// un margen de 3, se entra en ADVERTENCIA al bajar de 30% pero solo se
// vuelve a NORMAL al superar 33%, evitando que el estado oscile.
NivelAlerta evaluarAlerta(float nivelPct, float evaporacionMMDia,
                          float caudalLmin, NivelAlerta estadoPrevio) {
  if (nivelPct < 0) return NORMAL; // sin nivel válido la evaluación no aplica

  bool enAlerta = (estadoPrevio == ADVERTENCIA || estadoPrevio == CRITICO);

  float umbralCritico = NIVEL_CRITICO_PCT       + (enAlerta ? MARGEN_NIVEL_PCT   : 0.0);
  float umbralAlerta  = NIVEL_ALERTA_PCT        + (enAlerta ? MARGEN_NIVEL_PCT   : 0.0);
  float umbralEvap    = EVAPORACION_ALTA_MM_DIA - (enAlerta ? MARGEN_EVAP_MM_DIA : 0.0);
  float umbralCaudal  = CAUDAL_ANOMALO_LMIN     - (enAlerta ? MARGEN_CAUDAL_LMIN : 0.0);

  bool consumoAnomalo  = caudalLmin > umbralCaudal;
  bool evaporacionAlta = evaporacionMMDia > umbralEvap;

  if (nivelPct <= umbralCritico ||
      (nivelPct <= umbralAlerta && evaporacionAlta) ||
      (nivelPct <= umbralAlerta && consumoAnomalo)) {
    return CRITICO;
  }
  if (nivelPct <= umbralAlerta || evaporacionAlta || consumoAnomalo) {
    return ADVERTENCIA;
  }
  return NORMAL;
}

// ============================================================================
//  SALIDAS FÍSICAS IN SITU
// ============================================================================
// Los LEDs reflejan siempre el estado real, aunque la alarma esté silenciada:
// silenciar solo apaga el buzzer, nunca la señalización visual.
// FALLA se señaliza encendiendo verde y rojo a la vez: una combinación que
// no corresponde a ningún nivel de riesgo, por lo que es inconfundible en sitio.
void actualizarSalidas(NivelAlerta alerta, bool silenciada) {
  digitalWrite(LED_VERDE,    alerta == NORMAL      || alerta == FALLA);
  digitalWrite(LED_AMARILLO, alerta == ADVERTENCIA);
  digitalWrite(LED_ROJO,     alerta == CRITICO     || alerta == FALLA);

  if (silenciada || alerta == NORMAL) {
    noTone(BUZZER_PIN);
    return;
  }

  if (alerta == ADVERTENCIA) {
    tone(BUZZER_PIN, 1000, 300); // intermitente
  } else if (alerta == CRITICO) {
    tone(BUZZER_PIN, 2200);      // continuo
  } else {
    tone(BUZZER_PIN, 500, 150);  // pitido corto y grave: falla de sensor
  }
}

void mostrarLCD(const Lectura &d) {
  lcd.clear();
  int pantalla = (millis() / 4000) % 3;

  if (pantalla == 0) {
    lcd.setCursor(0, 0);
    lcd.print("N:");
    lcd.print(d.nivelPct, 0);
    lcd.print("% T:");
    lcd.print(d.tempC, 1);
    lcd.print((char)223);
    lcd.print("C");

    lcd.setCursor(0, 1);
    switch (d.alerta) {
      case NORMAL:      lcd.print("Estado: NORMAL"); break;
      case ADVERTENCIA: lcd.print("ALERTA: SEQUIA"); break;
      case CRITICO:     lcd.print("CRITICO!!");      break;
      case FALLA:       lcd.print("FALLA DE SENSOR"); break;
    }

  } else if (pantalla == 1) {
    lcd.setCursor(0, 0);
    lcd.print("Rad:");
    lcd.print(d.radiacionWm2, 0);
    lcd.print(" W/m2");

    // Ej: "H:70% P:70.1kPa" (15 caracteres). Un '*' al final indica
    // que la presión es estimada por altitud (BMP280 sin respuesta).
    lcd.setCursor(0, 1);
    lcd.print("H:");
    lcd.print(d.humedadPct, 0);
    lcd.print("% P:");
    lcd.print(d.presionKPa, 1);
    lcd.print(d.presionMedida ? "kPa" : "*");

  } else {
    lcd.setCursor(0, 0);
    lcd.print("Caudal:");
    lcd.print(d.caudalLmin, 1);
    lcd.print("L/m");

    lcd.setCursor(0, 1);
    lcd.print("Vol.acum:");
    lcd.print(d.volumenAcumuladoL, 1);
    lcd.print("L");
  }
}

// ============================================================================
//  TAREA DE MEDICIÓN (Core 0, separada del hilo principal)
// ============================================================================
// Nota: la LCD y el BMP280 comparten el bus I2C y ambos se usan solo desde
// esta tarea, así que no hay accesos concurrentes al bus.
void tareaMedicion(void *parametros) {
  const TickType_t periodo = pdMS_TO_TICKS(INTERVALO_LECTURA_MS);
  TickType_t ultimoDespertar = xTaskGetTickCount();

  float    volumenAcumuladoL = 0.0;
  uint8_t  contadorHistorico = 0;
  NivelAlerta alertaPrevia   = NORMAL;   // última evaluación válida (para la histéresis)
  uint8_t  alertaMostradaPrev = NORMAL;  // último estado publicado (para rearmar el silencio)

  for (;;) {
    float distancia = medirDistanciaCM();
    float nivelPct  = calcularNivelPct(distancia);

    TempAndHumidity datosDHT = dht.getTempAndHumidity();
    float tempC      = datosDHT.temperature;
    float humedadPct = datosDHT.humidity;

    bool fallaNivel = (nivelPct < 0);
    bool fallaDHT   = (isnan(tempC) || isnan(humedadPct));

    if (fallaDHT) {
      Serial.print("DHT22 SIN LECTURA VALIDA - Estado: ");
      Serial.println(dht.getStatusString());
      tempC      = 0;
      humedadPct = 0;
    }
    if (fallaNivel) {
      Serial.println("HC-SR04 SIN LECTURA VALIDA (eco fuera de rango o sin retorno)");
    }

    bool  presionMedida;
    float presionKPa   = leerPresionKPa(presionMedida);
    float radiacionWm2 = calcularRadiacionWm2();
    float evaporacion  = calcularEvaporacionMMDia(tempC, radiacionWm2, presionKPa);
    float caudalLmin   = leerCaudalLmin(INTERVALO_LECTURA_MS, volumenAcumuladoL);

    // Sin nivel válido no hay forma de evaluar el riesgo hídrico: FALLA directa.
    // Con nivel válido pero DHT caído se evalúa igual, y la falla solo se
    // reporta si no hay una emergencia hídrica confirmada: un CRITICO real
    // nunca debe quedar oculto tras un aviso de diagnóstico.
    // Una falla del BMP280 NO dispara FALLA: hay respaldo por altitud y su
    // efecto sobre la evaporación es pequeño; se indica como "estimada".
    NivelAlerta alerta;
    if (fallaNivel) {
      alerta = FALLA;
    } else {
      NivelAlerta evaluada = evaluarAlerta(nivelPct, evaporacion, caudalLmin, alertaPrevia);
      alertaPrevia = evaluada; // la histéresis se apoya en la última evaluación válida
      alerta = (fallaDHT && evaluada != CRITICO) ? FALLA : evaluada;
    }

    Lectura nueva;
    nueva.nivelPct          = nivelPct;
    nueva.tempC             = tempC;
    nueva.humedadPct        = humedadPct;
    nueva.presionKPa        = presionKPa;
    nueva.presionMedida     = presionMedida;
    nueva.radiacionWm2      = radiacionWm2;
    nueva.evaporacionMMDia  = evaporacion;
    nueva.caudalLmin        = caudalLmin;
    nueva.volumenAcumuladoL = volumenAcumuladoL;
    nueva.alerta            = alerta;
    nueva.tsSegundos        = millis() / 1000;

    bool silenciadaLocal;

    // --- sección crítica: publica el estado para el servidor web ---
    xSemaphoreTake(mutexDatos, portMAX_DELAY);

    // Si el estado de alerta cambió, se rearma la alarma automáticamente:
    // un silenciado no debe ocultar una eventualidad distinta a la silenciada.
    if (alerta != alertaMostradaPrev) {
      alarmaSilenciada = false;
    }
    silenciadaLocal = alarmaSilenciada;

    actual = nueva;

    contadorHistorico++;
    if (contadorHistorico >= MUESTRAS_POR_HISTORICO) {
      contadorHistorico = 0;
      uint8_t pos = (historicoInicio + historicoCantidad) % HISTORICO_MAX;
      historico[pos] = nueva;
      if (historicoCantidad < HISTORICO_MAX) {
        historicoCantidad++;
      } else {
        historicoInicio = (historicoInicio + 1) % HISTORICO_MAX; // descarta el más antiguo
      }
    }
    xSemaphoreGive(mutexDatos);
    // --- fin de la sección crítica ---

    alertaMostradaPrev = alerta;

    actualizarSalidas(alerta, silenciadaLocal);
    mostrarLCD(nueva);

    Serial.printf(
      "Nivel: %.1f%% | Temp: %.1fC | Hum: %.1f%% | Presion: %.2fkPa%s | "
      "Radiacion: %.0fW/m2 | Evaporacion(PT): %.2fmm/dia | Caudal: %.2fL/min | "
      "VolAcum: %.2fL | Alerta: %d\n",
      nivelPct, tempC, humedadPct, presionKPa, presionMedida ? "" : " (estimada)",
      radiacionWm2, evaporacion, caudalLmin, volumenAcumuladoL, alerta);

    vTaskDelayUntil(&ultimoDespertar, periodo);
  }
}

// ============================================================================
//  SERVIDOR WEB EMBEBIDO
// ============================================================================
// Exige autenticación antes de atender cualquier ruta del tablero.
bool autorizado() {
  if (server.authenticate(TABLERO_USUARIO, TABLERO_CLAVE)) return true;
  server.requestAuthentication();
  return false;
}

void handleRaiz() {
  if (!autorizado()) return;
  server.send_P(200, "text/html", PAGINA_HTML);
}

void handleDatos() {
  if (!autorizado()) return;

  Lectura d;
  bool silenciada;
  xSemaphoreTake(mutexDatos, portMAX_DELAY);
  d = actual;
  silenciada = alarmaSilenciada;
  xSemaphoreGive(mutexDatos);

  char json[384];
  snprintf(json, sizeof(json),
    "{\"nivel\":%.1f,\"caudal\":%.2f,\"volumen\":%.2f,\"temp\":%.1f,"
    "\"humedad\":%.1f,\"presion\":%.2f,\"presionMedida\":%s,"
    "\"radiacion\":%.0f,\"evap\":%.2f,\"alerta\":%u,"
    "\"silenciada\":%s,\"ts\":%u}",
    d.nivelPct, d.caudalLmin, d.volumenAcumuladoL, d.tempC,
    d.humedadPct, d.presionKPa, d.presionMedida ? "true" : "false",
    d.radiacionWm2, d.evaporacionMMDia, d.alerta,
    silenciada ? "true" : "false", d.tsSegundos);

  server.send(200, "application/json", json);
}

void handleHistorico() {
  if (!autorizado()) return;

  String json;
  json.reserve(HISTORICO_MAX * 90 + 8);
  json = "[";

  xSemaphoreTake(mutexDatos, portMAX_DELAY);
  for (uint8_t i = 0; i < historicoCantidad; i++) {
    const Lectura &p = historico[(historicoInicio + i) % HISTORICO_MAX];
    char item[110];
    snprintf(item, sizeof(item),
      "%s{\"ts\":%u,\"nivel\":%.1f,\"caudal\":%.2f,\"temp\":%.1f,"
      "\"humedad\":%.1f,\"alerta\":%u}",
      (i == 0) ? "" : ",", p.tsSegundos, p.nivelPct, p.caudalLmin,
      p.tempC, p.humedadPct, p.alerta);
    json += item;
  }
  xSemaphoreGive(mutexDatos);

  json += "]";
  server.send(200, "application/json", json);
}

// Silencia el buzzer sin apagar los LEDs. Se rearma solo cuando cambia el estado.
void handleSilenciar() {
  if (!autorizado()) return;

  xSemaphoreTake(mutexDatos, portMAX_DELAY);
  alarmaSilenciada = true;
  xSemaphoreGive(mutexDatos);

  noTone(BUZZER_PIN);
  server.send(200, "application/json", "{\"ok\":true}");
}

void handleNoEncontrado() {
  server.send(404, "text/plain", "Recurso no encontrado");
}

void conectarWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  WiFi.setSleep(false); // mejora la latencia de respuesta del servidor web

  Serial.print("Conectando a la WLAN");
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Conectando WLAN");

  uint8_t intentos = 0;
  while (WiFi.status() != WL_CONNECTED && intentos < 40) { // hasta 20 s
    delay(500);
    Serial.print(".");
    intentos++;
  }
  Serial.println();

  lcd.clear();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("Conectado. Tablero disponible en: http://");
    Serial.println(WiFi.localIP());
    lcd.setCursor(0, 0);
    lcd.print("Tablero en:");
    lcd.setCursor(0, 1);
    lcd.print(WiFi.localIP());
  } else {
    // El dispositivo sigue midiendo y alarmando in situ aunque no haya WLAN.
    Serial.println("Sin WLAN. El monitoreo local sigue activo.");
    lcd.setCursor(0, 0);
    lcd.print("Sin WLAN");
    lcd.setCursor(0, 1);
    lcd.print("Monitoreo local");
  }
  delay(3000);
  lcd.clear();
}

// Inicializa el BMP280 probando sus dos direcciones I2C posibles (0x76/0x77)
// y los dos IDs de chip (0x58 = BMP280, 0x60 = BME280, que muchas veces se
// vende rotulado como BMP280; para presión ambos son compatibles).
bool iniciarBMP280() {
  const uint8_t direcciones[] = {0x76, 0x77};
  const uint8_t chipIds[]     = {0x58, 0x60};
  for (uint8_t dir : direcciones) {
    for (uint8_t id : chipIds) {
      if (bmp.begin(dir, id)) {
        Serial.printf("BMP280 detectado en 0x%02X (chip ID 0x%02X).\n", dir, id);
        bmp.setSampling(Adafruit_BMP280::MODE_NORMAL,
                        Adafruit_BMP280::SAMPLING_X2,     // temperatura
                        Adafruit_BMP280::SAMPLING_X16,    // presión
                        Adafruit_BMP280::FILTER_X16,
                        Adafruit_BMP280::STANDBY_MS_500);
        return true;
      }
    }
  }
  Serial.println("BMP280 NO DETECTADO: se usara la presion estimada por altitud.");
  return false;
}

// ============================================================================
//  SETUP / LOOP
// ============================================================================
void setup() {
  Serial.begin(115200);

  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  pinMode(LED_VERDE, OUTPUT);
  pinMode(LED_AMARILLO, OUTPUT);
  pinMode(LED_ROJO, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(LDR_PIN, INPUT);

  pinMode(FLOW_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(FLOW_PIN), isrPulsoCaudal, FALLING);

  dht.setup(DHT_PIN, DHTesp::DHT22);

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0);
  lcd.print("Iniciando...");
  lcd.setCursor(0, 1);
  lcd.print("Reto IoT - Agua");

  bmpOk = iniciarBMP280();
  delay(2000);

  mutexDatos = xSemaphoreCreateMutex();

  conectarWiFi();

  server.on("/", HTTP_GET, handleRaiz);
  server.on("/api/datos", HTTP_GET, handleDatos);
  server.on("/api/historico", HTTP_GET, handleHistorico);
  server.on("/api/silenciar", HTTP_POST, handleSilenciar);
  server.onNotFound(handleNoEncontrado);
  server.begin();
  Serial.println("Servidor web embebido iniciado en el puerto 80.");

  // La medición se ancla al Core 0; loop() (servidor web) corre en el Core 1.
  xTaskCreatePinnedToCore(
    tareaMedicion,     // función de la tarea
    "medicion",        // nombre
    4096,              // tamaño de pila (bytes)
    NULL,              // parámetros
    1,                 // prioridad
    NULL,              // handle
    0                  // núcleo 0
  );
}

void loop() {
  server.handleClient();

  // Reconexión no bloqueante de la WLAN: la medición y la alarma física
  // siguen funcionando en la otra tarea aunque la red esté caída.
  static uint32_t ultimaRevisionWiFi = 0;
  if (millis() - ultimaRevisionWiFi > 10000) {
    ultimaRevisionWiFi = millis();
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("WLAN caida, reintentando...");
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    }
  }

  delay(2); // cede tiempo de CPU al scheduler de FreeRTOS
}
