#!/usr/bin/env bash
# A cold backup of the complete app volume plus its signing/API configuration.
set +x
set -Eeuo pipefail
umask 077

deploy_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(cd -- "$deploy_dir/.." && pwd -P)
env_file="$deploy_dir/.env"
volume=parrotgo_app_data
archive_image=alpine:3.22
case "${1:-}" in
  -h|--help)
    printf 'Usage: bash deploy/backup.sh [backup-directory]\nBriefly stops the app; leaves Caddy running.\n'
    exit 0 ;;
esac
[[ $# -le 1 ]] || { printf 'Too many arguments.\n' >&2; exit 2; }
[[ -f "$env_file" ]] || { printf 'Missing deploy/.env.\n' >&2; exit 1; }
command -v docker >/dev/null || { printf 'Docker is required.\n' >&2; exit 1; }
command -v flock >/dev/null || { printf 'flock from util-linux is required.\n' >&2; exit 1; }
docker compose version >/dev/null
unset DOMAIN ACME_EMAIL BETA_USER BETA_PASSWORD_HASH APP_SECRET
compose=(docker compose --project-name parrotgo --env-file "$env_file" -f "$deploy_dir/compose.yaml")
"${compose[@]}" config --quiet

exec 9> "$deploy_dir/.backup.lock"
flock -n 9 || { printf 'Another backup is already running.\n' >&2; exit 1; }
chmod 600 -- "$deploy_dir/.backup.lock"
backup_dir=${1:-"$deploy_dir/backups"}
mkdir -p -- "$backup_dir"
backup_dir=$(cd -- "$backup_dir" && pwd -P)
[[ "$backup_dir" != / && "$backup_dir" != "$repo_root" && "$backup_dir" != "$deploy_dir" ]] || {
  printf 'Choose a dedicated backup directory.\n' >&2; exit 1;
}
chmod 700 -- "$backup_dir"

app_id=$("${compose[@]}" ps --all --quiet app)
[[ -n "$app_id" && "$app_id" != *$'\n'* ]] || {
  printf 'Expected exactly one existing app container. Start the deployment before backing up.\n' >&2; exit 1;
}
mounted_volume=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/app/backend/data"}}{{.Name}}{{end}}{{end}}' "$app_id")
[[ "$mounted_volume" == "$volume" ]] || { printf 'App data volume does not match parrotgo_app_data. Backup stopped.\n' >&2; exit 1; }
docker volume inspect "$volume" >/dev/null
docker image inspect "$archive_image" >/dev/null 2>&1 || docker pull "$archive_image"

tag="parrotgo-$(date -u +%Y%m%dT%H%M%SZ)-$$"
archive_name="$tag.app-data.tar.gz"
env_name="$tag.env"
partial="$backup_dir/$archive_name.partial"
env_partial="$backup_dir/$env_name.partial"
restart_needed=false

finish() {
  local status=$?
  trap - EXIT INT TERM
  if $restart_needed; then
    printf 'Restarting the app after backup...\n'
    if ! "${compose[@]}" start --wait --wait-timeout 90 app; then
      printf 'ERROR: App restart/readiness failed. Backup files are retained. Run docker compose with deploy/.env and inspect app status.\n' >&2
      status=1
    fi
  fi
  rm -f -- "$partial" "$env_partial"
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ $(docker inspect --format '{{.State.Running}}' "$app_id") == true ]]; then
  restart_needed=true
  printf 'Stopping the app to keep business/checkpoint/quota SQLite files consistent...\n'
  "${compose[@]}" stop --timeout 60 app
fi
[[ $(docker inspect --format '{{.State.Running}}' "$app_id") == false ]] || {
  printf 'App is still running; refusing a live copy of SQLite files.\n' >&2; exit 1;
}
[[ -z $(docker ps --filter "volume=$volume" --format '{{.ID}}') ]] || {
  printf 'Another running container is using the app volume. Backup stopped.\n' >&2; exit 1;
}

docker run --rm --network none --mount "type=volume,src=$volume,dst=/data,readonly" \
  "$archive_image" tar -cz -C /data . > "$partial"
tar -tzf "$partial" >/dev/null
cp -- "$env_file" "$env_partial"
chmod 600 -- "$partial" "$env_partial"
mv -- "$partial" "$backup_dir/$archive_name"
mv -- "$env_partial" "$backup_dir/$env_name"
(
  cd -- "$backup_dir"
  sha256sum "$archive_name" "$env_name" > "$tag.sha256"
)
printf 'Backup created: %s\n' "$backup_dir/$archive_name"
printf 'Keep the matching .env and .sha256 files privately with this archive.\n'
