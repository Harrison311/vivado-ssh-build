function Read-XprConfig {
    param([string]$XprPath, [hashtable]$Config)
    $xpr = Get-Item -LiteralPath $XprPath -ErrorAction Stop
    [xml]$xml = Get-Content -LiteralPath $xpr.FullName -Raw
    $root = [IO.Path]::GetFullPath($Config.LocalProjectRoot).TrimEnd('\', '/')
    $prefix = $root + [IO.Path]::DirectorySeparatorChar
    $projectDir = $xpr.DirectoryName
    $stem = [IO.Path]::GetFileNameWithoutExtension($xpr.Name)
    $variables = @{
        '$PPRDIR' = $projectDir
        '$PSRCDIR' = (Join-Path $projectDir ($stem + '.srcs'))
        '$PGENDIR' = (Join-Path $projectDir ($stem + '.gen'))
        '$PIPUSERFILESDIR' = (Join-Path $projectDir ($stem + '.ip_user_files'))
    }
    function Resolve-InputPath([string]$value) {
        foreach ($key in $variables.Keys) { $value = $value.Replace($key, $variables[$key]) }
        if ($value.Contains('$')) { throw "Unresolved XPR path variable: $value" }
        if (![IO.Path]::IsPathRooted($value)) { $value = Join-Path $projectDir $value }
        $full = [IO.Path]::GetFullPath($value)
        if (!$full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "XPR input outside LocalProjectRoot; expand that root first: $full"
        }
        if (!(Test-Path -LiteralPath $full)) { throw "Missing XPR input: $full" }
        return $full.Substring($prefix.Length).Replace('\', '/')
    }
    $sources = $xml.SelectSingleNode("/Project/FileSets/FileSet[@Name='sources_1']")
    $constraints = $xml.SelectSingleNode("/Project/FileSets/FileSet[@Name='constrs_1']")
    if (!$sources -or !$constraints) { throw 'Expected sources_1 and constrs_1 in XPR.' }
    $Config.TopModule = [string]$sources.SelectSingleNode("Config/Option[@Name='TopModule']").Val
    $Config.Part = [string]$xml.SelectSingleNode("/Project/Configuration/Option[@Name='Part']").Val
    if (!$Config.TopModule -or !$Config.Part) { throw 'XPR top module or FPGA part is empty.' }
    # Extra fileset semantics must not be silently discarded by the generic build.
    foreach ($option in $sources.SelectNodes('Config/Option')) {
        if ($option.Name -in @('VerilogDefines', 'VerilogDefine', 'IncludeDirs') -and $option.Val) {
            throw "XPR option needs explicit build support: $($option.Name)"
        }
    }
    foreach ($pair in @(@($sources, 'SourceFiles'), @($constraints, 'ConstraintFiles'))) {
        $paths = @()
        foreach ($file in $pair[0].SelectNodes('File')) {
            $attrs = @($file.SelectNodes('FileInfo/Attr'))
            if ($attrs | Where-Object { $_.Name -eq 'IsEnabled' -and $_.Val -match '^(false|0)$' }) { continue }
            $used = @($attrs | Where-Object { $_.Name -eq 'UsedIn' } | ForEach-Object { $_.Val })
            if ($used.Count -and !($used -contains 'synthesis') -and !($used -contains 'implementation')) { continue }
            $path = Resolve-InputPath ([string]$file.Path)
            $ext = [IO.Path]::GetExtension($path).ToLowerInvariant()
            $allowed = if ($pair[1] -eq 'SourceFiles') { @('.v', '.sv', '.vh', '.svh', '.vhd', '.vhdl', '.xci', '.mem', '.coe') } else { @('.xdc') }
            if ($ext -notin $allowed) { throw "Unsupported XPR input type: $path" }
            foreach ($attr in $attrs) {
                if ($attr.Name -eq 'Library' -and $attr.Val -ne 'xil_defaultlib') { throw "Custom HDL library needs explicit support: $path" }
                if ($attr.Name -like 'ScopedTo*' -and $attr.Val) { throw "Scoped constraint needs explicit support: $path" }
            }
            $paths += $path
        }
        $Config[$pair[1]] = @($paths | Select-Object -Unique)
    }
    if (!$Config.SourceFiles.Count) { throw 'No synthesis sources found in saved XPR.' }
}
