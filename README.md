# MyralisBackend

Backend local de escritorio para Windows construido con Python 3.11 y PySide6. Incluye conversacion por texto y voz, integracion con OpenAI, TTS con ElevenLabs, seleccion persistente de dispositivos, TEST MODE, WebSocket local para Unreal y una UI tecnica de debug.

## Estructura

```text
MyralisBackend/
|-- main.py
|-- requirements.txt
|-- config/
|   |-- settings.json
|   `-- devices.json
|-- core/
|   |-- openai_manager.py
|   |-- elevenlabs_manager.py
|   |-- audio_manager.py
|   |-- settings_manager.py
|   |-- conversation_manager.py
|   `-- test_mode_manager.py
|-- ui/
|   |-- main_window.py
|   |-- settings_dialog.py
|   `-- chat_panel.py
`-- output/
    |-- audio/
    |-- logs/
    `-- runtime/
```

## Instalacion

1. Crear y activar entorno virtual:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Instalar dependencias:

```powershell
pip install -r requirements.txt
```

3. Configurar credenciales en `.env`:

```env
OPENAI_API_KEY=tu_api_key
ELEVENLABS_API_KEY=tu_api_key
DEEPGRAM_API_KEY=tu_api_key
```

## Ejecucion

```powershell
python main.py
```

La UI tecnica solo puede abrirse con `Ctrl+Shift+D` cuando la sesion validada tiene autorizacion de admin.

## Configuracion

- `config/settings.json`: settings tecnicos, modelos, voz, customization recibida desde Unreal y TEST MODE.
- `config/devices.json`: dispositivo de entrada y salida seleccionados.

## Ruta y nombre oficiales

- Nombre oficial del backend: `MyralisBackend`
- Nombre visible del producto: `Myralis Backend`
- Servicio logico: `myralis-backend`
- Ejecutable futuro: `MyralisBackend.exe`

Durante la migracion temporal, el launcher puede aceptar la carpeta legacy `PYTHON_AI_ASSISTANT` como fallback solo por compatibilidad explicita.

## Reportes

- [`PRICING_REPORT.md`](./PRICING_REPORT.md): reporte separado con la equivalencia actual de tokens a USD y ejemplos de uso.
- [`docs/runtime-layout.md`](./docs/runtime-layout.md): layout runtime, resolucion portable y pasos de renombrado manual.

## Customization

La identidad del personaje vive en el prompt configurado desde Unreal. Python no mantiene un editor manual de nombre, edad, genero, rol, historia o rasgos legacy.

Python solo interpreta la customization enviada por Unreal, como `personality_traits`, `profanity_filter`, `voice_id`, `use_custom_voice`, `custom_voice_id` y `selected_character`. `selected_character` se almacena de forma pasiva; la parte visual pertenece a Unreal.

## TEST MODE

Cuando `TEST MODE` esta activado:

- La primera respuesta de OpenAI se guarda en `output/logs/test_mode_response.json`.
- La primera generacion de ElevenLabs se guarda en `output/audio/test_mode_response.wav`.
- Las siguientes pruebas reutilizan esos archivos y no realizan nuevas llamadas para texto o audio mientras existan.

## Audio y voz

La app puede enviar audio a Unreal por WebSocket y mantiene `output/runtime/current_response.wav` como salida runtime. La reproduccion local en Python permanece apagada por defecto para evitar audio duplicado; activala con `TEST MODE CON AUDIO` solo para pruebas locales.

## Empaquetado

Ver [`docs/backend-packaging.md`](./docs/backend-packaging.md) para el flujo onedir, icono, paths portables, autorizacion de debug y despliegue local.
