# يولّد مشروع iOS/Android حول ملفات lib الحالية
Set-Location $PSScriptRoot
if (-not (Get-Command flutter -ErrorAction SilentlyContinue)) {
    Write-Host "ثبّت Flutter أولاً: https://docs.flutter.dev/get-started/install/windows"
    exit 1
}
flutter create --org net.alrsheed --project-name sales_app .
flutter pub get
Write-Host "جاهز. للتشغيل على السيرفر الحي:"
Write-Host "flutter run --dart-define=API_BASE=https://item.alrsheed.net"
