-- One database per service on one Postgres instance (CLAUDE.md §6). Runs once, on the
-- container's first start with an empty data volume.
CREATE DATABASE identity_db;
CREATE DATABASE course_db;
CREATE DATABASE learning_db;
CREATE DATABASE notification_db;
CREATE DATABASE analytics_db;
