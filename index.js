const TelegramBot = require("node-telegram-bot-api");
const express = require("express");
const fs = require("fs");

const token = process.env.BOT_TOKEN;
const ADMIN_CHAT_ID = "8703946647";

const bot = new TelegramBot(token, { polling: true });
const app = express();

app.use(express.json());

const BANK_NAME = "ACB";
const BANK_CODE = "ACB";
const BANK_ACCOUNT = "157181829";
const BANK_OWNER = "PHAM NGUYEN VUONG KHOI";

const ORDERS_FILE = "orders.json";

const products = {
  chatgpt_bhf: {
    name: "CHATGPT PLUS 1 Tháng BHF",
    category: "CHATGPT",
    price: 210000,
    file: "kho_chatgpt_bhf.txt",
    desc:
      "🔝 Tài khoản cá nhân riêng tư, đổi pass thoải mái\n" +
      "❤️ Bảo hành 30 ngày - Hàng pay xịn\n" +
      "💔 KHÔNG BẢO HÀNH LOGIN CODEX\n" +
      "📦 Định dạng: mail|pass hoặc mail|pass|2fa",
  },
  chatgpt_gmail: {
    name: "CHATGPT PLUS 1 tháng BH48H GMAIL",
    category: "CHATGPT",
    price: 35000,
    file: "kho_chatgpt_gmail.txt",
    desc:
      "🔝 Tài khoản Gmail đăng nhập ChatGPT Plus\n" +
      "❤️ Bảo hành 48H\n" +
      "📦 Định dạng: mail|pass",
  },
  grok_super: {
    name: "GROK SUPER",
    category: "GROK SUPER",
    price: 120000,
    file: "kho_grok_super.txt",
    desc:
      "🔝 Tài khoản Grok Super\n" +
      "📦 Định dạng: mail|pass hoặc mail|pass|2fa",
  },
  veo3_5k: {
    name: "SLOT 5k FAM VEO3 ULTRA 1 tháng",
    category: "VEO3 ULTRA",
    price: 400000,
    file: "kho_veo3_5k.txt",
    desc:
      "🔝 Slot FAM VEO3 ULTRA 1 tháng\n" +
      "📦 Định dạng giao: mail|pass hoặc mail|pass|2fa",
  },
  veo3_12k5: {
    name: "SLOT 12k5 FAM VEO3 ULTRA 1 tháng",
    category: "VEO3 ULTRA",
    price: 500000,
    file: "kho_veo3_12k5.txt",
    desc:
      "🔝 Slot FAM VEO3 ULTRA 1 tháng\n" +
      "📦 Định dạng giao: mail|pass hoặc mail|pass|2fa",
  },
  veo3_25k: {
    name: "SLOT 25K FAM VEO3 ULTRA 1 tháng",
    category: "VEO3 ULTRA",
    price: 850000,
    file: "kho_veo3_25k.txt",
    desc:
      "🔝 Slot FAM VEO3 ULTRA 1 tháng\n" +
      "📦 Định dạng giao: mail|pass hoặc mail|pass|2fa",
  },
};

const categories = [
  "CHATGPT",
  "GROK SUPER",
  "VEO3 ULTRA",
];

const waitingManualQty = {};

function money(n) {
  return Number(n).toLocaleString("vi-VN") + "đ";
}

function readLines(file) {
  if (!fs.existsSync(file)) return [];

  return fs
    .readFileSync(file, "utf8")
    .split(/\r?\n/)
    .map((x) => x.trim())
    .filter(Boolean);
}

function writeLines(file, lines) {
  fs.writeFileSync(file, lines.join("\n") + (lines.length ? "\n" : ""), "utf8");
}

function stockCount(pid) {
  if (!products[pid]) return 0;
  return readLines(products[pid].file).length;
}

function takeStock(pid, qty) {
  const product = products[pid];
  if (!product) return null;

  const file = product.file;
  const lines = readLines(file);

  if (lines.length < qty) return null;

  const delivered = lines.slice(0, qty);
  const remain = lines.slice(qty);

  writeLines(file, remain);
  return delivered;
}

function loadOrders() {
  if (!fs.existsSync(ORDERS_FILE)) return {};

  try {
    return JSON.parse(fs.readFileSync(ORDERS_FILE, "utf8"));
  } catch (e) {
    return {};
  }
}

function saveOrders(orders) {
  fs.writeFileSync(ORDERS_FILE, JSON.stringify(orders, null, 2), "utf8");
}

