import os
import json
import base64
import logging
import anthropic
import gspread
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
SHEET_ID       = os.environ["GOOGLE_SHEET_ID"]
ALLOWED_USERS  = set(os.getenv("ALLOWED_USER_IDS", "").split(","))

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

EXTRACT_PROMPT = """Bu ekran görüntüsü bir kripto borsasındaki işlem detayı sayfasıdır.
Aşağıdaki bilgileri JSON formatında çıkar. Bulunamazsa null yaz.

{
  "platform": "borsa/uygulama adı",
  "islem_cifti": "örn: NAORIS/USDT",
  "token": "sadece token adı, örn: NAORIS",
  "emir_tipi": "Limit Alış / Market Alış / Limit Satış / Market Satış",
  "durum": "Gerçekleşti / Beklemede / İptal",
  "emir_tarihi": "GG.AA.YYYY SS:DD:SS",
  "gerceklesme_tarihi": "GG.AA.YYYY SS:DD:SS",
  "limit_fiyat": sayı veya null,
  "girilen_tutar_usdt": sayı,
  "gerceklesen_miktar_token": sayı,
  "gerceklesen_fiyat_usdt": sayı,
  "gerceklesen_tutar_usdt": sayı,
  "komisyon_usdt": sayı,
  "toplam_usdt": sayı
}

Sadece JSON döndür, başka hiçbir şey yazma."""


