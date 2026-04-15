#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "uso: $0 <instance-name>"
  echo "ex:  $0 teste"
  exit 1
fi

name="$1"
sidecar="${name}-vpn"
android="$name"

have() {
  command -v "$1" >/dev/null 2>&1
}

run_in() {
  sudo docker exec "$1" sh -c "$2"
}

section() {
  printf '\n== %s ==\n' "$1"
}

section "Containers"
sudo docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Networks}}' | grep -E "^(NAMES|${sidecar}|${android})" || true

section "Sidecar Basic Health"
run_in "$sidecar" 'id || true; ip addr || true; ip route || true'

section "Tor Local Endpoints"
run_in "$sidecar" 'nc -z 127.0.0.1 9050 && echo SOCKS_9050=ok || echo SOCKS_9050=fail'
run_in "$sidecar" 'nc -z 127.0.0.1 9040 && echo TRANS_9040=ok || echo TRANS_9040=fail'
run_in "$sidecar" 'nc -z 127.0.0.1 5353 && echo DNS_5353=ok || echo DNS_5353=fail'

section "Tor NAT Rules"
run_in "$sidecar" 'iptables -t nat -L OUTPUT -n -v || true'

section "Tor Logs (last 120 lines)"
sudo docker logs "$sidecar" --tail 120 || true

section "External Egress From Sidecar"
run_in "$sidecar" 'wget -T 20 -qO- https://api.ipify.org 2>/dev/null && echo || echo ipify_failed'
run_in "$sidecar" 'wget -T 20 -qO- https://check.torproject.org/api/ip 2>/dev/null && echo || echo torproject_failed'

section "ADB Control Plane From Host"
adb connect "${sidecar}:5555" >/dev/null 2>&1 || true
adb devices || true
adb -s "${sidecar}:5555" shell getprop sys.boot_completed || true

section "Android Control Plane Inside Container"
run_in "$android" 'getprop sys.boot_completed || true; ps -A | grep adbd || true; ss -ltn 2>/dev/null | grep 5555 || true'

section "Interpretation"
cat <<'EOF'
1. Se SOCKS/TRANS/DNS estiverem ok e check.torproject.org retornar IsTor=true:
   Tor/egress está funcionando.

2. Se Tor estiver ok, mas adb devices não mostrar <instance>-vpn:5555 device:
   o problema é ADB/control-plane, não Tor.

3. Se ADB estiver ok, mas a UI continuar sem imagem/interação:
   o problema está no caminho websocket/screenrecord/input consumer.

4. Se ipify/check.torproject falharem no sidecar:
   o problema é egress/DNS/Tor bootstrap no sidecar.
EOF