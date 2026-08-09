# تحليل البيانات — Django

تطبيق Django داخلي: بحث الأصناف، سحب مخزون المجموعات، تحليل المخزون والمشتريات والمالية — مع قراءة Oracle للتقارير.

## هيكل الجذر

```
item/
├── config/           # إعدادات Django (settings, urls, wsgi)
├── search/           # التطبيق الرئيسي (views, oracle, insights)
├── templates/        # قوالب HTML
├── static/           # CSS / JS / أيقونات
├── deploy/           # إعدادات نشر (nginx)
├── scripts/          # أدوات تشغيل/صيانة
├── manage.py
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
├── requirements.txt
└── .env.example      # انسخه إلى .env محلياً (لا يُرفع)
```

ملفات محلية لا تُرفع: `.env` · `.secret_key` · `db.sqlite3`

## التشغيل محلياً

```bash
pip install -r requirements.txt
copy .env.example .env   # عدّل القيم
python manage.py migrate
python manage.py runserver
```

ثم: http://127.0.0.1:8000/

## الصفحات

| المسار | الوظيفة |
|--------|---------|
| `/` | البحث عن معلومات الصنف |
| `/browse/` | سحب مخزون المجموعات |
| `/inventory/` | تحليل المخزون |
| `/purchases/` | تحليل المشتريات |
| `/sales/` | تحليل المبيعات |
| `/sales/search/` | البحث عن مبيعات صنف |
| `/sales/performance/` | تحليل الأداء |
| `/income/` | قائمة الدخل |
| `/users/` | إدارة المستخدمين (مدير النظام) |

## Docker

```bash
docker compose up --build
```

راجع `deploy/nginx-ssl.conf` لتهيئة البروكسي العكسي إن لزم.
