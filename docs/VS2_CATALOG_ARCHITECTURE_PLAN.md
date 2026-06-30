# BOT VS2 Catalog Architecture Plan

Tài liệu này phân tích hệ thống Telegram Auto Sell Bot hiện tại và đề xuất thiết kế BOT VS2 để hỗ trợ catalog rộng nhiều brand, nhiều SKU, nhiều kiểu slot/credential.

Phạm vi tài liệu:

- Chỉ audit và thiết kế.
- Không sửa production bot.
- Không sửa DB.
- Không sửa script import hiện tại.
- Không deploy.

Kết luận ngắn: **không nên sửa trực tiếp production hiện tại để mở rộng catalog lớn**. Nên tạo BOT VS2 riêng hoặc ít nhất branch `v2`, chạy song song với token/test DB riêng.

---

## 1. Audit Hệ Thống Hiện Tại

### 1.1. Các File Quan Trọng

| File | Vai trò hiện tại |
|---|---|
| `telegram_license_bot.py` | Bot Telegram chính, menu, callback, order, QR, delivery, license flow |
| `repository/store_repository.py` | Repository SQLite cho products, inventory, orders, reservation, delivery |
| `scripts/reset_and_import_inventory.py` | Reset/import kho production theo 3 product mục tiêu: CHATGPT, GEMINI, CAPCUT |
| `scripts/import_inventory.py` | Import inventory tổng quát hơn, vẫn theo mô hình product hiện tại |
| `sepay_webhook.py` | Webhook SePay, match pending order theo amount/description, gọi fulfillment |
| `README_OPERATION.md` | Tài liệu vận hành production hiện tại |
| `render.yaml` | Render service, start command, env `STORE_DB_PATH=/var/data/store.db` |

Render hiện chạy:

```yaml
startCommand: python telegram_license_bot.py
envVars:
  - key: STORE_DB_PATH
    value: /var/data/store.db
autoDeploy: true
```

### 1.2. Product Code Hiện Có

#### Trong `database/store.db` local

Local `database/store.db` hiện có các `products.code` sau:

| product_code | name | price_vnd | show_in_menu | product_group | delivery_type |
|---|---|---:|---:|---|---|
| `CAPCUT` | `CAPCUT PRO` | 400000 | 1 | account | account |
| `GEM-AIPRO-1M-PRIVATE` | `Google AI Pro + Gemini Advanced 1 tháng` | 50000 | 1 | account | account |
| `GPT-PLUS-1M-PRIVATE` | `ChatGPT Plus 1 tháng` | 70000 | 1 | account | account |
| `GROK-SUPER-1M-PRIVATE` | `Grok Super 1 tháng` | 7000 | 1 | account | account |

Ghi chú: production `/var/data/store.db` có thể khác local. Các log runtime gần đây chứng minh production đang có ít nhất `CHATGPT`, `GEMINI`, `CAPCUT`, và có thể có các dòng `CATALOG-*`.

#### Trong code/catalog alias

`telegram_license_bot.py` và `repository/store_repository.py` có danh sách brand/alias rộng hơn DB:

| Brand/display | SKU alias đang map |
|---|---|
| `ADOBE` | `ADOBE-1M-PRIVATE` |
| `ARTLIST` | `ARTLIST-1M-PRIVATE` |
| `CANVA`, `CANVA PRO` | `CANVA-PRO-1M-PRIVATE` |
| `CAPCUT`, `CAPCUT PRO` | `CAPCUT-PRO-1M-PRIVATE` |
| `CHATGPT` | `GPT-PLUS-1M-PRIVATE` |
| `CLAUDE`, `CLAUDE AI` | `CLAUDE-PRO-1M-PRIVATE` |
| `CURSOR`, `CURSOR AI` | `CURSOR-PRO-1M-PRIVATE` |
| `ELEVEN`, `ELEVENLABS` | `ELEVENLABS-1M-PRIVATE` |
| `GAMMA`, `GAMMA AI` | `GAMMA-1M-PRIVATE` |
| `GEMINI`, `GEMINI AI` | `GEM-AIPRO-1M-PRIVATE` |
| `GROK`, `GROK SUPER` | `GROK-SUPER-1M-PRIVATE` |
| `HEYGEN`, `HEYGEN AI` | `HEYGEN-1M-PRIVATE` |
| `HIGGFIELD`, `HIGGSFIELD` | `HIGGSFIELD-1M-PRIVATE` |
| `KLING` | `KLING-1M-PRIVATE` |
| `KREA`, `KREA AI` | `KREA-1M-PRIVATE` |
| `OPENART`, `OPENART AI` | `OPENART-1M-PRIVATE` |
| `SUNO`, `SUNO AI` | `SUNO-1M-PRIVATE` |
| `VEO3`, `VEO3 ULTRA` | `VEO3-1M-PRIVATE` |
| `VIEWMAX` | `VIEWMAX-1M-PRIVATE` |

`PRODUCT_ORDER` trong `telegram_license_bot.py` là danh sách brand/display dùng để sắp menu:

```text
ADOBE, ARTLIST, CANVA, CAPCUT PRO, CHATGPT, CLAUDE AI, CURSOR AI,
ELEVEN, GAMMA AI, GEMINI AI, GROK SUPER, HEYGEN AI, HIGGFIELD,
KLING, KREA AI, OPENART AI, SUNO AI, VEO3 ULTRA, VIEWMAX
```

`scripts/import_inventory.py` cho phép product code:

```text
CHATGPT, GEMINI, GROK, CAPCUT, CLAUDE, CURSOR, CANVA, ADOBE,
ARTLIST, ELEVEN, GAMMA, HEYGEN, HIGGSFIELD, KLING, KREA,
OPENART, SUNO, VEO3, VIEWMAX
```

### 1.3. SQLite Tables Liên Quan

Các bảng chính hiện có:

| Bảng | Có trong DB | Vai trò |
|---|---:|---|
| `products` | Có | Product/SKU hiện tại, vừa làm catalog vừa làm gói bán |
| `inventory_items` | Có | Credential/item tồn kho, gắn trực tiếp với `products.id` |
| `orders` | Có | Đơn hàng, payment status, delivery status |
| `order_inventory_items` | Có | Link order với inventory item đã reserve/deliver |
| `payment_transactions` | Có | Log giao dịch payment provider |
| `payments` | Không có | Không tồn tại trong schema hiện tại |
| `inventory_movements` | Có trong schema bot | Audit movement import/reserve/deliver/release/disable |

### 1.4. Schema Hiện Tại

#### `products`

| Column | Type | Ghi chú |
|---|---|---|
| `id` | TEXT PK | UUID/string |
| `code` | TEXT UNIQUE NOT NULL | Product code hiện tại |
| `name` | TEXT NOT NULL | Display/package name |
| `active` | INTEGER | 0/1 |
| `delivery_type` | TEXT | `account` hoặc `license` |
| `created_at` | TEXT | ISO time |
| `updated_at` | TEXT | ISO time |
| `category` | TEXT | Category cũ |
| `account_type` | TEXT | private/shared... nhưng chưa chuẩn hóa VS2 |
| `duration` | TEXT | Chuỗi duration hiện tại |
| `price_vnd` | INTEGER | Giá đang dùng cho package |
| `warranty_days` | INTEGER | Bảo hành |
| `note` | TEXT | Ghi chú |
| `menu_order` | INTEGER | Thứ tự menu |
| `show_in_menu` | INTEGER | 0/1 |
| `product_group` | TEXT | Mặc định `account` |
| `category_key` | TEXT | Dùng gom package theo brand/category |
| `description` | TEXT | Mô tả |

#### `inventory_items`

