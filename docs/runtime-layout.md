# Runtime Layout

## Nombre oficial

- Backend project folder: `MyralisBackend`
- Display name: `Myralis Backend`
- Service name: `myralis-backend`
- Future executable: `MyralisBackend.exe`

## Resolucion portable

El launcher debe resolver la ruta del backend sin rutas personales hardcodeadas y con este orden:

1. `MYRALIS_BACKEND_SOURCE_DIR`
2. ruta explicita configurada por el usuario
3. `../MyralisBackend`
4. `../PYTHON_AI_ASSISTANT` como fallback legacy
5. error claro si ninguna existe

Si la carpeta legacy existe y se usa, debe seguir funcionando y registrar un warning de migracion. Si existen ambas carpetas, debe preferirse `MyralisBackend` e ignorar la legacy.

## Modo de desarrollo

La ejecucion en desarrollo debe apuntar al proyecto Python:

```powershell
../MyralisBackend/main.py
```

## Fallback temporal

Mientras dure la migracion, el launcher puede resolver:

```powershell
../PYTHON_AI_ASSISTANT/main.py
```

## Modo de produccion

La ruta futura del binario empaquetado debe ser:

```powershell
Runtime/Backend/MyralisBackend.exe
```

## Variables de entorno

- `MYRALIS_BACKEND_SOURCE_DIR`
- `MYRALIS_BACKEND_EXE`

## Renaming the backend project

Sigue estos pasos exactos para renombrar la carpeta externa sin cambiar imports Python internos:

1. Cierra el Launcher.
2. Cierra el backend Python.
3. Cierra PyCharm si tiene el proyecto abierto y bloquea archivos.
4. Renombra la carpeta `PYTHON_AI_ASSISTANT` a `MyralisBackend`.
5. Abre el proyecto renombrado en PyCharm.
6. Ejecuta `python main.py`.
7. Prueba `http://127.0.0.1:8766/health`.
8. Ejecuta `python scripts/check-runtime-layout.py` desde el Launcher.
9. Abre el Launcher.
10. Inicia full stack.

Renombrar la carpeta externa normalmente no obliga a cambiar imports Python si los paquetes internos siguen iguales. Solo cambia código si existe alguna referencia que dependa del nombre externo, rutas absolutas guardadas o working directories fijados a mano.

## Riesgos del renombrado

- `imports`: normalmente no cambian si la estructura interna se mantiene.
- `entorno virtual`: el `.venv` puede seguir sirviendo si apunta por ruta relativa o si se recrea dentro del nuevo root; las activaciones por ruta absoluta sí se rompen.
- `PyCharm`: puede necesitar reindexado y actualizar el proyecto abierto.
- `scripts`: cualquier script con rutas absolutas al root viejo debe actualizarse.
- `.env`: si contiene rutas absolutas al root viejo, hay que corregirlas.
- `subprocess`: comandos que dependan del nombre de carpeta o del cwd deben revisarse.
- `tests`: pruebas con paths hardcodeados al root viejo pueden fallar.
- `Launcher`: debe resolver por nombre nuevo con fallback legacy temporal.
