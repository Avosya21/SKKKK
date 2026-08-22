# Select OpenResearchTools Engine backend folder and GPU device name.
# CUDA build needs driver >= 580 (CUDA 13 runtime). Vulkan works on driver 577+.

param(
    [ValidateSet("auto", "cuda", "vulkan", "cpu")]
    [string]$Prefer = "auto"
)

$ErrorActionPreference = "Stop"
$Root = "C:\1C_NEW_SKKKK\data\tools"
$CudaDir = Join-Path $Root "openresearchtools-engine"
$VulkanDir = Join-Path $Root "openresearchtools-engine-vulkan"

function Get-EngineDevicesOutput {
    param([string]$Dir)
    $cli = Join-Path $Dir "example-cli.exe"
    if (-not (Test-Path $cli)) { return "" }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        return (& $cli list-devices 2>&1 | Out-String)
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Test-EngineCuda {
    param([string]$Dir)
    $out = Get-EngineDevicesOutput $Dir
    return ($out -match "backend=CUDA")
}

function Test-EngineVulkan {
    param([string]$Dir)
    $out = Get-EngineDevicesOutput $Dir
    return ($out -match "backend=Vulkan")
}

$driverLine = (nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>$null | Select-Object -First 1)
$driverMajor = 0
if ($driverLine -match '^(\d+)') { $driverMajor = [int]$Matches[1] }

$result = [ordered]@{
    driver_version = $driverLine
    driver_major   = $driverMajor
    cuda13_ready   = ($driverMajor -ge 580)
    backend        = "cpu"
    engine_dir     = $CudaDir
    device         = "CPU"
    gpu_index      = $null
    note           = ""
}

if ($Prefer -eq "cpu") {
    $result.backend = "cpu"
    $result.engine_dir = if (Test-Path (Join-Path $CudaDir "example-cli.exe")) { $CudaDir } else { $VulkanDir }
    $result.device = "CPU"
    $result.note = "CPU forced by -Prefer cpu"
    $result | ConvertTo-Json -Compress
    exit 0
}

if (($Prefer -eq "auto" -or $Prefer -eq "cuda") -and (Test-EngineCuda $CudaDir)) {
    $result.backend = "cuda"
    $result.engine_dir = $CudaDir
    $result.device = "CUDA0"
    $result.gpu_index = 0
    $result.note = "CUDA backend available"
    $result | ConvertTo-Json -Compress
    exit 0
}

if (($Prefer -eq "auto" -or $Prefer -eq "vulkan" -or $Prefer -eq "cuda") -and (Test-EngineVulkan $VulkanDir)) {
    $result.backend = "vulkan"
    $result.engine_dir = $VulkanDir
    $result.device = "Vulkan0"
    $result.gpu_index = 0
    if ($Prefer -eq "cuda" -and -not $result.cuda13_ready) {
        $result.note = "CUDA build needs driver >= 580 (CUDA 13). Using Vulkan GPU instead."
    } else {
        $result.note = "Vulkan GPU backend"
    }
    $result | ConvertTo-Json -Compress
    exit 0
}

$result.note = "No GPU backend detected; falling back to CPU"
$result | ConvertTo-Json -Compress
