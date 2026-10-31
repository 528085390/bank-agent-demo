<#
.SYNOPSIS
    Run the backend pytest suite against the LOCAL PostgreSQL instead of the cloud test DB.

.DESCRIPTION
    backend/tests/conftest.py hands every database-backed test a schema of its own and
    replays every alembic migration into it. Measured against the cloud instance (36 ms
    round trip) that costs about 8.4 s per test -- create schema 0.65 s + alembic upgrade
    head 5.61 s + Runtime/seed 1.94 s + drop 0.21 s -- and several hundred tests request that fixture,
    which is where the whole 70-minute full run goes. Against the local server the same
    fixture costs well under a second, so the suite is roughly 10x faster and the tests
    themselves are unchanged: conftest.py already prefers TEST_DATABASE_URL.

    Measured 2026-10-19: full suite serial on the local server = 1781 passed, 50 skipped in
    about 10 minutes. The 2026-10-10 reading was 1534 passed, 46 skipped in 9m24s, and
    -Parallel 8 cut that to 2m16s. For comparison the cloud DB took 1h11m39s and still lost
    14 setup errors to WAN timeouts.

    The local database must exist once:  psql -U postgres -c "CREATE DATABASE test"
    (it does not need to be migrated -- each test migrates its own schema).

    -Parallel <N> spreads the suite over N pytest-xdist workers. pytest-xdist is not a
    project dependency, so it is pulled in with `uv run --with` instead of editing
    pyproject.toml. Parallel mode also installs pgcrypto once up front: the billing
    baseline migration (docs/contracts/sql/001_init.sql:8) runs
    `CREATE EXTENSION IF NOT EXISTS pgcrypto` inside every test schema, and because
    extension names are database-wide two workers starting together both see it missing
    and one dies with psycopg.errors.UniqueViolation on pg_extension_name_index. The cloud
    DB already carries pgcrypto in public, which is why the race never shows up there.

.EXAMPLE
    ./pytest-local.ps1 -q tests/wealth
    ./pytest-local.ps1 -Root .worktrees/wealth-write-verification -q tests/test_wealth_write_api.py
    ./pytest-local.ps1 -Parallel 8 -q
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Root = '',
    [int]$Parallel = 0,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$PytestArgs
)

$ErrorActionPreference = 'Stop'

if (-not $Root) {
    $Root = if ($PSScriptRoot) { $PSScriptRoot } else { $PWD.Path }
}

# Local PostgreSQL 18 service (E:\postgre\data), listening on 127.0.0.1:5432.
$env:TEST_DATABASE_URL = 'postgresql+psycopg://postgres:123456@127.0.0.1:5432/test'
# The console code page is GBK and the suite prints Chinese; without this pytest dies on
# the first emoji in an assertion message.
$env:PYTHONIOENCODING = 'utf-8'

$uvArgs = @('run')
$pytestExtra = @()
if ($Parallel -gt 0) {
    $uvArgs += @('--with', 'pytest-xdist')
    $pytestExtra += @('-n', "$Parallel")

    $psql = (Get-Command psql -ErrorAction SilentlyContinue).Source
    if (-not $psql -and (Test-Path 'E:/postgre/bin/psql.exe')) { $psql = 'E:/postgre/bin/psql.exe' }
    if ($psql) {
        $env:PGPASSWORD = '123456'
        & $psql -U postgres -h 127.0.0.1 -p 5432 -d test -q -c 'CREATE EXTENSION IF NOT EXISTS pgcrypto;'
    }
    else {
        Write-Warning 'psql not found: make sure pgcrypto exists in the local test DB, or parallel workers can race on CREATE EXTENSION.'
    }
}

Set-Location (Join-Path $Root 'backend')
& uv @uvArgs pytest @pytestExtra @PytestArgs
exit $LASTEXITCODE
