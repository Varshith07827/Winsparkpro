# Build winSpark.exe with PyInstaller, using the project's virtualenv.
# Run from winspark_py/:   ./build_exe.ps1
# Output: dist/winSpark.exe
#
# The PyInstaller work directory lives OUTSIDE the project on purpose: this
# project sits inside OneDrive, whose sync engine locks the thousands of small
# files PyInstaller churns through (seen live as "Access is denied" deleting
# build\...\localpycs during --clean). Scratch goes to LOCALAPPDATA, which is
# never synced; only the final single exe lands in the project's dist/.
#
# ASCII only in this file: Windows PowerShell 5.1 reads BOM-less UTF-8 as
# ANSI, and a mangled em-dash inside a string becomes a smart quote that
# PowerShell treats as a string delimiter (seen live as "Missing closing '}'").

$ErrorActionPreference = "Stop"

# Find a Python to build with, in order of preference:
#   1. the virtualenv that's ACTIVE in this shell (works from any checkout),
#   2. a .venv sitting next to this script,
#   3. plain "python" from PATH.
$python = $null
if ($env:VIRTUAL_ENV) {
    $candidate = Join-Path $env:VIRTUAL_ENV "Scripts/python.exe"
    if (Test-Path $candidate) { $python = $candidate }
}
if (-not $python) {
    $candidate = Join-Path $PSScriptRoot ".venv/Scripts/python.exe"
    if (Test-Path $candidate) { $python = $candidate }
}
if (-not $python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source }
}
if (-not $python) {
    throw "No Python found. Activate your virtualenv (or create one at $PSScriptRoot\.venv) and try again."
}
Write-Host "Using Python: $python" -ForegroundColor Cyan

# Derive project name from folder name and sanitize for filesystem
$projectName = (Split-Path $PSScriptRoot -Leaf) -replace '[^A-Za-z0-9_]','_'
$exeName = "$projectName.exe"

# Choose entrypoint: prefer run.py, then wadam\__main__.py, then first .py in repo root
$entry = $null
if (Test-Path (Join-Path $PSScriptRoot "run.py")) {
    $entry = Join-Path $PSScriptRoot "run.py"
} elseif (Test-Path (Join-Path $PSScriptRoot "wadam\__main__.py")) {
    $entry = Join-Path $PSScriptRoot "wadam\__main__.py"
} else {
    $firstPy = Get-ChildItem -Path $PSScriptRoot -Filter "*.py" -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($firstPy) { $entry = $firstPy.FullName }
}
if (-not $entry) { throw "No entrypoint found. Add a run.py or wadam\__main__.py and try again." }
Write-Host "Entrypoint: $entry" -ForegroundColor Cyan

# If this project package exists, ensure dependencies are installed (quick sanity check)
if (Test-Path (Join-Path $PSScriptRoot "wadam")) {
    & $python -c "import wadam" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "This Python is missing the project's dependencies. Run:  & '$python' -m pip install -r '$PSScriptRoot\requirements.txt'  and build again."
    }
}

Write-Host "Ensuring PyInstaller is installed..." -ForegroundColor Cyan
& $python -m pip install --quiet --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

# Work and dist paths
$workpath = Join-Path $env:LOCALAPPDATA "$projectName\build"
$distpath = Join-Path $PSScriptRoot "dist"

# Clear leftover scratch from any previous (possibly OneDrive-locked) run.
foreach ($stale in @($workpath, (Join-Path $PSScriptRoot "build"))) {
    if (Test-Path $stale) {
        try {
            Remove-Item -Recurse -Force $stale -ErrorAction Stop
        } catch {
            Write-Host "Note: could not fully remove $stale - continuing." -ForegroundColor Yellow
        }
    }
}

# Detect if this project is a GUI app (has a UI folder)
$isGui = Test-Path (Join-Path $PSScriptRoot "wadam\ui")

Write-Host "Building $exeName..." -ForegroundColor Cyan

$pyinstallerArgs = @(
    "--noconfirm",
    "--workpath", $workpath,
    "--distpath", $distpath,
    "--name", $projectName,
    "--onefile"
)
if ($isGui) { $pyinstallerArgs += "--windowed" }
$pyinstallerArgs += $entry

& $python -m PyInstaller @pyinstallerArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

$exe = Join-Path $distpath $exeName
if (-not (Test-Path $exe)) { throw "Build reported success but $exe was not produced." }
Write-Host "Done. $exe" -ForegroundColor Green
