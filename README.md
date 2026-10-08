## MMD SELF Update 11
- Added branded panel image asset.
- Added share-phone and code-entry reply buttons.
- Phone parser accepts spaces, dashes, parentheses, Persian/Arabic digits, and international 00 prefix.

# Telegram Self Manager – Automation Update

این نسخه علاوه بر پنل کاربران و الماس، اتوماسیون سلف را اضافه می‌کند.

## قابلیت‌های جدید
- 👁 سین خودکار پیام‌های دریافتی با Telethon
- 💬 پاسخ خودکار به پیام دریافتی
- 🔎 پاسخ خودکار بر اساس کلمات کلیدی با قالب `کلمه=>پاسخ | کلمه2=>پاسخ2`
- ⏱ تأخیر قابل تنظیم پاسخ خودکار (۰ تا ۳۰ ثانیه)
- 🛡 فاصله امن بین پاسخ‌های خودکار (حداقل ۳۰ ثانیه)
- 🌐 محدوده پاسخ: فقط چت خصوصی یا همه چت‌ها
- 👍 ریکت خودکار
- 📊 ثبت فعالیت و آخرین پیام
- 🔄 اجرای دوباره اتوماسیون بعد از Restart برای سلف‌های فعال
- 🔐 نشست سلف همچنان با Fernet رمزنگاری می‌شود

## دستورات اتوماسیون
- `/automation`
- `/autoseen on` یا `/autoseen off`
- `/autoreply متن`
- `/autoreplydelay 3`
- `/autoreplycooldown 60`
- `/autoscope private` یا `/autoscope all`
- `/keywordreply سلام=>سلام! | قیمت=>لطفاً صبر کن`
- `/keywordreply clear`
- `/reaction ❤️`

پاسخ‌های خودکار عمداً rate-limit دارند تا از ارسال ناخواسته و تکراری جلوگیری شود.

## نکته گروه‌ها
اگر قرار است ربات پنل معمولی را با متن `پنل` در گروه ببیند، Privacy Mode ربات باید در BotFather خاموش باشد؛ این محدودیت از سمت تلگرام است و با کد قابل دور زدن نیست.

ورود سلف در این نسخه تغییر داده نشده است.
