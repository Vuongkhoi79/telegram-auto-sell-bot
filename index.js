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

const mainMenu = {
  reply_markup: {
    keyboard: [
      ["🛍 Sản Phẩm", "💰 Nạp tiền"],
      ["👤 TÀI KHOẢN", "📦 Đơn hàng"],
      ["🌐 Đổi ngôn ngữ", "💬 Hỗ trợ"],
      ["❌ Đóng"]
    ],
    resize_keyboard: true
  }
};

bot.onText(/\/start|Menu/, (msg) => {
  bot.sendMessage(
    msg.chat.id,
    "✅ Ngôn ngữ của bạn đã được chuyển sang Tiếng Việt.",
    mainMenu
  );
});

bot.on("message", (msg) => {
  const chatId = msg.chat.id;
  const text = msg.text;

  if (text === "🛍 Sản Phẩm") {
    bot.sendMessage(chatId, `🛍 DANH SÁCH SẢN PHẨM

1. Prompt Viral Reel - 99k
2. Tool AI giá rẻ - 199k
3. ChatGPT Plus - liên hệ
4. Claude Pro - liên hệ
5. Gemini - liên hệ`);
  }

  if (text === "💰 Nạp tiền") {
    bot.sendMessage(chatId, `💰 NẠP TIỀN

Ngân hàng: MB Bank
STK: 123456789
Tên: AI STORE

Nội dung CK: ${chatId}

Sau khi chuyển khoản, gửi bill tại đây.`);
  }

  if (text === "👤 TÀI KHOẢN") {
    bot.sendMessage(chatId, `👤 TÀI KHOẢN

ID của bạn: ${chatId}
Số dư: 0đ
Trạng thái: Đang hoạt động`);
  }

  if (text === "📦 Đơn hàng") {
    bot.sendMessage(chatId, "📦 Bạn chưa có đơn hàng nào.");
  }

  if (text === "🌐 Đổi ngôn ngữ") {
    bot.sendMessage(chatId, "🌐 Hiện bot đang dùng Tiếng Việt.");
  }

  if (text === "💬 Hỗ trợ") {
    bot.sendMessage(chatId, "💬 Liên hệ admin: @yourusername");
  }

  if (text === "❌ Đóng") {
    bot.sendMessage(chatId, "❌ Đã đóng menu.", {
      reply_markup: {
        remove_keyboard: true
      }
    });
  }
});
