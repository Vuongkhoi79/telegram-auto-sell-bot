# Telegram Auto Sell Bot - Tài Liệu Vận Hành Chính Thức

Tài liệu này là quy trình vận hành chính thức cho việc nhập kho và kiểm tra tồn kho của Telegram Auto Sell Bot.

Mục tiêu: một nhân viên mới có thể nhập hàng trong vòng 5 phút mà không cần biết SQLite, Python nội bộ, hay SQL.

---

## 1. Triết Lý Hệ Thống

Kể từ phiên bản hiện tại:

**Excel là Source of Truth duy nhất cho inventory.**

SQLite chỉ là database phục vụ bán hàng runtime. SQLite không phải nơi để nhập kho thủ công.

```text
[WINDOWS] Excel chuẩn
      |
      | [WINDOWS] git push
      v
[RENDER] Deploy code + file Excel mới
      |
      | [RENDER] run reset_and_import_inventory.py
      v
[RENDER] /var/data/store.db
      |
      | Bot đọc stock từ inventory_items status='available'
      v
[TELEGRAM] Menu sản phẩm
```

Nguyên tắc bắt buộc:

| Việc | Nguyên tắc |
|---|---|
| Thêm hàng | Sửa Excel, commit, deploy, chạy script import |
| Sửa stock | Sửa Excel, chạy lại script import |
| SQLite | Chỉ để bot bán hàng, không sửa tay |
| `inventory.json` | Không dùng cho stock, product detail, giao hàng |
| `CATALOG-*` | Chỉ là dòng catalog/display, không cạnh tranh stock với product thật |

Không bao giờ:

- Sửa trực tiếp `inventory_items` bằng SQLite.
- Enable/disable item bằng SQL.
- Update status bằng tay.
- Xóa inventory bằng tay.
- Lấy `inventory.json` làm fallback.

---

## 2. Cấu Trúc File Import

Hệ thống dùng 2 file Excel chuẩn trong thư mục `imports/`.

| File | Dùng cho | Ghi chú |
|---|---|---|
| `imports/import_CHATGPT_ONLY_READY1.xlsx` | CHATGPT | File riêng cho CHATGPT |
| `imports/import_inventory_ONE_SHEET_PER_PRODUCT.xlsx` | GEMINI, CAPCUT, sản phẩm khác sau này | Mỗi sheet nên đại diện một product |

Đường dẫn chuẩn trong repo hiện tại:

| Loại | Đường dẫn | Dùng ở đâu |
|---|---|---|
| Excel CHATGPT | `imports/import_CHATGPT_ONLY_READY1.xlsx` | `[WINDOWS]` sửa file, `[RENDER]` script đọc file |
| Excel nhiều sản phẩm | `imports/import_inventory_ONE_SHEET_PER_PRODUCT.xlsx` | `[WINDOWS]` sửa file, `[RENDER]` script đọc file |
| Script reset/import | `scripts/reset_and_import_inventory.py` | `[RENDER]` Shell |

### 2.1. Các Cột Excel

Script import đọc các cột sau. Nên giữ đúng tên cột như template.

| Cột | Bắt buộc | Ý nghĩa | Ví dụ |
|---|---:|---|---|
| `product_code` | Có | Mã sản phẩm canonical | `CHATGPT`, `GEMINI`, `CAPCUT` |
| `category` | Có | Nhóm/category hiển thị | `account` |
| `product_name` | Có | Tên sản phẩm | `GEMINI AI` |
| `account_type` | Có | Loại account/gói | `private`, `personal` |
| `duration` | Có | Thời hạn | `30D`, `1M` |
| `price_vnd` | Có | Giá bán VND | `70000` |
| `warranty_days` | Có | Số ngày bảo hành | `7` |
| `credential_text` | Có nếu không dùng email/password | Credential đầy đủ | `email@example.com\|password\|2fa` |
| `note` | Có | Ghi chú nội bộ | `READY` |
| `active` | Có | Có import/hiển thị hay không | `1` |
| `category_key` | Tùy chọn | Key category | `GEMINI` |
| `menu_order` | Tùy chọn | Thứ tự menu | `10` |
| `show_in_menu` | Tùy chọn | Có hiện menu không | `1` |
| `product_group` | Tùy chọn | Nhóm product | `account` |
| `description` | Tùy chọn | Mô tả | `Gemini AI Pro 1 tháng` |
| `package_name` | Tùy chọn | Tên gói | `Gemini AI Pro` |
| `plan_name` | Tùy chọn | Tên plan | `1 month` |
| `email` | Tùy chọn | Email account | `user@example.com` |
| `password` | Tùy chọn | Mật khẩu | `Pass123` |
| `2fa` | Tùy chọn | Secret 2FA | `JBSWY3DPEHPK3PXP` |
| `recovery_email` | Tùy chọn | Email khôi phục | `recovery@example.com` |

