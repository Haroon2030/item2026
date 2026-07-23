# بحث الأصناف — Django + API

واجهة Django بسيطة للبحث عن الأصناف عبر API خارجي.

## التشغيل

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

ثم افتح: http://127.0.0.1:8000/

## ربط الـ API

عدّل القاموس `EXTERNAL_API` في ملف `config/settings.py` بعد إرسال تفاصيل الـ API:

| الإعداد | الوصف |
|---------|--------|
| `BASE_URL` | رابط النظام الأساسي |
| `SEARCH_PATH` | مسار البحث |
| `QUERY_PARAM` | اسم معامل البحث (`q`, `barcode`, ...) |
| `METHOD` | `GET` أو `POST` |
| `API_KEY` | مفتاح الوصول إن وُجد |
| `RESULTS_PATH` | مسار قائمة النتائج داخل JSON |
| `FIELD_MAP` | مطابقة أسماء الحقول للعرض |

## ما نحتاجه منك لربط النظام

أرسل أيًا مما يلي:

1. رابط الـ API (Endpoint)
2. مثال طلب (Request) ومثال استجابة (Response JSON)
3. هل البحث بـ GET أم POST؟
4. اسم معامل البحث ومفتاح المصادقة إن وُجد
5. أسماء حقول الصنف (الرمز، الاسم، الباركود، السعر، ...)
