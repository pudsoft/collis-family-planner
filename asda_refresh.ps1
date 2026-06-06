<#
.SYNOPSIS
    Refreshes the ASDA regulars product list from order history and deploys.
    Run monthly or after a large shop with new items.
#>

$ProjectDir = $PSScriptRoot

Write-Host "`n=== ASDA Regulars Refresh ===" -ForegroundColor Cyan

# Step 1: Kill Edge and extract cookies
Write-Host "`n[1/3] Extracting Edge session cookies..." -ForegroundColor Yellow
Stop-Process -Name msedge -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
node "$ProjectDir\extract_edge_cookies.js"
if ($LASTEXITCODE -ne 0) { Write-Host "Cookie extraction failed." -ForegroundColor Red; exit 1 }

# Step 2: Enrich regulars (opens Edge — user clicks orders then presses Enter)
Write-Host "`n[2/3] Enriching regulars from order history..." -ForegroundColor Yellow
Write-Host "      Edge will open on Past Orders. Click each order, then press Enter here." -ForegroundColor Gray
Stop-Process -Name msedge -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
node "$ProjectDir\asda_enrich_regulars.js"
if ($LASTEXITCODE -ne 0) { Write-Host "Enrichment failed." -ForegroundColor Red; exit 1 }

# Step 3: Commit and deploy
Write-Host "`n[3/3] Committing and deploying..." -ForegroundColor Yellow
$count = (Get-Content "$ProjectDir\data\asda_regulars.json" | ConvertFrom-Json).Count
git -C $ProjectDir add data/asda_regulars.json
git -C $ProjectDir commit -m "Update ASDA regulars from order history ($count items)"
git -C $ProjectDir push origin master
& "C:\Claude_Helpers\claude-helpers\cfp_ssh.ps1" deploy

Write-Host "`n=== Done! $count products now in the shopping search ===" -ForegroundColor Green
