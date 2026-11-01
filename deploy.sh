#!/bin/bash
# Installs/updates the bot on the server. Run from /root/near-analyzer-bot.
set -e
cd /root/near-analyzer-bot
if [ ! -x venv/bin/python ]; then
  echo ">>> Creating Python environment..."
  apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
  python3 -m venv venv
fi
echo ">>> Installing libraries..."
apt-get install -y -qq libcairo2 >/dev/null 2>&1 || true   # for SVG token icons
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
cp near-analyzer.service /etc/systemd/system/near-analyzer.service
systemctl daemon-reload
systemctl enable -q near-analyzer
cat > /usr/local/bin/analyzer-update <<'UPD'
#!/bin/bash
# Download the latest version from GitHub and restart the bot
set -e
cd /root/near-analyzer-bot
git fetch -q origin main
git reset -q --hard origin/main
# Repo may hold zips uploaded from the phone (iPhone renames them: bot1.zip, bot2.zip...)
# — unpack the most recently committed one over the folder
ZIP=$(for z in *.zip; do [ -f "$z" ] && echo "$(git log -1 --format=%ct -- "$z") $z"; done | sort -n | tail -1 | cut -d' ' -f2-)
if [ -n "$ZIP" ]; then
  echo ">>> Unpacking $ZIP"
  rm -rf /tmp/nab && python3 -m zipfile -e "$ZIP" /tmp/nab
  SRC=/tmp/nab; [ -d /tmp/nab/near-analyzer-bot ] && SRC=/tmp/nab/near-analyzer-bot
  cp -a "$SRC"/. . && rm -rf /tmp/nab
fi
exec bash deploy.sh
UPD
chmod +x /usr/local/bin/analyzer-update
echo ">>> Restarting the bot..."
systemctl restart near-analyzer
sleep 8
if systemctl is-active --quiet near-analyzer; then
  echo "✅ Bot is running. Version: $(git log -1 --format='%h — %s' 2>/dev/null)"
else
  echo "❌ Bot didn't start, last log lines:"
  journalctl -u near-analyzer -n 25 --no-pager -o cat
fi
