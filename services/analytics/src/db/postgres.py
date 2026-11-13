"""analytics_db connection: engine, session factory, get_session dependency, check().
The mechanics (commit/rollback per request, connect timeout) live in platform_core.db.
"""

from platform_core.db import PostgresDatabase

database = PostgresDatabase()

init_postgres = database.init
close_postgres = database.close
get_session = database.get_session
check = database.check