function createOrder(user, pid, qty) {
  const orders = loadOrders();
  const orderId = "DH" + Date.now().toString().slice(-8) + String(user.id).slice(-3);
  const total = products[pid].price * qty;

  orders[orderId] = {
    orderId,
    userId: user.id,
    username: user.username || "",
    firstName: user.first_name || "",
    pid,
    productName: products[pid].name,
    category: products[pid].category,
    qty,
    total,
    status: "pending",
    createdAt: Date.now(),
  };

  saveOrders(orders);
  return orders[orderId];
}

function mainMenu() {
  return {
    reply_markup: {
      keyboard: [
        ["🛍 Sản Phẩm", "💰 Nạp tiền"],
        ["👤 TÀI KHOẢN", "📦 Đơn hàng"],
        ["🌐 Đổi ngôn ngữ", "💬 Hỗ trợ"],
        ["❌ Đóng"],
      ],
      resize_keyboard: true,
    },
  };
}

function categoryKeyboard() {
  return {
    reply_markup: {
      inline_keyboard: categories.map((cat) => [
        {
          text: cat,
          callback_data: "cat:" + cat,
        },
      ]),
    },
  };
}

function productKeyboard(category) {
  const rows = [];

  Object.keys(products).forEach((pid) => {
    const product = products[pid];

    if (product.category === category) {
      const stock = stockCount(pid);
      const status = stock > 0 ? `[${stock}]` : "[❌ Hết]";

      rows.push([
        {
          text: `${product.name} - ${money(product.price)} ${status}`,
          callback_data: stock > 0 ? `prod:${pid}` : "none",
        },
      ]);
    }
  });

  rows.push([{ text: "🔄 Làm mới", callback_data: "cat:" + category }]);
  rows.push([{ text: "🔙 Quay lại", callback_data: "back:categories" }]);

  return {
    reply_markup: {
      inline_keyboard: rows,
    },
  };
}

function qtyKeyboard(pid) {
  const stock = stockCount(pid);
  const nums = [1, 2, 3, 5, 10].filter((n) => n <= stock);
  const rows = [];

  if (nums.length) {
    rows.push(
      nums.map((n) => ({
        text: String(n),
        callback_data: `qty:${pid}:${n}`,
      }))
    );
  }

  rows.push([
    {
      text: "📝 Nhập số khác",
      callback_data: `manualqty:${pid}`,
    },
  ]);

  rows.push([
    {
      text: "🔙 Quay lại",
      callback_data: "cat:" + products[pid].category,
    },
    {
      text: "❌ Đóng",
      callback_data: "close",
    },
  ]);

  return {
    reply_markup: {
      inline_keyboard: rows,
    },
  };
}

function payKeyboard(orderId) {
  return {
    reply_markup: {
      inline_keyboard: [
        [
          {
            text: "🏦 ACB",
            callback_data: "bank:" + orderId,
          },
          {
            text: "💳 Trừ ví",
            callback_data: "wallet:" + orderId,
          },
        ],
        [
          {
            text: "🔙 Quay lại Sản Phẩm",
            callback_data: "back:categories",
          },
          {
            text: "🔙 Quay lại Menu",
            callback_data: "menu",
          },
        ],
      ],
    },
  };
}

function cancelKeyboard(orderId) {
  return {
    reply_markup: {
      inline_keyboard: [
        [
          {
            text: "❌ Hủy Giao Dịch",
            callback_data: "cancel:" + orderId,
          },
        ],
      ],
    },
  };
}

async function deliverOrder(orderId) {
  const orders = loadOrders();
  const order = orders[orderId];

  if (!order) return { ok: false, msg: "Không tìm thấy đơn." };
  if (order.status === "done") return { ok: false, msg: "Đơn đã giao rồi." };
  if (order.status === "cancel") return { ok: false, msg: "Đơn đã bị hủy." };

  const delivered = takeStock(order.pid, Number(order.qty));

  if (!delivered) return { ok: false, msg: "Kho không đủ hàng." };

  order.status = "done";
  order.delivered = delivered;
  order.doneAt = Date.now();

  orders[orderId] = order;
  saveOrders(orders);

  await bot.sendMessage(
    order.userId,
    `✅ THANH TOÁN THÀNH CÔNG

Mã đơn: ${orderId}
Sản phẩm: ${order.productName}
Số lượng: ${order.qty}

🎁 TÀI KHOẢN CỦA BẠN:
${delivered.join("\n")}

Định dạng:
mail|pass hoặc mail|pass|2fa

Cảm ơn bạn đã ủng hộ shop!`
  );

  await bot.sendMessage(
    ADMIN_CHAT_ID,
    `✅ ĐÃ TỰ ĐỘNG GIAO HÀNG

Mã đơn: ${orderId}
User: ${order.userId}
Sản phẩm: ${order.productName}
Số lượng: ${order.qty}`
  );

  return { ok: true };
}

