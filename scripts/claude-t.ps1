# claude-t.ps1: Claude Code through Trioditis, for this one process only. Same arguments as `claude`.
# Windows PowerShell 5.1 and PowerShell 7 (Windows, macOS, Linux); from cmd, use claude-t.cmd beside it.
#
# Your claude.ai login stays the credential: Trioditis passes it to Anthropic untouched and only counts
# tokens (`trioditis usage --plan`). If Trioditis isn't answering, Claude Code starts direct, unmetered,
# and says so. Nothing outside this process changes; plain `claude` is never routed.
#
#   TRIODITIS_URL      the router (default http://127.0.0.1:8787)
#   TRIODITIS_API_KEY  the router key, if the router has one: sent as X-Trioditis-Key, never as Authorization
$ErrorActionPreference = 'Stop'

$url = if ($env:TRIODITIS_URL) { $env:TRIODITIS_URL } else { 'http://127.0.0.1:8787' }
$url = $url.TrimEnd('/')

# The login token travels to this URL with every request: this machine or https only, and no user@host form.
if ($url.Contains('@')) {
    [Console]::Error.WriteLine("claude-t: TRIODITIS_URL has an @ in it. Give the router's address without a user or password.")
    exit 2
}
if ($url -notmatch '^(https://.+|http://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?)$') {
    [Console]::Error.WriteLine("claude-t: TRIODITIS_URL is $url. Plain http is only for this machine (127.0.0.1, localhost, [::1]): use https, or unset it.")
    exit 2
}

function Test-Gate([string]$base) {
    # HttpClient with no proxy: a system proxy must not see the health check, as in claude-t's curl.
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(2)
    try {
        $r = $client.GetAsync("$base/health").GetAwaiter().GetResult()
        if (-not $r.IsSuccessStatusCode) { return $false }
        $body = $r.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        return ($body -replace '\s', '').Contains('"anthropic_gate":true')
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

if (Test-Gate $url) {
    $env:ANTHROPIC_BASE_URL = $url
    # Claude Code 2.1.296 (not 2.1.322, .325 or .330) turns tool search off for any base URL that isn't
    # Anthropic's and sends every tool in full: 289 tools, about 375k tokens a turn, on 21 Nov 2026. The
    # gate passes the tool_reference blocks through, so tool search can stay on. A value you set is kept.
    if (-not $env:ENABLE_TOOL_SEARCH) { $env:ENABLE_TOOL_SEARCH = 'true' }
    if ($env:TRIODITIS_API_KEY) {
        $line = "X-Trioditis-Key: $($env:TRIODITIS_API_KEY)"
        $env:ANTHROPIC_CUSTOM_HEADERS = if ($env:ANTHROPIC_CUSTOM_HEADERS) { "$($env:ANTHROPIC_CUSTOM_HEADERS)`n$line" } else { $line }
    }
} else {
    [Console]::Error.WriteLine("claude-t: no Trioditis with the Anthropic gate at $url (down, or older than the gate); starting Claude Code direct, not metered.")
}

& claude @args
exit $LASTEXITCODE
