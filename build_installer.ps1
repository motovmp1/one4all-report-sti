$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$BuildRequirements = Join-Path $ProjectRoot "packaging\requirements-build.txt"
$IconBuilder = Join-Path $ProjectRoot "packaging\create_icon.py"
$VersionInfo = Join-Path $ProjectRoot "packaging\version_info.txt"
$InstallerScript = Join-Path $ProjectRoot "packaging\installer.iss"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "The project .venv was not found. Create it and install requirements.txt first."
}

Set-Location -LiteralPath $ProjectRoot
& $Python -m pip install --disable-pip-version-check -r $BuildRequirements
if ($LASTEXITCODE -ne 0) { throw "Could not install the build requirements." }

& $Python $IconBuilder
if ($LASTEXITCODE -ne 0) { throw "Could not create the application icon." }

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name One4AllViewer `
    --icon "assets\app_icon.ico" `
    --version-file $VersionInfo `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller could not build the application." }

$IsccCandidates = @(
    (Get-Command iscc.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

$Iscc = $IsccCandidates | Select-Object -First 1
if (-not $Iscc) {
    throw "Inno Setup 6 was not found. Install it with: winget install --id JRSoftware.InnoSetup -e -s winget"
}

& $Iscc $InstallerScript
if ($LASTEXITCODE -ne 0) { throw "Inno Setup could not build the installer." }

Write-Host ""
Write-Host "Installer created:" -ForegroundColor Green
Write-Host (Join-Path $ProjectRoot "release\Tridonic-One4All-Viewer-Setup-1.4.exe")
