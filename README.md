# Kripto İşlem Kayıt Botu

## Kurulum

### 1. Telegram Bot Token Al
- @BotFather → /newbot → token kopyala

### 2. Anthropic API Key Al
- console.anthropic.com → API Keys → Create

### 3. Google Sheets Kur
1. console.cloud.google.com → Proje oluştur
2. Google Sheets API + Google Drive API → Enable
3. IAM → Service Accounts → Anahtar oluştur (JSON indir)
4. sheets.google.com'da yeni sheet aç, paylaş → service account e-posta adresini "Düzenleyici" olarak ekle
5. Sheet ID'yi URL'den kopyala: docs.google.com/spreadsheets/d/**SHEET_ID**/edit

### 4. Railway.app Deploy
1. railway.app → GitHub ile giriş
2. New Project → Deploy from GitHub
3. Variables ekle (.env.example'dan)
4. GOOGLE_CREDENTIALS_JSON: JSON dosyasını aç, tüm içeriği tek satır yapıştır

### 5. Bot'u Başlat
Railway otomatik başlatır. Telegram'da bota mesaj at: herhangi bir metin
