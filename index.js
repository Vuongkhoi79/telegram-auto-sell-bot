const TelegramBot = require('node-telegram-bot-api');
const express = require('express');

const token = process.env.BOT_TOKEN;

// ID Telegram admin
const ADMIN_CHAT_ID = "8703946647";

const bot = new TelegramBot(token, { polling: true });
const app = express();

app.use(express.json());

app.get("/", (req, res) => {
  res.send("Telegram Bot Running");
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

  // SẢN PHẨM
  if (text === "🛍 Sản Phẩm") {
    bot.sendMessage(chatId, `🛍 DANH SÁCH SẢN PHẨM

1. Prompt Viral Reel - 99k
2. Tool AI giá rẻ - 199k
3. ChatGPT Plus - liên hệ
4. Claude Pro - liên hệ
5. Gemini - liên hệ`);
  }

  // NẠP TIỀN
  if (text === "💰 Nạp tiền") {

    bot.sendPhoto(
      chatId,
      "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=ACB-157181829",
      {
        caption: `💰 NẠP TIỀN

🏦 Ngân hàng: ACB
💳 STK: 157181829
👤 Tên: PHAM NGUYEN VUONG KHOI

📝 Nội dung CK:
${chatId}

⚡ Sau khi chuyển khoản hệ thống sẽ tự kiểm tra.`
      }
    );
  }

  // TÀI KHOẢN
  if (text === "👤 TÀI KHOẢN") {
    bot.sendMessage(chatId, `👤 TÀI KHOẢN

🆔 ID của bạn: ${chatId}

💰 Số dư: 0đ

✅ Trạng thái: Đang hoạt động

🏦 Tài khoản nhận tiền:
Ngân hàng: ACB
STK: 157181829
Chủ TK: PHAM NGUYEN VUONG KHOI`);
  }

  // ĐƠN HÀNG
  if (text === "📦 Đơn hàng") {
    bot.sendMessage(chatId, "📦 Bạn chưa có đơn hàng nào.");
  }

  // ĐỔI NGÔN NGỮ
  if (text === "🌐 Đổi ngôn ngữ") {
    bot.sendMessage(chatId, "🌐 Hiện bot đang dùng Tiếng Việt.");
  }

  // HỖ TRỢ
  if (text === "💬 Hỗ trợ") {
    bot.sendMessage(chatId, "💬 Telegram admin: @yourusername");
  }

  // ĐÓNG MENU
  if (text === "❌ Đóng") {
    bot.sendMessage(chatId, "❌ Đã đóng menu.", {
      reply_markup: {
        remove_keyboard: true
      }
    });
  }

});

// WEBHOOK TỪ SEPAY
app.post("/webhook", (req, res) => {

  console.log("Webhook received:", req.body);

  const amount =
    req.body.transferAmount ||
    req.body.amount ||
    "Không rõ";

  const content =
    req.body.content ||
    req.body.description ||
    "Không rõ";

  bot.sendMessage(
    ADMIN_CHAT_ID,
    `💸 CÓ THANH TOÁN MỚI!

💰 Số tiền: ${amount}

📝 Nội dung:
${content}

✅ SePay đã gửi webhook thành công.`
  );

  res.sendStatus(200);

});

const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
  console.log("Server running on port " + PORT);
});