| Column | Type | Ghi chú |
|---|---|---|
| `id` | TEXT PK | UUID/string |
| `product_id` | TEXT FK -> products.id | Inventory gắn trực tiếp vào product hiện tại |
| `secret_value` | TEXT NOT NULL | Credential dạng string `email|password|2fa|recovery` |
| `status` | TEXT | `available`, `reserved`, `delivered`, `disabled` |
| `reserved_order_id` | TEXT | Order đang reserve |
| `delivered_order_id` | TEXT | Order đã giao |
| `created_at` | TEXT | ISO time |
| `reserved_at` | TEXT | ISO time |
| `delivered_at` | TEXT | ISO time |
| `disabled_at` | TEXT | ISO time |

#### `orders`

| Column | Type | Ghi chú |
|---|---|---|
| `id` | TEXT PK | UUID/string |
| `order_id` | TEXT UNIQUE NOT NULL | Mã order dùng trong QR |
| `telegram_user_id` | INTEGER | User Telegram |
| `username` | TEXT | User label |
| `product_id` | TEXT | Hiện có lúc là code/package |
| `product_code` | TEXT | Product/SKU code bán |
| `product_name` | TEXT | Display brand/product |
| `package_name` | TEXT | Tên package |
| `quantity` | INTEGER | Số lượng |
| `unit_price_vnd` | INTEGER | Đơn giá |
| `total_vnd` | INTEGER | Thành tiền |
| `delivery_type` | TEXT | `account`/`license` |
| `machine_id` | TEXT | Cho license flow |
| `plan` | TEXT | Cho license flow |
| `payment_method` | TEXT | ACB/SEPAY... |
| `payment_status` | TEXT | pending/paid/failed/refunded/expired/cancelled |
| `order_status` | TEXT | pending/reserved/paid/delivered/manual_delivery/cancelled/expired/failed |
| `transaction_id` | TEXT | Provider transaction |
| `created_at` | TEXT | ISO time |
| `expire_at` | TEXT | TTL order |
| `paid_at` | TEXT | ISO time |
| `delivered_at` | TEXT | ISO time |
| `delivery_ref` | TEXT | Credential text hoặc license file ref |
| `inventory_source` | TEXT | Hiện mặc định `sqlite` |
| `duration_days` | INTEGER | Có thêm sau |
| `expire_date` | TEXT | License |
| `lifetime` | INTEGER | License |

#### `order_inventory_items`

| Column | Type | Ghi chú |
|---|---|---|
| `order_id` | TEXT PK/FK -> orders.id | Lưu internal `orders.id`, không phải `orders.order_id` |
| `inventory_item_id` | TEXT PK UNIQUE/FK -> inventory_items.id | 1 item chỉ gắn 1 order active |
| `state` | TEXT | `reserved`, `delivered`, `released` |
| `created_at` | TEXT | ISO time |
| `delivered_at` | TEXT | ISO time |
| `released_at` | TEXT | ISO time |

#### `payment_transactions`

| Column | Type | Ghi chú |
|---|---|---|
| `id` | TEXT PK | UUID/string |
| `provider` | TEXT | SEPAY/ACB... |
| `provider_transaction_id` | TEXT | ID từ provider |
| `order_id` | TEXT FK -> orders.id | Internal order id |
| `amount_vnd` | INTEGER | Số tiền nhận |
| `description` | TEXT | Nội dung chuyển khoản |
| `raw_payload_json` | TEXT | Payload raw |
| `status` | TEXT | received/matched/processed/duplicate/unmatched/failed |
| `received_at` | TEXT | ISO time |
| `processed_at` | TEXT | ISO time |

### 1.5. Logic Menu Đang Lấy Từ Đâu

Luồng chính:

```text
telegram_license_bot._product_menu_keyboard()
  -> _catalog_category_items()
    -> StoreRepository.list_menu_product_stock(product_group='account')
      -> products + inventory_items(status='available')
```

Chi tiết:

- `PRODUCT_ORDER` trong `telegram_license_bot.py` quyết định thứ tự brand/display.
- `TELEGRAM_PRODUCT_CODE_MAP` map brand/display sang product code/SKU alias.
- `StoreRepository.list_menu_product_stock()` gom `CATALOG-*` về canonical brand và đếm `inventory_items.status='available'`.
- Có hard-code mapping:
  - `GPT-PLUS-1M-PRIVATE` -> `CHATGPT`
  - `GEM-AIPRO-1M-PRIVATE` -> `GEMINI`
  - `CAPCUT-PRO-1M-PRIVATE` -> `CAPCUT`
  - `GROK-SUPER-1M-PRIVATE` -> `GROK`

### 1.6. Logic Giá Đang Lấy Từ Đâu

Luồng chính:

```text
_package_keyboard()
_quantity_text()
_create_sales_order()
  -> _get_package_info()
    -> StoreRepository.list_packages_by_category()
      -> products.price_vnd
```

Trong order:

- `unit_price = package["price_vnd"]`
- `total = unit_price * quantity`
- `amount = total`
- SQLite order lưu `unit_price_vnd` và `total_vnd`.

Ngoài account flow, license flow dùng `TOOL_LICENSE_PRODUCTS` hard-code trong `telegram_license_bot.py`.

`PRODUCT_PACKAGES = {"7D": 99000, "30D": 199000, "90D": 499000}` vẫn còn trong file nhưng hiện không còn là nguồn giá chính cho account package SQLite.

### 1.7. Logic Warranty/Bảo Hành Đang Nằm Ở Đâu

- Import script đọc `warranty_days` từ Excel vào `products.warranty_days`.
- `StoreRepository.get_product_details()` đọc `warranty_days`.
- UI dùng trong:
  - `_product_detail_text()`: nếu product có `warranty_days`, hiển thị `Bảo hành: X ngày`; nếu không thì `Theo từng gói`.
  - `_quantity_text()`: đọc `warranty_days` theo package/product code.

Điểm yếu: warranty gắn vào `products`, nhưng hiện `products` vừa là brand/catalog vừa là package/SKU. Khi nhiều SKU cùng brand có warranty khác nhau, mô hình hiện tại dễ nhầm nếu dùng brand-level product.

### 1.8. Logic Delivery Đang Lấy Credential Từ Đâu

Luồng account delivery:

```text
fulfill_order()
  -> _deliver_sales_order()
    -> StoreRepository.mark_account_order_paid_for_fulfillment()
    -> StoreRepository.deliver_reserved_items(order_id)
      -> order_inventory_items
      -> inventory_items.secret_value
```

Credential hiện là string trong `inventory_items.secret_value`, thường dạng:

```text
email|password
email|password|2fa
email|password|2fa|recovery_email
```

Sau delivery:

- `inventory_items.status = 'delivered'`
- `inventory_items.delivered_order_id = order_id`
- `order_inventory_items.state = 'delivered'`
- `orders.order_status = 'delivered'`
- Bot gửi credential text cho khách.

### 1.9. Logic Payment/QR Đang Tính Tiền Theo Field Nào

QR flow:

```text
_create_sales_order()
  -> order["total"] = unit_price * quantity
_send_payment_choice()
_send_acb_qr()
  -> _build_vietqr_url(order, payment_service)
    -> amount = int(order["total"])
  -> _qr_caption()
    -> hiển thị int(order["total"])
```

SePay webhook:

```text
sepay_webhook.process_sepay_payload()
  -> extract_amount(payload)
  -> find_pending_order(description, amount)
    -> bank_checker.load_pending_orders()
    -> match order_id trong description + amount == order total/amount
  -> bank_checker.update_order(... payment_status='paid')
  -> fulfill_order(order_id)
```

SQLite order cũng lưu `total_vnd`, nhưng webhook hiện vẫn đi qua `bank_checker`/JSON-compatible pending order path.

