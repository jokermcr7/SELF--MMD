"""Secure local Telegram user-session login for MMD SELF.

Run this script on your own computer, never inside the bot chat. It prompts for
phone/code/2FA locally and writes a Telethon StringSession to a private file.
"""
from __future__ import annotations

import asyncio
import getpass
import os
import re
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberInvalidError
from telethon.sessions import StringSession

DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def ascii_digits(value: str) -> str:
    return value.translate(DIGIT_MAP)


def normalize_phone(value: str) -> str:
    value = ascii_digits(value.strip())
    # Keep one leading + and discard spaces, hyphens, parentheses and other separators.
    value = re.sub(r"[^0-9+]", "", value)
    if value.count("+") > 1 or ("+" in value and not value.startswith("+")):
        raise ValueError("علامت + باید فقط یک‌بار و ابتدای شماره باشد.")
    digits = value[1:] if value.startswith("+") else value
    if not digits.isdigit() or not 7 <= len(digits) <= 15:
        raise ValueError("شماره معتبر نیست؛ کد کشور را هم وارد کن، مثلاً +989121234567")
    return "+" + digits


def normalize_code(value: str) -> str:
    # Accept digits copied with spaces/dashes or Persian/Arabic numerals.
    value = ascii_digits(value.strip())
    code = re.sub(r"\D", "", value)
    if not 4 <= len(code) <= 8:
        raise ValueError("کد باید معمولاً ۵ یا ۶ رقم باشد؛ دوباره وارد کن.")
    return code


async def main() -> int:
    api_id_raw = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    if not api_id_raw or not api_hash:
        print("ابتدا API_ID و API_HASH را از my.telegram.org بگیر و به‌صورت متغیر محیطی تنظیم کن.")
        print("برای امنیت، کد ورود یا رمز دومرحله‌ای را به هیچ باتی نفرست.")
        return 2
    try:
        api_id = int(api_id_raw)
    except ValueError:
        print("API_ID باید عددی باشد.")
        return 2

    while True:
        try:
            phone = normalize_phone(input("شماره با کد کشور (مثلاً +98912...): "))
            break
        except ValueError as exc:
            print(exc)

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        while True:
            try:
                code = normalize_code(getpass.getpass("کد تلگرام (ورودی مخفی؛ ارقام فارسی هم مجاز است): "))
                await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
                break
            except ValueError as exc:
                print(exc)
            except PhoneCodeInvalidError:
                print("کد اشتباه است؛ آخرین کدی را که تلگرام فرستاده وارد کن.")
        
    except SessionPasswordNeededError:
        password = getpass.getpass("رمز تأیید دومرحله‌ای (ورودی مخفی): ")
        await client.sign_in(password=password)
    except PhoneNumberInvalidError:
        print("تلگرام شماره را نپذیرفت؛ شماره و کد کشور را بررسی کن.")
        return 3
    except Exception as exc:
        print(f"ورود انجام نشد: {type(exc).__name__}. جزئیات حساس نمایش داده نمی‌شود.")
        return 4

    try:
        session_string = client.session.save()
        out = Path("MMD_SELF_SESSION_STRING.txt")
        out.write_text(session_string + "\n", encoding="utf-8")
        try:
            out.chmod(0o600)
        except OSError:
            pass
        print("ورود موفق بود. نشست در فایل خصوصی MMD_SELF_SESSION_STRING.txt ذخیره شد.")
        print("این فایل مثل رمز عبور حساب است: برای کسی نفرست و در GitHub آپلود نکن.")
        print("اگر پروژه‌ات نیاز دارد روی سرور اجرا شود، فقط خودت آن را در متغیر محرمانه TELETHON_SESSION سرویس قرار بده.")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (KeyboardInterrupt, EOFError):
        print("\nلغو شد.")
        raise SystemExit(130)
