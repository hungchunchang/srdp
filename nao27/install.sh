#!/bin/sh
# 在 NAO 上裝一次：問金鑰、寫設定、設成開機自動執行，然後馬上啟動。
# 之後換角色在網頁做（個人資源 › 機器人），不用再 SSH。
#
#   cd /home/nao/nao27 && sh install.sh           裝好並啟動
#   sh install.sh --remove                        拿掉開機自動執行並停止
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
ENV="$DIR/meme.env"
RUN="$DIR/run.sh"
LOG="$HOME/meme_nao27.log"
AUTOLOAD="$HOME/naoqi/preferences/autoload.ini"

stop_running() {
    # The launcher first (it is a loop and would start the client again), then the client.
    for pid in $(ps -ef 2>/dev/null | grep "[n]ao27/run.sh" | awk '{print $2}'); do
        kill "$pid" 2>/dev/null || true
    done
    for pid in $(ps -ef 2>/dev/null | grep "[m]eme_nao27.py" | awk '{print $2}'); do
        kill "$pid" 2>/dev/null || true
    done
}

if [ "$1" = "--remove" ]; then
    stop_running
    if command -v crontab >/dev/null 2>&1; then
        (crontab -l 2>/dev/null | grep -v "$RUN") | crontab - 2>/dev/null || true
    fi
    if [ -f "$AUTOLOAD" ]; then
        grep -v "^$RUN\$" "$AUTOLOAD" > "$AUTOLOAD.tmp" && mv "$AUTOLOAD.tmp" "$AUTOLOAD"
    fi
    echo "已拿掉開機自動執行，也停掉了。設定檔 $ENV 留著。"
    exit 0
fi

old_base=""; old_key=""; old_char=""
if [ -f "$ENV" ]; then
    old_base=$(sed -n 's/^export SR_BASE_URL="\(.*\)"$/\1/p' "$ENV")
    old_key=$(sed -n 's/^export SR_API_KEY="\(.*\)"$/\1/p' "$ENV")
    old_char=$(sed -n 's/^export NAO_CHARACTER="\(.*\)"$/\1/p' "$ENV")
fi
base_default=${old_base:-https://sociallab.duckdns.org/ntu_social_robot/api/v1}

printf "平台網址 [%s]: " "$base_default"; read base; base=${base:-$base_default}
# 不用金鑰：裝好之後 NAO 會念出配對碼，在網頁「個人資源 › 機器人 › 連上機器人」
# 輸入就連上（換人也一樣：摸頭頂 3 秒）。舊的 srk_ 金鑰還是能用，留空就不用。
printf "API 金鑰（可以留空，建議留空：NAO 會念配對碼）[%s]: " "${old_key:+沿用原本的}"
read key; key=${key:-$old_key}
case "$key" in ""|srk_*) ;; *) echo "金鑰要是 srk_ 開頭的那一串，或留空。"; exit 1;; esac
printf "固定跑哪個角色（留空＝在網頁上指定，建議）[%s]: " "$old_char"; read char; char=${char:-$old_char}

umask 077
{
    echo "export SR_BASE_URL=\"$base\""
    [ -n "$key" ] && echo "export SR_API_KEY=\"$key\""
    [ -n "$char" ] && echo "export NAO_CHARACTER=\"$char\""
    true
} > "$ENV"
umask 022

# run.sh 由程式自己寫（和「更新」時寫的是同一份：一個迴圈，程式起不來就重試；
# 更新後的新版起不來，就換回舊版）。
python -c "import sys; sys.path.insert(0, '$DIR'); import meme_nao27 as m; open('$RUN', 'w').write(m.RUN_SH.format(dir='$DIR', log='$LOG'))" \
  || { echo "寫不出 run.sh：python 在這台 NAO 上能跑 meme_nao27.py 嗎？"; exit 1; }
chmod +x "$RUN"

if command -v crontab >/dev/null 2>&1; then
    (crontab -l 2>/dev/null | grep -v "$RUN"; echo "@reboot $RUN") | crontab -
    how="crontab @reboot"
else
    mkdir -p "$(dirname "$AUTOLOAD")"
    touch "$AUTOLOAD"
    if ! grep -q "^$RUN\$" "$AUTOLOAD"; then
        if grep -q "^\[program\]" "$AUTOLOAD"; then
            awk -v run="$RUN" '{print} /^\[program\]/{print run}' "$AUTOLOAD" > "$AUTOLOAD.tmp" && mv "$AUTOLOAD.tmp" "$AUTOLOAD"
        else
            printf "\n[program]\n%s\n" "$RUN" >> "$AUTOLOAD"
        fi
    fi
    how="NAOqi autoload.ini"
fi

stop_running
nohup "$RUN" --now >/dev/null 2>&1 &

device=${NAO_DEVICE_ID:-nao-$(hostname)}
echo ""
echo "裝好了：開機自動執行（$how），現在也已經啟動。紀錄在 $LOG"
if [ -n "$char" ]; then
    echo "這台固定跑「$char」。"
else
    echo "NAO 會念出六位數的配對碼（也寫在 log 裡）：到網頁「個人資源 › 機器人 › 連上機器人」輸入。"
    echo "之後要換人用：摸 NAO 頭頂 3 秒，它會念新的配對碼。"
    echo "連上之後：個人資源 › 機器人，找「$device」，選一個角色。"
    echo "之後要換角色也在那裡換，NAO 大約十秒內會自己切過去。"
fi
echo "看它在做什麼：tail -f $LOG"