### 1.10. Hard-code CHATGPT/GEMINI/CAPCUT

Các điểm chính:

| File | Hard-code |
|---|---|
| `scripts/reset_and_import_inventory.py` | `TARGET_PRODUCTS = ("CHATGPT", "GEMINI", "CAPCUT")` |
| `scripts/reset_and_import_inventory.py` | reset chỉ 3 product target |
| `scripts/reset_and_import_inventory.py` | validation chỉ 3 product target |
| `scripts/reset_and_import_inventory.py` | debug riêng CHATGPT |
| `repository/store_repository.py` | `CANONICAL_CATALOG_PRODUCTS` chỉ có CAPCUT/GEMINI |
| `repository/store_repository.py` | `list_menu_product_stock()` map GPT/GEMINI/CAPCUT/GROK alias thủ công |
| `telegram_license_bot.py` | `CATALOG_DISPLAY_NAMES` chỉ CAPCUT/GEMINI |
| `telegram_license_bot.py` | `PRODUCT_ORDER` là danh sách brand hard-code |
| `telegram_license_bot.py` | `TELEGRAM_PRODUCT_CODE_MAP` là alias hard-code |
| `scripts/import_inventory.py` | `DEFAULT_PRODUCT_DISPLAY_NAMES` chỉ CAPCUT/GEMINI |

### 1.11. Hard-code GEMINI = 2 Slot

`scripts/reset_and_import_inventory.py`:

- `GEMINI_MAX_SLOTS_PER_CREDENTIAL = 2`
- `prepare_inventory()` đếm `gemini_seen_count`
- Nếu credential GEMINI xuất hiện lần 1 và 2 thì tạo slot 1/2.
- Nếu quá 2 lần thì skip warning.
- `gemini_slot_credential(credential, slot)` tạo identity dạng:

```text
{credential}|GEMINI_SLOT_{slot}
```

Điểm yếu: slot rule đang gắn vào product code `GEMINI`, không phải cấu hình từng SKU. VS2 cần đưa rule này vào `sku.slot_per_credential`.

### 1.12. Các Chỗ Còn Nhắc `inventory.json`

`telegram_license_bot.py`:

- `INVENTORY_PATH = PROJECT_ROOT / "inventory.json"` vẫn còn.
- Một số legacy helpers/order migration còn liên quan JSON order/inventory path.
- `_deliver_sales_order()` hiện đã không fallback `inventory.json` cho account delivery.

`README_OPERATION.md`:

- Ghi rõ không dùng `inventory.json`.

`test_sepay_order_fulfillment.py`:

- Có test đảm bảo account delivery không fallback `inventory.json`.

Kết luận: `inventory.json` vẫn tồn tại trong repo và còn được nhắc ở legacy/test/tài liệu, nhưng không còn nên là nguồn dữ liệu account production.

---

## 2. Vấn Đề Kiến Trúc Hiện Tại Với Catalog Rộng

Mô hình hiện tại:

```text
products
  vừa là brand/catalog
  vừa là package/SKU
  vừa chứa price/warranty/duration

inventory_items
  gắn trực tiếp với products.id
```

Điểm nghẽn khi catalog rộng:

| Vấn đề | Tác động |
|---|---|
| Brand và SKU chưa tách riêng | CHATGPT có nhiều gói sẽ khó hiển thị/menu đúng |
| Slot rule hard-code theo GEMINI | Không áp dụng được cho SKU khác có 2/3/5 slot |
| Credential là string | Khó giao nhiều field như login_url, note, recovery, template riêng |
| Menu dùng `PRODUCT_ORDER` hard-code | Thêm brand/SKU phải sửa code hoặc import rất cẩn thận |
| Alias `CATALOG-*` là vá tạm | Dễ cạnh tranh stock nếu dữ liệu lẫn brand/product |
| Import production chỉ target 3 product | Không scale lên hàng chục brand/SKU |
| Giá/warranty nằm trong `products` | Nếu `products` vừa là brand vừa là SKU sẽ dễ sai giá |

---

## 3. Đề Xuất Cấu Trúc BOT VS2

### 3.1. Mô Hình Chuẩn

```text
Brand
  |
  +-- Product Package / SKU
        |
        +-- Inventory Credential
              |
              +-- Inventory Slot
```

Nguyên tắc:

- Brand chỉ để nhóm và hiển thị.
- SKU là đơn vị bán hàng thật.
- Inventory phải gắn với SKU, không gắn trực tiếp với Brand.
- Slot rule nằm ở SKU: `slot_per_credential`.
- Payment amount lấy từ SKU price tại thời điểm tạo order.
- Delivery lấy credential/slot đã reserve, render theo `delivery_template`.

### 3.2. Bảng VS2 Đề Xuất

#### `brands`

| Column | Type | Ghi chú |
|---|---|---|
| `id` | TEXT PK | UUID |
| `brand_code` | TEXT UNIQUE | `CHATGPT`, `GEMINI`, `CAPCUT` |
| `display_name` | TEXT | Tên menu brand |
| `description` | TEXT | Mô tả |
| `active` | INTEGER | 0/1 |
| `show_in_menu` | INTEGER | 0/1 |
| `menu_order` | INTEGER | Thứ tự menu |
| `created_at` | TEXT | ISO time |
| `updated_at` | TEXT | ISO time |

#### `skus`

Mỗi SKU phải có:

| Column | Type | Ghi chú |
|---|---|---|
| `id` | TEXT PK | UUID |
| `sku_code` | TEXT UNIQUE | `CHATGPT_PLUS_PRIVATE_1M` |
| `brand_code` | TEXT FK/logical FK | `CHATGPT` |
| `display_name` | TEXT | `ChatGPT Plus Private 1 Month` |
| `price_vnd` | INTEGER | Giá bán |
| `duration_days` | INTEGER | Thời hạn sử dụng |
| `warranty_days` | INTEGER | Bảo hành |
| `account_type` | TEXT | `shared`, `private`, `team`, `education`, `other` |
| `slot_per_credential` | INTEGER | Số slot bán được trên 1 credential |
| `delivery_template` | TEXT | Template giao hàng |
| `delivery_type` | TEXT | `account`, `link`, `license`, `manual` |
| `active` | INTEGER | 0/1 |
| `show_in_menu` | INTEGER | 0/1 |
| `menu_order` | INTEGER | Thứ tự SKU trong brand |
| `created_at` | TEXT | ISO time |
| `updated_at` | TEXT | ISO time |

Ví dụ SKU:

```text
CHATGPT_PLUS_SHARED_1M
CHATGPT_PLUS_PRIVATE_1M
CHATGPT_PRO_1M
CHATGPT_BUSINESS_1M
CHATGPT_GO_1M
GEMINI_AI_SHARED_1M
GEMINI_PRO_PRIVATE_1M
```

#### `inventory_credentials`

Một credential gốc có thể sinh nhiều slot.

| Column | Type | Ghi chú |
|---|---|---|
| `id` | TEXT PK | UUID |
| `sku_code` | TEXT | Gắn credential với SKU |
| `credential_identity` | TEXT | Identity chống duplicate trong batch |
| `email` | TEXT | Email |
| `password` | TEXT | Password |
| `secret_2fa` | TEXT | 2FA secret |
| `recovery` | TEXT | Recovery |
| `login_url` | TEXT | Link login |
| `note` | TEXT | Ghi chú |
| `active` | INTEGER | 0/1 |
| `created_at` | TEXT | ISO time |
| `updated_at` | TEXT | ISO time |

#### `inventory_slots`

Slot là đơn vị bán thật.

