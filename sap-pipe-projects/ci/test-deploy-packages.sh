#!/bin/sh
# Exercises ci/deploy-packages.sh without calling SAP (incl. empty value mapping skip).
set -eu

unset CDPATH
root="$(cd -- "$(dirname "$0")/.." && pwd)"
script="$root/ci/deploy-packages.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

write_manifest() {
  dir="$1"
  bundle="$2"
  mkdir -p "$dir/META-INF"
  printf 'SAP-BundleType: %s\n' "$bundle" > "$dir/META-INF/MANIFEST.MF"
}

mkdir -p "$work/bin" "$work/repo/package2"
write_manifest "$work/repo/packagetest1/flow" IntegrationFlow
cat > "$work/repo/packages.json" << 'EOF'
{
  "packages": ["packagetest1", "package2"]
}
EOF
cat > "$work/bin/sap-pipe" << 'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "${SAP_PIPE_STUB_LOG:?}"
exit 0
EOF
chmod +x "$work/bin/sap-pipe"

git -C "$work/repo" init -q
git -C "$work/repo" config user.email "test@example.com"
git -C "$work/repo" config user.name "test"
git -C "$work/repo" add packages.json
git -C "$work/repo" commit -q -m "base"
git -C "$work/repo" add packagetest1
git -C "$work/repo" commit -q -m "change packagetest1"

export PATH="$work/bin:$PATH"
export SAP_PIPE_STUB_LOG="$work/calls.log"
export CI_PROJECT_DIR="$work/repo"
export PACKAGES_FILE="$work/repo/packages.json"
CI_COMMIT_BEFORE_SHA="$(git -C "$work/repo" rev-parse HEAD^)"
CI_COMMIT_SHA="$(git -C "$work/repo" rev-parse HEAD)"
export CI_COMMIT_BEFORE_SHA CI_COMMIT_SHA
: > "$SAP_PIPE_STUB_LOG"

echo "=== restore with empty input ==="
set +e
out="$(MODE=restore DEPLOY_PACKAGE="" sh "$script" 2>&1)"
code=$?
set -e
printf '%s\n' "$out"
[ "$code" -eq 0 ]
printf '%s\n' "$out" | grep -q "Refusing to deploy"
[ ! -s "$SAP_PIPE_STUB_LOG" ]

echo "=== promote only the changed package ==="
: > "$SAP_PIPE_STUB_LOG"
set +e
out="$(MODE=promote DEPLOY_PACKAGE="" sh "$script" 2>&1)"
code=$?
set -e
printf '%s\n' "$out"
[ "$code" -eq 0 ]
grep -q "packagetest1" "$SAP_PIPE_STUB_LOG"
if grep -q "package2" "$SAP_PIPE_STUB_LOG"; then
  echo "package2 should not be deployed" >&2
  exit 1
fi
grep -q "deploy --artifact-type Integration --artifact-ids flow" "$SAP_PIPE_STUB_LOG"

echo "=== restore one package ==="
: > "$SAP_PIPE_STUB_LOG"
MODE=restore DEPLOY_PACKAGE=package2 sh "$script" >/dev/null
grep -q -- "--package-id package2" "$SAP_PIPE_STUB_LOG"
if grep -q "packagetest1" "$SAP_PIPE_STUB_LOG"; then
  echo "packagetest1 should not be deployed" >&2
  exit 1
fi

echo "=== deploy groups by bundle type ==="
write_manifest "$work/repo/mixed/b_script" ScriptCollection
write_manifest "$work/repo/mixed/a_script" ScriptCollection
write_manifest "$work/repo/mixed/v_map" ValueMapping
write_manifest "$work/repo/mixed/m_map" MessageMapping
write_manifest "$work/repo/mixed/z_flow" IntegrationFlow
write_manifest "$work/repo/mixed/d_type" DataType
: > "$SAP_PIPE_STUB_LOG"
out="$(MODE=restore DEPLOY_PACKAGE=mixed sh "$script" 2>&1)"
printf '%s\n' "$out"
printf '%s\n' "$out" | grep -q "Skipping d_type (DataType)"
got="$(grep '^deploy ' "$SAP_PIPE_STUB_LOG")"
expected="$(cat << 'EOF'
deploy --artifact-type ScriptCollection --artifact-ids a_script,b_script
deploy --artifact-type ValueMapping --artifact-ids v_map
deploy --artifact-type MessageMapping --artifact-ids m_map
deploy --artifact-type Integration --artifact-ids z_flow
EOF
)"
[ "$got" = "$expected" ]

