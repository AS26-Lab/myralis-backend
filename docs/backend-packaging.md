# MyralisBackend Packaging

## Build mode

- Use PyInstaller in `onedir`.
- Keep `console=True` for the first packaging stage.
- Do not use `onefile` yet.
- The entrypoint is `main.py`.

## Official identity

- Backend folder: `MyralisBackend`
- Executable: `MyralisBackend.exe`
- Service name: `myralis-backend`
- Display name: `Myralis Backend`

## Icon

- Preferred icon path: `assets/icons/myralis_backend.ico`
- The spec resolves that path relative to the project root.
- If the icon is missing, the build continues with the default Windows icon.

To generate an `.ico` from a PNG, use:

```powershell
python scripts/prepare_backend_icon.py --png path\to\source.png --out assets/icons/myralis_backend.ico
```

## Portable paths

In source mode, the project root is resolved from the repository tree.
In frozen mode, the executable root is `Path(sys.executable).resolve().parent`.

Mutable runtime data should not live in `_internal`.

### Source/runtime locations

- Logs: `%LOCALAPPDATA%\Myralis AI\Logs\Backend` in frozen mode, `output/logs` in source mode
- Config: `%LOCALAPPDATA%\Myralis AI\Config` in frozen mode, `config/` in source mode
- Temp: `%LOCALAPPDATA%\Myralis AI\Temp\Backend` in frozen mode, `output/temp` in source mode

## Secrets

The build must not bundle real secrets.

Resolution order:

1. Environment variables
2. External config file under `%LOCALAPPDATA%\Myralis AI\Config\.env`
3. Launcher-provided secure contract, if present
4. Clear error

Never print full API keys, tokens, or full license keys to console, logs, health, or WebSocket payloads.

## License and authorization

The backend must fail closed for technical-panel access.

- Admin authorization is required to open the technical panel with `Ctrl+Shift+D`
- `developer_mode_allowed` alone is not enough
- A local editable file must never grant technical-panel access
- If the session loses authorization, the panel must close and debug must reset

Ser administrador no activa debug automaticamente.
Solo concede permiso para abrir el panel tecnico.

## Debug defaults

All debug flags start `false`.

Examples:

- `global_debug`
- `realtime_audio_debug`
- `verbose_logging`
- `technical_panel_visible`
- `websocket_debug`
- `audio_debug`
- `stt_debug`
- `tts_debug`

Do not restore these flags from local files unless authorization is revalidated.

## Health

`http://127.0.0.1:8766/health` should return:

```json
{
  "ok": true,
  "service": "myralis-backend",
  "status": "running",
  "websocket_running": true,
  "authorized_session": true
}
```

Do not expose:

- email
- full license key
- API keys
- tokens
- private paths

## WebSocket

- Unreal WebSocket: `127.0.0.1:8765`
- Preserve coordinated shutdown from Unreal.
- Shutdown must be idempotent.

## Build

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_backend.ps1
```

## Local install

Install the built folder to:

`%LOCALAPPDATA%\Myralis AI\Runtime\Backend`

The installed folder must contain:

- `MyralisBackend.exe`
- `_internal/`
- `assets/`
- `ui/`
- any required data files

## Launcher integration

The Launcher should use:

- `backend_launch_mode = "exe"`
- `runtime.backend_relative_path = "Backend/MyralisBackend.exe"`
- working directory: `%LOCALAPPDATA%\Myralis AI\Runtime\Backend`

The Launcher must pass only validated authorization context.