| Column | Type | Ghi chú |
|---|---|---|
| `id` | TEXT PK | UUID |
| `credential_id` | TEXT FK | Gắn credential gốc |
| `sku_code` | TEXT | Denormalize để query nhanh |
| `slot_index` | INTEGER | 1..slot_per_credential |
| `slot_label` | TEXT | `SLOT_1`, `SLOT_2`, `USER_A` |
| `status` | TEXT | available/reserved/delivered/disabled |
| `reserved_order_id` | TEXT | Order reserve |
| `delivered_order_id` | TEXT | Order delivered |
| `created_at` | TEXT | ISO time |
| `reserved_at` | TEXT | ISO time |
| `delivered_at` | TEXT | ISO time |
| `disabled_at` | TEXT | ISO time |

### 3.3. VS2 Delivery Template

SKU có `delivery_template`, ví dụ:

```text
Tài khoản: {email}
Mật khẩu: {password}
2FA: {secret_2fa}
Recovery: {recovery}
Link đăng nhập: {login_url}
Ghi chú: {note}
Slot: {slot_label}
```

Điều này giúp:

- CHATGPT shared/private dùng template khác nhau.
- Link-only product không cần email/password.
- SKU có note/hướng dẫn riêng.
- Không phải hard-code format giao hàng trong bot.

### 3.4. Final Recommended VS2 Database Schema

Đây là schema khuyến nghị cuối cùng cho `telegram-auto-sell-bot-v2`. Không migrate trực tiếp vào DB V1.

```text
brands
  1 -> n skus

skus
  1 -> n inventory_credentials

inventory_credentials
  1 -> n inventory_slots

orders
  1 -> n order_slots

inventory_slots
  n -> 1 order_slots

payment_transactions
  n -> 1 orders
```

#### `brands`

| Column | Type | Constraint | Ghi chú |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `brand_code` | TEXT | UNIQUE NOT NULL | `CHATGPT`, `GEMINI`, `CAPCUT` |
| `display_name` | TEXT | NOT NULL | Tên hiển thị |
| `description` | TEXT | NOT NULL DEFAULT '' | Mô tả brand |
| `active` | INTEGER | NOT NULL DEFAULT 1 | 0/1 |
| `show_in_menu` | INTEGER | NOT NULL DEFAULT 1 | 0/1 |
| `menu_order` | INTEGER | NOT NULL DEFAULT 100 | Thứ tự menu |
| `created_at` | TEXT | NOT NULL | ISO time |
| `updated_at` | TEXT | NOT NULL | ISO time |

#### `skus`

| Column | Type | Constraint | Ghi chú |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `sku_code` | TEXT | UNIQUE NOT NULL | Đơn vị bán thật |
| `brand_id` | TEXT | NOT NULL REFERENCES brands(id) | FK brand |
| `brand_code` | TEXT | NOT NULL | Denormalized để import/query dễ |
| `display_name` | TEXT | NOT NULL | Tên gói |
| `price_vnd` | INTEGER | NOT NULL CHECK >= 0 | Giá bán |
| `duration_days` | INTEGER | NOT NULL DEFAULT 0 | 0 nếu không áp dụng |
| `warranty_days` | INTEGER | NOT NULL DEFAULT 0 | Bảo hành |
| `account_type` | TEXT | NOT NULL | `shared/private/team/education/other` |
| `slot_per_credential` | INTEGER | NOT NULL DEFAULT 1 CHECK >= 1 | Rule slot |
| `delivery_type` | TEXT | NOT NULL DEFAULT 'account' | `account/link/license/manual` |
| `delivery_template` | TEXT | NOT NULL DEFAULT '' | Template giao hàng |
| `active` | INTEGER | NOT NULL DEFAULT 1 | 0/1 |
| `show_in_menu` | INTEGER | NOT NULL DEFAULT 1 | 0/1 |
| `menu_order` | INTEGER | NOT NULL DEFAULT 100 | Thứ tự trong brand |
| `created_at` | TEXT | NOT NULL | ISO time |
| `updated_at` | TEXT | NOT NULL | ISO time |

Index khuyến nghị:

```sql
CREATE UNIQUE INDEX idx_skus_sku_code ON skus(sku_code);
CREATE INDEX idx_skus_brand_menu ON skus(brand_code, active, show_in_menu, menu_order);
```

#### `inventory_credentials`

| Column | Type | Constraint | Ghi chú |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `sku_id` | TEXT | NOT NULL REFERENCES skus(id) | SKU sở hữu credential |
| `sku_code` | TEXT | NOT NULL | Denormalized |
| `credential_identity` | TEXT | NOT NULL | Identity chống duplicate trong một import run |
| `email` | TEXT | NOT NULL DEFAULT '' | Email |
| `password` | TEXT | NOT NULL DEFAULT '' | Password |
| `secret_2fa` | TEXT | NOT NULL DEFAULT '' | 2FA secret |
| `recovery` | TEXT | NOT NULL DEFAULT '' | Recovery |
| `login_url` | TEXT | NOT NULL DEFAULT '' | Link login |
| `note` | TEXT | NOT NULL DEFAULT '' | Ghi chú |
| `source_import_id` | TEXT | NOT NULL DEFAULT '' | Import run |
| `active` | INTEGER | NOT NULL DEFAULT 1 | 0/1 |
| `created_at` | TEXT | NOT NULL | ISO time |
| `updated_at` | TEXT | NOT NULL | ISO time |

Index khuyến nghị:

```sql
CREATE INDEX idx_inventory_credentials_sku ON inventory_credentials(sku_code);
CREATE INDEX idx_inventory_credentials_import ON inventory_credentials(source_import_id);
```

Không đặt unique global trên `credential_identity` vì credential đã delivered ở lần nhập trước có thể được restock hợp lệ trong tương lai. Duplicate nên được validate theo import batch và theo SKU.

#### `inventory_slots`

| Column | Type | Constraint | Ghi chú |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `credential_id` | TEXT | NOT NULL REFERENCES inventory_credentials(id) | Credential gốc |
| `sku_id` | TEXT | NOT NULL REFERENCES skus(id) | SKU |
| `sku_code` | TEXT | NOT NULL | Denormalized |
| `slot_index` | INTEGER | NOT NULL | 1..slot_per_credential |
| `slot_label` | TEXT | NOT NULL DEFAULT '' | `SLOT_1`, `SLOT_2` |
| `status` | TEXT | NOT NULL DEFAULT 'available' | available/reserved/delivered/disabled |
| `reserved_order_id` | TEXT | DEFAULT NULL | Public order id |
| `delivered_order_id` | TEXT | DEFAULT NULL | Public order id |
| `created_at` | TEXT | NOT NULL | ISO time |
| `reserved_at` | TEXT | DEFAULT NULL | ISO time |
| `delivered_at` | TEXT | DEFAULT NULL | ISO time |
| `disabled_at` | TEXT | DEFAULT NULL | ISO time |

Index khuyến nghị:

```sql
CREATE INDEX idx_inventory_slots_sku_status ON inventory_slots(sku_code, status);
CREATE UNIQUE INDEX idx_inventory_slots_active_reservation
  ON inventory_slots(id)
  WHERE status IN ('reserved', 'delivered');
```

#### `orders`