### 2.2. Định Dạng Credential

Có 2 cách nhập credential.

**Cách 1: dùng `credential_text`**

```text
email@example.com|password
email@example.com|password|2fa
email@example.com|password|2fa|recovery_email
```

**Cách 2: tách cột**

| email | password | 2FA | recovery_email |
|---|---|---|---|
| `email@example.com` | `Pass123` | `JBSWY3DPEHPK3PXP` | `recovery@example.com` |

Nếu dùng cách tách cột, script sẽ tự ghép thành credential nội bộ.

### 2.3. Chuẩn Hóa `product_code`

Script tự chuẩn hóa product code.

| Excel nhập | Hệ thống hiểu là |
|---|---|
| `CHATGPT` | `CHATGPT` |
| `CHAT GPT` | `CHATGPT` |
| `Chat GPT` | `CHATGPT` |
| `chat gpt` | `CHATGPT` |
| Sheet name `CHAT GPT` | `CHATGPT` |
| `GEMINI AI` | `GEMINI` |
| `CAPCUT PRO` | `CAPCUT` |

### 2.4. Rule Theo Sản Phẩm

| Sản phẩm | Rule Excel -> `inventory_items` | Ví dụ |
|---|---|---|
| CHATGPT | 1 dòng Excel = 1 inventory item bán được | 5 dòng = 5 item |
| CAPCUT | 1 dòng Excel = 1 inventory item bán được | 3 dòng = 3 item |
| GEMINI | 1 email = tối đa 2 slot bán được | 4 email x 2 slot = 8 item |

#### Ví Dụ GEMINI

GEMINI có rule riêng: **1 email GEMINI có 2 người dùng**, nên 1 email có thể tạo 2 slot bán được.

Excel:

| product_code | email | password |
|---|---|---|
| GEMINI | `gemini01@example.com` | `Pass123` |
| GEMINI | `gemini01@example.com` | `Pass123` |
| GEMINI | `gemini02@example.com` | `Pass456` |
| GEMINI | `gemini02@example.com` | `Pass456` |

Kết quả đúng:

```text
GEMINI rows in Excel: 4
GEMINI unique credentials/email: 2
GEMINI sellable items: 4
GEMINI available in DB: 4
```

Nếu một email GEMINI xuất hiện quá 2 dòng, phần dư sẽ bị skip và script in warning.

---

## 3. Quy Trình Nhập Hàng Mới

### Tổng Quan

```text
[WINDOWS] Sửa 2 file Excel
      |
      v
[WINDOWS] git add / commit / push
      |
      v
[RENDER] Deploy bản mới
      |
      v
[RENDER SHELL] Chạy reset_and_import_inventory.py
      |
      v
[TELEGRAM] /start -> Sản phẩm -> kiểm tra stock
```

### Quy Trình Nhập Hàng Chuẩn

| Bước | Chạy ở đâu | Lệnh hoặc thao tác | Kết quả mong đợi |
|---:|---|---|---|
| 1 | `[WINDOWS]` | Mở `imports/import_CHATGPT_ONLY_READY1.xlsx` và/hoặc `imports/import_inventory_ONE_SHEET_PER_PRODUCT.xlsx` | File Excel có dữ liệu kho mới, đúng `product_code`, đúng credential |
| 2 | `[WINDOWS]` | Lưu file Excel | File trong `imports/` đã được cập nhật |
| 3 | `[WINDOWS]` | `git add imports/` | Git stage các file Excel cần deploy |
| 4 | `[WINDOWS]` | `git commit -m "Update inventory"` | Có commit mới chứa file Excel |
| 5 | `[WINDOWS]` | `git push` | GitHub có commit inventory mới |
| 6 | `[RENDER]` | Auto Deploy hoặc Manual Deploy latest commit | Render chạy đúng commit mới |
| 7 | `[RENDER]` | Chạy `python scripts/reset_and_import_inventory.py ...` trong Render Shell | `/var/data/store.db` được reset/import lại từ Excel |
| 8 | `[TELEGRAM]` | `/start` -> `🎁 Sản phẩm` | Menu hiển thị đúng stock |
| 9 | `[TELEGRAM]` + `[RENDER]` | Mua thử 1 đơn và kiểm tra webhook/log | Thanh toán được nhận, giao hàng thành công, stock giảm đúng |