function showOrders(chatId, userId) {
  const orders = loadOrders();
  const mine = Object.values(orders).filter(
    (order) => String(order.userId) === String(userId)
  );

  if (!mine.length) {
    bot.sendMessage(chatId, "📦 Bạn chưa có đơn hàng nào.");
    return;
  }

  let text = "📦 ĐƠN HÀNG CỦA BẠN\n\n";

  mine.slice(-5).forEach((order) => {
    text += `${order.orderId} - ${order.productName} - ${money(order.total)} - ${order.status}\n`;
  });

  bot.sendMessage(chatId, text);
}

app.get("/", (req, res) => {
  res.send("Telegram Auto Sell Bot Running");
});

bot.onText(/\/start|Menu/, (msg) => {
  bot.sendMessage(
    msg.chat.id,
    `👋 Chào mừng ${msg.from.first_name || ""} đến với @Phuong_AI_bot

👋 Chào mừng đến với Tài Khoản AI Giá Rẻ!

🛍 Mua gói dịch vụ số — thanh toán nhanh — giao hàng tự động.

⚡ Lệnh nhanh
• /products — Danh sách sản phẩm
• /menu — Menu chính
• /topup — Nạp tiền ví VNĐ
• /orders — Đơn hàng của bạn
• /support — Liên hệ hỗ trợ
• /change_language — Thay đổi ngôn ngữ
• /me — Thông tin tài khoản`,
    mainMenu()
  );
});

bot.onText(/\/menu/, (msg) => {
  bot.sendMessage(msg.chat.id, "Menu chính:", mainMenu());
});

bot.onText(/\/products/, (msg) => {
  bot.sendMessage(
    msg.chat.id,
    "🛍 Chọn danh mục để xem gói 👇",
    categoryKeyboard()
  );
});

bot.onText(/\/orders/, (msg) => {
  showOrders(msg.chat.id, msg.from.id);
});

bot.onText(/\/support/, (msg) => {
  bot.sendMessage(msg.chat.id, "💬 Hỗ trợ: nhắn trực tiếp admin.");
});

bot.onText(/\/change_language/, (msg) => {
  bot.sendMessage(msg.chat.id, "🌐 Hiện bot đang dùng Tiếng Việt.");
});

bot.onText(/\/me/, (msg) => {
  bot.sendMessage(
    msg.chat.id,
    `👤 Tài khoản
ID: ${msg.from.id}
Tên: ${msg.from.first_name || ""}

💰 Số dư: 0đ`
  );
});

bot.onText(/\/topup/, (msg) => {
  const qrUrl =
    `https://img.vietqr.io/image/${BANK_CODE}-${BANK_ACCOUNT}-compact2.png?addInfo=${msg.from.id}&accountName=${encodeURIComponent(BANK_OWNER)}`;

  bot.sendPhoto(msg.chat.id, qrUrl, {
    caption: `💰 NẠP TIỀN VÍ

🏦 Ngân hàng: ${BANK_NAME}
👤 Chủ tài khoản: ${BANK_OWNER}
💳 STK: ${BANK_ACCOUNT}

🧾 Nội dung CK:
${msg.from.id}

Sau khi chuyển khoản, hệ thống sẽ ghi nhận qua SePay.`,
  });
});

bot.onText(/\/done (.+)/, async (msg, match) => {
  if (String(msg.from.id) !== ADMIN_CHAT_ID) return;

  const orderId = match[1].trim();
  const result = await deliverOrder(orderId);

  bot.sendMessage(
    msg.chat.id,
    result.ok ? "✅ Đã giao hàng." : "❌ " + result.msg
  );
});

bot.onText(/\/stock/, (msg) => {
  if (String(msg.from.id) !== ADMIN_CHAT_ID) return;

  let text = "📦 TỒN KHO\n\n";

  Object.keys(products).forEach((pid) => {
    text += `${pid}: ${stockCount(pid)} - ${products[pid].name}\n`;
  });

  bot.sendMessage(msg.chat.id, text);
});

