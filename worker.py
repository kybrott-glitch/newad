#!/usr/bin/env python3
"""
TG AdBot worker.
Polls the dashboard for jobs and executes them via Telethon.
Set DASHBOARD_URL and WORKER_TOKEN via .env (already filled in if you downloaded the bundle).
"""
import asyncio, os, random, time, traceback
from typing import Any, Dict, List, Optional

import httpx
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

DASHBOARD_URL = os.environ["DASHBOARD_URL"].rstrip("/")
WORKER_TOKEN = os.environ["WORKER_TOKEN"]
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
TICK_INTERVAL = float(os.environ.get("TICK_INTERVAL", "30"))

HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}", "Content-Type": "application/json"}

# Active TelegramClient per account_id, kept in memory between jobs
CLIENTS: Dict[str, TelegramClient] = {}


async def http_post(client: httpx.AsyncClient, path: str, body: dict | None = None):
    r = await client.post(f"{DASHBOARD_URL}{path}", json=body or {}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def parse_invite(link: str) -> Optional[str]:
    # https://t.me/+abc or t.me/joinchat/abc
    for marker in ("/+", "/joinchat/"):
        if marker in link:
            return link.split(marker, 1)[1].strip("/")
    return None


def parse_username(link: str, fallback: Optional[str]) -> Optional[str]:
    if fallback:
        return fallback
    # https://t.me/something
    if "t.me/" in link:
        tail = link.split("t.me/", 1)[1].strip("/")
        if tail and not tail.startswith("+") and not tail.startswith("joinchat"):
            return tail
    return None


async def get_or_make_client(account: dict) -> TelegramClient:
    aid = account["id"]
    if aid in CLIENTS:
        return CLIENTS[aid]
    session = StringSession(account.get("session_string") or "")
    c = TelegramClient(session, int(account["api_id"]), account["api_hash"])
    await c.connect()
    CLIENTS[aid] = c
    return c


async def handle_login_start(client: httpx.AsyncClient, job: dict, account: dict):
    tc = await get_or_make_client(account)
    phone = job["payload"]["phone"]
    sent = await tc.send_code_request(phone)
    await http_post(client, "/api/public/worker/result", {
        "job_id": job["id"],
        "ok": True,
        "result": {"need": "code"},
        "account_patch": {"phone_code_hash": sent.phone_code_hash, "status": "pending_login"},
    })


async def handle_login_code(client: httpx.AsyncClient, job: dict, account: dict):
    tc = await get_or_make_client(account)
    code = job["payload"]["code"]
    try:
        await tc.sign_in(phone=account["phone"], code=code, phone_code_hash=account.get("phone_code_hash"))
        session_str = StringSession.save(tc.session)
        await http_post(client, "/api/public/worker/result", {
            "job_id": job["id"], "ok": True, "result": {"logged_in": True},
            "account_patch": {"session_string": session_str, "status": "active", "phone_code_hash": None, "last_error": None},
        })
    except errors.SessionPasswordNeededError:
        await http_post(client, "/api/public/worker/result", {
            "job_id": job["id"], "ok": True, "result": {"need": "password"},
        })


async def handle_login_password(client: httpx.AsyncClient, job: dict, account: dict):
    tc = await get_or_make_client(account)
    pw = job["payload"]["password"]
    await tc.sign_in(password=pw)
    session_str = StringSession.save(tc.session)
    await http_post(client, "/api/public/worker/result", {
        "job_id": job["id"], "ok": True, "result": {"logged_in": True},
        "account_patch": {"session_string": session_str, "status": "active", "last_error": None},
    })


async def join_one(tc: TelegramClient, link: str, username: Optional[str]):
    invite = parse_invite(link)
    if invite:
        try:
            await tc(ImportChatInviteRequest(invite))
            return "joined", None
        except errors.UserAlreadyParticipantError:
            return "joined", None
        except errors.InviteHashExpiredError as e:
            return "failed", f"invite expired: {e}"
    uname = parse_username(link, username)
    if not uname:
        return "failed", "unresolvable link"
    try:
        await tc(JoinChannelRequest(uname))
        return "joined", None
    except errors.UserAlreadyParticipantError:
        return "joined", None
    except errors.ChannelPrivateError:
        return "failed", "channel private"
    except errors.UsernameNotOccupiedError:
        return "failed", "username not found"


async def handle_send_ad(client: httpx.AsyncClient, job: dict, account: dict):
    tc = await get_or_make_client(account)
    p = job["payload"]
    groups: List[dict] = p["groups"]
    ad = p["ad_text"]
    auto_join: bool = p.get("auto_join", True)
    gi = int(p.get("group_interval_sec", 30))
    gj = int(p.get("group_interval_jitter_sec", 10))

    send_logs = []
    ag_updates = []

    for i, g in enumerate(groups):
        link = g["link"]
        username = g.get("username")
        try:
            if auto_join:
                status, err = await join_one(tc, link, username)
                ag_updates.append({"group_id": g["id"], "status": status, "error": err})
                if status != "joined":
                    send_logs.append({"group_id": g["id"], "group_link": link, "status": "not_member", "error": err})
                    continue
                await asyncio.sleep(random.uniform(2, 5))

            target = parse_username(link, username)
            entity = target or link
            msg = await tc.send_message(entity, ad)
            mid = getattr(msg, "id", None)
            link_url = f"https://t.me/{target}/{mid}" if target and mid else None
            send_logs.append({"group_id": g["id"], "group_link": link, "status": "sent", "message_link": link_url})
        except errors.FloodWaitError as e:
            send_logs.append({"group_id": g["id"], "group_link": link, "status": "flood_wait", "error": f"wait {e.seconds}s"})
            # Honor flood wait, then bail out of this batch
            await asyncio.sleep(min(e.seconds, 60))
            await http_post(client, "/api/public/worker/result", {
                "job_id": job["id"], "ok": True,
                "result": {"flood_wait": e.seconds, "sent": sum(1 for s in send_logs if s["status"] == "sent")},
                "account_patch": {"status": "flood_wait", "last_error": f"flood wait {e.seconds}s"},
                "send_logs": send_logs, "account_group_updates": ag_updates,
            })
            return
        except errors.ChatWriteForbiddenError:
            send_logs.append({"group_id": g["id"], "group_link": link, "status": "forbidden", "error": "cannot post"})
        except errors.UserBannedInChannelError:
            send_logs.append({"group_id": g["id"], "group_link": link, "status": "banned", "error": "banned in chat"})
        except Exception as e:
            send_logs.append({"group_id": g["id"], "group_link": link, "status": "error", "error": str(e)[:200]})

        if i < len(groups) - 1:
            delay = max(1, gi + random.randint(-gj, gj))
            await asyncio.sleep(delay)

    await http_post(client, "/api/public/worker/result", {
        "job_id": job["id"], "ok": True,
        "result": {"sent": sum(1 for s in send_logs if s["status"] == "sent")},
        "send_logs": send_logs,
        "account_group_updates": ag_updates,
    })


HANDLERS = {
    "login_start": handle_login_start,
    "login_code": handle_login_code,
    "login_password": handle_login_password,
    "send_ad": handle_send_ad,
}


async def process_one(client: httpx.AsyncClient):
    r = await http_post(client, "/api/public/worker/poll")
    job = r.get("job")
    if not job:
        return False
    account = r.get("account")
    handler = HANDLERS.get(job["type"])
    if not handler:
        await http_post(client, "/api/public/worker/result", {
            "job_id": job["id"], "ok": False, "error": f"unknown job type {job['type']}"
        })
        return True
    try:
        await handler(client, job, account or {})
    except Exception as e:
        traceback.print_exc()
        await http_post(client, "/api/public/worker/result", {
            "job_id": job["id"], "ok": False, "error": str(e)[:500],
        })
    return True


async def main():
    print(f"[worker] connected to {DASHBOARD_URL}")
    async with httpx.AsyncClient() as client:
        last_tick = 0.0
        while True:
            try:
                worked = await process_one(client)
            except Exception as e:
                print(f"[worker] poll error: {e}")
                worked = False

            now = time.time()
            if now - last_tick > TICK_INTERVAL:
                try:
                    t = await http_post(client, "/api/public/worker/tick")
                    if t.get("created"):
                        print(f"[worker] tick: {t['created']} new send jobs queued")
                except Exception as e:
                    print(f"[worker] tick error: {e}")
                last_tick = now

            if not worked:
                await asyncio.sleep(POLL_INTERVAL)


async def _runner():
    try:
        await main()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(_runner())
    except (KeyboardInterrupt, SystemExit):
        print("[worker] shutting down")
