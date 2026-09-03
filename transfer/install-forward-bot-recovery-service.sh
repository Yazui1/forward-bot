NAME="forward-bot-recovery"
DIR="$(pwd)"

cat > "$NAME.service" <<EOF
[Unit]
Description=Forward bot recovery
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$DIR
ExecStart=$DIR/../bot/venv/bin/python3 $DIR/../bot/transfer/transfer.py --config $DIR/config.yml
Restart=on-failure
NoNewPrivileges=false
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

sudo cp "$NAME.service" /etc/systemd/system/
rm "$NAME.service"
sudo systemctl daemon-reload
sudo systemctl enable "$NAME.service"
sudo systemctl restart "$NAME.service"
sudo systemctl status "$NAME.service"