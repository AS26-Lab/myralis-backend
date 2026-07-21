# Pricing report

Este reporte estima el costo del modo default del asistente y lo traduce a una unidad comercial simple.

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

## Precio por token comercial

Si quieres conservar el mismo valor por minuto que antes:

- antes `100 tokens = 1 USD`
- antes `100 tokens = 10 minutos`
- entonces `1 token = 0.10 USD por minuto equivalente`

Con la nueva regla:

- `1 token = 10 segundos`
- por lo tanto `1 token = 1/6 de minuto`

Nuevo precio proporcional:

- `1 token = $0.0167`
- `10 tokens = $0.1667`
- `100 tokens = $1.6667`
- `1,000 tokens = $16.6667`

Formula:

- `precio nuevo = precio viejo * 1.6667`

## Fuentes oficiales usadas

- OpenAI pricing: [https://developers.openai.com/api/docs/pricing](https://developers.openai.com/api/docs/pricing)
- ElevenLabs pricing: [https://elevenlabs.io/pricing](https://elevenlabs.io/pricing)

## OpenAI: costo de generacion

Para `gpt-5.4-mini` en pricing standard corto, la pagina oficial muestra:

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
| 10 s | 250 | 33 | $0.00034 |
| 30 s | 250 | 100 | $0.00064 |
| 60 s | 250 | 200 | $0.00109 |

Conclusiones de OpenAI:

- el costo del modelo es muy bajo
- en este flujo, OpenAI no es el costo dominante
- el costo de voz y audio pesa mucho mas

## ElevenLabs: costo de audio

La pagina oficial indica:

- Free: 10k credits por mes
- Starter: 30k credits por $6
- Creator: 121k credits por $11
- Pro: 600k credits por $99
- Text to Speech usa aproximadamente 1 credit por caracter
- para modelos V2.5 Flash/Turbo hay descuento de 0.5 a 1 credit por caracter

Como tu app usa `eleven_turbo_v2_5`, uso el rango `0.5 a 1 credit por caracter`.

Primero convierto tiempo a caracteres usando la misma heuristica interna del proyecto:

- `chars = words * 7 + 80`

Con 150 palabras por minuto:

- 10 s = 25 palabras = 255 caracteres
- 30 s = 75 palabras = 605 caracteres
- 60 s = 150 palabras = 1,130 caracteres

### Costo por plan

Costo por 1 credit:

- Starter: `$6 / 30,000 = $0.000200`
- Creator: `$11 / 121,000 = $0.0000909`
- Pro: `$99 / 600,000 = $0.000165`

Como el modelo puede cobrar de `0.5` a `1` credit por caracter:

#### Starter

| Tiempo | Costo TTS aprox. |
|---|---:|
| 10 s | $0.0255 - $0.0510 |
| 30 s | $0.0605 - $0.1210 |
| 60 s | $0.1130 - $0.2260 |

#### Creator

| Tiempo | Costo TTS aprox. |
|---|---:|
| 10 s | $0.0116 - $0.0232 |
| 30 s | $0.0275 - $0.0550 |
| 60 s | $0.0512 - $0.1027 |

#### Pro

| Tiempo | Costo TTS aprox. |
|---|---:|
| 10 s | $0.0210 - $0.0420 |
| 30 s | $0.0499 - $0.0998 |
| 60 s | $0.0932 - $0.1865 |

## Costo total por generacion

Total = OpenAI + ElevenLabs.

Como OpenAI pesa muy poco, el total queda casi igual al costo TTS.

### Total con Starter

| Tiempo | Total aprox. |
|---|---:|
| 10 s | $0.0258 - $0.0513 |
| 30 s | $0.0611 - $0.1216 |
| 60 s | $0.1141 - $0.2271 |

### Total con Creator

| Tiempo | Total aprox. |
|---|---:|
| 10 s | $0.0119 - $0.0235 |
| 30 s | $0.0281 - $0.0556 |
| 60 s | $0.0523 - $0.1038 |

### Total con Pro

| Tiempo | Total aprox. |
|---|---:|
| 10 s | $0.0213 - $0.0423 |
| 30 s | $0.0505 - $0.1004 |
| 60 s | $0.0943 - $0.1876 |

## Costo del reply default

Tu default actual pide respuestas cortas:

- `response_length = short`
- `max_response_words = 64`

Con la misma heuristica:

- `64 palabras * 7 + 80 = 528 caracteres`

Eso equivale aprox. a una respuesta de 25 a 26 segundos de voz.

### Costo del reply corto default

| Plan | Total aprox. |
|---|---:|
| Starter | $0.0529 - $0.1060 |
| Creator | $0.0244 - $0.0484 |
| Pro | $0.0439 - $0.0877 |

## Lectura por unidad de tiempo

Si piensas el producto como tiempo vendido:

- 10 segundos cuestan aproximadamente lo que ves en la tabla de 10 s
- 30 segundos cuestan aproximadamente 3 veces eso
- 1 minuto cuesta aproximadamente 6 veces eso

Si quieres un numero rapido para operar:

- con Starter, el costo efectivo ronda `$0.03 a $0.05` por 10 s
- con Creator, el costo efectivo ronda `$0.01 a $0.02` por 10 s
- con Pro, el costo efectivo ronda `$0.02 a $0.04` por 10 s

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

> Un token equivale a 10 segundos de uso efectivo. En modo corto, una respuesta tipica cuesta entre 2 y 11 centavos segun el plan de voz. En el modo default completo, 1 minuto de uso cae aprox. entre 3 y 23 centavos de costo tecnico, por lo que el precio comercial puede fijarse con margen sin perder competitividad.

## Nota importante

Los numeros de arriba son aproximados y cambian si:

- sube o baja la longitud real de respuesta
- cambia el modelo de OpenAI
- cambia el plan de ElevenLabs
- activas STT de pago en lugar de local
- cambias la velocidad de habla o el estilo de voz

Si quieres, el siguiente paso es convertir este reporte en una pagina interna de la UI para que puedas mover precios sin tocar el codigo.
