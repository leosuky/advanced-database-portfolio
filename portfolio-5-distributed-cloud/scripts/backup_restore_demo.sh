#!/usr/bin/env bash
# Portfolio 5 - backup and restore, measured. Uses pg_dump (logical backup) in custom format and verifies the restore.
#   PGBIN=/usr/lib/postgresql/16/bin ./backup_restore_demo.sh 5433
set -euo pipefail
PGBIN="${PGBIN:-/usr/lib/postgresql/16/bin}"
PORT="${1:-5433}"
OUT="${OUT:-/tmp/ads_backup}"
mkdir -p "$OUT"
P="$PGBIN/psql -h 127.0.0.1 -p $PORT -U postgres -At"

echo "== source database"
$P -d dataxi -c "SELECT 'match', count(*) FROM dataxi.match UNION ALL SELECT 'player_match', count(*) FROM dataxi.player_match UNION ALL SELECT 'event', count(*) FROM dataxi.event UNION ALL SELECT 'standings', count(*) FROM dataxi.standings"
echo "size: $($P -d dataxi -c "SELECT pg_size_pretty(pg_database_size('dataxi'))")"

echo "== backup: pg_dump -Fc"
START=$(date +%s.%N)
"$PGBIN/pg_dump" -h 127.0.0.1 -p "$PORT" -U postgres -Fc -f "$OUT/dataxi.dump" dataxi
END=$(date +%s.%N)
printf "dump time: %.1f s   dump size: %s\n" "$(echo "$END - $START" | bc)" "$(du -h "$OUT/dataxi.dump" | cut -f1)"

echo "== restore into a NEW database (dataxi_restore) with 4 parallel jobs"
$P -d postgres -c "DROP DATABASE IF EXISTS dataxi_restore" -c "CREATE DATABASE dataxi_restore"
START=$(date +%s.%N)
"$PGBIN/pg_restore" -h 127.0.0.1 -p "$PORT" -U postgres -d dataxi_restore -j 4 "$OUT/dataxi.dump"
END=$(date +%s.%N)
printf "restore time: %.1f s\n" "$(echo "$END - $START" | bc)"

echo "== verify: the restored copy must match the source"
for t in match player_match event standings; do
  a=$($P -d dataxi -c "SELECT count(*) FROM dataxi.$t"); b=$($P -d dataxi_restore -c "SELECT count(*) FROM dataxi.$t")
  [ "$a" = "$b" ] && echo "$t: $a = $b  OK" || { echo "$t: $a <> $b  MISMATCH"; exit 1; }
done
# content checksum, not just counts
a=$($P -d dataxi -c "SELECT md5(string_agg(event_id::text||player_id::text||event_type, ',' ORDER BY event_id)) FROM dataxi.event")
b=$($P -d dataxi_restore -c "SELECT md5(string_agg(event_id::text||player_id::text||event_type, ',' ORDER BY event_id)) FROM dataxi.event")
[ "$a" = "$b" ] && echo "event content checksum identical ($a)" || { echo "CHECKSUM MISMATCH"; exit 1; }
$P -d postgres -c "DROP DATABASE dataxi_restore"
echo "RPO of a nightly logical backup = up to 24 h of lost writes; point-in-time recovery needs WAL archiving."
