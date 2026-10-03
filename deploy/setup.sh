#!/usr/bin/env bash
# Run on the VPS: bash deploy/setup.sh [--start [--image]]
set +x
set -Eeuo pipefail
umask 077

deploy_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(cd -- "$deploy_dir/.." && pwd -P)
env_file="$deploy_dir/.env"
start=false
image=false
usage() {
  printf 'Usage: bash deploy/setup.sh [--start [--image]]\n'
  printf 'Without --start: prepare private .env only.\n'
  printf 'With --start: build and start services.\n'
  printf 'With --start --image: start the loaded parrotgo:local image without building it.\n'
}
for option in "$@"; do
  case "$option" in
    --start) start=true ;;
    --image) image=true ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option. Use --help.\n' >&2; exit 2 ;;
  esac
done
if $image && ! $start; then
  printf '%s\n' '--image requires --start. Use --help.' >&2
  exit 2
fi

command -v docker >/dev/null || { printf 'Install Docker Engine and Compose v2 first; see deploy/README.md.\n' >&2; exit 1; }
docker compose version >/dev/null || { printf 'Docker Compose v2 is required.\n' >&2; exit 1; }
docker info >/dev/null 2>&1 || { printf 'Cannot reach Docker. Run as an account allowed to use the Docker daemon.\n' >&2; exit 1; }
[[ -f "$deploy_dir/compose.yaml" && -f "$deploy_dir/.env.example" ]] || {
  printf 'Deployment files are missing from this checkout.\n' >&2; exit 1;
}
if $image; then
  app_image=parrotgo:local
  if ! image_platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$app_image" 2>/dev/null); then
    printf 'Load parrotgo:local with docker load before using --start --image.\n' >&2
    exit 1
  fi
  if ! daemon_platform=$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null); then
    printf 'Could not determine the Docker daemon platform.\n' >&2
    exit 1
  fi
  if [[ ! "$image_platform" =~ ^[a-z0-9_-]+/[a-z0-9_-]+$ ||
        ! "$daemon_platform" =~ ^[a-z0-9_-]+/[a-z0-9_-]+$ ]]; then
    printf 'Could not validate the loaded image and Docker daemon platforms.\n' >&2
    exit 1
  fi
  if [[ "$image_platform" != "$daemon_platform" ]]; then
    printf 'Loaded app image platform (%s) does not match Docker daemon (%s). Load an image for this VPS.\n' \
      "$image_platform" "$daemon_platform" >&2
    exit 1
  fi
fi
[[ ! -L "$env_file" ]] || { printf 'Refusing to change a symlinked deploy/.env.\n' >&2; exit 1; }
if [[ ! -e "$env_file" ]]; then
  cp -- "$deploy_dir/.env.example" "$env_file"
  printf 'Created deploy/.env from the example.\n'
fi
chmod 600 -- "$env_file"