### Bước 1 - WINDOWS: Chỉnh Sửa Excel

Chạy ở đâu: **[WINDOWS]**

Mở và chỉnh sửa đúng 2 file:

```text
imports/import_CHATGPT_ONLY_READY1.xlsx
imports/import_inventory_ONE_SHEET_PER_PRODUCT.xlsx
```

Checklist:

- CHATGPT chỉ sửa trong `imports/import_CHATGPT_ONLY_READY1.xlsx`.
- GEMINI/CAPCUT sửa trong `imports/import_inventory_ONE_SHEET_PER_PRODUCT.xlsx`.
- Không sửa SQLite.
- Không sửa `inventory.json`.
- Không update status bằng SQL.

### Bước 2 - WINDOWS: Commit Và Push Excel

Chạy ở đâu: **[WINDOWS] PowerShell / Git Bash**

```bash
# [WINDOWS]
git add imports/
git commit -m "Update inventory"
git push
```

Nếu có thay đổi file script/tài liệu cùng lúc, chỉ add đúng file cần thiết. Việc nhập kho thông thường chỉ cần `git add imports/`.

### Bước 3 - RENDER: Deploy

Chạy ở đâu: **[RENDER] Dashboard**

Nếu Render đang bật Auto Deploy:

```text
[WINDOWS] git push xong -> [RENDER] Render tự deploy
```

Nếu cần Manual Deploy:

```text
[RENDER] Render Dashboard -> Service của bot -> Manual Deploy -> Deploy latest commit
```

Đợi deploy thành công trước khi chạy import.

### Bước 4 - RENDER SHELL: Reset Và Import Inventory

Chạy ở đâu: **[RENDER] Shell**

```bash
# [RENDER]
python scripts/reset_and_import_inventory.py \
  --database /var/data/store.db \
  --chatgpt-file imports/import_CHATGPT_ONLY_READY1.xlsx \
  --multi-file imports/import_inventory_ONE_SHEET_PER_PRODUCT.xlsx
```

Lệnh này sẽ:

- Đọc 2 file Excel trước.
- Reset chỉ 3 product: `CHATGPT`, `GEMINI`, `CAPCUT`.
- Chỉ xóa inventory có status `available`, `reserved`, `disabled`.
- Không xóa item `delivered`.
- Không xóa orders/payments/history.
- Import lại đúng theo Excel.
- Validate stock cuối.

---

## 4. Kiểm Tra Sau Import

### 4.1. Kiểm Tra Output Trong Render Shell

Chạy ở đâu: **[RENDER] Shell**

Sau khi chạy script, phải xem các block:

```text
EXPECTED FROM EXCEL:
CHATGPT rows: X
CHATGPT unique credentials/email: X
CHATGPT sellable items: X
GEMINI rows: X
GEMINI unique credentials/email: X
GEMINI sellable items: X
CAPCUT rows: X
CAPCUT unique credentials/email: X
CAPCUT sellable items: X

ACTUAL IN DATABASE:
CHATGPT available: X
GEMINI available: X
CAPCUT available: X
```

Điều kiện thành công:

```text
CHATGPT available = CHATGPT sellable items
GEMINI available = GEMINI sellable items
CAPCUT available = CAPCUT sellable items
```

Ví dụ kết quả đúng:

```text
ACTUAL IN DATABASE:
CHATGPT available: 5
GEMINI available: 8
CAPCUT available: 3
```

### 4.2. Kiểm Tra Trên Telegram

Chạy ở đâu: **[TELEGRAM]**

1. Gửi:

```text
/start
```

2. Bấm:

```text
🎁 Sản phẩm
```

3. Menu phải hiển thị đúng stock, ví dụ:

```text
CHATGPT (5)
GEMINI AI (8)
CAPCUT PRO (3)
```

Nếu database đã đúng thì Telegram menu phải đúng sau khi bot đang chạy đúng commit mới.

### 4.3. Kiểm Tra Sau Nhập Hàng Bằng Một Đơn Test

Sau mỗi lần nhập kho production, nên kiểm tra end-to-end bằng 1 đơn thật hoặc 1 đơn test có kiểm soát.

