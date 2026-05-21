const TelegramBot = require('node-telegram-bot-api');
const express = require('express');

const token = process.env.BOT_TOKEN;

const bot = new TelegramBot(token, { polling: true });

const app = express();

app.get("/", (req, res) => {
  res.send("Telegram Bot Running");
});

const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
  console.log("Server running on port " + PORT);
});

bot.onText(/\/start/, (msg) => {
  bot.sendMessage(
    msg.chat.id,
    `🔥 Chào mừng tới AI Store

📦 Sản phẩm:
- Prompt viral Reel
- Tool AI
- ChatGPT Plus
- Claude
- Gemini

💰 Liên hệ admin để mua`
  );
});