# Parse data, never source/eval a .env file as shell commands. Values generated
# below are single-line. Operator configuration should use KEY=value syntax.
env_value() {
  local wanted=$1 line value
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$wanted="* ]]; then
      value=${line#*=}
      value=${value%$'\r'}
      if [[ "$value" == \'*\' || "$value" == \"*\" ]]; then
        value=${value:1:${#value}-2}
      fi
      printf '%s' "$value"
      return
    fi
  done < "$env_file"
}

write_env() {
  local wanted=$1 value=$2 line replaced=false
  local temporary
  temporary=$(mktemp "$deploy_dir/.env.update.XXXXXX")
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$wanted="* ]]; then
      if ! $replaced; then
        printf "%s='%s'\n" "$wanted" "$value" >> "$temporary"
        replaced=true
      fi
    else
      printf '%s\n' "$line" >> "$temporary"
    fi
  done < "$env_file"
  $replaced || printf "%s='%s'\n" "$wanted" "$value" >> "$temporary"
  chmod 600 -- "$temporary"
  mv -- "$temporary" "$env_file"
}

secret=$(env_value APP_SECRET)
if [[ -z "$secret" || "$secret" == CHANGE_ME || "$secret" == replace_me ]]; then
  secret=$(od -An -N48 -tx1 /dev/urandom | tr -d ' \n')
  [[ "$secret" =~ ^[0-9a-f]{96}$ ]] || { printf 'Could not generate APP_SECRET.\n' >&2; exit 1; }
  write_env APP_SECRET "$secret"
  printf 'Generated APP_SECRET without displaying it.\n'
fi
unset secret

hash=$(env_value BETA_PASSWORD_HASH)
if [[ -z "$hash" || "$hash" == CHANGE_ME || "$hash" == replace_me ]]; then
  [[ -t 0 ]] || {
    printf 'Run setup in an interactive SSH terminal to create the beta password hash.\n' >&2
    exit 1
  }
  hash_file=$(mktemp "$deploy_dir/.caddy-hash.XXXXXX")
  trap 'rm -f -- "$hash_file"' EXIT
  printf 'Enter a strong beta-access password, then press Enter. Input is hidden.\n'
  printf 'Only its bcrypt hash will be saved; it will not be displayed.\n'
  docker run --rm -it caddy:2 caddy hash-password --algorithm bcrypt > "$hash_file"
  hash=$(tr -d '\r' < "$hash_file" | grep -Eo '\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}' | tail -n 1 || true)
  [[ "$hash" =~ ^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$ ]] || {
    printf 'Caddy did not produce a valid bcrypt hash. Existing .env values were kept.\n' >&2
    exit 1
  }
  write_env BETA_PASSWORD_HASH "$hash"
  rm -f -- "$hash_file"
  trap - EXIT
fi
unset hash

cd -- "$repo_root"
if ! $start; then
  printf 'Edit deploy/.env privately: domain, ACME email, beta username and API keys.\n'
  printf 'Then run: bash deploy/setup.sh --start\n'
  exit 0
fi

missing=false
for required in DOMAIN ACME_EMAIL BETA_USER APP_SECRET BETA_PASSWORD_HASH; do
  value=$(env_value "$required")
  if [[ -z "$value" || "$value" == CHANGE_ME || "$value" == replace_me || "$value" == *example.com* ]]; then
    printf 'Set %s in deploy/.env before starting.\n' "$required" >&2
    missing=true
  fi
done
profile=$(env_value APP_PROFILE)
if [[ "$profile" != fixture_demo ]]; then
  keys=()
  provider=$(env_value LLM_PROVIDER)
  case "$provider" in
    groq)
      if [[ $(env_value LLM_ALLOW_DEGRADED) != true || $(env_value LLM_FALLBACK_ENABLED) != true ]]; then
        keys+=(GROQ_API_KEY)
      fi
      [[ $(env_value LLM_FALLBACK_ENABLED) != true ]] || keys+=(GEMINI_API_KEY)
      ;;
    gemini) keys+=(GEMINI_API_KEY) ;;
    *) printf 'LLM_PROVIDER must be groq or gemini.\n' >&2; missing=true ;;
  esac
  [[ $(env_value MAPS_PROVIDER) != vietmap ]] || keys+=(VIETMAP_API_KEY)
  for required in "${keys[@]}"; do
    value=$(env_value "$required")
    if [[ -z "$value" || "$value" == CHANGE_ME || "$value" == replace_me ]]; then
      printf 'Set %s privately in deploy/.env, or use APP_PROFILE=fixture_demo for an offline dry run.\n' "$required" >&2
      missing=true
    fi
  done
fi
unset value
hash=$(env_value BETA_PASSWORD_HASH)
if [[ ! "$hash" =~ ^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$ ]]; then
  printf 'BETA_PASSWORD_HASH must be a complete bcrypt hash produced by Caddy.\n' >&2
  missing=true
else
  quoted_hash=false
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "${line%$'\r'}" != "BETA_PASSWORD_HASH='$hash'" ]] || quoted_hash=true
  done < "$env_file"
  if ! $quoted_hash; then
    printf 'Keep BETA_PASSWORD_HASH in single quotes so Compose preserves its dollar signs.\n' >&2
    missing=true
  fi
fi
unset hash
secret=$(env_value APP_SECRET)
if [[ ${#secret} -lt 32 ]]; then
  printf 'APP_SECRET must contain at least 32 characters; keep the generated value.\n' >&2
  missing=true
fi
unset secret
domain=$(env_value DOMAIN)
if [[ "$domain" == *://* || "$domain" != *.* || "$domain" == *..* ||
      ! "$domain" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ ]]; then
  printf 'DOMAIN must be a DNS hostname without scheme, path or port.\n' >&2
  missing=true
fi
beta_user=$(env_value BETA_USER)
if [[ ! "$beta_user" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
  printf 'BETA_USER must use 1 to 64 letters, digits, dots, underscores or hyphens.\n' >&2
  missing=true
fi
$missing && exit 1

# Required interpolation values come from this deployment's private file.
unset DOMAIN ACME_EMAIL BETA_USER BETA_PASSWORD_HASH APP_SECRET
compose=(docker compose --project-name parrotgo --env-file "$env_file" -f "$deploy_dir/compose.yaml")
"${compose[@]}" config --quiet
if $image; then
  "${compose[@]}" up -d --no-build --wait --wait-timeout 180
else
  "${compose[@]}" up -d --build --wait --wait-timeout 180
fi
"${compose[@]}" ps
printf 'Services started. Verify HTTPS and the text chat using deploy/README.md.\n'
