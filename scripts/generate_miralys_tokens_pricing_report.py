from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QSizeF
from PySide6.QtGui import QTextDocument
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QApplication


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output"
MD_PATH = OUTPUT_DIR / "miralys_tokens_pricing_report.md"
PDF_PATH = OUTPUT_DIR / "miralys_tokens_pricing_report.pdf"

MODEL_INPUT_PER_1M = 0.75
MODEL_OUTPUT_PER_1M = 4.50
WORDS_PER_MINUTE = 150.0
CHARS_PER_WORD = 7.0
CHARS_OVERHEAD = 80.0
TOKENS_PER_WORD = 1.33

STARTER_CREDIT_COST = 6.0 / 30000.0
CREATOR_CREDIT_COST = 11.0 / 121000.0
PRO_CREDIT_COST = 99.0 / 600000.0


def fmt_money(value: float, digits: int = 4) -> str:
    return f"${value:.{digits}f}"


def openai_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * MODEL_INPUT_PER_1M / 1_000_000.0) + (
        output_tokens * MODEL_OUTPUT_PER_1M / 1_000_000.0
    )


def tts_chars(seconds: float) -> int:
    words = seconds * WORDS_PER_MINUTE / 60.0
    return int(round((words * CHARS_PER_WORD) + CHARS_OVERHEAD))


def tts_cost(chars: int, credit_cost: float, credit_rate: float) -> float:
    return chars * credit_rate * credit_cost


def total_cost(openai_value: float, tts_value: float) -> float:
    return openai_value + tts_value


def money_range(low: float, high: float) -> str:
    return f"{fmt_money(low, 4)} - {fmt_money(high, 4)}"


