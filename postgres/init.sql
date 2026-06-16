-- Creates the test database used by the pytest suite.
-- This script runs automatically on the first boot of the postgres container
-- (PostgreSQL executes files in /docker-entrypoint-initdb.d/ only when the
-- data directory is empty, i.e. first start or after `docker compose down -v`).
CREATE DATABASE docextractor_test;
