# ESP-IDF build wrapper for long Windows project paths.
# This project path exceeds MAX_PATH; the component manager and ninja cannot
# copy or compile reliably from the default build/ directory.

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$IdfArgs = @("build")
)

$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot
$BuildDir = "C:\esp\sirena-p4-build"
$IdfPython = "C:\Users\udayk\.espressif\python_env\idf5.5_py3.10_env\Scripts\python.exe"

if (-not (Test-Path $IdfPython)) {
    $IdfPython = "C:\Users\udayk\.espressif\tools\idf-python\3.11.2\python.exe"
}

if (-not (Test-Path $IdfPython)) {
    throw "ESP-IDF Python not found. Open the ESP-IDF terminal first."
}

& $IdfPython "$ProjectDir\tools\install_managed_components.py"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install managed_components. Run idf.py reconfigure once to populate the component cache, then retry."
}

if (-not (Test-Path "C:\esp")) {
    New-Item -ItemType Directory -Path "C:\esp" | Out-Null
}

Push-Location $ProjectDir
try {
    idf.py -B $BuildDir @IdfArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
