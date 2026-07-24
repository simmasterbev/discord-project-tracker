[Unit]
Description=Discord project tracker bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tracker
Group=tracker
WorkingDirectory=/opt/tracker
EnvironmentFile=/etc/tracker.env
ExecStart=/opt/tracker/venv/bin/python /opt/tracker/bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# basic hardening — the bot only needs its own directory
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/tracker

[Install]
WantedBy=multi-user.target
