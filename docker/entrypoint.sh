#!/bin/bash
# Starts mullvad-daemon (there is no systemd in the container) and waits until
# its CLI answers before handing off to entrypoint.py. The processes that
# entrypoint.py starts call VPNConnector.initialize_vpn() (downloader.py),
# which talks to this daemon over its local socket, so it must be up first.
set -e

if [ -n "$MULLVAD_ACCOUNT_NUMBER" ] && [ "${MULLVAD_DAEMON_ENABLED:-0}" = "1" ]; then
    if command -v mullvad-daemon >/dev/null 2>&1; then
        echo "[docker-entrypoint] Starting mullvad-daemon..."
        # Run daemon in background, suppressing firewall warnings (read-only /proc/sys in containers)
        /usr/bin/mullvad-daemon --disable-stdout-timestamps 2>&1 | grep -v "Failed to apply" &
        DAEMON_PID=$!

        ready=0
        for i in $(seq 1 40); do
            if command -v mullvad >/dev/null 2>&1 && mullvad status >/dev/null 2>&1 2>&1; then
                ready=1
                break
            fi
            sleep 1
        done

        if [ "$ready" = "1" ]; then
            echo "[docker-entrypoint] mullvad-daemon is ready"

            # The daemon keeps its device in /etc/mullvad-vpn (a mounted
            # volume), so a login is only needed when there is none or it was
            # revoked from the account. Until then the tunnel stays in the
            # "Blocked" state and the container has no network at all.
            if ! mullvad account get 2>/dev/null | grep -q "^Mullvad account:" \
               || mullvad account get 2>&1 | grep -qi "revoked"; then
                echo "[docker-entrypoint] No valid Mullvad device; logging in..."
                login_out=$(mullvad account login "$MULLVAD_ACCOUNT_NUMBER" 2>&1)
                echo "[docker-entrypoint] $login_out"
                if echo "$login_out" | grep -qi "too many devices"; then
                    echo "[docker-entrypoint] ✗ Mullvad account is at its device limit. Revoke one with:"
                    echo "[docker-entrypoint]     mullvad account list-devices -a \$MULLVAD_ACCOUNT_NUMBER"
                    echo "[docker-entrypoint]     mullvad account revoke-device -a \$MULLVAD_ACCOUNT_NUMBER '<name>'"
                    echo "[docker-entrypoint]   then restart this container."
                fi
            fi

            # Let LAN traffic bypass the tunnel (Plex, Lidarr and the local AI
            # are reached directly).
            echo "[docker-entrypoint] Configuring Mullvad to allow local network access..."
            for i in {1..5}; do
                if mullvad lan set allow >/dev/null 2>&1; then
                    echo "[docker-entrypoint] ✓ Local network (LAN) access enabled"
                    break
                else
                    echo "[docker-entrypoint] Attempt $i: LAN config failed, retrying..."
                    sleep 2
                fi
            done

            # Mullvad's LAN setting covers the container's own Docker network,
            # not the host's LAN. Each subnet in LAN_SUBNETS (comma-separated,
            # e.g. 192.168.1.0/24) is routed through the Docker gateway, past
            # the tunnel; the gateway is read before Mullvad connects.
            GATEWAY=$(ip -4 route show default dev eth0 2>/dev/null | awk '{print $3; exit}')
            for subnet in $(echo "${LAN_SUBNETS:-}" | tr ',' ' '); do
                if [ -n "$GATEWAY" ] && ip route add "$subnet" via "$GATEWAY" dev eth0 2>/dev/null; then
                    echo "[docker-entrypoint] ✓ Route added: $subnet via Docker gateway ($GATEWAY)"
                else
                    echo "[docker-entrypoint] ⚠ Could not add a route for $subnet (it may already exist)"
                fi
            done

            # Connect with specified relay if configured, otherwise use automatic
            if [ -n "$MULLVAD_RELAY_COUNTRY" ] && [ -n "$MULLVAD_RELAY_CITY" ]; then
                echo "[docker-entrypoint] Connecting to Mullvad relay: $MULLVAD_RELAY_COUNTRY/$MULLVAD_RELAY_CITY"
                if mullvad relay set location "$MULLVAD_RELAY_COUNTRY" "$MULLVAD_RELAY_CITY" >/dev/null 2>&1; then
                    if mullvad connect >/dev/null 2>&1; then
                        echo "[docker-entrypoint] ✓ Successfully connected to specified relay"
                    else
                        echo "[docker-entrypoint] ⚠ Failed to connect to relay - retrying..."
                        sleep 3
                        mullvad connect >/dev/null 2>&1 || echo "[docker-entrypoint] Still unable to connect"
                    fi
                else
                    echo "[docker-entrypoint] ⚠ Could not set relay to $MULLVAD_RELAY_COUNTRY/$MULLVAD_RELAY_CITY - using automatic"
                    mullvad connect >/dev/null 2>&1
                fi
            else
                echo "[docker-entrypoint] Connecting to Mullvad (automatic relay selection)..."
                if mullvad connect >/dev/null 2>&1; then
                    echo "[docker-entrypoint] ✓ Connected to Mullvad with automatic relay selection"
                else
                    echo "[docker-entrypoint] ⚠ Could not auto-connect - will retry on first download"
                fi
            fi
            
            # Verify status
            if command -v mullvad >/dev/null 2>&1; then
                echo "[docker-entrypoint] Mullvad status:"
                mullvad status || echo "[docker-entrypoint] Could not retrieve Mullvad status"
            fi
        else
            echo "[docker-entrypoint] WARNING: mullvad-daemon did not become ready in time - continuing without VPN"
        fi
    else
        echo "[docker-entrypoint] mullvad-daemon not installed - continuing without VPN"
    fi
else
    echo "[docker-entrypoint] Skipping Mullvad daemon startup (MULLVAD_DAEMON_ENABLED=${MULLVAD_DAEMON_ENABLED:-0}) - continuing without VPN"
fi

exec python -u entrypoint.py
