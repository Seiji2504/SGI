#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 -v app_password="$APP_PASSWORD" -f /migrations/bootstrap.sql
