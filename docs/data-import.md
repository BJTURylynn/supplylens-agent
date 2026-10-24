# Excel / CSV 采购快照导入

页面侧栏选择“分析截至日期 / 导入快照日期”，展开“Excel / CSV 数据导入”：下载模板、填写数据、上传、预览并校验，最后确认导入。预览不会写业务表；预览有效期为十五分钟。

- [Excel 模板](../data/templates/supplylens-import-template.xlsx)：一个文件，六张业务工作表。
- [CSV 模板包](../data/templates/supplylens-import-csv.zip)：先解压，再一起上传 CSV 文件。
- 模板中 `PO-9001` 是格式示例，不是真实企业数据。正式使用前替换示例行。

每批必须包含 `suppliers` 和 `orders`，其他四类表可选。**保留只有表头的空表，表示确认该类记录为空；完全没有提供该表，表示数据未知。** 例如没有提供收货表时，已收、未收数量及逾期天数均显示未知，系统不会把它算成零收货。

## 字段与关联

工作表名 / CSV 文件名使用下表名称。第一行为列名，数据从第二行开始；不支持公式、宏或自动猜测字段含义。

| 表 | 字段 |
|---|---|
| `suppliers` | `id`, `name` |
| `orders` | `id`, `supplier_id`, `material_code`, `qty`, `promised_date`, `created_at` |
| `shipments` | `id`, `order_id`, `qty`, `shipped_at` |
| `eta_history` | `id`, `shipment_id`, `eta`, `updated_at` |
| `logistics_events` | `id`, `shipment_id`, `status`, `event_time`, `note`（备注可留空） |
| `receipts` | `id`, `shipment_id`, `qty`, `received_at` |

`orders.supplier_id` 对应供应商 ID；发运单关联订单；ETA、物流事件、收货记录都关联发运单。每张订单只有一种物料，允许多次发运和收货。没有 ETA 时可不填预测行，保留 `eta_history` 表头即可。

数量用正整数，单位为件。所有编号用文本，保留前导零。关联编号的大小写要完全一致；系统拒绝仅大小写不同的重复编号，以兼容 SQLite 与 MySQL。日期使用 `YYYY-MM-DD`，CSV 时间可使用 `2026-10-24T09:00:00+08:00`；Excel 日期时间按上海当地时间解释。

支持常见中文表名和列名，例如“供应商 / 采购订单 / 发运单 / 预计到货 / 物流事件 / 收货记录”，“采购单号 / 供应商编号 / 物料编码 / 采购数量 / 承诺到货日 / 创建时间”。可以直接使用下载模板中的英文列名，减少歧义。额外列需移除后再导入。

物流状态支持 `departed`、`in_transit`、`delayed`、`held`、`customs_hold`、`exception`、`delivered`、`arrived`，或对应中文“已发运、在途、延误、滞留、清关滞留、异常、已签收、已到达”。

## 快照与更新范围

- “截至日期”表示源文件能够覆盖到哪一天，按 Asia/Shanghai 当日结束分析；未来事件不会用于过去的判断。
- 分析日超过导入快照覆盖日时，后续收货状态显示未知，不能据旧快照认定仍未收货。
- 新订单直接导入。同名**导入订单**会替换其发运、ETA、物流、收货快照；其他订单和历史分析报告保留。
- 内置模拟订单不能被导入覆盖。与其他订单共用的发运编号、记录编号或供应商名称也不能被误覆盖。
- 再次导入前必须重新预览。预览后相关数据发生变化，会拒绝使用旧预览。
- 单文件最多 5 MB，合计 20 MB；每批最多 500 张订单，每类表最多 10000 行。

导入来源会出现在订单列表、报告、下载 Markdown 和证据中，标记为“用户导入数据”。该标识只说明来源，不表示数据真实性已经独立核验；默认检索资料仍是项目自编模拟 SOP。

## 使用独立业务数据库

若希望只显示导入数据，可在本地 `.env` 配置：

```dotenv
DATABASE_URL=sqlite:///./runtime/business.db
AUTO_SEED=false
```

然后重启 API。使用 MySQL 时同样可设置 `AUTO_SEED=false`。服务重启不会重新导入样例覆盖已有订单。实际文件保存在项目以外或 `data/private/` / `uploads/`，这些目录及运行数据库已被 Git 忽略。

## 命令行

```bash
# 只预览
uv run supplylens import-data data/templates/supplylens-import-template.xlsx --as-of 2026-10-24

# 确认后导入
uv run supplylens import-data data/templates/supplylens-import-template.xlsx --as-of 2026-10-24 --commit
```

CSV 可以把六个文件路径一起传给 `import-data`。命令默认只预览；`--commit` 表示确认源文件范围及更新同名订单。

API：`POST /api/imports/preview` 使用 multipart `files` 和 `as_of`；返回预览 ID、各表行数、样例、覆盖范围和警告。`POST /api/imports/commit` 接收 `preview_id` 与 `confirm_snapshot=true`。`POST /api/chat` 可额外传入 `as_of` 分析日期，标准订单号以外的企业编号也可通过自然语言指定。

