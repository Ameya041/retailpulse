#!/bin/bash
# Creates one database per service inside a single Postgres instance.
#
# Why one instance with several databases rather than one container each:
# the isolation property that matters for the database-per-service pattern is
# that no service can read or write another service's tables. Separate logical
# databases give exactly that -- there is no way to write a cross-service JOIN.
# Running nine Postgres containers on a laptop would burn memory to buy
# operational independence that only pays off in production, where each
# database would move to its own managed instance.
set -euo pipefail

DATABASES=(
  retailpulse_product
  retailpulse_inventory
  retailpulse_order
  retailpulse_user
  retailpulse_payment
  retailpulse_fulfilment
  retailpulse_analytics
)

for db in "${DATABASES[@]}"; do
  echo "Creating database: $db"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "postgres" <<-EOSQL
    SELECT 'CREATE DATABASE $db'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$db')\gexec
    GRANT ALL PRIVILEGES ON DATABASE $db TO $POSTGRES_USER;
EOSQL
done

echo "All RetailPulse databases created."