| Column | Type | Constraint | Ghi chú |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `order_id` | TEXT | UNIQUE NOT NULL | Mã order public/QR |
| `telegram_user_id` | INTEGER | NOT NULL | User Telegram |
| `username` | TEXT | NOT NULL DEFAULT '' | User label |
| `brand_code` | TEXT | NOT NULL | Snapshot |
| `sku_code` | TEXT | NOT NULL | Snapshot SKU bán |
| `sku_display_name` | TEXT | NOT NULL | Snapshot tên |
| `quantity` | INTEGER | NOT NULL CHECK > 0 | Số slot/item mua |
| `unit_price_vnd` | INTEGER | NOT NULL CHECK >= 0 | Snapshot giá |
| `total_vnd` | INTEGER | NOT NULL CHECK >= 0 | Snapshot tổng |
| `duration_days` | INTEGER | NOT NULL DEFAULT 0 | Snapshot |
| `warranty_days` | INTEGER | NOT NULL DEFAULT 0 | Snapshot |
| `account_type` | TEXT | NOT NULL DEFAULT '' | Snapshot |
| `payment_method` | TEXT | NOT NULL DEFAULT '' | ACB/SEPAY |
| `payment_status` | TEXT | NOT NULL DEFAULT 'pending' | pending/paid/failed/expired/cancelled/refunded |
| `order_status` | TEXT | NOT NULL DEFAULT 'pending' | pending/reserved/paid/delivered/manual_delivery/cancelled/expired/failed |
| `transaction_id` | TEXT | NOT NULL DEFAULT '' | Provider transaction |
| `created_at` | TEXT | NOT NULL | ISO time |
| `expire_at` | TEXT | DEFAULT NULL | TTL |
| `paid_at` | TEXT | DEFAULT NULL | ISO time |
| `delivered_at` | TEXT | DEFAULT NULL | ISO time |
| `delivery_ref` | TEXT | NOT NULL DEFAULT '' | Summary/ref, không phải source of truth |

#### `order_slots`

| Column | Type | Constraint | Ghi chú |
|---|---|---|---|
| `order_id` | TEXT | NOT NULL REFERENCES orders(id) | Internal order id |
| `slot_id` | TEXT | UNIQUE NOT NULL REFERENCES inventory_slots(id) | Slot đã reserve/deliver |
| `state` | TEXT | NOT NULL | reserved/delivered/released |
| `created_at` | TEXT | NOT NULL | ISO time |
| `delivered_at` | TEXT | DEFAULT NULL | ISO time |
| `released_at` | TEXT | DEFAULT NULL | ISO time |

#### `payment_transactions`

Giữ gần giống V1, nhưng match trực tiếp với `orders.id` V2:

| Column | Type | Constraint | Ghi chú |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `provider` | TEXT | NOT NULL | SEPAY/ACB |
| `provider_transaction_id` | TEXT | NOT NULL | Unique theo provider |
| `order_id` | TEXT | REFERENCES orders(id) | Internal order id |
| `amount_vnd` | INTEGER | NOT NULL | Amount nhận |
| `description` | TEXT | NOT NULL DEFAULT '' | Nội dung CK |
| `raw_payload_json` | TEXT | NOT NULL DEFAULT '' | Raw payload |
| `status` | TEXT | NOT NULL | received/matched/processed/duplicate/unmatched/failed |
| `received_at` | TEXT | NOT NULL | ISO time |
| `processed_at` | TEXT | DEFAULT NULL | ISO time |

#### `inventory_movements`

Nên giữ audit movement ngay từ đầu:

| Column | Type | Constraint | Ghi chú |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | UUID |
| `slot_id` | TEXT | REFERENCES inventory_slots(id) | Slot liên quan |
| `credential_id` | TEXT | REFERENCES inventory_credentials(id) | Credential liên quan |
| `action` | TEXT | NOT NULL | import/reserve/deliver/release/disable |
| `order_id` | TEXT | REFERENCES orders(id) | Nếu có |
| `source` | TEXT | NOT NULL DEFAULT '' | import file/run/admin |
| `created_at` | TEXT | NOT NULL | ISO time |

### 3.5. SKU Examples For Initial VS2 Catalog

#### CHATGPT

| sku_code | brand_code | display_name | price_vnd | duration_days | warranty_days | account_type | slot_per_credential | delivery_type |
|---|---|---|---:|---:|---:|---|---:|---|
| `CHATGPT_PLUS_SHARED_1M` | CHATGPT | ChatGPT Plus Shared 1 Month | 30000 | 30 | 3 | shared | 5 | account |
| `CHATGPT_PLUS_PRIVATE_1M` | CHATGPT | ChatGPT Plus Private 1 Month | 70000 | 30 | 7 | private | 1 | account |
| `CHATGPT_PLUS_PRIVATE_2M` | CHATGPT | ChatGPT Plus Private 2 Months | 130000 | 60 | 15 | private | 1 | account |
| `CHATGPT_PRO_1M` | CHATGPT | ChatGPT Pro 1 Month | 400000 | 30 | 30 | private | 1 | account |
| `CHATGPT_BUSINESS_1M` | CHATGPT | ChatGPT Business 1 Month | 250000 | 30 | 30 | team | 1 | account |
| `CHATGPT_GO_1M` | CHATGPT | ChatGPT Go 1 Month | 50000 | 30 | 7 | private | 1 | account |
| `CHATGPT_EDUCATION_1M` | CHATGPT | ChatGPT Education 1 Month | 90000 | 30 | 15 | education | 1 | account |
| `CHATGPT_TEACHERS_1M` | CHATGPT | ChatGPT Teachers 1 Month | 90000 | 30 | 15 | education | 1 | account |

#### GEMINI

| sku_code | brand_code | display_name | price_vnd | duration_days | warranty_days | account_type | slot_per_credential | delivery_type |
|---|---|---|---:|---:|---:|---|---:|---|
| `GEMINI_AI_SHARED_1M` | GEMINI | Gemini AI Shared 1 Month | 50000 | 30 | 7 | shared | 2 | account |
| `GEMINI_AI_PRIVATE_1M` | GEMINI | Gemini AI Private 1 Month | 90000 | 30 | 15 | private | 1 | account |
| `GEMINI_PRO_PRIVATE_1M` | GEMINI | Gemini Pro Private 1 Month | 150000 | 30 | 30 | private | 1 | account |

#### CAPCUT

| sku_code | brand_code | display_name | price_vnd | duration_days | warranty_days | account_type | slot_per_credential | delivery_type |
|---|---|---|---:|---:|---:|---|---:|---|
| `CAPCUT_PRO_PRIVATE_1M` | CAPCUT | CapCut Pro Private 1 Month | 400000 | 30 | 30 | private | 1 | account |
| `CAPCUT_PRO_SHARED_1M` | CAPCUT | CapCut Pro Shared 1 Month | 80000 | 30 | 7 | shared | 3 | account |
| `CAPCUT_PRO_TEAM_1M` | CAPCUT | CapCut Pro Team 1 Month | 180000 | 30 | 15 | team | 1 | account |

---

## 4. Thiết Kế Excel VS2

### 4.1. Option A - Một File, Hai Sheet `PRODUCTS` và `INVENTORY`

File đề xuất:

```text
imports/catalog_vs2.xlsx
```

Sheets:

```text
PRODUCTS
INVENTORY
```

#### Sheet `PRODUCTS`

| Cột | Ý nghĩa |
|---|---|
| `sku_code` | Mã SKU duy nhất |
| `brand_code` | Brand cha |
| `display_name` | Tên gói hiển thị |
| `price_vnd` | Giá bán |
| `duration_days` | Thời hạn |
| `warranty_days` | Bảo hành |
| `account_type` | shared/private/team/education/other |
| `slot_per_credential` | Số slot trên mỗi credential |
| `delivery_template` | Template giao hàng |
| `active` | 0/1 |
| `show_in_menu` | 0/1 |
| `menu_order` | Thứ tự menu |

#### Sheet `INVENTORY`

| Cột | Ý nghĩa |
|---|---|
| `sku_code` | SKU nhận inventory |
| `email` | Email |
| `password` | Password |
| `secret_2fa` | 2FA |
| `recovery` | Recovery |
| `login_url` | Link login |
| `note` | Ghi chú |
| `slot_label` | Optional label; nếu trống script tự tạo |
| `status` | Thường là `available` |