| Hạng mục | Chạy ở đâu | Cách kiểm tra | Kết quả mong đợi |
|---|---|---|---|
| Telegram hiển thị đúng stock | `[TELEGRAM]` | `/start` -> `🎁 Sản phẩm` | CHATGPT/GEMINI/CAPCUT hiển thị đúng số `available` |
| Mua thử 1 đơn | `[TELEGRAM]` | Chọn sản phẩm -> chọn gói -> chọn số lượng -> tạo QR | Bot tạo đơn và QR thanh toán |
| Webhook nhận thanh toán | `[RENDER] Logs` | Tìm log `[SEPAY] received transaction` | Webhook nhận giao dịch |
| Match đúng đơn | `[RENDER] Logs` | Tìm log `[SEPAY] matched order_id` | Giao dịch match đúng `order_id` |
| Giao hàng thành công | `[RENDER] Logs` và `[TELEGRAM]` | Tìm `DELIVERY SENT SUCCESS`, kiểm tra tin nhắn nhận account | Khách nhận credential sau khi thanh toán |
| Stock giảm đúng | `[TELEGRAM]` | Quay lại `🎁 Sản phẩm` | Product vừa mua giảm đúng số lượng đã bán |

Log cần thấy theo thứ tự:

```text
[SEPAY] received transaction
[SEPAY] parsed order code
[SEPAY] matched order_id
[SEPAY] marked paid
[SEPAY] delivery start
DELIVERY START
DELIVERY SELECT INVENTORY
DELIVERY SENT SUCCESS
[SEPAY] delivery result
```

