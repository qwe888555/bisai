"""项目配置包。

把 PyMySQL 注册为 MySQLdb 的 shim 放在包初始化里，
这样 manage.py、pytest、gunicorn、脚本等所有入口都能生效；
只在 manage.py 里装会导致 pytest / WSGI 启动阶段报 ImproperlyConfigured。
"""

try:  # 未安装 PyMySQL（例如只跑 SQLite）时静默跳过
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:  # pragma: no cover
    pass
