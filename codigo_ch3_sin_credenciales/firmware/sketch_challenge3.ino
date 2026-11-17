/*
  Challenge#3 - Internet de las Cosas
  Sistema IoT de monitoreo y alerta de escasez de agua - Sabana Centro (Cundinamarca)
  Universidad de La Sabana, 2026-2

  * FIRMWARE DEL DISPOSITIVO IoT (ESP32) - CHALLENGE #3 *
  Responsable de la capa: Rafael Alejandro Rincón

  Contrato MQTT: el acordado por el equipo (sección 2.2 de la wiki)
    neusa/estacion01/telemetria   ESP32 -> Gateway   QoS 1, sin retener
    neusa/estacion01/comando      Gateway -> ESP32   QoS 1, sin retener
    neusa/estacion01/estado       LWT                QoS 1, RETENIDO ("online"/"offline")
  Extensiones acordadas con el equipo (no rompen el contrato):
    neusa/estacion01/comando/ack  ESP32 -> Gateway   acuse de cada comando (latencia de bajada)
    neusa/estacion01/ia           Gateway -> ESP32   recomendación de IA para el tablero local
    neusa/estacion01/historico    ESP32 -> Gateway   agregados de 5 min del periodo sin conexión
    Campos extra en la telemetría: seq, ts_ms, nivel_pct, altura_cm, tasa_cmh,
    tendencia_24h_cmh, silenciada

  Cambios respecto al Challenge #2:
  - Cliente MQTT con la librería nativa del ESP32 (esp-mqtt, incluida en el
    core): permite publicar con QoS 1, tiene "outbox" propio que reenvía lo no
    confirmado al reconectar, LWT y reconexión automática. (PubSubClient solo
    publica con QoS 0, por eso no se usó.)
  - La transmisión corre en su propia tarea FreeRTOS (tareaTransmision) y la
    medición en otra (tareaMedicion), independientes del hilo principal. Se
    comunican por una cola: la medición y la alarma física nunca esperan a la red.
  - Se CONSERVA el servidor web local del Ch2 (loop()).
  - Escala de estado del contrato: 0 normal, 1 vigilancia, 2 alerta, 3 crítico.
    Las fallas de sensor van aparte, en la lista "falla".
  - Desactivación remota: {"accion":"silenciar_alarma","hasta":<epoch s>} y
    {"accion":"reactivar_alarma"}. Un estado más grave que el silenciado
    reactiva el buzzer de inmediato (seguridad).
  - Dos tasas de cambio del nivel, en cm/h (negativas = el agua baja):
      tasa_cmh          ventana de 5 min a 2 s: detecta EVENTOS bruscos (extracción,
                        fuga, compuerta abierta). Umbral de alarma: descenso > 2 cm/h.
      tendencia_24h_cmh 288 agregados de 5 min (24 h): descenso natural del embalse;
                        el buffer se guarda en NVS cada 30 min y sobrevive reinicios.
  - El ESP32 mantiene la serie continua aunque la Raspberry Pi esté apagada: al
    reconectar publica los agregados de 5 min del periodo sin cobertura.
  - Correcciones pedidas en el Ch2: mediana
    de 5 disparos del HC-SR04, divisores de tensión en ECHO y caudal, histéresis
    por condición, filtro de la radiación, calibración del LDR por tabla.

  Componentes:
  - HC-SR04       : nivel de agua (ECHO por divisor 1k/2k -> 3.3 V)
  - ZJ-S201       : caudal por pulsos (ISR) (salida por divisor 10k/20k -> 3.3 V)
  - DHT22         : temperatura y humedad relativa
  - BMP280        : presión atmosférica (I2C, comparte bus con la LCD)
  - LDR           : proxy de radiación solar (divisor de voltaje con 10kΩ)
  - LCD1602 I2C   : visualización in situ, rota entre 4 pantallas
  - LEDs verde/amarillo/rojo + buzzer : alarma física in situ

  Librerías requeridas (Gestor de librerías del IDE de Arduino):
  - "DHT sensor library for ESPx" (beegee-tokyo)
  - "LiquidCrystal I2C" (Frank de Brabander / John Rickman)
  - "Adafruit BMP280 Library" (Adafruit) + Adafruit Unified Sensor + Adafruit BusIO
  - "ArduinoJson" (Benoit Blanchon), versión 7.x        <- NUEVA en Ch3
  (WiFi.h, WebServer.h y mqtt_client.h vienen incluidas en el core ESP32 3.x)
*/

#include <WiFi.h>
#include <WebServer.h>
#include <Wire.h>
#include <time.h>
#include <sys/time.h>
#include <LiquidCrystal_I2C.h>
#include <Adafruit_BMP280.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include "mqtt_client.h"
#include "DHTesp.h"

// Declaraciones adelantadas: el IDE de Arduino genera los prototipos de las
// funciones al inicio del archivo, antes de que estas estructuras existan.
// Sin esto, la compilación falla con "'Agregado5m' does not name a type".
struct Agregado5m;
struct EstadoFusion;

// ---------- Credenciales de la WLAN (proporcionada por las autoridades locales) ----------
// CAMBIAR antes de desplegar. No subir credenciales reales a un repositorio público.
const char* WIFI_SSID     = " ";   // la misma WLAN de la Raspberry Pi
const char* WIFI_PASSWORD = " ";

// ---------- Credenciales de acceso al tablero de control local ----------
const char* TABLERO_USUARIO = " ";
const char* TABLERO_CLAVE   = " ";

// ---------- Broker MQTT local (Mosquitto en la Raspberry Pi / Gateway) ----------
// Se intenta primero por nombre (mDNS, hostname de la Pi = "iotpi"); si no
// resuelve, se usa la IP de respaldo. Verificar la IP en la Pi con `hostname -I`.
const char*    MQTT_HOST           = "iotpi.local";
const char*    MQTT_HOST_RESPALDO  = " ";   // IP de la Raspberry Pi en la WLAN
const uint16_t MQTT_PUERTO  = 1883;
const char*    MQTT_USUARIO = " ";          // usuario del passwd de Mosquitto (" " = sin usuario)
const char*    MQTT_CLAVE   = " ";

// Raíz de los tópicos (contrato del equipo).
const char*    MQTT_BASE    = "neusa/estacion01";

// Servidores NTP públicos: la WLAN del laboratorio tiene salida a internet.
const char*    NTP_1        = "pool.ntp.org";
const char*    NTP_2        = "time.google.com";

// ---------- Pines ----------
// HC-SR04 y ZJ-S201 se alimentan a 5 V: sus salidas llegan al ESP32 a través
// de divisores de tensión (ver sección 3.1.1 de la wiki).
#define TRIG_PIN      5     // salida 3.3 V del ESP32: el HC-SR04 la acepta como alto
#define ECHO_PIN      18    // ECHO (5 V) -> R1=1kΩ -> GPIO18 -> R2=2kΩ -> GND  (3.33 V)
#define DHT_PIN       15
#define LDR_PIN       35
#define FLOW_PIN      32    // salida (5 V) -> R1=10kΩ -> GPIO32 -> R2=20kΩ -> GND (3.33 V)
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
const float ALTURA_UTIL_CM     = DISTANCIA_VACIO_CM - DISTANCIA_LLENO_CM;

const float NIVEL_ALERTA_PCT  = 30.0;
const float NIVEL_CRITICO_PCT = 15.0;

const float FACTOR_PULSOS_POR_HZ = 3.88;
const float CAUDAL_ANOMALO_LMIN  = 15.0;

// ---------- Márgenes de histéresis ----------
const float MARGEN_NIVEL_PCT   = 3.0;   // puntos porcentuales de nivel
const float MARGEN_EVAP_MM_DIA = 0.5;   // mm/día
const float MARGEN_CAUDAL_LMIN = 2.0;   // L/min
const float MARGEN_TEMP_C      = 1.0;   // °C

// ---------- Parámetros de la ecuación de Priestley-Taylor ----------
const float ALTITUD_SITIO_M = 3000.0;
const float ALBEDO_AGUA = 0.06;
const float ALFA_PT = 1.26;
const float EVAPORACION_ALTA_MM_DIA = 5.0;

// ---------- Tasa de cambio del nivel para detectar EVENTOS (alarma) ----------
// Pendiente por mínimos cuadrados de la ALTURA del agua (cm) sobre una ventana
// deslizante. La desviación de la pendiente estimada es
//     σ_b = σ_n / sqrt( Σ (t_i - t̄)² ),   Σ (t_i - t̄)² = n (n² - 1) Δt² / 12
// Para que el umbral de 2 cm/h quede a ~4σ (σ_b ≈ 0.5 cm/h), con Δt = 2 s:
//     σ_n = 0.16 cm (tanque quieto del laboratorio) -> n ≈ 150 (5 min)   <- prototipo
//     σ_n = 1.0  cm (embalse con oleaje)            -> n ≈ 540 (18 min)  <- campo
// Una ventana más larga que la necesaria retrasa la alarma y la arrastra
// después de que el evento termina (en las pruebas, 15 min daban 5 falsas
// alarmas de 10 tramos normales). En campo hay que medir σ_n y ajustar la ventana.
// Propósito: el descenso NATURAL de un embalse (~0.1 cm/h) no se ve en esta
// escala; esta tasa detecta eventos bruscos (extracción, fuga, compuerta
// abierta), que mueven centímetros por hora. El descenso natural lo da la
// tendencia de 24 h (más abajo).
const uint16_t VENTANA_TASA_MUESTRAS  = 150;    // 150 x 2 s = 5 min (prototipo); 540 en campo
const uint16_t MIN_MUESTRAS_TASA      = 150;    // solo con la ventana completa
const float    TASA_EVENTO_CMH        = 2.0;    // descenso (cm/h) que se considera evento
const float    MARGEN_TASA_CMH        = 0.5;    // histéresis: sale con descenso < 1.5 cm/h

// ---------- Agregados de 5 min y tendencia de 24 h ----------
// El ESP32 puede quedarse encendido y la Raspberry Pi no: la serie continua
// vive aquí. Cada 5 min se cierra un agregado (medias de las variables); los
// 288 últimos (24 h, ~12 kB de RAM) dan la tendencia de 24 h por mínimos
// cuadrados (σ ≈ 0.01-0.02 cm/h con oleaje de ±1 cm) y alimentan el reenvío
// del periodo sin conexión.
const uint32_t PERIODO_AGREGADO_MS     = 300000UL;   // 5 min
const uint16_t AGREGADOS_MAX           = 288;        // 24 h
const uint16_t MIN_AGREGADOS_TENDENCIA = 72;         // la tendencia se reporta con >= 6 h
const uint32_t PERIODO_NVS_MS          = 1800000UL;  // guardar en flash cada 30 min (desgaste)
const uint8_t  AGREGADOS_POR_MENSAJE   = 8;          // reenvío: ~1.5 kB por mensaje