Ưu điểm:

- Dễ validate toàn cục.
- Dễ import một lần toàn catalog.
- Không cần tạo nhiều sheet.
- Phù hợp automation/CSV export.

Nhược điểm:

- Sheet `INVENTORY` có thể rất dài.
- Người nhập kho dễ lọc nhầm SKU nếu không dùng filter.
- Với nhân viên không quen Excel filter, có thể khó thao tác.

### 4.2. Option B - Một File, `PRODUCTS` + Mỗi SKU Một Sheet Inventory Riêng

File đề xuất:

```text
imports/catalog_vs2.xlsx
```

Sheets:

```text
PRODUCTS
CHATGPT_PLUS_SHARED_1M
CHATGPT_PLUS_PRIVATE_1M
GEMINI_AI_SHARED_1M
CAPCUT_PRO_1M
...
```

Trong mỗi sheet SKU inventory:

| Cột | Ý nghĩa |
|---|---|
| `email` | Email |
| `password` | Password |
| `secret_2fa` | 2FA |
| `recovery` | Recovery |
| `login_url` | Link login |
| `note` | Ghi chú |
| `slot_label` | Optional label |
| `status` | Thường là `available` |

`sku_code` lấy từ tên sheet.

Ưu điểm:

- Dễ cho nhân viên nhập kho theo từng SKU.
- Ít nhầm lẫn giữa SKU.
- Gần với thói quen hiện tại “one sheet per product”.

Nhược điểm:

- Nhiều SKU sẽ tạo rất nhiều sheet.
- Rename sheet sai gây lỗi import.
- Khó bulk edit nhiều SKU.
- Validation cần kiểm tra sheet có tồn tại trong `PRODUCTS`.

### 4.3. Khuyến Nghị Excel VS2

Khuyến nghị production:

```text
Option A cho hệ thống chuẩn lâu dài.
Option B nếu ưu tiên nhập liệu thủ công dễ hiểu trong giai đoạn đầu.
```

Thực tế nên hỗ trợ cả hai trong importer VS2:

- `PRODUCTS` luôn là bắt buộc.
- Nếu có sheet `INVENTORY`, đọc Option A.
- Nếu không, đọc các sheet có tên trùng `sku_code` theo Option B.

### 4.4. Final Recommended Excel Format

Khuyến nghị cuối cùng cho VS2:

```text
File: imports/catalog_vs2.xlsx

Sheet bắt buộc:
- BRANDS
- PRODUCTS
- INVENTORY
```

Lý do chọn format này:

- `BRANDS` giúp menu brand không hard-code.
- `PRODUCTS` là source of truth cho SKU.
- `INVENTORY` là source of truth cho credential/slot input.
- Một file duy nhất dễ commit/deploy/import.
- Dễ validate toàn bộ catalog trước khi reset/import.

#### Sheet `BRANDS`

| Column | Required | Example | Note |
|---|---:|---|---|
| `brand_code` | Yes | `CHATGPT` | Uppercase canonical code |
| `display_name` | Yes | `ChatGPT` | Tên hiển thị |
| `description` | No | `OpenAI account products` | Mô tả |
| `active` | Yes | `1` | 0/1 |
| `show_in_menu` | Yes | `1` | 0/1 |
| `menu_order` | Yes | `10` | Thứ tự menu |

Example:

| brand_code | display_name | description | active | show_in_menu | menu_order |
|---|---|---|---:|---:|---:|
| CHATGPT | ChatGPT | OpenAI account products | 1 | 1 | 10 |
| GEMINI | Gemini AI | Google AI account products | 1 | 1 | 20 |
| CAPCUT | CapCut Pro | CapCut account products | 1 | 1 | 30 |

#### Sheet `PRODUCTS`

| Column | Required | Example | Note |
|---|---:|---|---|
| `sku_code` | Yes | `CHATGPT_PLUS_PRIVATE_1M` | Unique SKU |
| `brand_code` | Yes | `CHATGPT` | Must exist in `BRANDS` |
| `display_name` | Yes | `ChatGPT Plus Private 1 Month` | Tên gói |
| `price_vnd` | Yes | `70000` | Giá |
| `duration_days` | Yes | `30` | Thời hạn |
| `warranty_days` | Yes | `7` | Bảo hành |
| `account_type` | Yes | `private` | shared/private/team/education/other |
| `slot_per_credential` | Yes | `1` | >= 1 |
| `delivery_type` | Yes | `account` | account/link/license/manual |
| `delivery_template` | Yes | `Email: {email}\nPass: {password}` | Template giao hàng |
| `active` | Yes | `1` | 0/1 |
| `show_in_menu` | Yes | `1` | 0/1 |
| `menu_order` | Yes | `10` | Thứ tự SKU trong brand |

Example:

| sku_code | brand_code | display_name | price_vnd | duration_days | warranty_days | account_type | slot_per_credential | delivery_type | active | show_in_menu | menu_order |
|---|---|---|---:|---:|---:|---|---:|---|---:|---:|---:|
| CHATGPT_PLUS_PRIVATE_1M | CHATGPT | ChatGPT Plus Private 1 Month | 70000 | 30 | 7 | private | 1 | account | 1 | 1 | 10 |
| GEMINI_AI_SHARED_1M | GEMINI | Gemini AI Shared 1 Month | 50000 | 30 | 7 | shared | 2 | account | 1 | 1 | 10 |
| CAPCUT_PRO_PRIVATE_1M | CAPCUT | CapCut Pro Private 1 Month | 400000 | 30 | 30 | private | 1 | account | 1 | 1 | 10 |

#### Sheet `INVENTORY`

| Column | Required | Example | Note |
|---|---:|---|---|
| `sku_code` | Yes | `GEMINI_AI_SHARED_1M` | Must exist in `PRODUCTS` |
| `email` | No | `user@example.com` | Required for account template if template uses it |
| `password` | No | `Pass123` | Required if template uses it |
| `secret_2fa` | No | `JBSWY3DPEHPK3PXP` | Optional |
| `recovery` | No | `recovery@example.com` | Optional |
| `login_url` | No | `https://...` | Optional/link products |
| `note` | No | `Use profile 1` | Internal or customer note |
| `slot_label` | No | `USER_A` | Optional; script can auto-create |
| `status` | Yes | `available` | Import usually expects available |

Example:

| sku_code | email | password | secret_2fa | recovery | login_url | note | slot_label | status |
|---|---|---|---|---|---|---|---|---|
| CHATGPT_PLUS_PRIVATE_1M | chatgpt01@example.com | Pass123 | ABCDEF | recovery@example.com |  | Private account |  | available |
| GEMINI_AI_SHARED_1M | gemini01@example.com | Pass123 |  |  |  | Shared Gemini | SLOT_1 | available |
| GEMINI_AI_SHARED_1M | gemini01@example.com | Pass123 |  |  |  | Shared Gemini | SLOT_2 | available |
| CAPCUT_PRO_PRIVATE_1M | capcut01@example.com | Pass123 |  |  | https://capcut.com/login | Private CapCut |  | available |

Importer rule:

- `PRODUCTS.slot_per_credential` decides how many slots can be created per credential.
- If `INVENTORY` has one row per credential, importer may generate slots automatically.
- If `INVENTORY` has one row per slot, importer validates it does not exceed `slot_per_credential`.
- Recommended for staff: one row per slot for shared products, because it is easier to audit.

---

## 5. Ảnh Hưởng Nếu Nâng Cấp

