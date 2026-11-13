# -*- coding: utf-8 -*-
"""
MyCustomer Smart Store — GitHub Actions 24/7 Cloud Outreach Runner
==================================================================
يعمل في سحابة GitHub تلقائياً حتى عندما يكون الحاسوب والهاتف مغلقين تماماً:
- يستخدم StringSession المشفرة من GitHub Secrets.
- يفرض قيد الشهر الواحد (30 يوماً فأكثر) بالاعتماد على تاريخ آخر رسالة فعلية في تيليجرام.
- يفحص سجل المحادثة الحقيقي في تيليجرام قبل إرسال كل رسالة لمنع أي تكرار نهائياً.
- يلتزم بحد زمني آمن لكل دورة سحابية (20 دقيقة) ويحفظ التقدم لحظياً في Google Sheets.
"""

import os
import sys
import time
import json
import random
import asyncio
import logging
import urllib.request
import urllib.parse
import ssl
from datetime import datetime, timezone
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("CloudOutreach")

API_ID = int(os.environ.get("TELEGRAM_API_ID") or "21646565")
API_HASH = os.environ.get("TELEGRAM_API_HASH") or ""
STRING_SESSION = (os.environ.get("TELEGRAM_STRING_SESSION") or "").strip()
WEBAPP_BASE = (
    os.environ.get("WEBAPP_BASE")
    or "https://script.google.com/macros/s/AKfycby_aFomRXGJLtau7jcRaOitHj95YS_JCgm5blBqR2SSMg7Dm_G8NAkEnS_47TeFXo8C/exec"
).strip()
ADMIN_NOTIFY_ID = 268455254
MIN_REQUIRED_DAYS = 30
MAX_CLOUD_RUN_SECONDS = 18 * 60  # 18 دقيقة كحد أقصى لكل دورة سحابية وتكمل الدورة التالية تلقائياً

DEFAULT_MESSAGE_TEMPLATE = """أهلاً وسهلاً بحضرتك عزيزنا {name} 🌸✨
مشتاقين لتواصلك ويانا بمتجر Erbil Medical Market!

حبينا نطمن عنك ونشاركك أحدث التطورات بمتجرنا:
🔥 وصلتنا تشكيلة جديدة ومميزة جداً من أحدث المستلزمات والمنتجات بأسعار جملة تنافسية وتواريخ صلاحية حديثة 100%.
📦 تحديث شامل لكتالوج المنتجات وتفاصيل الأسعار والمواصفات لنسهل عليك اختيار طلبك بثواني!

🎁 كزبون عزيز وقديم، يسعدنا نخصص لك أولوية التجهيز والمتابعة المباشرة مع الإدارة لأي طلبية تحتاجها!

👇 تفضل بالاطلاع على أحدث المعروضات والأسعار عبر قناتنا أو أرسل لنا أي منتج ترغب بتفاصيله وبخدمتك فوراً:
https://t.me/Kur_Me"""


