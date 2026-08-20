param(
    [string]$BaseUrl = "https://api.deepseek.com/anthropic",
    [string]$Model = "deepseek-v4-pro",
    [string]$ClaudeCommand = "D:\claude\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
)

if (-not (Test-Path -LiteralPath $ClaudeCommand -PathType Leaf)) {
    throw "找不到 Claude Code 可执行文件：$ClaudeCommand"
}
$resolvedClaudeCommand = (Resolve-Path -LiteralPath $ClaudeCommand).Path

$secureToken = Read-Host "DeepSeek API Key" -AsSecureString
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)

try {
    $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    if ([string]::IsNullOrWhiteSpace($plainToken)) {
        throw "API Key 不能为空。"
    }

    [Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $BaseUrl, "User")
    [Environment]::SetEnvironmentVariable("ANTHROPIC_AUTH_TOKEN", $plainToken, "User")
    [Environment]::SetEnvironmentVariable("ANTHROPIC_MODEL", $Model, "User")
    [Environment]::SetEnvironmentVariable("AUTO_CODING_CLAUDE_MODEL", $Model, "User")
    [Environment]::SetEnvironmentVariable(
        "AUTO_CODING_CLAUDE_COMMAND", $resolvedClaudeCommand, "User"
    )

    Write-Host "DeepSeek Claude Code 配置已写入当前用户环境变量。" -ForegroundColor Green
    Write-Host "请关闭并重新打开终端后再启动项目。"
}
finally {
    if ($tokenPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
    }
    $plainToken = $null
}