echo "=== empty value mapping is skipped and the job succeeds ==="
write_manifest "$work/repo/badvm/value_map1" ValueMapping
write_manifest "$work/repo/badvm/flow" IntegrationFlow
printf '%s\n' '<vm version="2.0"><group id="g"><entry><agency>test1</agency><schema>test2</schema><value/></entry></group></vm>' \
  > "$work/repo/badvm/value_map1/value_mapping.xml"
: > "$SAP_PIPE_STUB_LOG"
out="$(MODE=restore DEPLOY_PACKAGE=badvm sh "$script" 2>&1)"
printf '%s\n' "$out"
printf '%s\n' "$out" | grep -q "Skipping value mapping value_map1"
printf '%s\n' "$out" | grep -q "agency=test1 schema=test2"
grep -q "deploy --artifact-type Integration --artifact-ids flow" "$SAP_PIPE_STUB_LOG"
if grep -q "deploy --artifact-type ValueMapping" "$SAP_PIPE_STUB_LOG"; then
  echo "empty value mapping should not be deployed" >&2
  exit 1
fi

echo "=== value mapping with values is deployed ==="
write_manifest "$work/repo/goodvm/value_map1" ValueMapping
printf '%s\n' '<vm version="2.0"><group id="g"><entry><agency>test1</agency><schema>test2</schema><value>one</value></entry></group></vm>' \
  > "$work/repo/goodvm/value_map1/value_mapping.xml"
: > "$SAP_PIPE_STUB_LOG"
MODE=restore DEPLOY_PACKAGE=goodvm sh "$script" >/dev/null
grep -q "deploy --artifact-type ValueMapping --artifact-ids value_map1" "$SAP_PIPE_STUB_LOG"

echo "=== missing SAP-BundleType fails ==="
mkdir -p "$work/repo/broken/x/META-INF"
printf '%s\n' 'Bundle-Name: x' > "$work/repo/broken/x/META-INF/MANIFEST.MF"
set +e
out="$(MODE=restore DEPLOY_PACKAGE=broken sh "$script" 2>&1)"
code=$?
set -e
printf '%s\n' "$out"
[ "$code" -ne 0 ]
printf '%s\n' "$out" | grep -q "SAP-BundleType missing"

echo "=== packages.json format change deploys nothing ==="
printf '%s\n' '{' '  "packages": ["packagetest1", "package2" ]' '}' > "$work/repo/packages.json"
git -C "$work/repo" add packages.json
git -C "$work/repo" commit -q -m "reformat packages"
CI_COMMIT_BEFORE_SHA="$(git -C "$work/repo" rev-parse HEAD^)"
CI_COMMIT_SHA="$(git -C "$work/repo" rev-parse HEAD)"
export CI_COMMIT_BEFORE_SHA CI_COMMIT_SHA
: > "$SAP_PIPE_STUB_LOG"
out="$(MODE=promote DEPLOY_PACKAGE="" sh "$script" 2>&1)"
printf '%s\n' "$out"
printf '%s\n' "$out" | grep -q "Nothing to deploy"
[ ! -s "$SAP_PIPE_STUB_LOG" ]

echo "=== packages.json addition deploys only the new package ==="
printf '%s\n' '{' '  "packages": ["packagetest1", "package2", "extra"]' '}' > "$work/repo/packages.json"
git -C "$work/repo" add packages.json
git -C "$work/repo" commit -q -m "add extra"
CI_COMMIT_BEFORE_SHA="$(git -C "$work/repo" rev-parse HEAD^)"
CI_COMMIT_SHA="$(git -C "$work/repo" rev-parse HEAD)"
export CI_COMMIT_BEFORE_SHA CI_COMMIT_SHA
: > "$SAP_PIPE_STUB_LOG"
MODE=promote DEPLOY_PACKAGE="" sh "$script" >/dev/null
grep -q -- "--package-id extra" "$SAP_PIPE_STUB_LOG"
if grep -q "packagetest1" "$SAP_PIPE_STUB_LOG"; then
  echo "editing packages.json should not redeploy existing packages" >&2
  exit 1
fi

echo "deploy-packages.sh ok"
