param()

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir '..')
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { 'python' }
$distDir = Join-Path $repoRoot 'dist'
$buildDir = Join-Path $repoRoot 'build'
$exePath = Join-Path $distDir 'MyralisBackend\MyralisBackend.exe'
$specPath = Join-Path $repoRoot 'MyralisBackend.spec'
$iconPath = Join-Path $repoRoot 'assets\icons\myralis_backend.ico'

Write-Host "Project root: $repoRoot"
Write-Host "Python: $python"

& $python -c "import sys; print(sys.version)"
& $python -m pip install -r (Join-Path $repoRoot 'requirements.txt')
& $python -m pip install -r (Join-Path $repoRoot 'requirements-dev.txt')

if (Test-Path $buildDir) {
    Remove-Item -Recurse -Force $buildDir
}
if (Test-Path $distDir) {
    Remove-Item -Recurse -Force $distDir
}

if (Test-Path $iconPath) {
    Write-Host "Using icon: $iconPath"
} else {
    Write-Host "Icono no encontrado; se generará la build con el icono predeterminado."
}

& $python -m PyInstaller --noconfirm $specPath

if (-not (Test-Path $exePath)) {
    throw "Executable not found: $exePath"
}

$item = Get-Item $exePath
Write-Host "Built executable: $($item.FullName)"
Write-Host "Size bytes: $($item.Length)"

