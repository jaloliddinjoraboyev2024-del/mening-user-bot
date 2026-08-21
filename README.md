# Telegram Userbot — Professional Buttons v9

## Muhim
Bu versiya tanlangan chatlarga **bir martalik, navbat bilan yuborish** oqimini qo'llaydi. U cheksiz avtomatik tarqatish yoki Telegram cheklovlarini chetlab o'tish uchun mo'ljallanmagan.

## Asosiy imkoniyatlar
- QR-login va 2FA
- OS-level instance lock
- Owner-only boshqaruv
- Professional tugmali menyu
- Guruh/kanal/shaxsiy/bot chatlarini tanlash
- Sahifalash va qidirish
- Tanlangan chatlar o'chirish/yuborishdan keyin ham saqlanadi
- O'z xabarlarini o'chirishda sender-ID fallback
- Bir martalik navbatli xabar yuborish
- Jarayonni To'xtatish tugmasi
- Amal tarixi va status
- User session faol bo'lsa, background presence refresh

## Ishga tushirish
1. `.env.example` nusxasini `.env` qiling.
2. `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID` ni kiriting.
3. Virtual environment ichida `pip install -r requirements.txt` qiling.
4. `python main.py` ni ishga tushiring.
5. Yangi user session bo'lsa QR Telegram orqali tasdiqlanadi; 2FA yoqilgan bo'lsa parol terminalda yashirin so'raladi.

## Xabar yuborish oqimi
`Bosh menyu → Amallar markazi → 📤 Navbat bilan 1 marta yuborish`

Tanlangan chatlar snapshot qilinadi, xabar kiritiladi va ular ketma-ket qayta ishlanadi. Har bir chatning xatosi keyingi chatni to'xtatmaydi. `🛑 To'xtatish` bilan kampaniyani bekor qilish mumkin.

## Tanlovning saqlanishi
Xabar yuborish yoki o'z xabarlarini o'chirish tugagandan keyin chatlar avtomatik ravishda tanlovdan chiqarilmaydi. Tanlovni faqat `Tanlanganlar` bo'limidan qo'lda olib tashlash mumkin.

## Security
`.env`, Telegram `.session` fayllari, lokal SQLite state va loglar repositoryga yuborilmasligi kerak. Ular `.gitignore` orqali chiqarib tashlangan.
