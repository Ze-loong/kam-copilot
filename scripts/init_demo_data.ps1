$ErrorActionPreference = 'Stop'
$agentDir = Join-Path $PSScriptRoot '..\kam_agent'
$env:PYTHONIOENCODING = 'utf-8'
$steps = @(
    'experiments.seed_tags',
    'experiments.mock_data_init',
    'experiments.mock_data_expand_customers'
)
Push-Location $agentDir
try {
    foreach ($step in $steps) {
        uv run python -m $step
        if ($LASTEXITCODE -ne 0) { throw "初始化失败：$step" }
    }
    uv run python verify_kb_pipeline.py
    if ($LASTEXITCODE -ne 0) { throw '初始化失败：verify_kb_pipeline.py' }
    uv run python -m experiments.kb_ingest_demo_content
    if ($LASTEXITCODE -ne 0) { throw '初始化失败：kb_ingest_demo_content' }
} finally {
    Pop-Location
}