// "Calor extremo" para la regla de fusión descenso + calor (máxima media en
// Zipaquirá/Cogua ≈ 19-20 °C; 24 °C ya es un día anómalamente cálido).
const float TEMP_CALOR_C = 24.0;

// ---------- Calibración del LDR -> radiación solar (W/m²) ----------
// Observación del Ch2: "calibrar la irradiancia contra un instrumento". Tabla de
// N puntos (ADC, W/m²) con interpolación por tramos, generada por
// gateway/herramientas/calibrar_ldr.py. Los valores de abajo son los 2 puntos
// de literatura del Ch2, porque la calibración con instrumento no se alcanzó a
// hacer; la salida de calibrar_ldr.py es la tabla que los reemplaza.
const uint8_t N_CAL_LDR = 2;
const float   CAL_LDR_ADC[N_CAL_LDR] = {170.0, 3357.0};   // ordenados de menor a mayor
const float   CAL_LDR_WM2[N_CAL_LDR] = {150.0,  900.0};
#define CALIBRACION_LDR_CON_INSTRUMENTO 0   // poner 1 cuando la tabla venga del instrumento
const int     LDR_ADC_SATURADO = 4090;      // ADC >= esto: LDR en corto o saturado -> falla "ldr"

// Filtro exponencial de la radiación: el ruido del LDR (±15 W/m²) movía la
// evaporación ±0.4 mm/día y hacía oscilar el estado. α = 0.1 -> τ ≈ 20 s.
const float ALFA_EMA_RADIACION = 0.1;

// ---------- Rangos válidos del contrato (el ESP32 no envía valores imposibles) ----------
const float RAD_MAX_WM2 = 1200.0;

// ---------- Muestreo e histórico ----------
const uint32_t INTERVALO_LECTURA_MS   = 2000;  // periodo de la tarea de medición
const uint8_t  MUESTRAS_POR_HISTORICO = 5;     // 1 punto de histórico cada 5 lecturas (10 s)
const uint8_t  HISTORICO_MAX          = 60;    // 60 puntos x 10 s = 10 minutos
const uint8_t  DISPAROS_NIVEL         = 5;     // disparos del HC-SR04 por lectura (mediana)

// ---------- MQTT ----------
const uint8_t  COLA_TX_MAX         = 30;     // desacopla la medición de la red (60 s)
const int      MQTT_KEEPALIVE_S    = 15;     // el broker detecta la caída en ~22 s y publica el LWT
const int      MQTT_REINTENTO_MS   = 5000;
const int      MQTT_BUFFER_BYTES   = 2048;   // la recomendación de IA supera 1 kB
const uint32_t MQTT_OUTBOX_BYTES   = 32768;  // mensajes QoS 1 sin confirmar (~100 lecturas)

// ---------- Silenciado de la alarma ----------
const uint32_t SILENCIO_DEFECTO_S  = 1800;   // botón local, o comando sin "hasta" o sin hora NTP
const uint32_t SILENCIO_MAXIMO_S   = 86400;  // nunca más de 24 h

#define DEBUG_LDR 0   // 1: imprime la lectura cruda del LDR (calibración)

// Observación del Ch2: "medir latencia y consumo". El ahorro de energía del WiFi
// reduce el consumo pero aumenta la latencia; se deja como parámetro para medir
// ambos modos (página 10 de la wiki).
const bool WIFI_AHORRO_ENERGIA = false;

DHTesp dht;
LiquidCrystal_I2C lcd(0x27, 16, 2);
Adafruit_BMP280 bmp;
WebServer server(80);
esp_mqtt_client_handle_t clienteMqtt = nullptr;

bool bmpOk = false;

// Escala de estado del contrato del equipo
enum Estado { NORMAL = 0, VIGILANCIA = 1, ALERTA = 2, CRITICO = 3 };
const char* NOMBRE_ESTADO[] = {"NORMAL", "VIGILANCIA", "ALERTA", "CRITICO"};

// Fallas de sensor (bits internos; en el JSON van como lista de nombres)
#define FALLA_NIVEL   0x01
#define FALLA_DHT     0x02
#define FALLA_BMP     0x04
#define FALLA_LDR     0x08
// "caudal": el contrato lo contempla, pero un caudalímetro dañado no se
// distingue de un caudal de 0 desde el ESP32. Esa inconsistencia (el nivel baja
// y el caudal no registra salida) la detecta el balance hídrico del Gateway.

// ---------- Estado compartido ----------
struct Lectura {
  uint32_t seq;               // número de secuencia (detecta pérdidas en el Gateway)
  float    nivelPct;          // NAN si el HC-SR04 falla
  float    distanciaCM;       // NAN si el HC-SR04 falla  (= nivel_cm del contrato)
  float    alturaCM;          // altura del agua sobre el fondo útil (cm); NAN si falla el nivel
  float    tasaCmh;           // tasa de eventos (cm/h, negativa = baja); NAN en los primeros 5 min
  float    tendencia24hCmh;   // tendencia de 24 h (cm/h); NAN con menos de 6 h de datos
  float    tempC;             // NAN si el DHT22 falla
  float    humedadPct;        // NAN si el DHT22 falla
  float    presionKPa;        // NAN si el BMP280 falla (internamente se usa la estimada)
  float    radiacionWm2;      // NAN si el LDR falla
  float    evaporacionMMDia;  // TASA instantánea en mm/día (no acumulado); NAN si falta T o radiación
  float    caudalLmin;
  float    volumenAcumuladoL;
  uint8_t  estado;
  bool     silenciada;
  uint8_t  fallas;
  uint32_t tsSegundos;        // segundos desde el arranque (histórico local)
  uint64_t epochMs;           // hora absoluta (ms) si hay NTP; 0 si no
};

Lectura actual = {};
Lectura historico[HISTORICO_MAX];
uint8_t historicoCantidad = 0;
uint8_t historicoInicio   = 0;

// Silenciado de la alarma (protegido por mutexDatos)
bool     silencioActivo  = false;
uint32_t silencioFinMs   = 0;        // millis() en que vence
uint8_t  silencioEstado  = NORMAL;   // estado que se silenció; uno más grave lo cancela

// Recomendación del módulo de IA recibida desde el Gateway
char     iaRiesgo[16]   = "";
char     iaResumen[220] = "";
char     iaFuente[16]   = "";
uint32_t iaRecibidaS    = 0;

// Diagnóstico de comunicaciones
volatile bool     mqttConectado    = false;
volatile uint32_t mqttEncolados    = 0;   // lecturas entregadas al cliente MQTT
volatile uint32_t mqttConfirmados  = 0;   // PUBACK recibidos del broker (QoS 1)
volatile uint32_t mqttDescartados  = 0;   // lecturas perdidas (cola o outbox llenos)

SemaphoreHandle_t mutexDatos;
QueueHandle_t     colaTx;

char TOP_TELEMETRIA[64];
char TOP_COMANDO[64];
char TOP_ESTADO[64];
char TOP_ACK[72];
char TOP_IA[64];
char TOP_HISTORICO[64];
char MQTT_URI[64];
char MQTT_CLIENT_ID[40];

// ---------- Conteo de pulsos de caudal (ISR) ----------
portMUX_TYPE muxCaudal = portMUX_INITIALIZER_UNLOCKED;
volatile uint32_t pulsosCaudal = 0;

void IRAM_ATTR isrPulsoCaudal() {
  portENTER_CRITICAL_ISR(&muxCaudal);
  pulsosCaudal = pulsosCaudal + 1;
  portEXIT_CRITICAL_ISR(&muxCaudal);
}