def build_markdown() -> str:
    openai_10 = openai_cost(250, int(round(25 * TOKENS_PER_WORD)))
    openai_30 = openai_cost(250, int(round(75 * TOKENS_PER_WORD)))
    openai_60 = openai_cost(250, int(round(150 * TOKENS_PER_WORD)))

    chars_10 = tts_chars(10)
    chars_30 = tts_chars(30)
    chars_60 = tts_chars(60)
    chars_25 = tts_chars(25.6)

    starter_10_low = tts_cost(chars_10, STARTER_CREDIT_COST, 0.5)
    starter_10_high = tts_cost(chars_10, STARTER_CREDIT_COST, 1.0)
    starter_30_low = tts_cost(chars_30, STARTER_CREDIT_COST, 0.5)
    starter_30_high = tts_cost(chars_30, STARTER_CREDIT_COST, 1.0)
    starter_60_low = tts_cost(chars_60, STARTER_CREDIT_COST, 0.5)
    starter_60_high = tts_cost(chars_60, STARTER_CREDIT_COST, 1.0)

    creator_10_low = tts_cost(chars_10, CREATOR_CREDIT_COST, 0.5)
    creator_10_high = tts_cost(chars_10, CREATOR_CREDIT_COST, 1.0)
    creator_30_low = tts_cost(chars_30, CREATOR_CREDIT_COST, 0.5)
    creator_30_high = tts_cost(chars_30, CREATOR_CREDIT_COST, 1.0)
    creator_60_low = tts_cost(chars_60, CREATOR_CREDIT_COST, 0.5)
    creator_60_high = tts_cost(chars_60, CREATOR_CREDIT_COST, 1.0)

    pro_10_low = tts_cost(chars_10, PRO_CREDIT_COST, 0.5)
    pro_10_high = tts_cost(chars_10, PRO_CREDIT_COST, 1.0)
    pro_30_low = tts_cost(chars_30, PRO_CREDIT_COST, 0.5)
    pro_30_high = tts_cost(chars_30, PRO_CREDIT_COST, 1.0)
    pro_60_low = tts_cost(chars_60, PRO_CREDIT_COST, 0.5)
    pro_60_high = tts_cost(chars_60, PRO_CREDIT_COST, 1.0)

    reply_words = 64
    reply_seconds = reply_words / WORDS_PER_MINUTE * 60.0
    reply_chars = tts_chars(reply_seconds)
    reply_output_tokens = int(round(reply_words * TOKENS_PER_WORD))
    reply_openai = openai_cost(250, reply_output_tokens)

    md = f"""# Miralys Tokens Pricing Report

Este reporte estima el costo tecnico del modo default del asistente y lo traduce a una unidad comercial simple.

## Base comercial

- `1 token = 10 segundos` de uso efectivo
- uso efectivo = procesamiento de respuesta + generacion de audio

## Modo default del proyecto

Segun la configuracion actual del repo, el modo default es:

- `interaction_mode = voice`
- `openai_model = gpt-5.4-mini`
- `response_length = short`
- `history_level = normal`
- `stt_engine = local`
- `elevenlabs_model = eleven_turbo_v2_5`
- `tts_realtime = false`

Implicacion:

- STT local no agrega costo variable por llamada
- el costo variable principal viene de OpenAI + ElevenLabs
- en voz, ElevenLabs suele dominar el costo total

## Que significa "mismo valor por minuto"

Conserva la misma tarifa tecnica que antes:

- antes `100 tokens = 1 USD`
- antes `100 tokens = 10 minutos`
- eso equivale a `0.10 USD por minuto`

Si ahora quieres que `1 token = 10 segundos`, entonces:

- `100 tokens = 1000 segundos`
- `100 tokens = 16 minutos 40 segundos`

Si mantienes el mismo valor por minuto (`$0.10/min`), el nuevo precio es:

- `1 token = $0.0167`
- `100 tokens = $1.6667`

Formula:

- `precio nuevo = precio viejo * 1.6667`

## Fuentes oficiales usadas

- OpenAI pricing: https://developers.openai.com/api/docs/pricing
- ElevenLabs pricing: https://elevenlabs.io/pricing

## OpenAI: costo de generacion

Para `gpt-5.4-mini` la pagina oficial muestra:

- input: `$0.75 / 1M tokens`
- cached input: `$0.075 / 1M tokens`
- output: `$4.50 / 1M tokens`

Para este analisis uso input normal, no cached.

Suposiciones para convertir tiempo a costo:

- habla a 150 palabras por minuto
- 10 segundos de habla equivalen a unas 25 palabras
- el texto de salida usa unas 1.33 tokens por palabra
- cada generacion lleva un prompt de aproximadamente 250 tokens de entrada

Con eso:

| Tiempo | Input tokens aprox. | Output tokens aprox. | OpenAI costo aprox. |
|---|---:|---:|---:|
| 10 s | 250 | 33 | {fmt_money(openai_10, 5)} |
| 30 s | 250 | 100 | {fmt_money(openai_30, 5)} |
| 60 s | 250 | 200 | {fmt_money(openai_60, 5)} |

Conclusiones de OpenAI:

- el costo del modelo es muy bajo
- en este flujo, OpenAI no es el costo dominante
- el costo de voz y audio pesa mucho mas

## ElevenLabs: costo de audio

La pagina oficial indica:

- Free: 10k credits per month
- Starter: 30k credits for $6
- Creator: 121k credits for $11
- Pro: 600k credits for $99
- Text to Speech usa aproximadamente 1 credit por caracter
- para modelos V2.5 Flash/Turbo hay descuento de 0.5 a 1 credit por caracter

Como tu app usa `eleven_turbo_v2_5`, uso el rango `0.5 a 1 credit por caracter`.

Primero convierto tiempo a caracteres usando la misma heuristica interna del proyecto:

- `chars = words * 7 + 80`

Con 150 palabras por minuto:

- 10 s = 25 palabras = {chars_10} caracteres
- 30 s = 75 palabras = {chars_30} caracteres
- 60 s = 150 palabras = {chars_60} caracteres

### Costo por plan

Costo por 1 credit:

- Starter: {fmt_money(STARTER_CREDIT_COST, 6)}
- Creator: {fmt_money(CREATOR_CREDIT_COST, 6)}
- Pro: {fmt_money(PRO_CREDIT_COST, 6)}

Como el modelo puede cobrar de 0.5 a 1 credit por caracter:

#### Starter

| Tiempo | Costo TTS aprox. |
|---|---:|
| 10 s | {money_range(starter_10_low, starter_10_high)} |
| 30 s | {money_range(starter_30_low, starter_30_high)} |
| 60 s | {money_range(starter_60_low, starter_60_high)} |

#### Creator

| Tiempo | Costo TTS aprox. |
|---|---:|
| 10 s | {money_range(creator_10_low, creator_10_high)} |
| 30 s | {money_range(creator_30_low, creator_30_high)} |
| 60 s | {money_range(creator_60_low, creator_60_high)} |

#### Pro

| Tiempo | Costo TTS aprox. |
|---|---:|
| 10 s | {money_range(pro_10_low, pro_10_high)} |
| 30 s | {money_range(pro_30_low, pro_30_high)} |
| 60 s | {money_range(pro_60_low, pro_60_high)} |

## Costo total por generacion

Total = OpenAI + ElevenLabs.

Como OpenAI pesa muy poco, el total queda casi igual al costo TTS.

### Total con Starter

| Tiempo | Total aprox. |
|---|---:|
| 10 s | {money_range(total_cost(openai_10, starter_10_low), total_cost(openai_10, starter_10_high))} |
| 30 s | {money_range(total_cost(openai_30, starter_30_low), total_cost(openai_30, starter_30_high))} |
| 60 s | {money_range(total_cost(openai_60, starter_60_low), total_cost(openai_60, starter_60_high))} |

### Total con Creator

| Tiempo | Total aprox. |
|---|---:|
| 10 s | {money_range(total_cost(openai_10, creator_10_low), total_cost(openai_10, creator_10_high))} |
| 30 s | {money_range(total_cost(openai_30, creator_30_low), total_cost(openai_30, creator_30_high))} |
| 60 s | {money_range(total_cost(openai_60, creator_60_low), total_cost(openai_60, creator_60_high))} |

### Total con Pro

| Tiempo | Total aprox. |
|---|---:|
| 10 s | {money_range(total_cost(openai_10, pro_10_low), total_cost(openai_10, pro_10_high))} |
| 30 s | {money_range(total_cost(openai_30, pro_30_low), total_cost(openai_30, pro_30_high))} |
| 60 s | {money_range(total_cost(openai_60, pro_60_low), total_cost(openai_60, pro_60_high))} |

## Costo del reply default

Tu default actual pide respuestas cortas:

- `response_length = short`
- `max_response_words = 64`

Con la misma heuristica:

- `64 palabras` = {reply_seconds:.1f} segundos de habla aprox.
- `64 palabras` = {reply_chars} caracteres de audio aprox.

### Costo del reply corto default

| Plan | Total aprox. |
|---|---:|
| Starter | {money_range(total_cost(reply_openai, tts_cost(reply_chars, STARTER_CREDIT_COST, 0.5)), total_cost(reply_openai, tts_cost(reply_chars, STARTER_CREDIT_COST, 1.0)))} |
| Creator | {money_range(total_cost(reply_openai, tts_cost(reply_chars, CREATOR_CREDIT_COST, 0.5)), total_cost(reply_openai, tts_cost(reply_chars, CREATOR_CREDIT_COST, 1.0)))} |
| Pro | {money_range(total_cost(reply_openai, tts_cost(reply_chars, PRO_CREDIT_COST, 0.5)), total_cost(reply_openai, tts_cost(reply_chars, PRO_CREDIT_COST, 1.0)))} |

## Lectura por unidad de tiempo

Si piensas el producto como tiempo vendido:

- 10 segundos cuestan aproximadamente lo que ves en la tabla de 10 s
- 30 segundos cuestan aproximadamente 3 veces eso
- 1 minuto cuesta aproximadamente 6 veces eso

Como atajo operativo:

- con Starter, el costo efectivo ronda entre 2 y 22 centavos por 10 s
- con Creator, el costo efectivo ronda entre 1 y 10 centavos por 10 s
- con Pro, el costo efectivo ronda entre 2 y 9 centavos por 10 s

## Precio sugerido de venta

Si quieres margen sano, un multiplicador simple es 2.5x a 3x sobre costo.

### Starter

| Tiempo | Precio sugerido |
|---|---:|
| 10 s | $0.06 - $0.15 |
| 30 s | $0.15 - $0.36 |
| 60 s | $0.29 - $0.68 |

### Creator

| Tiempo | Precio sugerido |
|---|---:|
| 10 s | $0.03 - $0.07 |
| 30 s | $0.07 - $0.17 |
| 60 s | $0.13 - $0.31 |

### Pro

| Tiempo | Precio sugerido |
|---|---:|
| 10 s | $0.05 - $0.13 |
| 30 s | $0.13 - $0.30 |
| 60 s | $0.24 - $0.56 |

## Texto sugerido para oferta

> Un token equivale a 10 segundos de uso efectivo. En modo corto, una respuesta tipica cuesta entre unos centavos bajos y centavos medios segun el plan de voz. En el modo default completo, 1 minuto de uso cae aproximadamente en el rango de pocos centavos de costo tecnico, por lo que el precio comercial puede fijarse con margen sin perder competitividad.

## Nota importante

Los numeros de arriba son aproximados y cambian si:

- sube o baja la longitud real de respuesta
- cambia el modelo de OpenAI
- cambia el plan de ElevenLabs
- activas STT de pago en lugar de local
- cambias la velocidad de habla o el estilo de voz
"""
    return md


