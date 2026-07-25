"""تهيئة الحزمة — تفعيل PyMySQL كبديل لـ MySQLdb في الإنتاج."""

try:
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:
    pass