Nếu một bước không đạt, xem mục [7. Troubleshooting](#7-troubleshooting) và [8. Khôi Phục Khi Có Lỗi](#8-khôi-phục-khi-có-lỗi). Không sửa SQLite để chữa nhanh.

---

## 5. Không Được Làm

Đây là danh sách cấm trong vận hành.

| Không được làm | Lý do |
|---|---|
| Sửa SQLite trực tiếp | Excel mới là Source of Truth |
| `UPDATE inventory_items SET status=...` | Phá quy trình reset/import |
| Enable item bằng SQL | Dễ sai stock và sai lịch sử |
| Disable item bằng SQL | Dễ lệch giữa Excel và DB |
| Delete inventory bằng tay | Có nguy cơ mất liên kết order/history |
| Sửa `orders` / `payments` bằng tay | Có nguy cơ sai vòng đời đơn hàng |
| Dùng `inventory.json` | Bot hiện tại SQLite-first tuyệt đối |
| Fallback `inventory.json` | Có thể giao sai hàng/stock ảo |
| Lấy stock từ Excel khi render menu | Menu phải đọc DB runtime |
| Lấy stock từ `products.stock` hoặc cache cũ | Stock thật nằm ở `inventory_items.status='available'` |

Các lệnh SQL sau là ví dụ cấm. **Không chạy ở WINDOWS, không chạy ở RENDER, không chạy ở bất kỳ đâu trong vận hành production.**

```sql
UPDATE inventory_items SET status='available';
UPDATE inventory_items SET status='disabled';
DELETE FROM inventory_items;
UPDATE products SET stock=...;
```

---

## 6. Khi Nhập Thêm Hàng

Quy trình lặp lại mỗi lần nhập hàng:

| Bước | Chạy ở đâu | Thao tác |
|---:|---|---|
| 1 | `[WINDOWS]` | Sửa Excel |
| 2 | `[WINDOWS]` | `git add imports/` |
| 3 | `[WINDOWS]` | `git commit -m "Update inventory"` |
| 4 | `[WINDOWS]` | `git push` |
| 5 | `[RENDER]` | Deploy commit mới |
| 6 | `[RENDER]` | Chạy `reset_and_import_inventory.py` |
| 7 | `[TELEGRAM]` | `/start` -> `🎁 Sản phẩm` -> kiểm tra stock |

Không cần làm gì khác.

Không cần:

- Sửa DB.
- Mở SQLite.
- Chạy SQL.
- Enable/disable item.
- Sửa `inventory.json`.
- Sửa code.

---

## 7. Troubleshooting

### 7.1. Telegram Hiện Sai Stock

Ví dụ:

```text
Database đúng:
CHATGPT available: 5
GEMINI available: 8
CAPCUT available: 3

Telegram sai:
CHATGPT (10)
GEMINI AI (2)
CAPCUT PRO (0)
```

Kiểm tra theo thứ tự sau.

#### 1. RENDER: Deploy Đúng Commit Chưa?

Chạy ở đâu: **[RENDER] Dashboard**

Kiểm tra service đang deploy commit mới nhất.

Nếu chưa:

```text
[RENDER] Manual Deploy -> Deploy latest commit
```

#### 2. RENDER SHELL: Đã Chạy Script Import Chưa?

Chạy ở đâu: **[RENDER] Shell**

```bash
# [RENDER]
python scripts/reset_and_import_inventory.py \
  --database /var/data/store.db \
  --chatgpt-file imports/import_CHATGPT_ONLY_READY1.xlsx \
  --multi-file imports/import_inventory_ONE_SHEET_PER_PRODUCT.xlsx
```

Đọc block:

```text
ACTUAL IN DATABASE:
CHATGPT available: X
GEMINI available: X
CAPCUT available: X
```

#### 3. RENDER LOGS: Kiểm Tra `MENU BUILD`

Chạy ở đâu: **[RENDER] Logs**

Tìm log:

```text
MENU BUILD
CHATGPT = X
GEMINI = X
CAPCUT = X
```

Nếu `MENU BUILD` đúng thì Telegram sẽ đúng.

Nếu `MENU BUILD` sai, kiểm tra tiếp log DB path và rows:

```text
DB PATH:
/var/data/store.db

ROWS:
('CHATGPT', 5)
('GEMINI', 8)
('CAPCUT', 3)
```

Nếu DB path khác `/var/data/store.db`, bot đang đọc sai database.

### 7.2. Script Báo Validation Fail

Chạy ở đâu: **[RENDER] Shell**

Đọc các dòng:

```text
EXPECTED FROM EXCEL:
...

IMPORT RESULT:
...

ACTUAL IN DATABASE:
...
```

Nguyên nhân thường gặp:

| Dấu hiệu | Nguyên nhân | Cách xử lý |
|---|---|---|
| CHATGPT expected 5 actual 4 | 1 dòng Excel invalid/duplicate trong batch | Kiểm tra debug row script in ra |
| GEMINI actual chỉ bằng unique email | Excel chỉ có 1 dòng/email hoặc rule slot sai | Mỗi email GEMINI cần 2 dòng nếu muốn 2 slot |
| CAPCUT actual 0 | Sai `product_code` hoặc sheet không đọc được | Dùng `CAPCUT` hoặc sheet name `CAPCUT` |
| Script báo missing credential columns | Thiếu `credential_text` hoặc cặp `email/password` | Thêm cột đúng tên |

### 7.3. GEMINI Bị Skip Slot

Nếu log có:

```text
WARNING: GEMINI extra slot skipped
```

Nghĩa là một email GEMINI xuất hiện quá 2 lần trong Excel.

Rule đúng:

```text
1 email GEMINI = tối đa 2 slot bán
```

Sửa Excel để mỗi email chỉ có tối đa 2 dòng.

### 7.4. Khách Thanh Toán Nhưng Chưa Giao Hàng

Chạy ở đâu: **[RENDER] Logs**

Tìm các log theo thứ tự:

```text
[SEPAY] received transaction
[SEPAY] parsed order code
[SEPAY] matched order_id
[SEPAY] marked paid
[SEPAY] delivery start
DELIVERY START
DELIVERY SELECT INVENTORY
DELIVERY SENT SUCCESS
[SEPAY] delivery result
```

Nếu order đã `paid` nhưng delivery fail, log phải có lý do fail. Không sửa DB bằng tay; sửa Excel/import nếu lỗi do thiếu inventory.

---

## 8. Khôi Phục Khi Có Lỗi

Nguyên tắc khôi phục production:

```text
Không sửa SQLite.
Không chạy SQL tay.
Không enable/disable item.
Không sửa status trong database.
Không dùng inventory.json.
```

Luồng khôi phục chuẩn luôn là:

```text
[WINDOWS] Sửa Excel đúng
      |
      v
[WINDOWS] git push
      |
      v
[RENDER] Deploy commit mới
      |
      v
[RENDER] Chạy reset_and_import_inventory.py
      |
      v
[TELEGRAM] Kiểm tra lại stock và mua thử
```

| Tình huống lỗi | Không làm | Cách khôi phục đúng |
|---|---|---|
| Stock Telegram sai | Không update `inventory_items` bằng SQL | `[RENDER]` kiểm tra deploy, chạy lại script import, xem `MENU BUILD` |
| Thiếu 1 credential | Không insert trực tiếp vào SQLite | `[WINDOWS]` thêm credential vào Excel, push, deploy, `[RENDER]` chạy script |
| GEMINI thiếu slot | Không duplicate bằng SQL | `[WINDOWS]` thêm đúng dòng GEMINI thứ 2 cho email đó, push, deploy, `[RENDER]` chạy script |
| CAPCUT/CHATGPT hết hàng | Không enable item cũ | `[WINDOWS]` thêm dòng mới vào Excel, push, deploy, `[RENDER]` chạy script |
| Import validation fail | Không sửa DB để khớp validation | Sửa lỗi Excel theo debug output rồi chạy lại quy trình chuẩn |
| Paid nhưng không giao | Không tự đổi order status trong DB | Kiểm tra log delivery, sửa nguồn hàng trong Excel nếu thiếu inventory, import lại |

Lệnh khôi phục chuẩn:

Chạy ở đâu: **[WINDOWS] PowerShell / Git Bash**

```bash
# [WINDOWS]
git add imports/
git commit -m "Fix inventory data"
git push
```

Chạy ở đâu: **[RENDER] Dashboard**

```text
[RENDER] Deploy latest commit
```

Chạy ở đâu: **[RENDER] Shell**

```bash
# [RENDER]
python scripts/reset_and_import_inventory.py \
  --database /var/data/store.db \
  --chatgpt-file imports/import_CHATGPT_ONLY_READY1.xlsx \
  --multi-file imports/import_inventory_ONE_SHEET_PER_PRODUCT.xlsx
```

---

## 9. Checklist Nhanh Cho Nhân Viên Mới

### WINDOWS

```text
[ ] Mở imports/import_CHATGPT_ONLY_READY1.xlsx nếu nhập CHATGPT
[ ] Mở imports/import_inventory_ONE_SHEET_PER_PRODUCT.xlsx nếu nhập GEMINI/CAPCUT
[ ] Kiểm tra product_code đúng: CHATGPT / GEMINI / CAPCUT
[ ] Kiểm tra credential đầy đủ
[ ] GEMINI: mỗi email tối đa 2 dòng
[ ] Lưu file Excel
```

Chạy ở đâu: **[WINDOWS] PowerShell / Git Bash**

```bash
# [WINDOWS]
git add imports/
git commit -m "Update inventory"
git push
```

### RENDER

Chạy ở đâu: **[RENDER] Dashboard**

```text
[RENDER] Kiểm tra deploy commit mới đã thành công
```

Chạy ở đâu: **[RENDER] Shell**

```bash
# [RENDER]
python scripts/reset_and_import_inventory.py \
  --database /var/data/store.db \
  --chatgpt-file imports/import_CHATGPT_ONLY_READY1.xlsx \
  --multi-file imports/import_inventory_ONE_SHEET_PER_PRODUCT.xlsx
```

Chạy ở đâu: **[TELEGRAM]**

```text
/start
🎁 Sản phẩm
```

Kiểm tra stock hiển thị đúng.

---

## 10. Lịch Sử Thay Đổi Quy Trình

| Thay đổi | Ý nghĩa vận hành |
|---|---|
| Chuyển từ `inventory.json` sang SQLite-first | Bot không còn dùng `inventory.json` để tính stock, product detail, hoặc giao hàng account |
| Excel trở thành Source of Truth | Mọi thay đổi kho phải đi từ file Excel chuẩn trong `imports/` |
| GEMINI: 1 email = 2 slot | 1 credential/email GEMINI có thể tạo tối đa 2 inventory item bán được |
| Không còn enable/disable bằng SQL | Không sửa status thủ công; reset/import từ Excel là cách duy nhất để thay đổi kho |
| Menu sản phẩm đọc `inventory_items.status='available'` | Stock Telegram phản ánh số item bán được trong SQLite runtime |
| `CATALOG-*` chỉ dùng cho display/menu | Không để `CATALOG-*` cạnh tranh stock với product thật |

---

## 11. Mục Tiêu Cuối

Quy trình vận hành chuẩn:

```text
Sửa Excel
  -> Git Push
    -> Render Deploy
      -> Chạy script
        -> Xong
```

Nhân viên mới không cần biết:

- SQLite.
- Cấu trúc bảng `inventory_items`.
- SQL.
- Python nội bộ.
- Webhook/payment/reservation.

Chỉ cần nhớ:

```text
Excel là nguồn duy nhất.
SQLite không sửa tay.
Mỗi lần nhập kho: sửa Excel -> push -> deploy -> chạy script.
```
