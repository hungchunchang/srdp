"""`socialrobot` — the platform from a terminal: keep a character's flow in git.

    export SR_BASE_URL=https://sociallab.duckdns.org/ntu_social_robot/api/v1
    export SR_API_KEY=srk_…

    socialrobot characters                       # id and name of each
    socialrobot export 博物館導覽 -o guide.json   # by name or id
    git add guide.json && git commit -m "導覽：改開場白"
    socialrobot import guide.json --into 博物館導覽   # back into the SAME character
    socialrobot import guide.json                # or as a new private character

The file is the platform's own export — nothing is lost on the way out or in.
`--into` keeps the character's id, so the robots bound to it, its collaborators,
its people and its logs stay attached; nodes keep their ids too. If someone
else changed the flow on the web after the file was exported, `--into` stops
and says who — `--force` overwrites anyway (what it replaces goes to the
web's trash, not away).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from .client import SocialRobotClient, SocialRobotError


def _client(args) -> SocialRobotClient:
    if not args.api_key:
        raise SystemExit("給 --api-key 或設 SR_API_KEY（srk_ 開頭，網頁 › 個人設定 › 機器人與金鑰 › 開發者 API 金鑰）")
    try:
        return SocialRobotClient(args.base_url, api_key=args.api_key)
    except ValueError as e:
        raise SystemExit(f"--base-url／SR_BASE_URL：{e}")


def _character_id(client: SocialRobotClient, wanted: str) -> str:
    """An id as it is; a name resolved among this account's characters."""
    if "-" in wanted and len(wanted) >= 32:
        return wanted
    found = [c for c in client.list_characters() if c.get("name") == wanted]
    if not found:
        raise SystemExit(f"找不到叫 {wanted!r} 的角色；`socialrobot characters` 列出你有的")
    if len(found) > 1:
        ids = "、".join(c["id"] for c in found)
        raise SystemExit(f"有 {len(found)} 個角色叫 {wanted!r}，請改用 id：{ids}")
    return found[0]["id"]


def dumps(bundle: dict) -> str:
    """Stable text for git: sorted keys, two-space indent, real characters,
    a trailing newline — the same bundle always writes the same bytes."""
    return json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def cmd_whoami(args) -> int:
    me = _client(args).whoami()
    print(f"{me.get('display_name') or ''} <{me.get('email')}>  {me.get('id')}".strip())
    return 0


def cmd_characters(args) -> int:
    for c in _client(args).list_characters():
        print(f"{c['id']}  {c.get('name')}")
    return 0


def cmd_export(args) -> int:
    client = _client(args)
    bundle = client.export_character(_character_id(client, args.character))
    text = dumps(bundle)
    if args.output and args.output != "-":
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        n = len(bundle.get("workflows") or [])
        print(f"{bundle['character'].get('name')} → {args.output}（{n} 個節點）", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def cmd_import(args) -> int:
    client = _client(args)
    with open(args.file, encoding="utf-8") as f:
        bundle = json.load(f)
    if not args.into:
        made = client.import_character(bundle)
        print(f"新角色 {made.get('name')}  {made.get('id')}")
        return 0
    target = _character_id(client, args.into)
    try:
        out = client.replace_character(target, bundle, force=args.force)
    except SocialRobotError as e:
        if e.status_code == 409:
            print(f"沒有匯入：{e.detail}", file=sys.stderr)
            print("確定要蓋掉，加上 --force（被換掉的會進網頁的垃圾桶）。", file=sys.stderr)
            return 2
        raise
    print(f"{out['name']}  {out['character_id']}：保留 {out['kept']} 個節點、新增 {out['added']}、"
          f"移到垃圾桶 {out['removed']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="socialrobot", description="社會機器人平台的命令列工具")
    p.add_argument("--base-url", default=os.environ.get("SR_BASE_URL"),
                   help="平台的 API 網址，結尾 /api/v1（預設讀 SR_BASE_URL）")
    p.add_argument("--api-key", default=os.environ.get("SR_API_KEY"),
                   help="srk_ 開頭的開發者 API 金鑰（預設讀 SR_API_KEY）")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("whoami", help="這把金鑰是誰的").set_defaults(fn=cmd_whoami)
    sub.add_parser("characters", help="列出你的角色").set_defaults(fn=cmd_characters)
    e = sub.add_parser("export", help="把一個角色匯出成 JSON（不掉資料）")
    e.add_argument("character", help="角色 id 或名稱")
    e.add_argument("-o", "--output", help="寫到這個檔案（預設印到螢幕）")
    e.set_defaults(fn=cmd_export)
    i = sub.add_parser("import", help="匯入 JSON：新角色，或用 --into 蓋回原本的角色")
    i.add_argument("file", help="socialrobot export 匯出的 JSON")
    i.add_argument("--into", help="蓋回這個角色（id 或名稱）：id 不變，綁定的機器人、協作者、紀錄都留著")
    i.add_argument("--force", action="store_true",
                   help="別人在匯出之後也改過，還是要蓋掉")
    i.set_defaults(fn=cmd_import)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except SocialRobotError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
