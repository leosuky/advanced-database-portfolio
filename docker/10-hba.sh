#!/bin/bash
# Allow replication connections (pg_basebackup) from the host - needed only for Portfolio 5.
echo "host replication all all trust" >> "$PGDATA/pg_hba.conf"