bot.onText(/\/addstock ([\s\S]+)/, (msg, match) => {
  if (String(msg.from.id) !== ADMIN_CHAT_ID) return;

  const lines = match[1]
    .split(/\r?\n/)
    .map((x) => x.trim())
    .filter(Boolean);

  const pid = lines[0];

  if (!products[pid]) {
    bot.sendMessage(
      msg.chat.id,
      "❌ Sai product_id. Gõ /stock để xem mã sản phẩm."
    );
    return;
  }

  const items = lines.slice(1).filter((line) => {
    const parts = line.split("|");
    return (parts.length === 2 || parts.length === 3) &&
      parts.every((x) => x.trim());
  });

  if (!items.length) {
    bot.sendMessage(
      msg.chat.id,
      "❌ Không có dòng đúng format mail|pass hoặc mail|pass|2fa."
    );
    return;
  }

  const old = readLines(products[pid].file);
  writeLines(products[pid].file, old.concat(items));

  bot.sendMessage(
    msg.chat.id,
    `✅ Đã thêm ${items.length} dòng vào kho ${pid}.
Tồn kho mới: ${stockCount(pid)}`
  );
});

bot.on("message", (msg) => {
  const chatId = msg.chat.id;
  const text = msg.text;

  if (!text) return;

  if (waitingManualQty[msg.from.id]) {
    const pid = waitingManualQty[msg.from.id];
    const qty = Number(text);

    if (!Number.isInteger(qty) || qty <= 0) {
      bot.sendMessage(chatId, "❌ Số lượng không hợp lệ.");
      return;
    }

    if (qty > stockCount(pid)) {
      bot.sendMessage(chatId, "❌ Kho không đủ số lượng.");
      return;
    }

    delete waitingManualQty[msg.from.id];

    const order = createOrder(msg.from, pid, qty);
    const p = products[pid];

    bot.sendMessage(
      chatId,
      `🧾 Chi tiết đơn

📦 Sản phẩm: ${p.category}
🛒 Gói: ${p.name}
🔢 Số lượng: ${qty}
💵 Đơn giá: ${money(p.price)}

🛒 Tổng thanh toán: ${money(order.total)}
💰 Số dư: 0đ

Chọn cách thanh toán`,
      payKeyboard(order.orderId)
    );

    return;
  }

  if (text === "🛍 Sản Phẩm") {
    bot.sendMessage(
      chatId,
      "🛍 Chọn danh mục để xem gói 👇",
      categoryKeyboard()
    );
  }

  if (text === "💰 Nạp tiền") {
    const qrUrl =
      `https://img.vietqr.io/image/${BANK_CODE}-${BANK_ACCOUNT}-compact2.png?addInfo=${msg.from.id}&accountName=${encodeURIComponent(BANK_OWNER)}`;

    bot.sendPhoto(chatId, qrUrl, {
      caption: `💰 NẠP TIỀN VÍ

🏦 Ngân hàng: ${BANK_NAME}
👤 Chủ tài khoản: ${BANK_OWNER}
💳 STK: ${BANK_ACCOUNT}

🧾 Nội dung CK:
${msg.from.id}

Sau khi chuyển khoản, hệ thống sẽ ghi nhận qua SePay.`,
    });
  }

  if (text === "📦 Đơn hàng") {
    showOrders(chatId, msg.from.id);
  }

  if (text === "👤 TÀI KHOẢN") {
    bot.sendMessage(
      chatId,
      `👤 Tài khoản
ID: ${msg.from.id}
Tên: ${msg.from.first_name || ""}

💰 Số dư: 0đ`
    );
  }

  if (text === "🌐 Đổi ngôn ngữ") {
    bot.sendMessage(chatId, "🌐 Hiện bot đang dùng Tiếng Việt.");
  }

  if (text === "💬 Hỗ trợ") {
    bot.sendMessage(chatId, "💬 Hỗ trợ: nhắn trực tiếp admin.");
  }

  if (text === "❌ Đóng") {
    bot.sendMessage(chatId, "❌ Đã đóng menu.", {
      reply_markup: {
        remove_keyboard: true,
      },
    });
  }
});