def fetch_json_sync(url, timeout=30, post_data=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    headers = {"User-Agent": "MyCustomerCloudOutreach/1.0"}
    data_bytes = None
    if post_data is not None:
        data_bytes = json.dumps(post_data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data_bytes, headers=headers)
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def fetch_json(url, timeout=30, post_data=None):
    return await asyncio.to_thread(fetch_json_sync, url, timeout, post_data)


async def poll_campaign_state():
    return await fetch_json(f"{WEBAPP_BASE}?diag=campaign_poll", timeout=25)


async def fetch_campaign_audience(days_inactive, tier, batch_limit):
    days_eff = max(int(days_inactive or MIN_REQUIRED_DAYS), MIN_REQUIRED_DAYS)
    qs = urllib.parse.urlencode({
        "diag": "campaign_audience",
        "days": str(days_eff),
        "tier": str(tier),
        "limit": str(batch_limit),
    })
    return await fetch_json(f"{WEBAPP_BASE}?{qs}", timeout=45)


async def mark_customer_promo(chat_id, status="sent", deactivated=False, last_ts=0, is_promo=False):
    try:
        qs = urllib.parse.urlencode({
            "diag": "mark_promo_sent",
            "chat_id": str(chat_id),
            "status": status,
            "deactivated": "true" if deactivated else "false",
            "last_ts": str(int(last_ts or 0)),
            "is_promo": "true" if is_promo else "false",
        })
        await fetch_json(f"{WEBAPP_BASE}?{qs}", timeout=20)
    except Exception as ex:
        log.warning(f"Failed to mark promo status for {chat_id}: {ex}")


async def report_campaign_progress(campaign_id, sent, failed, remaining, total, completed=False):
    try:
        qs = urllib.parse.urlencode({
            "diag": "campaign_report",
            "campaign_id": str(campaign_id or ""),
            "sent": str(sent),
            "failed": str(failed),
            "remaining": str(remaining),
            "total": str(total),
            "completed": "true" if completed else "false",
        })
        await fetch_json(f"{WEBAPP_BASE}?{qs}", timeout=20)
    except Exception as ex:
        log.warning(f"Failed to report progress: {ex}")


def is_promo_text(msg_text):
    t = str(msg_text or "")
    return (
        "Erbil Medical Market" in t
        or "تشكيلة جديدة ومميزة" in t
        or "وجبة بضاعة جديدة" in t
        or "https://t.me/Kur_Me" in t
    )


async def scan_and_sync_real_dialogs(client, sync_to_sheet=True):
    live_map = {}
    sync_items = []
    try:
        async for d in client.iter_dialogs():
            if not d.is_user or (d.entity and getattr(d.entity, "bot", False)):
                continue
            uid = str(d.entity.id) if d.entity else ""
            if not uid or not d.date:
                continue
            dt = d.date if d.date.tzinfo else d.date.replace(tzinfo=timezone.utc)
            msg_txt = (d.message.text or d.message.message or "") if d.message else ""
            promo_flag = bool(d.message and d.message.out and is_promo_text(msg_txt))
            live_map[uid] = (dt, promo_flag)
            sync_items.append({
                "id": uid,
                "lastTs": int(dt.timestamp()),
                "isPromo": promo_flag,
            })

        if sync_to_sheet and sync_items:
            for i in range(0, len(sync_items), 350):
                chunk = sync_items[i : i + 350]
                try:
                    await fetch_json(f"{WEBAPP_BASE}?action=sync_real_contacts", timeout=45, post_data={"items": chunk})
                except Exception as e_sync:
                    log.warning(f"Batch sync chunk warning: {e_sync}")
            log.info(f"Synced real last-message timestamps for {len(sync_items)} Telegram dialogs.")
    except Exception as ex:
        log.warning(f"Dialog scan error: {ex}")
    return live_map


def format_outreach_message(template_text, btn_text, btn_url, cust_name, cust_id):
    text = (template_text or "").strip() or DEFAULT_MESSAGE_TEMPLATE
    text = text.replace("{name}", cust_name).replace("{الاسم}", cust_name)
    text = text.replace("{id}", str(cust_id)).replace("{المعرف}", str(cust_id))
    btn_url = (btn_url or "").strip()
    btn_text = (btn_text or "").strip()
    if btn_url and btn_url not in text:
        if btn_text:
            text += f"\n\n🔗 {btn_text}:\n{btn_url}"
        else:
            text += f"\n\n{btn_url}"
    return text


async def resolve_customer_entity(client, cust):
    c_id = int(cust["id"]) if str(cust["id"]).lstrip("-").isdigit() else cust["id"]
    username = str(cust.get("username") or "").lstrip("@").strip()
    phone = str(cust.get("phone") or "").strip()

    candidates = []
    if username:
        candidates.append(username)
    if phone:
        candidates.append(phone)
    candidates.append(c_id)

    for cand in candidates:
        try:
            ent = await client.get_entity(cand)
            if ent:
                return ent
        except Exception:
            continue
    return None


async def main():
    if not STRING_SESSION or not API_HASH:
        log.error("Missing TELEGRAM_STRING_SESSION or TELEGRAM_API_HASH in environment secrets.")
        sys.exit(1)

    start_wall = time.monotonic()
    poll = await poll_campaign_state()
    if not poll.get("ok"):
        log.error(f"Could not poll campaign state: {poll}")
        return

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        log.error("StringSession is not authorized.")
        await client.disconnect()
        sys.exit(1)

    try:
        # إذا لم تكن هناك حملة نشطة، نفحص إن كان هناك زبائن منقطعون منذ 30 يوماً ونزامن التواريخ
        if not poll.get("active"):
            manual_stop_at = str(poll.get("manualStopAt") or "").strip()
            if manual_stop_at:
                try:
                    stop_dt = datetime.fromisoformat(manual_stop_at.replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - stop_dt).total_seconds() < 12 * 3600:
                        log.info("Campaign was manually stopped recently. Respecting stop state.")
                        return
                except Exception:
                    pass

            await scan_and_sync_real_dialogs(client, sync_to_sheet=True)
            aud = await fetch_campaign_audience(MIN_REQUIRED_DAYS, "all", 0)
            if aud.get("ok") and int(aud.get("total") or 0) > 0:
                log.info(f"Discovered {aud.get('total')} inactive customers (>= 30d). Starting campaign...")
                await fetch_json(f"{WEBAPP_BASE}?diag=start_campaign&reset=false", timeout=30)
                poll = await poll_campaign_state()
            else:
                log.info("No active campaign and 0 customers due for 30d outreach.")
                return

        if not poll.get("active"):
            return

        campaign_id = poll.get("campaignId") or ""
        days_inactive = max(int(poll.get("daysInactive") or MIN_REQUIRED_DAYS), MIN_REQUIRED_DAYS)
        tier = str(poll.get("tier") or "all")
        batch_limit = int(poll.get("batchLimit") or 0)
        template_text = poll.get("message") or DEFAULT_MESSAGE_TEMPLATE
        btn_text = poll.get("buttonText") or ""
        btn_url = poll.get("buttonUrl") or ""
        sent_count = int(poll.get("sent") or 0)
        failed_count = int(poll.get("failed") or 0)

        live_map = await scan_and_sync_real_dialogs(client, sync_to_sheet=True)
        aud_res = await fetch_campaign_audience(days_inactive, tier, batch_limit)
        raw_customers = aud_res.get("customers") or []

        now_utc = datetime.now(timezone.utc)
        customers = []
        for c in raw_customers:
            cid = str(c.get("id") or "")
            if cid in live_map:
                dt_real, _ = live_map[cid]
                if (now_utc - dt_real).total_seconds() / 86400.0 < days_inactive:
                    continue
            customers.append(c)

        if not customers:
            log.info("All eligible customers have been contacted. Marking campaign completed.")
            await report_campaign_progress(campaign_id, sent_count, failed_count, 0, sent_count + failed_count, completed=True)
            return

        total_this_pass = len(customers)
        total_target = max(int(poll.get("totalTarget") or 0), sent_count + failed_count + total_this_pass)
        log.info(f"Cloud batch started: {total_this_pass} customers queued.")

        for idx, cust in enumerate(customers, start=1):
            if time.monotonic() - start_wall >= MAX_CLOUD_RUN_SECONDS:
                log.info("Reached cloud cycle time limit (18m). Progress saved; next cron run will continue automatically.")
                break

            if idx % 5 == 0:
                chk = await poll_campaign_state()
                if not chk.get("active"):
                    log.info("Campaign stopped by admin. Exiting cleanly.")
                    return

            c_id = cust.get("id")
            c_name = cust.get("name") or "عزيزنا الزبون"
            text = format_outreach_message(template_text, btn_text, btn_url, c_name, c_id)
            remaining_after = total_this_pass - idx

            try:
                entity = await resolve_customer_entity(client, cust)
                if not entity:
                    failed_count += 1
                    await mark_customer_promo(c_id, status="permanent_skip", deactivated=False)
                    await report_campaign_progress(campaign_id, sent_count, failed_count, remaining_after, total_target, completed=False)
                    continue

                latest_msgs = await client.get_messages(entity, limit=1)
                if latest_msgs and len(latest_msgs) > 0 and latest_msgs[0].date:
                    last_msg = latest_msgs[0]
                    last_dt = last_msg.date if last_msg.date.tzinfo else last_msg.date.replace(tzinfo=timezone.utc)
                    age_days = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0
                    if age_days < days_inactive:
                        promo_flag = bool(last_msg.out and is_promo_text(last_msg.text or last_msg.message or ""))
                        await mark_customer_promo(c_id, status="recent_contact", last_ts=int(last_dt.timestamp()), is_promo=promo_flag)
                        await report_campaign_progress(campaign_id, sent_count, failed_count, remaining_after, total_target, completed=False)
                        continue

                await client.send_message(entity, text, link_preview=False)
                sent_count += 1
                log.info(f"[{idx}/{total_this_pass}] Sent to {c_name} ({c_id})")
                await mark_customer_promo(c_id, status="sent", deactivated=False)

            except errors.FloodWaitError as fwe:
                wait_s = int(getattr(fwe, "seconds", 30)) + 2
                log.warning(f"FloodWait {wait_s}s. Waiting...")
                if wait_s > 300:
                    log.warning("FloodWait > 5m; deferring to next scheduled cron cycle.")
                    break
                await asyncio.sleep(wait_s)
            except (errors.UserPrivacyRestrictedError, errors.UserIsBlockedError):
                failed_count += 1
                await mark_customer_promo(c_id, status="permanent_skip", deactivated=False)
            except (errors.UserDeactivatedError, errors.UserDeactivatedBanError, errors.InputUserDeactivatedError, errors.PeerIdInvalidError) as e_deact:
                is_deact = not isinstance(e_deact, errors.PeerIdInvalidError)
                failed_count += 1
                await mark_customer_promo(c_id, status="permanent_skip", deactivated=is_deact)
            except Exception as ex:
                log.error(f"Error sending to {c_id}: {ex}")
                failed_count += 1

            await report_campaign_progress(campaign_id, sent_count, failed_count, remaining_after, total_target, completed=False)
            await asyncio.sleep(random.uniform(6.5, 10.5))

            if idx % 15 == 0 and remaining_after > 0:
                await asyncio.sleep(20)
        else:
            # اكتملت جميع عناصر القائمة دون انقطاع
            await report_campaign_progress(campaign_id, sent_count, failed_count, 0, total_target, completed=True)
            try:
                await client.send_message(
                    ADMIN_NOTIFY_ID,
                    f"🎉 <b>اكتملت الحملة السحابية بنجاح تام!</b>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"• تم الإرسال بنجاح: <b>{sent_count}</b> رسالة\n"
                    f"• متعذر / خصوصية: <b>{failed_count}</b>\n"
                    f"• تم التنفيذ عبر السحابة (GitHub Cloud) 🌸",
                    parse_mode="html",
                )
            except Exception:
                pass
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
