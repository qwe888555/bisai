"""Project package.

If PyMySQL is installed, register it as MySQLdb so Django can use it as the
MySQL driver.  With the default SQLite database this block is a no-op.
"""

try:  # pragma: no cover - only relevant when MySQL is used
    import pymysql

    pymysql.install_as_MySQLdb()
except ImportError:  # pragma: no cover
    pass
