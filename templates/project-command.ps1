param(
    [ValidateSet('check', 'submit', 'submit_start', 'status', 'fetch')]
    [string]$Action = 'check',
    [string]$JobId = '',
    [switch]$ForceUpload
)
$ErrorActionPreference = 'Stop'
# Copy this file into YOUR project, then edit this section.
# Empty Password uses your existing SSH key/agent/config. Password may be stored below if desired; keep this file local.
$ToolRoot = 'D:\tools\vivado-ssh-build'
$Python = 'python'
$Config = @{
    SshTarget = 'REPLACE_ME'                 # SSH alias or user@host
    SshPort = 22
    Password = ''                          # Optional plaintext password; single quote becomes two single quotes
    IdentityFile = ''                       # Optional LOCAL private-key path, not key contents
    RemoteProjectRoot = '/home/REPLACE_ME/fpga_builds/my_project'
    LocalProjectRoot = $PSScriptRoot        # Project root; can use another absolute path
    LocalResultRoot = 'D:\fpga-results\my_project'
    VivadoSettings = '/opt/Xilinx/Vivado/2023.2/settings64.sh'
    Part = 'xcku040-ffva1156-2-i'
    TopModule = 'top'
    Jobs = 4                               # Concurrent Vivado runs, not a CPU-thread cap
    SourceFiles = @('rtl/top.v')            # All HDL compilation inputs, relative to project root
    ConstraintFiles = @('constraints/top.xdc')
    IpSetupTcl = @()                        # Scripts to recreate/configure standalone IPs
    IncludeDirs = @()                       # Header search paths; contents are also uploaded
    ExtraUploadPaths = @()                  # .mem/.coe/data/helper scripts referenced by HDL/Tcl
    ExcludeNames = @('.git', '.Xil', '__pycache__', '.cache', 'node_modules')
    ConfigFile = $PSCommandPath             # Exclude this credential file from uploads
    PollSeconds = 3
    JobId = $JobId
    ForceUpload = [bool]$ForceUpload
}
# Do not change the launcher below unless customizing the tool.
if ($Config.SshTarget -eq 'REPLACE_ME') { throw 'Edit SshTarget and the project settings first.' }
$env:PYTHONDONTWRITEBYTECODE = '1'
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$Config | ConvertTo-Json -Depth 8 | & $Python -X utf8 -B (Join-Path $ToolRoot 'vivado_ssh.py') $Action
if ($LASTEXITCODE -ne 0) { throw "Remote Vivado tool failed (exit $LASTEXITCODE)." }