| Phần | Ảnh hưởng | Rủi ro |
|---|---|---|
| Database migration | Cần thêm `brands`, `skus`, `inventory_credentials`, `inventory_slots`; giữ orders cũ | HIGH |
| Import script | Viết importer VS2 mới; không nên sửa script production 3 product | HIGH |
| Telegram menu | Đổi từ brand list hard-code sang brand/SKU DB-driven | HIGH |
| Product detail | Hiển thị list SKU theo brand, giá/warranty/duration từng SKU | HIGH |
| QR/payment amount | Phải lấy `sku.price_vnd * quantity`, snapshot vào order | MEDIUM |
| Order creation | Order cần lưu `sku_code`, brand, price snapshot, warranty snapshot | HIGH |
| Reservation | Reserve `inventory_slots`, không reserve `inventory_items` trực tiếp | HIGH |
| Auto delivery | Render từ `inventory_credentials` + `inventory_slots` + `delivery_template` | HIGH |
| Purchase history | Hiển thị brand/SKU/order cũ và mới | MEDIUM |
| Admin/report | Báo cáo theo brand, SKU, slot, status | MEDIUM |
| Existing delivered orders | Không được migrate phá lịch sử; cần compatibility read-only | HIGH |
| README_OPERATION | Viết tài liệu vận hành VS2 mới | MEDIUM |
| Tests | Cần test migration/import/menu/order/payment/delivery/slot | HIGH |

### Ước Lượng Thời Gian

| Cách làm | Thời gian | Rủi ro | Ghi chú |
|---|---:|---|---|
| Làm nhanh nhưng rủi ro | 2-4 ngày | HIGH | Vá schema hiện tại, thêm SKU bằng convention; dễ phát sinh lỗi production |
| Làm chuẩn trong repo hiện tại | 7-12 ngày | MEDIUM/HIGH | Cần migration, compatibility, tests đầy đủ |
| Làm BOT VS2 riêng | 5-10 ngày MVP, 10-15 ngày production-ready | MEDIUM | Không đụng bot production; có thể reuse payment/delivery concepts |

---

## 6. Chiến Lược An Toàn

### Phương Án 1 - Mở Rộng Bot Hiện Tại

Mô tả: sửa trực tiếp code hiện tại để hỗ trợ brand/SKU/slot.

Ưu điểm:

- Không cần service mới.
- Tận dụng token, DB, webhook hiện tại.
- Ít thay đổi vận hành ban đầu.

Nhược điểm:

- Rủi ro cao với production đang bán hàng.
- Migration phức tạp.
- Dễ phá luồng 3 product đang ổn.
- Rollback khó nếu DB đã migrate.

Khi nên chọn:

- Chỉ khi catalog mở rộng rất ít.
- Có staging DB giống production.
- Có backup/rollback rõ.

### Phương Án 2 - Tạo Branch `v2` Trên Repo Hiện Tại

Mô tả: tạo branch `v2`, phát triển kiến trúc mới cùng repo.

Ưu điểm:

- Không ảnh hưởng `main` production.
- Reuse test/config/dependency hiện tại.
- Dễ diff và cherry-pick bugfix.

Nhược điểm:

- Vẫn dễ nhầm deploy nếu Render trỏ sai branch.
- Repo có thể phình to vì tồn tại cả V1 và V2.
- Cần discipline cao về branch/deploy.

Khi nên chọn:

- Muốn giữ lịch sử chung.
- Có thể tạo Render service staging riêng trỏ branch `v2`.

### Phương Án 3 - BOT VS2 Riêng, Chạy Song Song Token/Test DB Riêng

Mô tả: copy phần cần thiết từ bot cũ sang dự án/service VS2, dùng bot token riêng và DB riêng.

Ưu điểm:

- An toàn nhất cho production hiện tại.
- Thiết kế schema sạch từ đầu.
- Test end-to-end mà không ảnh hưởng khách thật.
- Có thể chuyển traffic khi VS2 ổn.

Nhược điểm:

- Cần cấu hình bot token/webhook/payment riêng.
- Có một số phần phải copy/refactor.
- Cần quy trình cutover sau này.

Khi nên chọn:

- Catalog rộng nhiều brand/SKU.
- Cần đổi mô hình dữ liệu lớn.
- Không muốn mất doanh thu do lỗi production.

Khuyến nghị: **chọn phương án 3**.

---

## 7. BOT VS2 - Copy Gì, Viết Lại Gì, Giữ Gì

### 7.0. Migration Boundary: V1 vs V2

Boundary bắt buộc:

```text
V1 production remains untouched.
V2 is a new bot/project: telegram-auto-sell-bot-v2.
V2 uses its own bot token, Render service, database, Excel file, and import command.
No in-place migration on /var/data/store.db during V2 development.
```

#### Copy From V1

| Copy từ V1 | Copy mức nào | Ghi chú |
|---|---|---|
| Telegram app bootstrap | Partial | Chỉ copy cấu trúc start app/handler cơ bản |
| Safe edit/send helper | Direct/adapt | Giữ logic không crash khi callback từ QR/photo |
| Navigation UX | Concept | Giữ nguyên tắc global navigation |
| Payment config | Concept/direct | Bank name/account/account name env vẫn dùng được |
| VietQR URL builder | Adapt | Amount/order_id lấy từ V2 order snapshot |
| SePay payload parser | Adapt | Parser dùng lại, matcher phải viết lại theo V2 orders |
| Logging style | Direct/adapt | Giữ log payment/delivery rõ ràng |
| Tests payment/delivery/navigation | Adapt | Chuyển expected data sang SKU/slot |

#### Must Be Redesigned

| Phần phải thiết kế lại | Lý do |
|---|---|
| Database schema | V1 không tách Brand/SKU/Credential/Slot |
| Repository layer | Query/reservation/delivery phải theo SKU slot |
| Excel importer | V2 dùng `catalog_vs2.xlsx`, không dùng reset 3 product |
| Menu renderer | Brand/SKU DB-driven, không hard-code `PRODUCT_ORDER` |
| Product detail | Hiển thị SKU list theo brand, mỗi SKU có giá/warranty riêng |
| Order model | Order phải snapshot brand/sku/price/warranty/account_type |
| Reservation | Reserve `inventory_slots`, không reserve `inventory_items` |
| Delivery | Render `delivery_template`, không giao raw `secret_value` string |
| Admin/report | Báo cáo theo brand, SKU, credential, slot, status |
| Operation manual | Viết `README_OPERATION_V2.md` riêng |

#### Keep Untouched In V1

| Thành phần V1 | Quyết định |
|---|---|
| `telegram_license_bot.py` production | Không sửa để làm VS2 |
| `repository/store_repository.py` production | Không migrate schema VS2 vào đây |
| `scripts/reset_and_import_inventory.py` | Giữ phục vụ V1 CHATGPT/GEMINI/CAPCUT |
| `/var/data/store.db` production | Không ALTER/MIGRATE cho VS2 |
| Existing delivered orders | Giữ read-only trong V1 |
| Render production service | Không đổi sang V2 cho đến cutover |

### 7.1. Nên Copy/Tái Sử Dụng

| Thành phần | Lý do |
|---|---|
| Payment config / VietQR builder concept | Luồng QR hiện đã hoạt động |
| SePay webhook parsing concept | Có logic nhận amount/description/transaction_id |
| Safe Telegram edit/send helper | Tránh crash khi callback từ photo/QR |
| Navigation keyboard concept | Menu global đã ổn UX hơn |
| Order TTL/reservation concept | Cần giữ hàng trong thời gian thanh toán |
| Logging pattern cho payment/delivery | Rất cần khi debug production |
| Một phần tests payment/delivery | Có thể chuyển sang VS2 |

### 7.2. Phải Viết Lại

