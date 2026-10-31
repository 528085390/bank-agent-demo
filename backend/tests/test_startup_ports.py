"""Startup must avoid foreign listeners and only reuse a verified process record."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import serve

ROOT = Path(__file__).resolve().parents[2]


def test_agent_serve_accepts_selected_port_and_keeps_windows_event_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["serve.py", "--port", "8002"])
    monkeypatch.setattr(serve, "env_sources", lambda root: {"BANK_AGENT_HOST": "0.0.0.0", "BANK_AGENT_PORT": "8123"})
    monkeypatch.setattr(serve, "configure_event_loop", lambda: None)
    monkeypatch.setattr(serve.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    assert serve.main() == 0
    assert calls[0]["host"] == "0.0.0.0"
    assert calls[0]["port"] == 8002
    assert calls[0]["loop"] == "app.core.eventloop:selector_loop"


def test_agent_serve_refuses_invalid_port(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["serve.py", "--port", "0"])
    with pytest.raises(SystemExit) as error:
        serve.main()
    assert error.value.code == 2


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ("$null", 8002),
        (
            "[pscustomobject]@{pid=$PID;started=$self.StartTime.ToUniversalTime().ToString('o');port=8019}",
            8019,
        ),
        ("[pscustomobject]@{pid=$PID;started='2000-01-01T00:00:00Z';port=8019}", 8002),
    ],
)
@pytest.mark.parametrize("json_roundtrip", [False, True])
def test_powershell_selects_free_port_or_verified_existing_record(record, expected, json_roundtrip):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("Windows startup integration requires PowerShell")
    # Load only the pure startup helpers, never the startup script's top-level mutations.
    source = r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $env:STARTUP_TEST_ROOT 'start.ps1'),[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Startup parse error' }
foreach ($functionName in @('Test-RecordedProcess','Find-ServicePort')) {
    $node=$ast.Find({param($item) $item -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $item.Name -eq $functionName},$true)
    . ([scriptblock]::Create($node.Extent.Text))
}
function Test-PortOpen { param([int]$Port) return $Port -in @(8000,8001,8019) }
$self=Get-Process -Id $PID
"""
    source += "\n$entry=" + record + "\n"
    if json_roundtrip:
        source += "$entry=$entry | ConvertTo-Json | ConvertFrom-Json\n"
    source += "$selected=Find-ServicePort -Preferred 8000 -Reserved @(8001,5173) -Entry $entry -ExpectedExe $self.Path\n"
    source += "Write-Output ('RESULT:'+[string]$selected)\n"
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", source],
        env={**os.environ, "STARTUP_TEST_ROOT": str(ROOT)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert f"RESULT:{expected}" in result.stdout
