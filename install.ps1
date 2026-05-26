# cosmonapse-core/install.ps1
# ----------------------------
# Windows convenience wrapper around install.py.
#
# Usage (from anywhere):
#
#   powershell -ExecutionPolicy Bypass -File cosmonapse-core\install.ps1
#
# Or from the repo root:
#
#   .\cosmonapse-core\install.ps1
#
# Flags are forwarded to install.py, e.g.
#
#   .\cosmonapse-core\install.ps1 -User
#   .\cosmonapse-core\install.ps1 -NoPath

[CmdletBinding()]
param(
    [switch]$User,
    [switch]$NoPath,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# Pick a python interpreter: explicit -Python wins, else py launcher, else python.exe
if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Python = (& py -c "import sys; print(sys.executable)").Trim()
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $Python = (& python -c "import sys; print(sys.executable)").Trim()
    } else {
        Write-Error "No python interpreter found on PATH. Install Python 3.11+ from python.org first."
        exit 1
    }
}

$args = @("$here\install.py", "--python", $Python)
if ($User)   { $args += "--user" }
if ($NoPath) { $args += "--no-path" }

& $Python @args
exit $LASTEXITCODE
