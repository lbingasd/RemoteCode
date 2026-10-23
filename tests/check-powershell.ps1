param([string]$Package)
if (-not $Package) { $Package = Split-Path -Parent $PSScriptRoot }
$ErrorActionPreference = 'Stop'
foreach ($Relative in @('deploy-local.ps1', 'deploy-qq.ps1', 'scripts/local-windows.ps1')) {
    $Tokens = $null
    $Errors = $null
    $Ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $Package $Relative), [ref]$Tokens, [ref]$Errors)
    if ($Errors.Count) { throw ($Errors | Out-String) }
    if ($Relative -eq 'deploy-local.ps1') {
        $Function = $Ast.Find({ param($Node) $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq 'ConvertTo-NativeArgument' }, $true)
        . ([scriptblock]::Create($Function.Extent.Text))
    }
    Write-Output "$Relative syntax OK"
}
$Cases = @(
    @('', '""'),
    @('C:\Path With Space\', '"C:\Path With Space\\"'),
    @('a"b', '"a\"b"'),
    @('C:\plain', '"C:\plain"'),
    @('C:\a\"b\', '"C:\a\\\"b\\"')
)
foreach ($Case in $Cases) {
    $Actual = ConvertTo-NativeArgument $Case[0]
    if ($Actual -cne $Case[1]) { throw "Argument quoting mismatch: $Actual" }
}
Write-Output 'Windows CRT argument quoting: 5 cases OK (not native Windows execution)'