def build_html() -> str:
    openai_10 = openai_cost(250, int(round(25 * TOKENS_PER_WORD)))
    openai_30 = openai_cost(250, int(round(75 * TOKENS_PER_WORD)))
    openai_60 = openai_cost(250, int(round(150 * TOKENS_PER_WORD)))

    chars_10 = tts_chars(10)
    chars_30 = tts_chars(30)
    chars_60 = tts_chars(60)
    chars_25 = tts_chars(25.6)

    starter_10_low = tts_cost(chars_10, STARTER_CREDIT_COST, 0.5)
    starter_10_high = tts_cost(chars_10, STARTER_CREDIT_COST, 1.0)
    starter_30_low = tts_cost(chars_30, STARTER_CREDIT_COST, 0.5)
    starter_30_high = tts_cost(chars_30, STARTER_CREDIT_COST, 1.0)
    starter_60_low = tts_cost(chars_60, STARTER_CREDIT_COST, 0.5)
    starter_60_high = tts_cost(chars_60, STARTER_CREDIT_COST, 1.0)

    creator_10_low = tts_cost(chars_10, CREATOR_CREDIT_COST, 0.5)
    creator_10_high = tts_cost(chars_10, CREATOR_CREDIT_COST, 1.0)
    creator_30_low = tts_cost(chars_30, CREATOR_CREDIT_COST, 0.5)
    creator_30_high = tts_cost(chars_30, CREATOR_CREDIT_COST, 1.0)
    creator_60_low = tts_cost(chars_60, CREATOR_CREDIT_COST, 0.5)
    creator_60_high = tts_cost(chars_60, CREATOR_CREDIT_COST, 1.0)

    pro_10_low = tts_cost(chars_10, PRO_CREDIT_COST, 0.5)
    pro_10_high = tts_cost(chars_10, PRO_CREDIT_COST, 1.0)
    pro_30_low = tts_cost(chars_30, PRO_CREDIT_COST, 0.5)
    pro_30_high = tts_cost(chars_30, PRO_CREDIT_COST, 1.0)
    pro_60_low = tts_cost(chars_60, PRO_CREDIT_COST, 0.5)
    pro_60_high = tts_cost(chars_60, PRO_CREDIT_COST, 1.0)

    reply_words = 64
    reply_seconds = reply_words / WORDS_PER_MINUTE * 60.0
    reply_chars = tts_chars(reply_seconds)
    reply_output_tokens = int(round(reply_words * TOKENS_PER_WORD))
    reply_openai = openai_cost(250, reply_output_tokens)

    def row(*cells: str, header: bool = False) -> str:
        tag = "th" if header else "td"
        inner = "".join(f"<{tag}>{cell}</{tag}>" for cell in cells)
        return f"<tr>{inner}</tr>"

    return f"""
    <html>
    <head>
      <meta charset="utf-8" />
      <style>
        body {{
          font-family: Arial, sans-serif;
          font-size: 10.5pt;
          line-height: 1.35;
          color: #111;
        }}
        h1 {{ font-size: 22pt; margin-bottom: 10px; }}
        h2 {{ font-size: 15pt; margin-top: 18px; margin-bottom: 8px; }}
        h3 {{ font-size: 12pt; margin-top: 14px; margin-bottom: 6px; }}
        table {{
          border-collapse: collapse;
          width: 100%;
          margin: 8px 0 12px 0;
        }}
        th, td {{
          border: 1px solid #999;
          padding: 5px 7px;
          vertical-align: top;
        }}
        th {{
          background: #efefef;
        }}
        p {{ margin: 0 0 8px 0; }}
        ul {{ margin: 4px 0 10px 24px; }}
        code {{
          font-family: Consolas, "Courier New", monospace;
          font-size: 9.5pt;
        }}
      </style>
    </head>
    <body>
      <h1>Miralys Tokens Pricing Report</h1>
      <p>Este reporte estima el costo tecnico del modo default del asistente y lo traduce a una unidad comercial simple.</p>

      <h2>Base comercial</h2>
      <ul>
        <li><code>1 token = 10 segundos</code> de uso efectivo</li>
        <li>uso efectivo = procesamiento de respuesta + generacion de audio</li>
      </ul>

      <h2>Modo default del proyecto</h2>
      <p>Segun la configuracion actual del repo, el modo default es:</p>
      <ul>
        <li><code>interaction_mode = voice</code></li>
        <li><code>openai_model = gpt-5.4-mini</code></li>
        <li><code>response_length = short</code></li>
        <li><code>history_level = normal</code></li>
        <li><code>stt_engine = local</code></li>
        <li><code>elevenlabs_model = eleven_turbo_v2_5</code></li>
        <li><code>tts_realtime = false</code></li>
      </ul>
      <p>Implicacion:</p>
      <ul>
        <li>STT local no agrega costo variable por llamada</li>
        <li>el costo variable principal viene de OpenAI + ElevenLabs</li>
        <li>en voz, ElevenLabs suele dominar el costo total</li>
      </ul>

      <h2>Que significa "mismo valor por minuto"</h2>
      <ul>
        <li>antes <code>100 tokens = 1 USD</code></li>
        <li>antes <code>100 tokens = 10 minutos</code></li>
        <li>eso equivale a <code>0.10 USD por minuto</code></li>
      </ul>
      <p>Si ahora quieres que <code>1 token = 10 segundos</code>, entonces <code>100 tokens = 16 minutos 40 segundos</code>.</p>
      <p>Si mantienes el mismo valor por minuto (<code>$0.10/min</code>), el nuevo precio es <code>1 token = $0.0167</code> y <code>100 tokens = $1.6667</code>.</p>
      <p>Formula: <code>precio nuevo = precio viejo * 1.6667</code></p>

      <h2>OpenAI: costo de generacion</h2>
      <p>Para <code>gpt-5.4-mini</code> la pagina oficial muestra:</p>
      <ul>
        <li>input: <code>$0.75 / 1M tokens</code></li>
        <li>cached input: <code>$0.075 / 1M tokens</code></li>
        <li>output: <code>$4.50 / 1M tokens</code></li>
      </ul>
      <p>Suposiciones para convertir tiempo a costo:</p>
      <ul>
        <li>habla a 150 palabras por minuto</li>
        <li>10 segundos de habla equivalen a unas 25 palabras</li>
        <li>el texto de salida usa unas 1.33 tokens por palabra</li>
        <li>cada generacion lleva un prompt de aproximadamente 250 tokens de entrada</li>
      </ul>
      <table>
        {row("Tiempo", "Input tokens aprox.", "Output tokens aprox.", "OpenAI costo aprox.", header=True)}
        {row("10 s", "250", "33", fmt_money(openai_10, 5))}
        {row("30 s", "250", "100", fmt_money(openai_30, 5))}
        {row("60 s", "250", "200", fmt_money(openai_60, 5))}
      </table>

      <h2>ElevenLabs: costo de audio</h2>
      <ul>
        <li>Free: 10k credits per month</li>
        <li>Starter: 30k credits for $6</li>
        <li>Creator: 121k credits for $11</li>
        <li>Pro: 600k credits for $99</li>
        <li>Text to Speech usa aproximadamente 1 credit por caracter</li>
        <li>para modelos V2.5 Flash/Turbo hay descuento de 0.5 a 1 credit por caracter</li>
      </ul>
      <p>Como tu app usa <code>eleven_turbo_v2_5</code>, uso el rango <code>0.5 a 1 credit por caracter</code>.</p>
      <p>Primero convierto tiempo a caracteres usando la misma heuristica interna del proyecto: <code>chars = words * 7 + 80</code>.</p>
      <p>Con 150 palabras por minuto:</p>
      <ul>
        <li>10 s = 25 palabras = {chars_10} caracteres</li>
        <li>30 s = 75 palabras = {chars_30} caracteres</li>
        <li>60 s = 150 palabras = {chars_60} caracteres</li>
      </ul>
      <p>Costo por 1 credit:</p>
      <ul>
        <li>Starter: {fmt_money(STARTER_CREDIT_COST, 6)}</li>
        <li>Creator: {fmt_money(CREATOR_CREDIT_COST, 6)}</li>
        <li>Pro: {fmt_money(PRO_CREDIT_COST, 6)}</li>
      </ul>

      <h3>Starter</h3>
      <table>
        {row("Tiempo", "Costo TTS aprox.", header=True)}
        {row("10 s", money_range(starter_10_low, starter_10_high))}
        {row("30 s", money_range(starter_30_low, starter_30_high))}
        {row("60 s", money_range(starter_60_low, starter_60_high))}
      </table>

      <h3>Creator</h3>
      <table>
        {row("Tiempo", "Costo TTS aprox.", header=True)}
        {row("10 s", money_range(creator_10_low, creator_10_high))}
        {row("30 s", money_range(creator_30_low, creator_30_high))}
        {row("60 s", money_range(creator_60_low, creator_60_high))}
      </table>

      <h3>Pro</h3>
      <table>
        {row("Tiempo", "Costo TTS aprox.", header=True)}
        {row("10 s", money_range(pro_10_low, pro_10_high))}
        {row("30 s", money_range(pro_30_low, pro_30_high))}
        {row("60 s", money_range(pro_60_low, pro_60_high))}
      </table>

      <h2>Costo total por generacion</h2>
      <p>Total = OpenAI + ElevenLabs. Como OpenAI pesa muy poco, el total queda casi igual al costo TTS.</p>

      <h3>Starter</h3>
      <table>
        {row("Tiempo", "Total aprox.", header=True)}
        {row("10 s", money_range(openai_10 + starter_10_low, openai_10 + starter_10_high))}
        {row("30 s", money_range(openai_30 + starter_30_low, openai_30 + starter_30_high))}
        {row("60 s", money_range(openai_60 + starter_60_low, openai_60 + starter_60_high))}
      </table>

      <h3>Creator</h3>
      <table>
        {row("Tiempo", "Total aprox.", header=True)}
        {row("10 s", money_range(openai_10 + creator_10_low, openai_10 + creator_10_high))}
        {row("30 s", money_range(openai_30 + creator_30_low, openai_30 + creator_30_high))}
        {row("60 s", money_range(openai_60 + creator_60_low, openai_60 + creator_60_high))}
      </table>

      <h3>Pro</h3>
      <table>
        {row("Tiempo", "Total aprox.", header=True)}
        {row("10 s", money_range(openai_10 + pro_10_low, openai_10 + pro_10_high))}
        {row("30 s", money_range(openai_30 + pro_30_low, openai_30 + pro_30_high))}
        {row("60 s", money_range(openai_60 + pro_60_low, openai_60 + pro_60_high))}
      </table>

      <h2>Costo del reply default</h2>
      <p>Tu default actual pide respuestas cortas: <code>response_length = short</code> y <code>max_response_words = 64</code>.</p>
      <p>Con la misma heuristica, 64 palabras son aproximadamente {reply_seconds:.1f} segundos de habla y {reply_chars} caracteres de audio.</p>
      <table>
        {row("Plan", "Total aprox.", header=True)}
        {row("Starter", money_range(reply_openai + tts_cost(reply_chars, STARTER_CREDIT_COST, 0.5), reply_openai + tts_cost(reply_chars, STARTER_CREDIT_COST, 1.0)))}
        {row("Creator", money_range(reply_openai + tts_cost(reply_chars, CREATOR_CREDIT_COST, 0.5), reply_openai + tts_cost(reply_chars, CREATOR_CREDIT_COST, 1.0)))}
        {row("Pro", money_range(reply_openai + tts_cost(reply_chars, PRO_CREDIT_COST, 0.5), reply_openai + tts_cost(reply_chars, PRO_CREDIT_COST, 1.0)))}
      </table>

      <h2>Lectura por unidad de tiempo</h2>
      <ul>
        <li>10 segundos cuestan aproximadamente lo que ves en la tabla de 10 s</li>
        <li>30 segundos cuestan aproximadamente 3 veces eso</li>
        <li>1 minuto cuesta aproximadamente 6 veces eso</li>
      </ul>
      <p>Como atajo operativo:</p>
      <ul>
        <li>con Starter, el costo efectivo ronda entre 2 y 22 centavos por 10 s</li>
        <li>con Creator, el costo efectivo ronda entre 1 y 10 centavos por 10 s</li>
        <li>con Pro, el costo efectivo ronda entre 2 y 9 centavos por 10 s</li>
      </ul>

      <h2>Precio sugerido de venta</h2>
      <p>Si quieres margen sano, un multiplicador simple es 2.5x a 3x sobre costo.</p>
      <h3>Starter</h3>
      <table>
        {row("Tiempo", "Precio sugerido", header=True)}
        {row("10 s", "$0.06 - $0.15")}
        {row("30 s", "$0.15 - $0.36")}
        {row("60 s", "$0.29 - $0.68")}
      </table>
      <h3>Creator</h3>
      <table>
        {row("Tiempo", "Precio sugerido", header=True)}
        {row("10 s", "$0.03 - $0.07")}
        {row("30 s", "$0.07 - $0.17")}
        {row("60 s", "$0.13 - $0.31")}
      </table>
      <h3>Pro</h3>
      <table>
        {row("Tiempo", "Precio sugerido", header=True)}
        {row("10 s", "$0.05 - $0.13")}
        {row("30 s", "$0.13 - $0.30")}
        {row("60 s", "$0.24 - $0.56")}
      </table>

      <h2>Texto sugerido para oferta</h2>
      <blockquote>
        Un token equivale a 10 segundos de uso efectivo. En modo corto, una respuesta tipica cuesta entre unos centavos bajos y centavos medios segun el plan de voz. En el modo default completo, 1 minuto de uso cae aproximadamente en el rango de pocos centavos de costo tecnico, por lo que el precio comercial puede fijarse con margen sin perder competitividad.
      </blockquote>

      <h2>Nota importante</h2>
      <p>Los numeros de arriba son aproximados y cambian si sube o baja la longitud real de respuesta, cambia el modelo de OpenAI, cambia el plan de ElevenLabs, activas STT de pago en lugar de local o cambias la velocidad de habla o el estilo de voz.</p>
    </body>
    </html>
    """


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    markdown = build_markdown()
    MD_PATH.write_text(markdown, encoding="utf-8")

    app = QApplication([])
    doc = QTextDocument()
    doc.setHtml(build_html())

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(PDF_PATH))
    printer.setResolution(300)
    doc.print_(printer)
    app.quit()


if __name__ == "__main__":
    main()
