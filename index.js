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
    "🔥 Chào mừng tới AI Store",
    {
      reply_markup: {
        keyboard: [
          ["📦 Xem sản phẩm"],
          ["💰 Thanh toán"],
          ["👤 Liên hệ admin"]
        ],
        resize_keyboard: true
      }
    }
  );
});

bot.on("message", (msg) => {

  if (msg.text === "📦 Xem sản phẩm") {
    bot.sendMessage(
      msg.chat.id,
      `📦 Danh sách sản phẩm:

- Prompt Viral Reel
- Tool AI
- ChatGPT Plus
- Claude Pro
- Gemini`
    );
  }

  if (msg.text === "💰 Thanh toán") {
    bot.sendMessage(
      msg.chat.id,
      `💳 Thông tin thanh toán

MB Bank
STK: 123456789
Tên: AI STORE

⚡ Gửi bill sau khi chuyển khoản`
    );
  }

  if (msg.text === "👤 Liên hệ admin") {
    bot.sendMessage(
      msg.chat.id,
      "📩 Telegram admin: @yourusername"
    );
  }

});
