# مسار المشروع العربي يعطل Gradle على Windows — نبني من نسخة إنجليزية.
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$dst = "C:\src\sales_app"
$sdk = "$env:LOCALAPPDATA\Android\Sdk"
$java = "C:\Program Files\Android\Android Studio\jbr"
$env:ANDROID_HOME = $sdk
$env:ANDROID_SDK_ROOT = $sdk
$env:JAVA_HOME = $java
$env:Path = "C:\Users\L\flutter\bin;$sdk\platform-tools;$java\bin;" + $env:Path

if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
New-Item -ItemType Directory -Force -Path "C:\src" | Out-Null
Copy-Item $src $dst -Recurse -Force
Set-Location $dst
flutter pub get
flutter build apk --debug --dart-define=API_BASE=https://item.alrsheed.net
$apk = Join-Path $dst "build\app\outputs\flutter-apk\app-debug.apk"
$out = Join-Path $src "build\app\outputs\flutter-apk"
New-Item -ItemType Directory -Force -Path $out | Out-Null
Copy-Item $apk (Join-Path $out "app-debug.apk") -Force
Write-Host "APK: $out\app-debug.apk"
