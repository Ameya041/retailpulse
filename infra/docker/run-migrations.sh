#!/bin/sh
# Apply every service's migrations, then exit.
#
# `set -e` matters here: without it a failed migration would be logged and the
# script would carry on to the next service and exit 0, so Compose would start
# the application against a half-migrated database.
set -eu

SERVICES="product-service inventory-service order-service user-service payment-service fulfilment-service analytics-service"

echo "Applying migrations for: $SERVICES"

for service in $SERVICES; do
    echo ""
    echo "--- $service ---"
    cd "/app/$service"
    alembic upgrade head
done

echo ""
echo "All migrations applied."
