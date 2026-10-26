"""项目配置包。

统一注册 PyMySQL 作为 MySQLdb 的替身：团队约定 DATABASE_URL 走
mysql+pymysql 驱动（见 .env 与 manage.py），pytest 等不走 manage.py 的
入口同样需要此 shim，否则 django.db.backends.mysql 报
"Error loading MySQLdb module"。若部署环境改装 mysqlclient（编译驱动），
此处 ImportError 被静默跳过，不影响启动。
"""

try:
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:  # pragma: no cover - 部署环境用 mysqlclient 时无 PyMySQL
    pass
