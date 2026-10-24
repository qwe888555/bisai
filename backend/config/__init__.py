"""项目配置包。"""

# PyMySQL 兼容层：Django 的 mysql 后端硬编码 import MySQLdb（官方要求 mysqlclient），
# 本项目依赖为 PyMySQL（requirements.txt），必须在其加载前完成别名注册，
# 否则首个模型类定义（触发默认连接后端加载）即报 "No module named 'MySQLdb'"。
# 本包先于 config.settings 导入执行，覆盖 manage.py / wsgi / asgi 全部启动入口。
import pymysql

pymysql.install_as_MySQLdb()
