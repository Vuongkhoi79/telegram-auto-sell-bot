# telegram-auto-sell-bot

## Reset and import inventory

Use `scripts/reset_and_import_inventory.py` as the only workflow for reloading CHATGPT, GEMINI, and CAPCUT stock. Do not edit SQLite inventory manually.

### WINDOWS

Dat 2 file Excel vao `imports` roi chay:

```powershell
git add imports/import_CHATGPT_ONLY_READY1.xlsx imports/import_inventory_ONE_SHEET_PER_PRODUCT.xlsx scripts/reset_and_import_inventory.py
git commit -m "Add reset and import inventory workflow"
git push
```

### RENDER

Sau deploy, chay:

```bash
python scripts/reset_and_import_inventory.py --database /var/data/store.db --chatgpt-file imports/import_CHATGPT_ONLY_READY1.xlsx --multi-file imports/import_inventory_ONE_SHEET_PER_PRODUCT.xlsx
```
Bot bán tool AI tự động bằng Telegram + chuyển khoản ngân hàng
