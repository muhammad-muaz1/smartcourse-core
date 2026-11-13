"""Redis client for this service's DB index: get_redis dependency, check() for health."""

from platform_core.db import RedisConnection

connection = RedisConnection()

init_redis = connection.init
close_redis = connection.close
get_redis = connection.get_redis
check = connection.check