bot.on("callback_query", async (query) => {
  const chatId = query.message.chat.id;
  const msgId = query.message.message_id;
  const data = query.data;

  if (data === "none") {
    bot.answerCallbackQuery(query.id, {
      text: "Sản phẩm đã hết hàng.",
    });
    return;
  }

  if (data === "close") {
    bot.deleteMessage(chatId, msgId).catch(() => {});
    return;
  }

  if (data === "menu") {
    bot.sendMessage(chatId, "Menu chính:", mainMenu());
    return;
  }

  if (data === "back:categories") {
    bot.editMessageText("🛍 Chọn danh mục để xem gói 👇", {
      chat_id: chatId,
      message_id: msgId,
      ...categoryKeyboard(),
    });
    return;
  }

  if (data.startsWith("cat:")) {
    const category = data.split(":")[1];

    bot.editMessageText(`${category}

Chọn gói 👇`, {
      chat_id: chatId,
      message_id: msgId,
      ...productKeyboard(category),
    });

    return;
  }

  if (data.startsWith("prod:")) {
    const pid = data.split(":")[1];
    const p = products[pid];
    const stock = stockCount(pid);

    bot.editMessageText(
      `✅ ${p.category} ${p.name}

${p.desc}

━━━━━━━━━━━━━━
📊 Còn lại: ${stock}
💵 Giá: ${money(p.price)} / tài khoản

💡 Nhấn các nút bên dưới.`,
      {
        chat_id: chatId,
        message_id: msgId,
        ...qtyKeyboard(pid),
      }
    );

    return;
  }

  if (data.startsWith("manualqty:")) {
    const pid = data.split(":")[1];
    waitingManualQty[query.from.id] = pid;

    bot.sendMessage(
      chatId,
      `📝 Nhập số lượng muốn mua cho gói:
${products[pid].name}`
    );

    return;
  }

  if (data.startsWith("qty:")) {
    const [, pid, qtyText] = data.split(":");
    const qty = Number(qtyText);
    const p = products[pid];

    if (stockCount(pid) < qty) {
      bot.answerCallbackQuery(query.id, {
        text: "Kho không đủ hàng.",
      });
      return;
    }

    const order = createOrder(query.from, pid, qty);

    bot.editMessageText(
      `Chọn cách thanh toán

🧾 Chi tiết đơn
📦 Sản phẩm: ${p.category}
🛒 Gói: ${p.name}
🔢 Số lượng: ${qty}
💵 Đơn giá: ${money(p.price)}

🛒 Tổng thanh toán: ${money(order.total)}
💰 Số dư: 0đ

• Ví: trừ số dư (nhanh, không cần CK).`,
      {
        chat_id: chatId,
        message_id: msgId,
        ...payKeyboard(order.orderId),
      }
    );

    return;
  }

  if (data.startsWith("wallet:")) {
    bot.answerCallbackQuery(query.id, {
      text: "Ví chưa đủ số dư. Vui lòng chọn ACB để chuyển khoản.",
      show_alert: true,
    });
    return;
  }

  if (data.startsWith("cancel:")) {
    const orderId = data.split(":")[1];
    const orders = loadOrders();

    if (orders[orderId]) {
      orders[orderId].status = "cancel";
      saveOrders(orders);
    }

    bot.editMessageText("❌ Giao dịch đã bị hủy.", {
      chat_id: chatId,
      message_id: msgId,
    });

    return;
  }

  if (data.startsWith("bank:")) {
    const orderId = data.split(":")[1];
    const orders = loadOrders();
    const order = orders[orderId];

    if (!order) {
      bot.sendMessage(chatId, "❌ Không tìm thấy đơn.");
      return;
    }

    const qrUrl =
      `https://img.vietqr.io/image/${BANK_CODE}-${BANK_ACCOUNT}-compact2.png?amount=${order.total}&addInfo=${orderId}&accountName=${encodeURIComponent(BANK_OWNER)}`;

    bot.sendPhoto(chatId, qrUrl, {
      caption: `💳 MÃ QR THANH TOÁN

💰 Số tiền: ${money(order.total)}

🏦 Ngân hàng: ${BANK_NAME}
👤 Chủ tài khoản: ${BANK_OWNER}
💳 STK: ${BANK_ACCOUNT}

🧾 Mã giao dịch: ${orderId}

📦 Sản phẩm:
${order.productName}

🔢 Số lượng:
${order.qty}

⏰ Mã order có hiệu lực trong 5 phút

⚡ Sau khi chuyển khoản đúng nội dung, hệ thống sẽ tự động giao hàng.`,
      ...cancelKeyboard(orderId),
    });

    return;
  }
});

app.post("/webhook", async (req, res) => {
  console.log("Webhook received:", req.body);

  const amount = Number(
    req.body.transferAmount ||
      req.body.amount ||
      req.body.transfer_amount ||
      0
  );

  const content = String(
    req.body.content ||
      req.body.description ||
      req.body.transferContent ||
      ""
  );

  const orders = loadOrders();

  const order = Object.values(orders).find(
    (o) =>
      o.status === "pending" &&
      content.includes(o.orderId) &&
      Number(amount) >= Number(o.total)
  );

  await bot.sendMessage(
    ADMIN_CHAT_ID,
    `💸 SEPAY WEBHOOK

Số tiền: ${money(amount)}
Nội dung: ${content}

${order ? "✅ Khớp đơn: " + order.orderId : "⚠️ Chưa khớp đơn nào"}`
  );

  if (order) {
    await deliverOrder(order.orderId);
  }

  res.sendStatus(200);
});

const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
  console.log("Server running on port " + PORT);
});
