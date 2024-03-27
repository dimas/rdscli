#!/bin/bash

MONITOR_SCRIPT=/root/inactivity-monitor.sh

SYSTEMD_DIR=/etc/systemd/system
SYSTEMD_UNIT=inactivity-monitor.service

(
cat <<'EOF'
{{INCLUDE:inactivity-monitor.sh}}
EOF
) > $MONITOR_SCRIPT


if [ -f $MONITOR_SCRIPT ]; then
    chmod +x $MONITOR_SCRIPT
else
    echo "Failed to make $MONITOR_SCRIPT"
    exit 1
fi

(
cat <<'EOF'
{{INCLUDE:inactivity-monitor.service}}
EOF
) > "$SYSTEMD_DIR/$SYSTEMD_UNIT"

systemctl daemon-reload

systemctl start $SYSTEMD_UNIT

exit 0