// ============================================================================
//  PÁGINA DEL TABLERO DE CONTROL LOCAL (embebida: no requiere internet)
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
            margin-bottom:8px; font-size:1.05rem; }
  #fallas { padding:8px 14px; border-radius:8px; margin-bottom:16px; font-size:0.85rem;
            background:#3d2a5c; color:#c9a3ff; display:none; }
  .normal      { background:#1a4d2e; color:#7ee2a8; }
  .vigilancia  { background:#4a4a1a; color:#f0e68c; }
  .alerta      { background:#5c4813; color:#ffd866; }
  .critico     { background:#5c1a1a; color:#ff8a8a; }
  .sinconexion { background:#30363d; color:#8b949e; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; }
  .card { background:#1a2027; border-radius:8px; padding:12px; }
  .etq { font-size:0.72rem; color:#8b949e; text-transform:uppercase; letter-spacing:0.5px; }
  .val { font-size:1.5rem; font-weight:bold; margin-top:4px; }
  .uni { font-size:0.85rem; color:#8b949e; font-weight:normal; }
  #ia { background:#1a2027; border-left:4px solid #2d5f8a; border-radius:8px; padding:12px;
        margin-top:16px; font-size:0.9rem; }
  #ia .etq { margin-bottom:6px; }
  .estado { font-size:0.75rem; color:#8b949e; margin-top:10px; }
  .ok { color:#7ee2a8; } .nok { color:#ff8a8a; }
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
  <div id="fallas"></div>

  <div class="grid">
    <div class="card"><div class="etq">Nivel</div><div class="val"><span id="nivel">--</span><span class="uni"> %</span></div></div>
    <div class="card"><div class="etq">Altura del agua</div><div class="val"><span id="altura">--</span><span class="uni"> cm</span></div></div>
    <div class="card"><div class="etq">Cambio (5 min)</div><div class="val"><span id="tasa">--</span><span class="uni"> cm/h</span></div></div>
    <div class="card"><div class="etq">Tendencia 24 h</div><div class="val"><span id="tend">--</span><span class="uni"> cm/h</span></div></div>
    <div class="card"><div class="etq">Caudal</div><div class="val"><span id="caudal">--</span><span class="uni"> L/min</span></div></div>
    <div class="card"><div class="etq">Volumen acumulado</div><div class="val"><span id="volumen">--</span><span class="uni"> L</span></div></div>
    <div class="card"><div class="etq">Temperatura</div><div class="val"><span id="temp">--</span><span class="uni"> &deg;C</span></div></div>
    <div class="card"><div class="etq">Humedad relativa</div><div class="val"><span id="humedad">--</span><span class="uni"> %</span></div></div>
    <div class="card"><div class="etq">Presion</div><div class="val"><span id="presion">--</span><span class="uni"> hPa</span></div></div>
    <div class="card"><div class="etq">Radiacion solar</div><div class="val"><span id="radiacion">--</span><span class="uni"> W/m2</span></div></div>
    <div class="card"><div class="etq">Evaporacion (tasa P-T)</div><div class="val"><span id="evap">--</span><span class="uni"> mm/dia</span></div></div>
  </div>

  <div id="ia"><div class="etq">Recomendacion del modulo de IA</div><div id="iaTxt">Sin recomendacion recibida</div></div>

  <button id="btnSilenciar" onclick="silenciar()">Silenciar alarma fisica (30 min)</button>
  <div class="estado">Gateway MQTT: <span id="mqtt">--</span> &middot; Enviados: <span id="pub">--</span>
    &middot; Confirmados: <span id="conf">--</span></div>

  <h2>Historico reciente</h2>
  <table>
    <thead><tr><th>Hace</th><th>Altura cm</th><th>dH cm/h</th><th>Caudal</th><th>Temp</th><th>Hum %</th><th>Estado</th></tr></thead>
    <tbody id="tbody"><tr><td colspan="7">Sin datos aun</td></tr></tbody>
  </table>

<script>
var ESTADOS = ["NORMAL", "VIGILANCIA", "ALERTA", "CRITICO"];
var CLASES  = ["normal", "vigilancia", "alerta", "critico"];
var ultimoEstado = -1;
var tsActual = 0;

function f(x, d) { return (x === null || x === undefined) ? "--" : x.toFixed(d); }
function esc(s) { var e = document.createElement("span"); e.textContent = s; return e.innerHTML; }

function hace(ts) {
  var s = tsActual - ts;
  if (s < 60) { return s + "s"; }
  return Math.floor(s / 60) + "m " + (s % 60) + "s";
}

function pintar(d) {
  tsActual = d.ts;
  document.getElementById("nivel").textContent     = f(d.nivel, 0);
  document.getElementById("altura").textContent    = f(d.altura, 1);
  document.getElementById("tasa").textContent      = f(d.tasa, 2);
  document.getElementById("tend").textContent      = f(d.tendencia24h, 3);
  document.getElementById("caudal").textContent    = f(d.caudal, 1);
  document.getElementById("volumen").textContent   = f(d.volumen, 1);
  document.getElementById("temp").textContent      = f(d.temp, 1);
  document.getElementById("humedad").textContent   = f(d.humedad, 0);
  document.getElementById("presion").textContent   = f(d.presion, 1);
  document.getElementById("radiacion").textContent = f(d.radiacion, 0);
  document.getElementById("evap").textContent      = f(d.evap, 2);

  var b = document.getElementById("banner");
  b.className = CLASES[d.estado];
  var txt = "Estado: " + ESTADOS[d.estado];
  if (d.estado > 0 && d.estado !== ultimoEstado) { txt = "NUEVO ESTADO - " + ESTADOS[d.estado]; }
  if (d.silenciada && d.estado > 0) { txt += " (alarma silenciada)"; }
  b.textContent = txt;
  ultimoEstado = d.estado;

  var fa = document.getElementById("fallas");
  if (d.falla && d.falla.length) {
    fa.style.display = "block";
    fa.textContent = "FALLA DE SENSOR: " + d.falla.join(", ") + " - revisar el dispositivo en sitio";
  } else {
    fa.style.display = "none";
  }

  var btn = document.getElementById("btnSilenciar");
  btn.disabled = (d.estado < 2 && !(d.falla && d.falla.length)) || d.silenciada;

  var m = document.getElementById("mqtt");
  m.textContent = d.mqtt ? "conectado" : "desconectado";
  m.className = d.mqtt ? "ok" : "nok";
  document.getElementById("pub").textContent = d.enviados;
  document.getElementById("conf").textContent = d.confirmados;

  var ia = document.getElementById("iaTxt");
  if (d.iaRiesgo) {
    ia.innerHTML = "<b>Riesgo " + esc(d.iaRiesgo.toUpperCase()) + "</b> (" + esc(d.iaFuente) +
                   ", hace " + hace(d.iaTs) + ")<br>" + esc(d.iaResumen);
  }
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
    if (!h.length) { tb.innerHTML = "<tr><td colspan='7'>Sin datos aun</td></tr>"; return; }
    var filas = "";
    for (var i = h.length - 1; i >= 0; i--) {
      var p = h[i];
      filas += "<tr><td>" + hace(p.ts) + "</td><td>" + f(p.altura, 1) + "</td><td>" +
               f(p.tasa, 2) + "</td><td>" + f(p.caudal, 1) + "</td><td>" + f(p.temp, 1) +
               "</td><td>" + f(p.humedad, 0) + "</td><td>" + ESTADOS[p.estado] + "</td></tr>";
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
//  UTILIDADES
// ============================================================================
// Escribe un número con 'dec' decimales, o "null" si no es finito.
// JSON no admite NaN; el contrato manda null para un dato no válido.
void numJSON(char *dst, size_t n, float v, uint8_t dec) {
  if (isnan(v) || isinf(v)) snprintf(dst, n, "null");
  else                      snprintf(dst, n, "%.*f", dec, v);
}

// Lista de fallas en formato JSON: ["nivel","dht"]
void fallasJSON(char *dst, size_t n, uint8_t f) {
  snprintf(dst, n, "[%s%s%s%s%s%s%s%s]",
    (f & FALLA_NIVEL) ? "\"nivel\"" : "",
    (f & FALLA_NIVEL) && (f & (FALLA_LDR | FALLA_DHT | FALLA_BMP)) ? "," : "",
    (f & FALLA_LDR) ? "\"ldr\"" : "",
    (f & FALLA_LDR) && (f & (FALLA_DHT | FALLA_BMP)) ? "," : "",
    (f & FALLA_DHT) ? "\"dht\"" : "",
    (f & FALLA_DHT) && (f & FALLA_BMP) ? "," : "",
    (f & FALLA_BMP) ? "\"bmp\"" : "",
    "");
}

// Hora absoluta en ms si NTP ya sincronizó; 0 si no.
uint64_t epochMsActual() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  if (tv.tv_sec < 1760000000) return 0;   // reloj aún no sincronizado (regla del contrato)
  return (uint64_t)tv.tv_sec * 1000ULL + (uint64_t)(tv.tv_usec / 1000);
}

// nullptr si la cadena está vacía o solo tiene espacios (credencial no configurada)
const char* opcional(const char *s) {
  for (const char *c = s; *c; c++) if (*c != ' ') return s;
  return nullptr;
}

// ============================================================================
//  LECTURA DE SENSORES
// ============================================================================
float medirDistanciaUnaVezCM() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  long duracion = pulseIn(ECHO_PIN, HIGH, 30000); // timeout 30 ms (~5 m)
  if (duracion == 0) return -1;
  return duracion * 0.0343 / 2.0;
}

// Mediana de DISPAROS_NIVEL disparos separados 60 ms. La mediana descarta ecos
// espurios aislados, que de otro modo contaminarían la tasa de descenso.
// Se exige al menos 3 disparos válidos; si no, la lectura es inválida (-1).
float medirDistanciaCM() {
  float v[DISPAROS_NIVEL];
  uint8_t n = 0;
  for (uint8_t i = 0; i < DISPAROS_NIVEL; i++) {
    float d = medirDistanciaUnaVezCM();
    if (d > 0) v[n++] = d;
    vTaskDelay(pdMS_TO_TICKS(60));
  }
  if (n < 3) return -1;
  for (uint8_t i = 1; i < n; i++) {           // ordenamiento por inserción (n <= 5)
    float x = v[i];
    int8_t j = i - 1;
    while (j >= 0 && v[j] > x) { v[j + 1] = v[j]; j--; }
    v[j + 1] = x;
  }
  return (n % 2) ? v[n / 2] : 0.5f * (v[n / 2 - 1] + v[n / 2]);
}

float calcularNivelPct(float distanciaCM) {
  if (distanciaCM < 0) return -1;
  float pct = (DISTANCIA_VACIO_CM - distanciaCM) / ALTURA_UTIL_CM * 100.0;
  return constrain(pct, 0.0, 100.0);
}

float leerCaudalLmin(uint32_t intervaloMs, float &volumenAcumuladoL) {
  portENTER_CRITICAL(&muxCaudal);
  uint32_t pulsos = pulsosCaudal;
  pulsosCaudal = 0;
  portEXIT_CRITICAL(&muxCaudal);

  float intervaloSeg = intervaloMs / 1000.0;
  float caudalLmin   = (pulsos / intervaloSeg) / FACTOR_PULSOS_POR_HZ;
  volumenAcumuladoL += caudalLmin * (intervaloSeg / 60.0);
  return caudalLmin;
}

// Interpolación lineal por tramos sobre la tabla de calibración. Fuera del
// rango calibrado se extrapola con el tramo extremo.
float interpolarCalibracion(float x, const float *xs, const float *ys, uint8_t n) {
  if (n < 2) return ys[0];
  uint8_t i = 1;
  while (i < n - 1 && x > xs[i]) i++;
  float t = (x - xs[i - 1]) / (xs[i] - xs[i - 1]);
  return ys[i - 1] + t * (ys[i] - ys[i - 1]);
}

// Promedia 8 conversiones: el ADC del ESP32 tiene ruido de ±10-20 cuentas.
int leerLDRCrudo() {
  uint32_t suma = 0;
  for (uint8_t i = 0; i < 8; i++) suma += analogRead(LDR_PIN);
  return suma / 8;
}

// Radiación en W/m² o NAN si el LDR está en corto o saturado.
float calcularRadiacionWm2() {
  int lectura = leerLDRCrudo(); // 0-4095 en ESP32
#if DEBUG_LDR
  // Formato CSV para calibrar: copiar junto con la lectura del instrumento.
  Serial.printf("LDR_ADC,%lu,%d\n", (unsigned long)(millis() / 1000), lectura);
#endif
  if (lectura >= LDR_ADC_SATURADO) return NAN;
  float rs = interpolarCalibracion(lectura, CAL_LDR_ADC, CAL_LDR_WM2, N_CAL_LDR);
  return constrain(rs, 0.0, RAD_MAX_WM2);
}

float presionEstimadaKPa() {
  return 101.3 * pow((293.0 - 0.0065 * ALTITUD_SITIO_M) / 293.0, 5.26);
}

// Presión medida por el BMP280 en kPa, o NAN si no responde.
float leerPresionKPa() {
  if (bmpOk) {
    float pa = bmp.readPressure();
    if (!isnan(pa) && pa > 30000.0 && pa < 110000.0) return pa / 1000.0;
  }
  return NAN;
}

// Evaporación potencial: Priestley-Taylor (1972), sin cambios respecto al Ch2.
float calcularEvaporacionMMDia(float tempC, float radiacionWm2, float presionKPa) {
  float es     = 0.6108 * exp((17.27 * tempC) / (tempC + 237.3));
  float delta  = (4098.0 * es) / ((tempC + 237.3) * (tempC + 237.3));
  float gamma  = 0.665e-3 * presionKPa;
  float lambda = 2.501 - 0.002361 * tempC;
  float rn     = (1.0 - ALBEDO_AGUA) * radiacionWm2 * 0.0864;
  float et = ALFA_PT * (delta / (delta + gamma)) * (rn / lambda);
  return (et < 0.0) ? 0.0 : et;
}

// ============================================================================
//  TASA DE EVENTOS (pendiente por mínimos cuadrados, ventana de 5 min)
// ============================================================================
// Buffer circular con las últimas alturas VÁLIDAS (cm) y su instante (ms).
// Solo lo usa la tarea de medición: no requiere mutex.
float    tasaAltura[VENTANA_TASA_MUESTRAS];
uint32_t tasaT[VENTANA_TASA_MUESTRAS];
uint16_t tasaCantidad = 0;
uint16_t tasaInicio   = 0;

void registrarAlturaParaTasa(float alturaCM, uint32_t tMs) {
  uint16_t pos = (tasaInicio + tasaCantidad) % VENTANA_TASA_MUESTRAS;
  tasaAltura[pos] = alturaCM;
  tasaT[pos]      = tMs;
  if (tasaCantidad < VENTANA_TASA_MUESTRAS) tasaCantidad++;
  else tasaInicio = (tasaInicio + 1) % VENTANA_TASA_MUESTRAS;
}

// Pendiente por mínimos cuadrados ordinarios de pares (t [s], y). Devuelve la
// pendiente en unidades de y por hora, o NAN si no hay variación de t.
float pendientePorHora(const float *ts, const float *ys, uint16_t n) {
  double sumT = 0, sumY = 0;
  for (uint16_t i = 0; i < n; i++) { sumT += ts[i]; sumY += ys[i]; }
  double tMed = sumT / n, yMed = sumY / n, sxy = 0, sxx = 0;
  for (uint16_t i = 0; i < n; i++) {
    double dt = ts[i] - tMed;
    sxy += dt * (ys[i] - yMed);
    sxx += dt * dt;
  }
  return (sxx > 0) ? (float)(sxy / sxx * 3600.0) : NAN;
}

// Tasa de cambio de la altura en cm/h (negativa = el agua baja) o NAN.
// El tiempo se toma relativo a la última muestra para no perder precisión.
float calcularTasaCmh() {
  if (tasaCantidad < MIN_MUESTRAS_TASA) return NAN;
  static float ts[VENTANA_TASA_MUESTRAS], ys[VENTANA_TASA_MUESTRAS];   // fuera de la pila
  uint32_t tRef = tasaT[(tasaInicio + tasaCantidad - 1) % VENTANA_TASA_MUESTRAS];
  for (uint16_t i = 0; i < tasaCantidad; i++) {
    uint16_t k = (tasaInicio + i) % VENTANA_TASA_MUESTRAS;
    ts[i] = -(float)(tRef - tasaT[k]) / 1000.0f;
    ys[i] = tasaAltura[k];
  }
  return pendientePorHora(ts, ys, tasaCantidad);
}

// ============================================================================
//  AGREGADOS DE 5 MIN, TENDENCIA DE 24 H Y PERSISTENCIA EN NVS
// ============================================================================
struct Agregado5m {
  uint32_t epoch;        // inicio de la ventana (s UTC); 0 si no había NTP
  uint32_t finMs;        // millis() al cerrar (para la edad si no hay NTP)
  float    alturaCM, caudalLmin, tempC, humedadPct, presionHpa, evapTasa, radiacionWm2;
  uint16_t n;            // lecturas con nivel válido
  uint8_t  estadoMax;
  bool     pendiente;    // cerró sin conexión MQTT: hay que reenviarlo
  bool     restaurado;   // vino de NVS tras un reinicio (solo altura y hora)
};

Agregado5m agregados[AGREGADOS_MAX];      // protegido por mutexDatos
uint16_t   agregadosCantidad = 0;
uint16_t   agregadosInicio   = 0;

// Acumulador de la ventana en curso (solo lo usa la tarea de medición)
struct Acumulador {
  double   suma[7];
  uint16_t cuenta[7];
  uint16_t nNivel;
  uint8_t  estadoMax;
  uint32_t epochInicio;
  uint32_t inicioMs;
  uint32_t desconexionesInicio;
};
Acumulador acum = {};
volatile uint32_t mqttDesconexiones = 0;   // lo incrementa el evento DISCONNECTED

void acumular(uint8_t i, float v) {
  if (!isnan(v)) { acum.suma[i] += v; acum.cuenta[i]++; }
}

float mediaAcum(uint8_t i) {
  return acum.cuenta[i] ? (float)(acum.suma[i] / acum.cuenta[i]) : NAN;
}

void reiniciarAcumulador(uint32_t ahoraMs) {
  memset(&acum, 0, sizeof(acum));
  acum.inicioMs = ahoraMs;
  acum.epochInicio = (uint32_t)(epochMsActual() / 1000ULL);
  acum.desconexionesInicio = mqttDesconexiones;
}

// Se llama con mutexDatos tomado.
void guardarAgregado(const Agregado5m &a) {
  uint16_t pos = (agregadosInicio + agregadosCantidad) % AGREGADOS_MAX;
  agregados[pos] = a;
  if (agregadosCantidad < AGREGADOS_MAX) agregadosCantidad++;
  else agregadosInicio = (agregadosInicio + 1) % AGREGADOS_MAX;
}

// Tendencia de 24 h: mínimos cuadrados sobre las alturas medias de los
// agregados. Con hora NTP se usan todos los de las últimas 24 h (también los
// restaurados de NVS); sin NTP, solo los de esta sesión (por millis()).
// Se llama con mutexDatos tomado.
float calcularTendencia24h() {
  static float ts[AGREGADOS_MAX], ys[AGREGADOS_MAX];
  uint32_t ahora = (uint32_t)(epochMsActual() / 1000ULL);
  uint32_t ahoraMs = millis();
  uint16_t n = 0;
  float tMin = 0, tMax = 0;
  for (uint16_t i = 0; i < agregadosCantidad; i++) {
    const Agregado5m &a = agregados[(agregadosInicio + i) % AGREGADOS_MAX];
    if (isnan(a.alturaCM)) continue;
    float t;
    if (ahora) {
      if (!a.epoch || ahora - a.epoch > 86400UL) continue;
      t = -(float)(ahora - a.epoch);
    } else {
      if (a.restaurado) continue;
      t = -(float)(ahoraMs - a.finMs) / 1000.0f;
    }
    if (n == 0 || t < tMin) tMin = t;
    if (n == 0 || t > tMax) tMax = t;
    ts[n] = t; ys[n] = a.alturaCM; n++;
  }
  if (n < MIN_AGREGADOS_TENDENCIA || (tMax - tMin) < 6 * 3600.0f) return NAN;
  return pendientePorHora(ts, ys, n);
}

// NVS: solo lo necesario para la tendencia (altura en décimas de mm y hora UTC),
// ~1.7 kB. Se escribe cada 30 min para no desgastar la flash.
Preferences nvs;

void guardarNVS() {
  static int16_t alt[AGREGADOS_MAX];
  static uint32_t ep[AGREGADOS_MAX];
  uint16_t n = 0;
  xSemaphoreTake(mutexDatos, portMAX_DELAY);
  for (uint16_t i = 0; i < agregadosCantidad; i++) {
    const Agregado5m &a = agregados[(agregadosInicio + i) % AGREGADOS_MAX];
    if (!a.epoch || isnan(a.alturaCM)) continue;
    alt[n] = (int16_t)lroundf(a.alturaCM * 100.0f);   // cm -> décimas de mm (hasta 327 cm)
    ep[n]  = a.epoch;
    n++;
  }
  xSemaphoreGive(mutexDatos);
  nvs.begin("serie24h", false);
  nvs.putUShort("n", n);
  nvs.putBytes("alt", alt, n * sizeof(int16_t));
  nvs.putBytes("ep",  ep,  n * sizeof(uint32_t));
  nvs.end();
  Serial.printf("NVS: %u agregados guardados\n", n);
}

void restaurarNVS() {
  static int16_t alt[AGREGADOS_MAX];
  static uint32_t ep[AGREGADOS_MAX];
  nvs.begin("serie24h", true);
  uint16_t n = nvs.getUShort("n", 0);
  if (n > AGREGADOS_MAX) n = 0;
  if (n) {
    nvs.getBytes("alt", alt, n * sizeof(int16_t));
    nvs.getBytes("ep",  ep,  n * sizeof(uint32_t));
  }
  nvs.end();
  for (uint16_t i = 0; i < n; i++) {
    Agregado5m a = {};
    a.epoch = ep[i];
    a.alturaCM = alt[i] / 100.0f;
    a.caudalLmin = a.tempC = a.humedadPct = a.presionHpa = a.evapTasa = a.radiacionWm2 = NAN;
    a.restaurado = true;
    a.pendiente  = false;
    guardarAgregado(a);
  }
  Serial.printf("NVS: %u agregados restaurados para la tendencia de 24 h\n", n);
}

// ============================================================================
//  LÓGICA DE FUSIÓN (escala de 4 estados del contrato) CON HISTÉRESIS POR CONDICIÓN
// ============================================================================
//   CRITICO    : nivel ≤ 15 %, o nivel bajo + (evaporación alta | consumo | descenso),
//                o descenso anómalo + calor (el evento compuesto del enunciado:
//                "tasas anómalas de descenso de nivel bajo condiciones de calor extremo")
//   ALERTA     : nivel bajo (≤ 30 %)
//   VIGILANCIA : evaporación alta, consumo anómalo o descenso anómalo, con el nivel aún bien
//   "descenso anómalo" = evento: el agua baja más de 2 cm/h (ventana de 5 min).
//   "evaporación alta" = TASA instantánea de Priestley-Taylor > 5 mm/día.
//   NORMAL     : ninguna condición
// Cada condición es un disparador de Schmitt con memoria propia: solo se
// desactiva al cruzar umbral ± margen. (En el Ch2 la histéresis era global y el
// estado oscilaba entre niveles cuando una variable rondaba su umbral.)
// Las condiciones cuyo dato es NAN (sensor caído, tasa aún no disponible) son falsas.
struct EstadoFusion {
  bool nivelCritico, nivelBajo, evapAlta, consumo, descenso, calorTemp;
};

bool sobreUmbral(bool previo, float v, float umbral, float margen) {
  if (isnan(v)) return false;
  return previo ? (v > umbral - margen) : (v > umbral);
}

bool bajoUmbral(bool previo, float v, float umbral, float margen) {
  if (isnan(v)) return false;
  return previo ? (v < umbral + margen) : (v <= umbral);
}

Estado evaluarEstado(float nivelPct, float evaporacionMMDia, float caudalLmin,
                     float tasaCmh, float tempC, EstadoFusion &f) {
  f.nivelCritico = bajoUmbral (f.nivelCritico, nivelPct,         NIVEL_CRITICO_PCT,        MARGEN_NIVEL_PCT);
  f.nivelBajo    = bajoUmbral (f.nivelBajo,    nivelPct,         NIVEL_ALERTA_PCT,         MARGEN_NIVEL_PCT);
  f.evapAlta     = sobreUmbral(f.evapAlta,     evaporacionMMDia, EVAPORACION_ALTA_MM_DIA,  MARGEN_EVAP_MM_DIA);
  f.consumo      = sobreUmbral(f.consumo,      caudalLmin,       CAUDAL_ANOMALO_LMIN,      MARGEN_CAUDAL_LMIN);
  f.descenso     = sobreUmbral(f.descenso,     -tasaCmh,         TASA_EVENTO_CMH,          MARGEN_TASA_CMH);
  f.calorTemp    = sobreUmbral(f.calorTemp,    tempC,            TEMP_CALOR_C,             MARGEN_TEMP_C);
  bool calor = f.evapAlta || f.calorTemp;

  if (f.nivelCritico ||
      (f.nivelBajo && (f.evapAlta || f.consumo || f.descenso)) ||
      (f.descenso && calor)) {
    return CRITICO;
  }
  if (f.nivelBajo) return ALERTA;
  if (f.evapAlta || f.consumo || f.descenso) return VIGILANCIA;
  return NORMAL;
}

// ============================================================================
//  SILENCIADO DE LA ALARMA (local y remoto)
// ============================================================================
// Silencia el buzzer 'duracionS' segundos. Los LEDs NUNCA se apagan.
// Se llama con mutexDatos tomado.
void activarSilencio(uint32_t duracionS) {
  if (duracionS > SILENCIO_MAXIMO_S) duracionS = SILENCIO_MAXIMO_S;
  silencioActivo = true;
  silencioFinMs  = millis() + duracionS * 1000UL;
  silencioEstado = actual.estado;
  noTone(BUZZER_PIN);
}

// ¿Sigue vigente el silencio para el estado actual? (con mutexDatos tomado)
// - Al vencer la hora, el buzzer vuelve a sonar si la condición sigue (contrato).
// - Un estado MÁS GRAVE que el silenciado cancela el silencio de inmediato:
//   silenciar una ALERTA no debe ocultar un CRITICO nuevo.
bool silencioVigente(uint8_t estado) {
  if (!silencioActivo) return false;
  if (estado > silencioEstado || (int32_t)(millis() - silencioFinMs) >= 0) {
    silencioActivo = false;
    return false;
  }
  return true;
}

// ============================================================================
//  SALIDAS FÍSICAS IN SITU
// ============================================================================
// verde = NORMAL · amarillo = VIGILANCIA / ALERTA · rojo = CRITICO
// Falla de sensor (sin CRITICO): verde + rojo a la vez, pitido corto y grave.
// Buzzer: VIGILANCIA sin sonido · ALERTA intermitente · CRITICO continuo.
void actualizarSalidas(uint8_t estado, bool fallaSensor, bool silenciada) {
  bool patronFalla = fallaSensor && estado < CRITICO;
  digitalWrite(LED_VERDE,    (estado == NORMAL && !fallaSensor) || patronFalla);
  digitalWrite(LED_AMARILLO, !patronFalla && (estado == VIGILANCIA || estado == ALERTA));
  digitalWrite(LED_ROJO,     estado == CRITICO || patronFalla);

  if (silenciada) { noTone(BUZZER_PIN); return; }
  if (estado == CRITICO)      tone(BUZZER_PIN, 2200);
  else if (patronFalla)       tone(BUZZER_PIN, 500, 150);
  else if (estado == ALERTA)  tone(BUZZER_PIN, 1000, 300);
  else                        noTone(BUZZER_PIN);
}

void mostrarLCD(const Lectura &d) {
  lcd.clear();
  int pantalla = (millis() / 4000) % 4;

  if (pantalla == 0) {
    lcd.setCursor(0, 0);
    lcd.print("N:");
    if (isnan(d.alturaCM)) lcd.print("--"); else lcd.print(d.alturaCM, 1);
    lcd.print("cm T:");
    if (isnan(d.tempC)) lcd.print("--"); else lcd.print(d.tempC, 1);
    lcd.print((char)223);
    lcd.print("C");

    lcd.setCursor(0, 1);
    if (d.fallas & (FALLA_NIVEL | FALLA_DHT | FALLA_LDR)) {
      lcd.print("FALLA:");
      if (d.fallas & FALLA_NIVEL) lcd.print("niv ");
      if (d.fallas & FALLA_DHT)   lcd.print("dht ");
      if (d.fallas & FALLA_LDR)   lcd.print("ldr");
    } else {
      switch (d.estado) {
        case NORMAL:     lcd.print("Estado: NORMAL"); break;
        case VIGILANCIA: lcd.print("VIGILANCIA");     break;
        case ALERTA:     lcd.print("ALERTA: SEQUIA"); break;
        case CRITICO:    lcd.print("CRITICO!!");      break;
      }
    }

  } else if (pantalla == 1) {
    lcd.setCursor(0, 0);
    lcd.print("Rad:");
    if (isnan(d.radiacionWm2)) lcd.print("--"); else lcd.print(d.radiacionWm2, 0);
    lcd.print(" W/m2");

    lcd.setCursor(0, 1);                     // Ej: "H:70% P:701hPa"
    lcd.print("H:");
    if (isnan(d.humedadPct)) lcd.print("--"); else lcd.print(d.humedadPct, 0);
    lcd.print("% P:");
    if (isnan(d.presionKPa)) lcd.print("--"); else lcd.print(d.presionKPa * 10.0, 0);
    lcd.print("hPa");

  } else if (pantalla == 2) {
    lcd.setCursor(0, 0);
    lcd.print("Caudal:");
    lcd.print(d.caudalLmin, 1);
    lcd.print("L/m");

    lcd.setCursor(0, 1);
    lcd.print("Vol.acum:");
    lcd.print(d.volumenAcumuladoL, 1);
    lcd.print("L");

  } else {
    lcd.setCursor(0, 0);                     // "dH:-0.42cm/h" / "IA:ALTO  MQTT:ok"
    lcd.print("dH:");
    if (isnan(d.tasaCmh)) lcd.print("calc..."); else { lcd.print(d.tasaCmh, 2); lcd.print("cm/h"); }

    char riesgo[6] = "--";
    xSemaphoreTake(mutexDatos, portMAX_DELAY);
    if (iaRiesgo[0]) { strncpy(riesgo, iaRiesgo, 5); riesgo[5] = '\0'; }
    xSemaphoreGive(mutexDatos);
    for (char *c = riesgo; *c; c++) *c = toupper(*c);

    lcd.setCursor(0, 1);
    lcd.print("IA:");
    lcd.print(riesgo);
    lcd.setCursor(9, 1);
    lcd.print(mqttConectado ? "MQTT:ok" : "MQTT:--");
  }
}

// ============================================================================
//  TAREA DE MEDICIÓN (Core 0, separada del hilo principal)
// ============================================================================
void encolarParaTransmision(const Lectura &l) {
  if (xQueueSend(colaTx, &l, 0) != pdTRUE) {
    // Cola llena: se descarta la lectura más antigua y se conserva la nueva.
    Lectura descartada;
    xQueueReceive(colaTx, &descartada, 0);
    xQueueSend(colaTx, &l, 0);
    mqttDescartados = mqttDescartados + 1;
  }
}

void tareaMedicion(void *parametros) {
  const TickType_t periodo = pdMS_TO_TICKS(INTERVALO_LECTURA_MS);
  TickType_t ultimoDespertar = xTaskGetTickCount();

  float        volumenAcumuladoL = 0.0;
  float        radiacionFiltrada = NAN;
  uint8_t      contadorHistorico = 0;
  uint32_t     seq               = 0;
  EstadoFusion fusion            = {};   // memoria de la histéresis por condición
  float        tendencia24h      = NAN;
  uint32_t     ultimoNVS         = millis();
  reiniciarAcumulador(millis());

  for (;;) {
    // --- Nivel ---
    float distancia = medirDistanciaCM();
    bool  fallaNivel = (distancia < 0);
    float nivelPct  = fallaNivel ? NAN : calcularNivelPct(distancia);
    // Altura del agua sobre el fondo útil (cm): sube cuando sube el agua.
    float alturaCM  = fallaNivel ? NAN : constrain(DISTANCIA_VACIO_CM - distancia, 0.0, ALTURA_UTIL_CM);
    uint32_t ahoraMs = millis();
    if (!fallaNivel) registrarAlturaParaTasa(alturaCM, ahoraMs);
    float tasaCmh = calcularTasaCmh();

    // --- Clima ---
    TempAndHumidity datosDHT = dht.getTempAndHumidity();
    bool  fallaDHT   = isnan(datosDHT.temperature) || isnan(datosDHT.humidity);
    float tempC      = fallaDHT ? NAN : datosDHT.temperature;
    float humedadPct = fallaDHT ? NAN : datosDHT.humidity;

    float presionKPa = leerPresionKPa();
    bool  fallaBMP   = isnan(presionKPa);

    float radCruda = calcularRadiacionWm2();
    bool  fallaLDR = isnan(radCruda);
    if (!fallaLDR) {
      radiacionFiltrada = isnan(radiacionFiltrada)
          ? radCruda
          : radiacionFiltrada + ALFA_EMA_RADIACION * (radCruda - radiacionFiltrada);
    }
    float radiacionWm2 = fallaLDR ? NAN : radiacionFiltrada;

    // Priestley-Taylor: sin BMP280 se usa la presión estimada por altitud
    // (efecto pequeño); sin temperatura o radiación no hay evaporación.
    float evaporacion = (fallaDHT || fallaLDR)
        ? NAN
        : calcularEvaporacionMMDia(tempC, radiacionWm2, fallaBMP ? presionEstimadaKPa() : presionKPa);

    float caudalLmin = leerCaudalLmin(INTERVALO_LECTURA_MS, volumenAcumuladoL);

    if (fallaNivel) Serial.println("HC-SR04 SIN LECTURA VALIDA (eco fuera de rango o sin retorno)");
    if (fallaDHT)   { Serial.print("DHT22 SIN LECTURA VALIDA - "); Serial.println(dht.getStatusString()); }

    // --- Estado ---
    // Sin nivel no se puede descartar el riesgo hídrico: el estado nunca baja de
    // VIGILANCIA mientras el sistema esté "ciego" (y la falla va en la lista).
    Estado estado = evaluarEstado(nivelPct, evaporacion, caudalLmin, tasaCmh, tempC, fusion);
    if (fallaNivel && estado < VIGILANCIA) estado = VIGILANCIA;

    Lectura nueva;
    nueva.seq               = ++seq;
    nueva.nivelPct          = nivelPct;
    nueva.distanciaCM       = fallaNivel ? NAN : distancia;
    nueva.alturaCM          = alturaCM;
    nueva.tasaCmh           = tasaCmh;
    nueva.tendencia24hCmh   = tendencia24h;
    nueva.tempC             = tempC;
    nueva.humedadPct        = humedadPct;
    nueva.presionKPa        = presionKPa;
    nueva.radiacionWm2      = radiacionWm2;
    nueva.evaporacionMMDia  = evaporacion;
    nueva.caudalLmin        = caudalLmin;
    nueva.volumenAcumuladoL = volumenAcumuladoL;
    nueva.estado            = estado;
    nueva.fallas            = (fallaNivel ? FALLA_NIVEL : 0) | (fallaDHT ? FALLA_DHT : 0) |
                              (fallaBMP   ? FALLA_BMP   : 0) | (fallaLDR ? FALLA_LDR : 0);
    nueva.tsSegundos        = ahoraMs / 1000;
    nueva.epochMs           = epochMsActual();

    // --- sección crítica: estado compartido con el servidor web y MQTT ---
    xSemaphoreTake(mutexDatos, portMAX_DELAY);
    nueva.silenciada = silencioVigente(estado);
    actual = nueva;
    if (++contadorHistorico >= MUESTRAS_POR_HISTORICO) {
      contadorHistorico = 0;
      uint8_t pos = (historicoInicio + historicoCantidad) % HISTORICO_MAX;
      historico[pos] = nueva;
      if (historicoCantidad < HISTORICO_MAX) historicoCantidad++;
      else historicoInicio = (historicoInicio + 1) % HISTORICO_MAX;
    }
    xSemaphoreGive(mutexDatos);

    // --- Agregado de 5 min (serie continua aunque la Raspberry Pi esté apagada) ---
    if (!fallaNivel) acum.nNivel++;
    acumular(0, alturaCM);   acumular(1, caudalLmin);   acumular(2, tempC);
    acumular(3, humedadPct); acumular(4, presionKPa * 10.0f);
    acumular(5, evaporacion); acumular(6, radiacionWm2);
    if (estado > acum.estadoMax) acum.estadoMax = estado;
    if (ahoraMs - acum.inicioMs >= PERIODO_AGREGADO_MS) {
      Agregado5m a = {};
      a.epoch = acum.epochInicio ? acum.epochInicio : (uint32_t)(epochMsActual() / 1000ULL);
      a.finMs = ahoraMs;
      a.alturaCM = mediaAcum(0); a.caudalLmin = mediaAcum(1); a.tempC = mediaAcum(2);
      a.humedadPct = mediaAcum(3); a.presionHpa = mediaAcum(4); a.evapTasa = mediaAcum(5);
      a.radiacionWm2 = mediaAcum(6);
      a.n = acum.nNivel;
      a.estadoMax = acum.estadoMax;
      // Pendiente de reenvío si en algún momento de la ventana no hubo conexión MQTT
      a.pendiente = !mqttConectado || (mqttDesconexiones != acum.desconexionesInicio);
      a.restaurado = false;
      xSemaphoreTake(mutexDatos, portMAX_DELAY);
      guardarAgregado(a);
      tendencia24h = calcularTendencia24h();
      xSemaphoreGive(mutexDatos);
      reiniciarAcumulador(ahoraMs);
    }
    if (ahoraMs - ultimoNVS >= PERIODO_NVS_MS) {
      ultimoNVS = ahoraMs;
      guardarNVS();
    }

    encolarParaTransmision(nueva);   // la tarea de transmisión la publica
    actualizarSalidas(estado, nueva.fallas & (FALLA_NIVEL | FALLA_DHT | FALLA_LDR), nueva.silenciada);
    mostrarLCD(nueva);

    char tasaTxt[12];
    numJSON(tasaTxt, sizeof(tasaTxt), tasaCmh, 2);
    Serial.printf("#%lu Altura: %.2f cm (%.1f%%) | dH: %s cm/h | T: %.1fC | H: %.1f%% | "
                  "P: %.1fhPa | Rad: %.0fW/m2 | Evap(tasa): %.2fmm/d | Caudal: %.2fL/min | Estado: %s\n",
                  (unsigned long)nueva.seq, alturaCM, nivelPct, tasaTxt, tempC, humedadPct,
                  presionKPa * 10.0, radiacionWm2, evaporacion, caudalLmin, NOMBRE_ESTADO[estado]);

    vTaskDelayUntil(&ultimoDespertar, periodo);
  }
}

// ============================================================================
//  MQTT (esp-mqtt): telemetría QoS 1, comandos de bajada y recomendación de IA
// ============================================================================
// Telemetría con los campos del contrato + extensiones.
int armarTelemetria(const Lectura &l, char *json, size_t n) {
  char dist[12], rad[12], temp[12], hum[12], pres[12], evap[12], nivelPct[12], altura[12],
       tasaCmh[12], tend[12], falla[48];
  numJSON(dist,     sizeof(dist),     l.distanciaCM, 2);
  numJSON(rad,      sizeof(rad),      l.radiacionWm2, 0);
  numJSON(temp,     sizeof(temp),     l.tempC, 1);
  numJSON(hum,      sizeof(hum),      l.humedadPct, 1);
  numJSON(pres,     sizeof(pres),     l.presionKPa * 10.0, 1);          // kPa -> hPa
  numJSON(evap,     sizeof(evap),     l.evaporacionMMDia, 2);
  numJSON(nivelPct, sizeof(nivelPct), l.nivelPct, 1);
  numJSON(altura,   sizeof(altura),   l.alturaCM, 2);
  // Tasas de cambio de la altura del agua en cm/h: NEGATIVAS cuando el nivel baja.
  numJSON(tasaCmh,  sizeof(tasaCmh),  l.tasaCmh, 3);
  numJSON(tend,     sizeof(tend),     l.tendencia24hCmh, 4);
  fallasJSON(falla, sizeof(falla), l.fallas);

  return snprintf(json, n,
    "{\"ts\":%lu,\"nivel_cm\":%s,\"caudal_lmin\":%.2f,\"radiacion_wm2\":%s,"
    "\"temp_c\":%s,\"hum_pct\":%s,\"presion_hpa\":%s,\"evap_tasa_mmdia\":%s,"
    "\"estado\":%u,\"falla\":%s,"
    "\"seq\":%lu,\"ts_ms\":%llu,\"nivel_pct\":%s,\"altura_cm\":%s,\"tasa_cmh\":%s,"
    "\"tendencia_24h_cmh\":%s,\"silenciada\":%s}",
    (unsigned long)(l.epochMs / 1000ULL), dist, l.caudalLmin, rad,
    temp, hum, pres, evap,
    l.estado, falla,
    (unsigned long)l.seq, (unsigned long long)l.epochMs, nivelPct, altura, tasaCmh,
    tend, l.silenciada ? "true" : "false");
}

void publicarAck(const char *accion, bool ok, const char *idCmd, uint32_t hastaS, const char *nota) {
  char ack[200];
  snprintf(ack, sizeof(ack),
    "{\"accion\":\"%s\",\"ok\":%s,\"cmd_id\":\"%s\",\"hasta\":%lu,\"ts\":%lu,\"nota\":\"%s\"}",
    accion, ok ? "true" : "false", idCmd, (unsigned long)hastaS,
    (unsigned long)(epochMsActual() / 1000ULL), nota);
  esp_mqtt_client_enqueue(clienteMqtt, TOP_ACK, ack, 0, 1, 0, true);
}

// Comando de bajada (contrato):
//   {"accion":"silenciar_alarma","hasta":1762563600}   hasta: epoch en segundos
//   {"accion":"reactivar_alarma"}
// Extensiones: "cmd_id" (se repite en el acuse) y la acción "ping" (solo acuse,
// para medir la latencia sin tocar la alarma).
void procesarComando(const char *payload, int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len) != DeserializationError::Ok || !doc.is<JsonObject>()) {
    publicarAck("invalida", false, "", 0, "json_invalido");
    return;
  }
  char accion[24], idCmd[40];
  strlcpy(accion, doc["accion"] | "", sizeof(accion));
  strlcpy(idCmd,  doc["cmd_id"] | "", sizeof(idCmd));
  uint32_t hasta = doc["hasta"] | 0UL;
  uint32_t ahora = (uint32_t)(epochMsActual() / 1000ULL);

  if (strcmp(accion, "silenciar_alarma") == 0) {
    uint32_t duracion = SILENCIO_DEFECTO_S;
    const char *nota = "sin_hasta";
    if (hasta > 0 && ahora > 0) {
      if (hasta <= ahora) { publicarAck(accion, false, idCmd, hasta, "hasta_vencido"); return; }
      duracion = hasta - ahora;
      nota = (duracion > SILENCIO_MAXIMO_S) ? "limitado_24h" : "ok";
    } else if (hasta > 0) {
      nota = "sin_hora_ntp_30min";      // no se puede comparar con "hasta"
    }
    xSemaphoreTake(mutexDatos, portMAX_DELAY);
    activarSilencio(duracion);
    xSemaphoreGive(mutexDatos);
    if (duracion > SILENCIO_MAXIMO_S) duracion = SILENCIO_MAXIMO_S;
    Serial.printf("MQTT: alarma silenciada %lu s desde el tablero global\n", (unsigned long)duracion);
    publicarAck(accion, true, idCmd, ahora ? ahora + duracion : 0, nota);

  } else if (strcmp(accion, "reactivar_alarma") == 0) {
    xSemaphoreTake(mutexDatos, portMAX_DELAY);
    silencioActivo = false;
    xSemaphoreGive(mutexDatos);
    Serial.println("MQTT: alarma reactivada desde el tablero global");
    publicarAck(accion, true, idCmd, 0, "ok");

  } else if (strcmp(accion, "ping") == 0) {
    publicarAck(accion, true, idCmd, 0, "ok");

  } else {
    publicarAck(accion[0] ? accion : "desconocida", false, idCmd, 0, "accion_desconocida");
  }
}

// Recomendación del módulo de IA (extensión): {"riesgo":"alto","resumen":"...","fuente":"gemini","t":...}
void procesarRecomendacionIA(const char *payload, int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len) != DeserializationError::Ok) return;
  xSemaphoreTake(mutexDatos, portMAX_DELAY);
  strlcpy(iaRiesgo,  doc["riesgo"]  | "", sizeof(iaRiesgo));
  strlcpy(iaResumen, doc["resumen"] | "", sizeof(iaResumen));
  strlcpy(iaFuente,  doc["fuente"]  | "", sizeof(iaFuente));
  iaRecibidaS = millis() / 1000;
  xSemaphoreGive(mutexDatos);
  Serial.printf("IA: riesgo %s (%s)\n", iaRiesgo, iaFuente);
}

bool topicoEs(const esp_mqtt_event_handle_t ev, const char *topico) {
  return ev->topic_len == (int)strlen(topico) && strncmp(ev->topic, topico, ev->topic_len) == 0;
}

// Manejador de eventos del cliente esp-mqtt (corre en la tarea interna de esp-mqtt).
void eventoMqtt(void *args, esp_event_base_t base, int32_t id, void *datos) {
  esp_mqtt_event_handle_t ev = (esp_mqtt_event_handle_t)datos;
  switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
      mqttConectado = true;
      esp_mqtt_client_enqueue(clienteMqtt, TOP_ESTADO, "online", 0, 1, 1, true);  // retenido
      esp_mqtt_client_subscribe(clienteMqtt, TOP_COMANDO, 1);
      esp_mqtt_client_subscribe(clienteMqtt, TOP_IA, 1);
      Serial.printf("MQTT: conectado a %s\n", MQTT_URI);
      break;
    case MQTT_EVENT_DISCONNECTED:
      mqttConectado = false;
      mqttDesconexiones = mqttDesconexiones + 1;
      Serial.println("MQTT: desconectado (esp-mqtt reintenta solo)");
      break;
    case MQTT_EVENT_PUBLISHED:
      mqttConfirmados = mqttConfirmados + 1;   // PUBACK del broker (QoS 1)
      break;
    case MQTT_EVENT_DATA:
      // Mensajes más grandes que el búfer llegan fragmentados: no se esperan aquí.
      if (ev->current_data_offset != 0 || ev->data_len != ev->total_data_len) {
        Serial.println("MQTT: mensaje fragmentado ignorado");
        break;
      }
      if (topicoEs(ev, TOP_COMANDO))  procesarComando(ev->data, ev->data_len);
      else if (topicoEs(ev, TOP_IA))  procesarRecomendacionIA(ev->data, ev->data_len);
      break;
    default:
      break;
  }
}

void iniciarMQTT() {
  uint8_t mac[6];
  WiFi.macAddress(mac);
  snprintf(MQTT_CLIENT_ID, sizeof(MQTT_CLIENT_ID), "esp32-estacion01-%02X%02X%02X", mac[3], mac[4], mac[5]);
  // Resuelve el broker una vez al arrancar: por nombre si se puede, si no por
  // la IP de respaldo. Si la IP de la Pi cambia, basta reiniciar el ESP32.
  IPAddress ipBroker;
  if (WiFi.status() == WL_CONNECTED && WiFi.hostByName(MQTT_HOST, ipBroker) &&
      ipBroker != IPAddress(0, 0, 0, 0)) {
    snprintf(MQTT_URI, sizeof(MQTT_URI), "mqtt://%s:%u", ipBroker.toString().c_str(), MQTT_PUERTO);
    Serial.printf("MQTT: %s -> %s\n", MQTT_HOST, ipBroker.toString().c_str());
  } else {
    snprintf(MQTT_URI, sizeof(MQTT_URI), "mqtt://%s:%u", MQTT_HOST_RESPALDO, MQTT_PUERTO);
    Serial.printf("MQTT: %s no resolvio, usando IP de respaldo %s\n", MQTT_HOST, MQTT_HOST_RESPALDO);
  }

  esp_mqtt_client_config_t cfg = {};
  cfg.broker.address.uri                 = MQTT_URI;
  cfg.credentials.client_id              = MQTT_CLIENT_ID;
  cfg.credentials.username               = opcional(MQTT_USUARIO);
  cfg.credentials.authentication.password = opcional(MQTT_CLAVE);
  cfg.session.last_will.topic            = TOP_ESTADO;      // LWT del contrato
  cfg.session.last_will.msg              = "offline";
  cfg.session.last_will.qos              = 1;
  cfg.session.last_will.retain           = 1;
  cfg.session.keepalive                  = MQTT_KEEPALIVE_S;
  cfg.network.reconnect_timeout_ms       = MQTT_REINTENTO_MS;
  cfg.network.timeout_ms                 = 5000;
  cfg.buffer.size                        = MQTT_BUFFER_BYTES;
  cfg.outbox.limit                       = MQTT_OUTBOX_BYTES;

  clienteMqtt = esp_mqtt_client_init(&cfg);
  esp_mqtt_client_register_event(clienteMqtt, MQTT_EVENT_ANY, eventoMqtt, nullptr);
  esp_mqtt_client_start(clienteMqtt);
}

// Reenvío del periodo sin cobertura (extensión): agregados de 5 min que
// cerraron sin conexión MQTT, en mensajes de hasta AGREGADOS_POR_MENSAJE.
//   {"agregados":[{"ts":...,"edad_s":...,"altura_cm":...,"caudal_lmin":...,"temp_c":...,
//     "hum_pct":...,"presion_hpa":...,"evap_tasa_mmdia":...,"radiacion_wm2":...,
//     "estado_max":...,"n":...}, ...], "pendientes": <los que faltan>}
// "edad_s" = segundos desde el cierre del agregado: permite ubicarlo en el
// tiempo aunque el ESP32 no tuviera NTP cuando lo cerró (ts = 0).
// Devuelve true si envió algo.
bool reenviarPendientes() {
  static char json[2048];
  uint16_t idx[AGREGADOS_POR_MENSAJE];
  uint8_t k = 0;
  uint16_t restantes = 0;
  int n = snprintf(json, sizeof(json), "{\"agregados\":[");

  xSemaphoreTake(mutexDatos, portMAX_DELAY);
  for (uint16_t i = 0; i < agregadosCantidad; i++) {
    uint16_t pos = (agregadosInicio + i) % AGREGADOS_MAX;
    const Agregado5m &a = agregados[pos];
    if (!a.pendiente) continue;
    if (k >= AGREGADOS_POR_MENSAJE) { restantes++; continue; }
    char alt[12], cau[12], te[12], hu[12], pr[12], ev[12], ra[12];
    numJSON(alt, sizeof(alt), a.alturaCM, 2);   numJSON(cau, sizeof(cau), a.caudalLmin, 2);
    numJSON(te,  sizeof(te),  a.tempC, 1);      numJSON(hu,  sizeof(hu),  a.humedadPct, 1);
    numJSON(pr,  sizeof(pr),  a.presionHpa, 1); numJSON(ev,  sizeof(ev),  a.evapTasa, 2);
    numJSON(ra,  sizeof(ra),  a.radiacionWm2, 0);
    n += snprintf(json + n, sizeof(json) - n,
      "%s{\"ts\":%lu,\"edad_s\":%lu,\"altura_cm\":%s,\"caudal_lmin\":%s,\"temp_c\":%s,"
      "\"hum_pct\":%s,\"presion_hpa\":%s,\"evap_tasa_mmdia\":%s,\"radiacion_wm2\":%s,"
      "\"estado_max\":%u,\"n\":%u}",
      k ? "," : "", (unsigned long)a.epoch, (unsigned long)((millis() - a.finMs) / 1000UL),
      alt, cau, te, hu, pr, ev, ra, a.estadoMax, a.n);
    idx[k++] = pos;
  }
  xSemaphoreGive(mutexDatos);
  if (k == 0) return false;
  n += snprintf(json + n, sizeof(json) - n, "],\"pendientes\":%u}", restantes);
  if (n >= (int)sizeof(json)) return false;

  if (esp_mqtt_client_enqueue(clienteMqtt, TOP_HISTORICO, json, n, 1, 0, true) < 0) return false;
  xSemaphoreTake(mutexDatos, portMAX_DELAY);
  for (uint8_t i = 0; i < k; i++) agregados[idx[i]].pendiente = false;
  xSemaphoreGive(mutexDatos);
  Serial.printf("MQTT: reenviados %u agregados del periodo sin conexion (faltan %u)\n", k, restantes);
  return true;
}

// Tarea de transmisión (Core 1): toma las lecturas de la cola y las entrega al
// "outbox" de esp-mqtt con QoS 1. esp-mqtt las envía, espera el PUBACK y las
// reenvía si la conexión se cae antes de confirmarlas. Con la conexión
// activa, también reenvía los agregados del periodo sin cobertura.
void tareaTransmision(void *parametros) {
  static char json[512];
  Lectura l;
  for (;;) {
    if (xQueueReceive(colaTx, &l, pdMS_TO_TICKS(1000)) == pdTRUE) {
      int n = armarTelemetria(l, json, sizeof(json));
      if (n <= 0 || n >= (int)sizeof(json)) {
        mqttDescartados = mqttDescartados + 1;
      } else if (esp_mqtt_client_enqueue(clienteMqtt, TOP_TELEMETRIA, json, n, 1, 0, true) < 0) {
        mqttDescartados = mqttDescartados + 1;   // -2: outbox lleno
      } else {
        mqttEncolados = mqttEncolados + 1;
      }
    }
    // Reenvío pausado: un mensaje por ciclo para no saturar al reconectar.
    if (mqttConectado) reenviarPendientes();
  }
}

// ============================================================================
//  SERVIDOR WEB EMBEBIDO (tablero local; se conserva el del Ch2)
// ============================================================================
bool autorizado() {
  if (server.authenticate(TABLERO_USUARIO, TABLERO_CLAVE)) return true;
  server.requestAuthentication();
  return false;
}

void handleRaiz() {
  if (!autorizado()) return;
  server.send_P(200, "text/html", PAGINA_HTML);
}

// Copia de un texto para JSON: escapa comillas y barras, elimina controles.
void escaparJSON(const char *src, char *dst, size_t n) {
  size_t j = 0;
  for (size_t i = 0; src[i] && j + 2 < n; i++) {
    char c = src[i];
    if (c == '"' || c == '\\') { dst[j++] = '\\'; dst[j++] = c; }
    else if ((uint8_t)c >= 0x20) { dst[j++] = c; }
  }
  dst[j] = '\0';
}

// /api/datos: se conservan los nombres del Ch2 (nivel, caudal, temp, ...) para
// no romper páginas que ya lo consumen. Cambios: "alerta" usa la escala nueva
// (0 normal, 1 vigilancia, 2 alerta, 3 crítico; igual a "estado"), la presión
// va en hPa, "tasa" va en cm/h (negativa = baja) y los datos inválidos llegan
// como null. Nuevos: "altura" (cm de agua) y "tendencia24h" (cm/h).
void handleDatos() {
  if (!autorizado()) return;

  Lectura d;
  char riesgo[16], resumen[220], fuente[16];
  uint32_t iaTs;
  xSemaphoreTake(mutexDatos, portMAX_DELAY);
  d = actual;
  strlcpy(riesgo, iaRiesgo, sizeof(riesgo));
  strlcpy(resumen, iaResumen, sizeof(resumen));
  strlcpy(fuente, iaFuente, sizeof(fuente));
  iaTs = iaRecibidaS;
  xSemaphoreGive(mutexDatos);

  char resumenEsc[460];
  escaparJSON(resumen, resumenEsc, sizeof(resumenEsc));
  char nivel[12], dist[12], alt[12], tasa[12], tend[12], temp[12], hum[12], pres[12], rad[12],
       evap[12], falla[48];
  numJSON(nivel, sizeof(nivel), d.nivelPct, 1);
  numJSON(dist,  sizeof(dist),  d.distanciaCM, 2);
  numJSON(alt,   sizeof(alt),   d.alturaCM, 2);
  numJSON(tasa,  sizeof(tasa),  d.tasaCmh, 3);
  numJSON(tend,  sizeof(tend),  d.tendencia24hCmh, 4);
  numJSON(temp,  sizeof(temp),  d.tempC, 1);
  numJSON(hum,   sizeof(hum),   d.humedadPct, 1);
  numJSON(pres,  sizeof(pres),  d.presionKPa * 10.0, 1);
  numJSON(rad,   sizeof(rad),   d.radiacionWm2, 0);
  numJSON(evap,  sizeof(evap),  d.evaporacionMMDia, 2);
  fallasJSON(falla, sizeof(falla), d.fallas);

  char json[1200];
  snprintf(json, sizeof(json),
    "{\"nivel\":%s,\"dist\":%s,\"altura\":%s,\"tasa\":%s,\"tendencia24h\":%s,\"caudal\":%.2f,\"volumen\":%.2f,\"temp\":%s,"
    "\"humedad\":%s,\"presion\":%s,\"radiacion\":%s,\"evap\":%s,"
    "\"estado\":%u,\"alerta\":%u,\"falla\":%s,\"silenciada\":%s,\"ts\":%lu,"
    "\"mqtt\":%s,\"enviados\":%lu,\"confirmados\":%lu,\"descartados\":%lu,"
    "\"iaRiesgo\":\"%s\",\"iaResumen\":\"%s\",\"iaFuente\":\"%s\",\"iaTs\":%lu}",
    nivel, dist, alt, tasa, tend, d.caudalLmin, d.volumenAcumuladoL, temp,
    hum, pres, rad, evap,
    d.estado, d.estado, falla, d.silenciada ? "true" : "false", (unsigned long)d.tsSegundos,
    mqttConectado ? "true" : "false", (unsigned long)mqttEncolados,
    (unsigned long)mqttConfirmados, (unsigned long)mqttDescartados,
    riesgo, resumenEsc, fuente, (unsigned long)iaTs);

  server.send(200, "application/json", json);
}

void handleHistorico() {
  if (!autorizado()) return;

  String json;
  json.reserve(HISTORICO_MAX * 150 + 8);
  json = "[";

  xSemaphoreTake(mutexDatos, portMAX_DELAY);
  for (uint8_t i = 0; i < historicoCantidad; i++) {
    const Lectura &p = historico[(historicoInicio + i) % HISTORICO_MAX];
    char nivel[12], alt[12], tasa[12], temp[12], hum[12];
    numJSON(nivel, sizeof(nivel), p.nivelPct, 1);
    numJSON(alt,   sizeof(alt),   p.alturaCM, 2);
    numJSON(tasa,  sizeof(tasa),  p.tasaCmh, 3);
    numJSON(temp,  sizeof(temp),  p.tempC, 1);
    numJSON(hum,   sizeof(hum),   p.humedadPct, 1);
    char item[190];
    snprintf(item, sizeof(item),
      "%s{\"ts\":%lu,\"nivel\":%s,\"altura\":%s,\"tasa\":%s,\"caudal\":%.2f,\"temp\":%s,"
      "\"humedad\":%s,\"estado\":%u,\"alerta\":%u}",
      (i == 0) ? "" : ",", (unsigned long)p.tsSegundos, nivel, alt, tasa, p.caudalLmin,
      temp, hum, p.estado, p.estado);
    json += item;
  }
  xSemaphoreGive(mutexDatos);

  json += "]";
  server.send(200, "application/json", json);
}

// Botón del tablero local: silencia el buzzer SILENCIO_DEFECTO_S (30 min).
void handleSilenciar() {
  if (!autorizado()) return;
  xSemaphoreTake(mutexDatos, portMAX_DELAY);
  activarSilencio(SILENCIO_DEFECTO_S);
  xSemaphoreGive(mutexDatos);
  server.send(200, "application/json", "{\"ok\":true}");
}

void handleNoEncontrado() {
  server.send(404, "text/plain", "Recurso no encontrado");
}

void conectarWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  WiFi.setSleep(WIFI_AHORRO_ENERGIA);

  Serial.print("Conectando a la WLAN");
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Conectando WLAN");

  uint8_t intentos = 0;
  while (WiFi.status() != WL_CONNECTED && intentos < 40) {
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
    Serial.println("Sin WLAN. El monitoreo local sigue activo.");
    lcd.setCursor(0, 0);
    lcd.print("Sin WLAN");
    lcd.setCursor(0, 1);
    lcd.print("Monitoreo local");
  }
  delay(3000);
  lcd.clear();
}

bool iniciarBMP280() {
  const uint8_t direcciones[] = {0x76, 0x77};
  const uint8_t chipIds[]     = {0x58, 0x60};
  for (uint8_t dir : direcciones) {
    for (uint8_t id : chipIds) {
      if (bmp.begin(dir, id)) {
        Serial.printf("BMP280 detectado en 0x%02X (chip ID 0x%02X).\n", dir, id);
        bmp.setSampling(Adafruit_BMP280::MODE_NORMAL,
                        Adafruit_BMP280::SAMPLING_X2,
                        Adafruit_BMP280::SAMPLING_X16,
                        Adafruit_BMP280::FILTER_X16,
                        Adafruit_BMP280::STANDBY_MS_500);
        return true;
      }
    }
  }
  Serial.println("BMP280 NO DETECTADO: se reporta falla \"bmp\" y se usa la presion por altitud.");
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

  // Con el divisor 10k/20k la resistencia a GND fija el nivel bajo; el
  // pull-up interno (a 3.3 V) ya no se usa porque desplazaría ese divisor.
  pinMode(FLOW_PIN, INPUT);
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
#if !CALIBRACION_LDR_CON_INSTRUMENTO
  Serial.println("AVISO: calibracion del LDR con valores de literatura (2 puntos). "
                 "Calibrar con gateway/herramientas/calibrar_ldr.py");
#endif
  delay(2000);

  mutexDatos = xSemaphoreCreateMutex();
  colaTx     = xQueueCreate(COLA_TX_MAX, sizeof(Lectura));
  restaurarNVS();   // serie de 24 h guardada antes del último reinicio

  snprintf(TOP_TELEMETRIA, sizeof(TOP_TELEMETRIA), "%s/telemetria", MQTT_BASE);
  snprintf(TOP_COMANDO,    sizeof(TOP_COMANDO),    "%s/comando", MQTT_BASE);
  snprintf(TOP_ESTADO,     sizeof(TOP_ESTADO),     "%s/estado", MQTT_BASE);
  snprintf(TOP_ACK,        sizeof(TOP_ACK),        "%s/comando/ack", MQTT_BASE);
  snprintf(TOP_IA,         sizeof(TOP_IA),         "%s/ia", MQTT_BASE);
  snprintf(TOP_HISTORICO,  sizeof(TOP_HISTORICO),  "%s/historico", MQTT_BASE);

  conectarWiFi();
  configTime(0, 0, NTP_1, NTP_2);   // UTC: el contrato usa epoch UTC
  iniciarMQTT();

  server.on("/", HTTP_GET, handleRaiz);
  server.on("/api/datos", HTTP_GET, handleDatos);
  server.on("/api/historico", HTTP_GET, handleHistorico);
  server.on("/api/silenciar", HTTP_POST, handleSilenciar);
  server.onNotFound(handleNoEncontrado);
  server.begin();
  Serial.println("Servidor web embebido iniciado en el puerto 80.");

  // Medición en el Core 0; transmisión y servidor web (loop) en el Core 1.
  xTaskCreatePinnedToCore(tareaMedicion,    "medicion",    6144, NULL, 2, NULL, 0);
  xTaskCreatePinnedToCore(tareaTransmision, "transmision", 8192, NULL, 1, NULL, 1);
}

void loop() {
  server.handleClient();

  // Reconexión no bloqueante de la WLAN (esp-mqtt reconecta solo al broker).
  static uint32_t ultimaRevisionWiFi = 0;
  if (millis() - ultimaRevisionWiFi > 10000) {
    ultimaRevisionWiFi = millis();
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("WLAN caida, reintentando...");
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    }
  }
  delay(2);
}
