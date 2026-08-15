# مبيعات الرشيد — تطبيق iOS

تطبيق Flutter يعرض **مبيعات الشركة اليومية** و**مبيعات المجموعات** من نفس مصدر بيانات موقع التحليل (`item.alrsheed.net`).

## المتطلبات

- [Flutter SDK](https://docs.flutter.dev/get-started/install)
- لتشغيل iOS: جهاز Mac + Xcode + CocoaPods
- حساب دخول موجود في تطبيق التحليل (نفس المستخدم وكلمة المرور)

على Windows يمكن تطوير الواجهة وتشغيلها على Android أو المتصفح. بناء ملف iPhone يحتاج Mac.

## ما تم تثبيته على هذا الجهاز

- Flutter 3.47 (`C:\Users\L\flutter`) وأُضيف إلى PATH
- Android Studio + Android SDK 36 (`C:\Users\L\AppData\Local\Android\Sdk`)
- حزم التطبيق (`flutter pub get`)
- بناء ويب ناجح، واختبار الدخول ناجح
- APK تجريبي: شغّل `.\build-android.ps1` (Gradle لا يقبل المسار العربي مباشرة)

## التجهيز لأول مرة

من مجلد `mobile`:

```bash
flutter create --org net.alrsheed --project-name sales_app .
flutter pub get
```

الأمر `flutter create .` يولّد مجلدات `ios/` و`android/` دون حذف ملفات `lib/`.

## التشغيل

على الآيفون (Safari) بعد النشر:

https://item.alrsheed.net/app/

الإنتاج (المصدر الحي) من الكمبيوتر:

```bash
flutter run --dart-define=API_BASE=https://item.alrsheed.net
```

سيرفر محلي:

```bash
flutter run --dart-define=API_BASE=http://127.0.0.1:8000
```

من محاكي iOS إلى جهاز Windows استخدم IP الجهاز وليس `127.0.0.1`.

## الشاشات

1. تسجيل الدخول (نفس حساب الموقع)
2. **اليوم** — صافي نقاط البيع، المرتجع، نظام المبيعات، أونكس، وترتيب الفروع
3. **المجموعات** — مبيعات كل مجموعة مع النسبة والكمية

التاريخ الافتراضي هو اليوم. يمكن تغيير الفترة والفرع من أعلى الشاشة.

## واجهة الخادم

| المسار | الوظيفة |
|--------|---------|
| `POST /api/mobile/login/` | دخول وإصدار رمز |
| `GET /api/mobile/sales/daily/` | مبيعات الشركة |
| `GET /api/mobile/sales/groups/` | مبيعات المجموعات |
| `GET /api/mobile/filters/` | الفروع والمجموعات |
