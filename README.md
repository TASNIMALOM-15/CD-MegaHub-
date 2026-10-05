# MEGA Telegram Manager Bot

## আগে যা লাগবে
1. **BOT_TOKEN** – @BotFather থেকে বট বানিয়ে নাও।
2. **API_ID / API_HASH** – https://my.telegram.org → API development tools।
3. **ADMIN_IDS** – তোমার Telegram numeric ID (@userinfobot থেকে পাবে)। শুধু এই আইডিগুলো বট চালাতে পারবে।
4. **MONGO_URI** – MongoDB Atlas (Free M0) ক্লাস্টার বানিয়ে connection string নাও
   (Network Access-এ `0.0.0.0/0` অ্যালাউ করো)। Render-এর ফ্রি ডিস্ক স্থায়ী নয়, তাই অ্যাকাউন্ট সেশন এখানে জমা থাকবে।
5. **SECRET_KEY** – যেকোনো লম্বা র‍্যান্ডম স্ট্রিং। MEGA সেশন এই কী দিয়ে এনক্রিপ্ট হয়ে DB-তে থাকে।
   ⚠️ একবার সেট করে আর বদলিও না, বদলালে সেভ করা অ্যাকাউন্ট খুলবে না।

## Deploy
1. এই সব ফাইল একটি (Private) GitHub repo-তে আপলোড করো।
2. Render → New → **Blueprint** (বা Web Service, Build: `pip install -r requirements.txt`, Start: `python bot.py`)।
3. Environment Variables-এ উপরের ৬টি মান বসাও → Deploy।
4. ফ্রি প্ল্যানে ১৫ মিনিট ট্রাফিক না থাকলে সার্ভিস ঘুমায়। https://uptimerobot.com এ
   `https://<your-app>.onrender.com/health` ৫ মিনিট পরপর ping করার মনিটর বানাও।
5. বটে `/start` দাও → **➕ অ্যাকাউন্ট যোগ**।

## নোট
- পাসওয়ার্ড সেভ হয় না; শুধু লগইন-সেশন (এনক্রিপ্টেড) সেভ হয়। পাসওয়ার্ড/কোডের মেসেজ বট মুছে দেয়।
- Telegram limit: প্রতি ফাইল সর্বোচ্চ ২GB। MEGA ফ্রি অ্যাকাউন্টে ডাউনলোড কোটা সীমিত।
- Shared Items থেকে ডাউনলোড চলে; আপলোড/ডিলিট শুধু নিজের Cloud Drive-এ।