| Thành phần | Lý do |
|---|---|
| Data model repository | Cần brand/SKU/credential/slot rõ ràng |
| Importer | Excel VS2 khác hoàn toàn source of truth hiện tại |
| Product menu | Menu phải DB-driven theo brand/SKU, không hard-code `PRODUCT_ORDER` |
| Product detail/SKU selection | Brand có nhiều SKU với giá/warranty khác nhau |
| Reservation | Reserve slot, không reserve product item trực tiếp |
| Delivery renderer | Dùng `delivery_template` theo SKU |
| Admin/report | Cần báo cáo brand/SKU/slot |
| Migration/cutover tooling | Cần giữ lịch sử V1, chuyển data an toàn nếu cần |

### 7.3. Có Thể Giữ Nguyên Tạm Thời

| Thành phần | Điều kiện |
|---|---|
| License/tool flow | Nếu không thuộc catalog account VS2 |
| Payment provider config | Nếu dùng cùng ngân hàng/provider |
| Webhook endpoint shape | Nếu provider không đổi |
| Purchase history UI cơ bản | Nhưng data source cần hỗ trợ V2 orders |

---

## 8. V2 Development Phases

Mục tiêu là xây dựng `telegram-auto-sell-bot-v2` theo từng lớp, không rewrite lẫn vào V1.

### Phase 1 - Schema + Excel

Thời gian: 1-2 ngày.

Deliverables:

- Repo/project `telegram-auto-sell-bot-v2`.
- SQLite schema V2:
  - `brands`
  - `skus`
  - `inventory_credentials`
  - `inventory_slots`
  - `orders`
  - `order_slots`
  - `payment_transactions`
  - `inventory_movements`
- Excel chuẩn:
  - `imports/catalog_vs2.xlsx`
  - Sheet `BRANDS`
  - Sheet `PRODUCTS`
  - Sheet `INVENTORY`
- Seed file mẫu có CHATGPT/GEMINI/CAPCUT.

Acceptance:

- Tạo DB rỗng thành công.
- Có thể validate Excel headers.
- Không có dependency vào V1 production DB.

### Phase 2 - Import

Thời gian: 1-2 ngày.

Deliverables:

- Importer V2 đọc `catalog_vs2.xlsx`.
- Import brands/SKUs.
- Import credentials.
- Generate hoặc validate slots theo `slot_per_credential`.
- Report rõ:
  - brands imported
  - SKUs imported
  - credentials imported
  - slots available
  - duplicates skipped
  - invalid rows

Acceptance:

- CHATGPT private 1 credential -> 1 slot.
- GEMINI shared 1 credential -> 2 slots.
- CAPCUT private 1 credential -> 1 slot.
- Duplicate trong cùng batch bị phát hiện.
- Delivered history không bị overwrite trong các lần import sau.

### Phase 3 - Menu + Reservation

Thời gian: 2-3 ngày.

Deliverables:

- Telegram menu brand list từ `brands`.
- Brand detail hiển thị SKU list từ `skus`.
- SKU detail hiển thị:
  - price
  - duration
  - warranty
  - account_type
  - available slots
- Quantity selection.
- Order creation snapshot theo SKU.
- Reservation trên `inventory_slots`.

Acceptance:

```text
/start
  -> Brand list
  -> CHATGPT
  -> CHATGPT_PLUS_PRIVATE_1M
  -> quantity
  -> order reserved
```

Stock phải đếm từ:

```sql
inventory_slots.status = 'available'
```

Không đếm brand, không đếm unique email, không đọc Excel runtime.

### Phase 4 - Payment + Webhook + Delivery

Thời gian: 2-3 ngày.

Deliverables:

- QR amount lấy từ `orders.total_vnd`.
- QR content dùng `orders.order_id`.
- SePay webhook match trực tiếp V2 order.
- Mark paid.
- Deliver reserved slots.
- Render message bằng `skus.delivery_template`.
- Mark slots delivered.
- Mark order delivered.

Acceptance:

```text
Start -> Brand -> SKU -> QR -> webhook paid -> auto delivery -> stock giảm đúng
```

Log bắt buộc:

```text
WEBHOOK RECEIVED
ORDER MATCHED
ORDER MARKED PAID
DELIVERY START
SLOT SELECTED
DELIVERY SENT SUCCESS/FAIL
ORDER FINAL STATUS
```

### Phase 5 - Admin / History / Testing

Thời gian: 2-4 ngày.

Deliverables:

- Purchase history theo V2 orders.
- Admin/report theo:
  - brand
  - SKU
  - available/reserved/delivered/disabled slots
  - revenue/order status
- Manual delivery fallback nếu SKU `delivery_type=manual`.
- Tests:
  - schema
  - Excel import
  - SKU stock counts
  - reservation race/conflict
  - payment webhook
  - delivery template
  - purchase history
  - navigation from QR/photo/cancel/delivery
- `README_OPERATION_V2.md`.

Acceptance:

- Có thể pilot 3 brand: CHATGPT, GEMINI, CAPCUT.
- Có ít nhất 2 SKU/brand trong test data.
- End-to-end pass trước khi thêm catalog rộng.

---

## 9. Kết Luận Đề Xuất

### Final Decision

```text
V1 remains untouched.
V2 should be built as telegram-auto-sell-bot-v2.
```

V1 tiếp tục phục vụ production hiện tại. V2 được phát triển độc lập với:

- bot token riêng;
- Render service riêng;
- SQLite database riêng;
- Excel `imports/catalog_vs2.xlsx` riêng;
- importer riêng;
- operation manual riêng.

### Có nên sửa trực tiếp production hiện tại không?

**Không.**

Lý do: production hiện tại vừa ổn định lại sau nhiều lỗi về import/menu/navigation. Mở rộng catalog lớn sẽ chạm schema, importer, menu, order, reservation, delivery, webhook. Rủi ro làm hỏng luồng đang bán thật là cao.

### Có nên tạo BOT VS2 riêng không?

**Có.**

Đây là hướng an toàn nhất vì VS2 cần mô hình dữ liệu khác: Brand -> SKU -> Credential -> Slot.

Tên project/repo/service đề xuất:

```text
telegram-auto-sell-bot-v2
```

### Nếu tạo BOT VS2 riêng thì copy những phần nào từ bot cũ?

Copy có chọn lọc:

- Telegram bootstrap cơ bản.
- Safe edit/send.
- Navigation keyboard UX.
- Payment config và VietQR builder concept.
- SePay webhook parsing concept.
- Logging payment/delivery.
- Một phần test cases payment/delivery/navigation.

### Những phần nào phải viết lại?

- Repository/data model.
- Import Excel.
- Menu brand/SKU.
- Product detail.
- Order creation.
- Reservation slot.
- Delivery template renderer.
- Admin/report theo SKU.
- README_OPERATION VS2.

### Những phần nào giữ nguyên?

- Production bot hiện tại.
- Production DB hiện tại.
- Production import script hiện tại.
- Production Render service hiện tại.
- Existing delivered orders ở V1.
- README_OPERATION.md của V1.

### Làm sao để không mất thêm 15 ngày mà vẫn tiến đúng hướng?

Không rewrite toàn bộ bot ngay. Làm VS2 MVP theo 5 mốc:

```text
1. Schema + Excel
2. Import
3. Menu + reservation
4. Payment + webhook + delivery
5. Admin/history/testing
```

Không migrate production cho đến khi VS2 đã test end-to-end:

```text
Start -> Brand -> SKU -> QR -> webhook paid -> auto delivery -> stock giảm đúng
```

Định nghĩa thành công của VS2:

- Không hard-code CHATGPT/GEMINI/CAPCUT.
- Không hard-code GEMINI = 2 slot.
- Mỗi SKU tự định nghĩa giá, warranty, duration, account_type, slot_per_credential, delivery_template.
- Inventory gắn với SKU.
- Delivery gắn với slot đã reserve.
- Excel vẫn là Source of Truth.