def sheets_client():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def extract_trade_data(image_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode()
    msg = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": EXTRACT_PROMPT},
            ],
        }],
    )
    raw = msg.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def append_to_sheet(data: dict):
    gc = sheets_client()
    sh = gc.open_by_key(SHEET_ID)

    # ── İşlem Kayıtları sekmesi ──────────────────────────────────────────────
    try:
        ws = sh.worksheet("İşlem Kayıtları")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("İşlem Kayıtları", rows=1000, cols=20)
        ws.append_row([
            "PLATFORM","İŞLEM ÇİFTİ","EMİR TİPİ","DURUM",
            "EMİR TARİHİ","GERÇEKLEŞME TARİHİ",
            "LİMİT FİYAT (USDT)","GİRİLEN TUTAR (USDT)",
            "GERÇEKLEŞEN MİKTAR (TOKEN)","GERÇEKLEŞEN FİYAT (USDT)",
            "GERÇEKLEŞEN TUTAR (USDT)","KOMİSYON (USDT)","TOPLAM (USDT)","NOT",
        ])

    row = [
        data.get("platform")              or "",
        data.get("islem_cifti")           or "",
        data.get("emir_tipi")             or "",
        data.get("durum")                 or "",
        data.get("emir_tarihi")           or "",
        data.get("gerceklesme_tarihi")    or "",
        data.get("limit_fiyat")           or "",
        data.get("girilen_tutar_usdt")    or "",
        data.get("gerceklesen_miktar_token") or "",
        data.get("gerceklesen_fiyat_usdt")   or "",
        data.get("gerceklesen_tutar_usdt")   or "",
        data.get("komisyon_usdt")         or "",
        data.get("toplam_usdt")           or "",
        f"Bot · {datetime.now().strftime('%d.%m.%Y %H:%M')}",
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")

    # ── Kar-Zarar sekmesi: alış ile satış eşleştir ──────────────────────────
    emir = (data.get("emir_tipi") or "").lower()
    is_buy  = "alış" in emir or "buy" in emir
    is_sell = "satış" in emir or "sell" in emir
    token   = data.get("token") or ""

    try:
        pnl_ws = sh.worksheet("Kar-Zarar")
    except gspread.WorksheetNotFound:
        pnl_ws = sh.add_worksheet("Kar-Zarar", rows=500, cols=20)
        pnl_ws.append_row([
            "TOKEN","ALIŞ TARİHİ","ALIŞ FİYAT","ALIŞ MİKTAR","ALIŞ TUTAR","ALIŞ KOM",
            "SATIŞ TARİHİ","SATIŞ FİYAT","SATIŞ MİKTAR","SATIŞ TUTAR","SATIŞ KOM",
            "BRÜT K/Z","K/Z %","NET K/Z","DURUM",
        ])

    if is_buy:
        pnl_ws.append_row([
            token,
            data.get("gerceklesme_tarihi") or data.get("emir_tarihi") or "",
            data.get("gerceklesen_fiyat_usdt") or data.get("limit_fiyat") or "",
            data.get("gerceklesen_miktar_token") or "",
            data.get("gerceklesen_tutar_usdt") or "",
            data.get("komisyon_usdt") or "",
            "","","","","",   # satış alanları boş
            "","","",
            "AÇIK",
        ], value_input_option="USER_ENTERED")

    elif is_sell and token:
        # Aynı token'ın en eski AÇIK alışını bul
        all_rows = pnl_ws.get_all_values()
        for i, r in enumerate(all_rows[1:], start=2):
            if r[0] == token and r[14] == "AÇIK":
                sell_price  = data.get("gerceklesen_fiyat_usdt") or data.get("limit_fiyat") or 0
                sell_qty    = data.get("gerceklesen_miktar_token") or 0
                sell_amount = data.get("gerceklesen_tutar_usdt") or 0
                sell_fee    = data.get("komisyon_usdt") or 0

                buy_amount  = float(r[4]) if r[4] else 0
                buy_fee     = float(r[5]) if r[5] else 0

                brut_pnl = float(sell_amount) - buy_amount
                pct      = (brut_pnl / buy_amount * 100) if buy_amount else 0
                net_pnl  = brut_pnl - buy_fee - float(sell_fee)
                status   = "KAR ✅" if net_pnl > 0 else "ZARAR ❌"

                row_num = i + 1
                pnl_ws.update(f"G{row_num}:O{row_num}", [[
                    data.get("gerceklesme_tarihi") or "",
                    sell_price, sell_qty, sell_amount, sell_fee,
                    round(brut_pnl, 4),
                    round(pct, 2),
                    round(net_pnl, 4),
                    status,
                ]], value_input_option="USER_ENTERED")
                break

    return is_buy, is_sell


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USERS != {""} and user_id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Erişim izniniz yok.")
        return

    await update.message.reply_text("📸 Görsel alındı, işleniyor...")

    photo = await update.message.photo[-1].get_file()
    img_bytes = await photo.download_as_bytearray()

    try:
        data = extract_trade_data(bytes(img_bytes))
        is_buy, is_sell = append_to_sheet(data)

        emir_tipi = data.get("emir_tipi", "")
        token     = data.get("token", "")
        fiyat     = data.get("gerceklesen_fiyat_usdt") or data.get("limit_fiyat") or ""
        miktar    = data.get("gerceklesen_miktar_token") or ""
        tutar     = data.get("gerceklesen_tutar_usdt") or ""
        komisyon  = data.get("komisyon_usdt") or ""
        toplam    = data.get("toplam_usdt") or ""

        icon = "🟢" if is_buy else "🔴" if is_sell else "📊"
        msg = (
            f"{icon} *{emir_tipi}* kaydedildi!\n\n"
            f"🪙 Token: `{token}`\n"
            f"💲 Fiyat: `{fiyat} USDT`\n"
            f"📦 Miktar: `{miktar}`\n"
            f"💰 Tutar: `{tutar} USDT`\n"
            f"🏷 Komisyon: `{komisyon} USDT`\n"
            f"✅ Toplam: `{toplam} USDT`\n\n"
        )
        if is_sell:
            msg += "📊 Kar-Zarar hesaplandı ve eşleştirildi!"
        elif is_buy:
            msg += "📝 Kar-Zarar sekmesine AÇIK olarak eklendi."

        await update.message.reply_text(msg, parse_mode="Markdown")

    except json.JSONDecodeError:
        await update.message.reply_text("❌ Veri okunamadı. Lütfen net bir ekran görüntüsü gönderin.")
    except Exception as e:
        log.exception("Hata")
        await update.message.reply_text(f"⚠️ Hata: {e}")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Merhaba! İşlem detayı ekran görüntüsü gönderin, otomatik kaydedeyim.\n\n"
        "📸 Desteklenen: Binance, Bybit, OKX, Naoris Protocol ve diğer tüm borsalar."
    )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot başlatıldı...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
