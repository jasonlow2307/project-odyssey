#!/usr/bin/env bash
set -euo pipefail

# Move reviewed duplicate candidates to Immich Trash in batches.
#
# Candidate CSV format:
#   api_key_env,asset_id
#   IMMICH_USER_A_API_KEY,00000000-0000-0000-0000-000000000001
#
# The script is preview-only unless CONFIRM_TRASH=YES is set. It always uses
# force:false, so this endpoint moves assets to Trash rather than permanently
# deleting them.

CANDIDATES="${CANDIDATES:-/srv/migration/immich-dedupe-candidates.csv}"
IMMICH_URL="${IMMICH_URL:?Set IMMICH_URL without the /api suffix}"
BATCH_SIZE="${BATCH_SIZE:-100}"
CONFIRM_TRASH="${CONFIRM_TRASH:-NO}"

command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
test -s "$CANDIDATES" || { echo "Candidate CSV not found: $CANDIDATES" >&2; exit 1; }

RUN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/immich-dedupe.XXXXXX")
trap 'rm -rf "$RUN_DIR"' EXIT

if [[ "$CONFIRM_TRASH" != "YES" ]]; then
    echo "Preview only. Set CONFIRM_TRASH=YES to move candidates to Immich Trash."
    tail -n +2 "$CANDIDATES" | awk -F',' 'NF >= 2 { count++ } END { print "candidate rows:", count + 0 }'
    exit 0
fi

send_batch() {
    local api_key_env="$1"
    local key="$2"
    shift 2

    local payload status
    payload=$(printf '%s\n' "$@" | jq -R -s '
        split("\n") | map(select(length > 0)) | {force: false, ids: .}
    ')

    status=$(curl -sS \
        -o "$RUN_DIR/response.json" \
        -w '%{http_code}' \
        -X DELETE "$IMMICH_URL/api/assets" \
        -H 'Content-Type: application/json' \
        -H "x-api-key: $key" \
        -d "$payload")

    if [[ "$status" != "204" ]]; then
        echo "FAILED for $api_key_env: HTTP $status" >&2
        cat "$RUN_DIR/response.json" >&2
        exit 1
    fi
}

process_owner() {
    local api_key_env="$1"
    local key="${!api_key_env:-}"
    local count=0
    local batch=()

    if [[ -z "$key" ]]; then
        echo "$api_key_env is not set; skipping its rows" >&2
        return
    fi

    while IFS=, read -r row_key asset_id _; do
        [[ "$row_key" == "$api_key_env" ]] || continue
        [[ "$asset_id" =~ ^[0-9a-fA-F-]{36}$ ]] || {
            echo "Skipping invalid asset ID for $api_key_env: $asset_id" >&2
            continue
        }
        batch+=("$asset_id")
        if (( ${#batch[@]} == BATCH_SIZE )); then
            send_batch "$api_key_env" "$key" "${batch[@]}"
            count=$((count + ${#batch[@]}))
            echo "$api_key_env: moved $count assets to Trash"
            batch=()
        fi
    done < <(tail -n +2 "$CANDIDATES")

    if (( ${#batch[@]} > 0 )); then
        send_batch "$api_key_env" "$key" "${batch[@]}"
        count=$((count + ${#batch[@]}))
        echo "$api_key_env: moved $count assets to Trash"
    fi
}

mapfile -t API_KEY_ENVS < <(tail -n +2 "$CANDIDATES" | awk -F',' 'NF >= 2 { print $1 }' | sort -u)
for api_key_env in "${API_KEY_ENVS[@]}"; do
    process_owner "$api_key_env"
done

echo "Completed: reviewed candidates were moved to Immich Trash. Do not empty Trash until verification passes."
