#!/usr/bin/env bash
set -euo pipefail

logs_dir="$(pwd)/logs"
archive_path=""
staging_dir=""
dry_run=0
keep_staging=0
archive_override=0

usage() {
  cat <<'EOF'
Usage: scripts/latest_logs.sh [options]

Collects the most recent files from key log directories and builds
logs/logs-summary.tar.gz.

Options:
  --logs-dir PATH     Logs root (default: ./logs)
  --archive PATH      Archive path (default: <logs-dir>/logs-summary.tar.gz)
  --dry-run           Print selected files without copying or archiving
  --keep-staging      Keep staging directory after archiving
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --logs-dir)
      logs_dir="$2"
      shift 2
      ;;
    --archive)
      archive_path="$2"
      archive_override=1
      shift 2
      ;;
    --dry-run)
      dry_run=1
      shift 1
      ;;
    --keep-staging)
      keep_staging=1
      shift 1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$archive_path" || "$archive_override" -eq 0 ]]; then
  archive_path="${logs_dir}/logs-summary.tar.gz"
fi
staging_dir="${logs_dir}/.logs-summary"

if [[ ! -d "$logs_dir" ]]; then
  echo "Logs directory not found: $logs_dir" >&2
  exit 1
fi

declare -A counts
counts["validation"]=30
counts["pm2"]=10
counts["ledger"]=10
counts["sandbox"]=30
counts["handshake"]=30

if [[ -d "$logs_dir/legder" && ! -d "$logs_dir/ledger" ]]; then
  counts["legder"]="${counts["ledger"]}"
  unset counts["ledger"]
fi

selected=()
selected_sizes=()

for key in "${!counts[@]}"; do
  dir="${logs_dir}/${key}"
  count="${counts[$key]}"
  if [[ ! -d "$dir" ]]; then
    echo "Skipping missing directory: $dir" >&2
    continue
  fi
  mapfile -t files < <(find "$dir" -type f -printf '%T@ %p\n' | sort -nr | head -n "$count" | cut -d' ' -f2-)
  for file in "${files[@]}"; do
    size="$(stat -c '%s' "$file")"
    selected+=("$file")
    selected_sizes+=("$size")
  done
done

if (( ${#selected[@]} == 0 )); then
  echo "No files selected." >&2
  exit 1
fi

total=0
for size in "${selected_sizes[@]}"; do
  total=$((total + size))
done
total_mb="$(awk -v bytes="$total" 'BEGIN { printf "%.2f", bytes/1048576 }')"

if (( dry_run == 1 )); then
  echo "Selected files (total ${total} bytes, ${total_mb} MB):"
  for i in "${!selected[@]}"; do
    printf '%s\t%s\n' "${selected_sizes[$i]}" "${selected[$i]}"
  done
  exit 0
fi

rm -rf "$staging_dir"
mkdir -p "$staging_dir"
manifest="$staging_dir/manifest.txt"
{
  echo "logs_dir=$logs_dir"
  echo "archive_path=$archive_path"
  echo "total_bytes=$total"
  echo "total_mb=$total_mb"
  echo "selected_files=${#selected[@]}"
  echo ""
} > "$manifest"

for i in "${!selected[@]}"; do
  file="${selected[$i]}"
  size="${selected_sizes[$i]}"
  rel="${file#"$logs_dir"/}"
  if [[ "$rel" == "$file" ]]; then
    rel="$(basename "$file")"
  fi
  if [[ "$rel" == */* ]]; then
    dest_dir="$staging_dir/${rel%/*}"
  else
    dest_dir="$staging_dir"
  fi
  mkdir -p "$dest_dir"
  cp -p "$file" "$dest_dir/"
  printf '%s\t%s\n' "$size" "$rel" >> "$manifest"
done

mkdir -p "$(dirname "$archive_path")"
tar -czf "$archive_path" -C "$staging_dir" .

if (( keep_staging == 0 )); then
  rm -rf "$staging_dir"
fi

echo "Wrote ${#selected[@]} files to $archive_path (total ${total} bytes, ${total_mb} MB)."
