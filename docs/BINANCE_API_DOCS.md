# Binance Futures Order API — Reference Guide

> วิธีเรียก Binance Futures API เพื่อเปิด/ปิด Order ทั้ง Demo และ Live Mode

---

## 1. Base URL

| Mode | URL |
|---|---|
| **Demo** (เงินจำลอง) | `https://demo-fapi.binance.com` |
| **Live** (เงินจริง) | `https://fapi.binance.com` |

โค้ดและ endpoint path **เหมือนกันทุกประการ** — เปลี่ยนแค่ Base URL กับ API Key

---

## 2. API Key

| Mode | วิธีสร้าง |
|---|---|
| Demo | สร้างจาก [Binance Testnet](https://testnet.binancefuture.com/) |
| Live | สร้างจาก [Binance API Management](https://www.binance.com/en/my/settings/api-management) |

---

## 3. Authentication (Signed Request)

ทุก request ที่เกี่ยวกับบัญชี/order ต้อง signed:

### Header

```
X-MBX-APIKEY: <API_KEY>
```

### Signature

1. เพิ่ม `timestamp` (Unix ms) เข้าไปใน params
2. สร้าง query string จาก params ทั้งหมด
3. HMAC SHA256 ด้วย API Secret
4. แนบ `signature` เป็น param สุดท้าย

```python
import hmac, hashlib, time

params["timestamp"] = int(time.time() * 1000)
query_string = "&".join([f"{k}={v}" for k, v in params.items()])
signature = hmac.new(
    api_secret.encode("utf-8"),
    query_string.encode("utf-8"),
    hashlib.sha256
).hexdigest()
params["signature"] = signature
```

---

## 4. ตั้ง Leverage

```
POST /fapi/v1/leverage  [SIGNED]
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | STRING | ✅ | เช่น `BTCUSDT` |
| `leverage` | INT | ✅ | 1–125 |

---

## 5. เปิด Position (Market Order)

```
POST /fapi/v1/order  [SIGNED]
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | STRING | ✅ | เช่น `BTCUSDT` |
| `side` | STRING | ✅ | `BUY` = Long, `SELL` = Short |
| `type` | STRING | ✅ | `MARKET` |
| `quantity` | DECIMAL | ✅ | จำนวน base asset |
| `newOrderRespType` | STRING | ❌ | `RESULT` เพื่อให้ return avgPrice |

### ตัวอย่าง

```
# เปิด Long 0.001 BTC
POST /fapi/v1/order
symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.001

# เปิด Short 0.01 ETH
POST /fapi/v1/order
symbol=ETHUSDT&side=SELL&type=MARKET&quantity=0.01
```

### Response

```json
{
    "orderId": 123456789,
    "symbol": "BTCUSDT",
    "status": "FILLED",
    "side": "BUY",
    "type": "MARKET",
    "origQty": "0.001",
    "executedQty": "0.001",
    "avgPrice": "67500.00"
}
```

---

## 6. วาง Stop Loss (Algo Order)

```
POST /fapi/v1/algoOrder  [SIGNED]
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | STRING | ✅ | |
| `side` | STRING | ✅ | **ตรงข้ามกับ position** — `SELL` ถ้า Long, `BUY` ถ้า Short |
| `type` | STRING | ✅ | `STOP_MARKET` |
| `triggerPrice` | DECIMAL | ✅ | ราคาที่จะ trigger |
| `closePosition` | BOOLEAN | ✅ | `true` = ปิดทั้ง position |
| `timeInForce` | STRING | ✅ | `GTC` |
| `algoType` | STRING | ✅ | `CONDITIONAL` |

### ตัวอย่าง

```
# SL สำหรับ Long BTC entry $67,500 → SL ที่ $67,230
POST /fapi/v1/algoOrder
symbol=BTCUSDT&side=SELL&type=STOP_MARKET&triggerPrice=67230
&closePosition=true&timeInForce=GTC&algoType=CONDITIONAL

# SL สำหรับ Short ETH entry $3,200 → SL ที่ $3,213
POST /fapi/v1/algoOrder
symbol=ETHUSDT&side=BUY&type=STOP_MARKET&triggerPrice=3213
&closePosition=true&timeInForce=GTC&algoType=CONDITIONAL
```

---

## 7. วาง Take Profit (Algo Order)

```
POST /fapi/v1/algoOrder  [SIGNED]
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | STRING | ✅ | |
| `side` | STRING | ✅ | **ตรงข้ามกับ position** |
| `type` | STRING | ✅ | `TAKE_PROFIT_MARKET` |
| `triggerPrice` | DECIMAL | ✅ | ราคาที่จะ trigger |
| `closePosition` | BOOLEAN | ✅ | `true` |
| `timeInForce` | STRING | ✅ | `GTC` |
| `algoType` | STRING | ✅ | `CONDITIONAL` |

### ตัวอย่าง

```
# TP สำหรับ Long BTC entry $67,500 → TP ที่ $68,175
POST /fapi/v1/algoOrder
symbol=BTCUSDT&side=SELL&type=TAKE_PROFIT_MARKET&triggerPrice=68175
&closePosition=true&timeInForce=GTC&algoType=CONDITIONAL
```

---

## 8. ปิด Position

ใช้ Market Order ด้านตรงข้าม:

```
POST /fapi/v1/order  [SIGNED]
```

| Position | side | quantity |
|---|---|---|
| ปิด Long | `SELL` | `abs(position_amt)` |
| ปิด Short | `BUY` | `abs(position_amt)` |

---

## 9. ยกเลิก Order ทั้งหมด

```
DELETE /fapi/v1/allOpenOrders  [SIGNED]
```

| Parameter | Type | Required |
|---|---|---|
| `symbol` | STRING | ✅ |

---

## 10. Endpoints อ่านข้อมูล

| Endpoint | Method | Signed | Description |
|---|---|---|---|
| `/fapi/v1/ping` | GET | ❌ | ทดสอบ connection |
| `/fapi/v1/ticker/price` | GET | ❌ | ราคาปัจจุบัน |
| `/fapi/v1/ticker/24hr` | GET | ❌ | สถิติ 24 ชม. |
| `/fapi/v1/klines` | GET | ❌ | ข้อมูลแท่งเทียน |
| `/fapi/v2/balance` | GET | ✅ | ยอดเงินในบัญชี |
| `/fapi/v2/positionRisk` | GET | ✅ | Position ที่เปิดอยู่ |
| `/fapi/v1/openOrders` | GET | ✅ | Order ที่ค้างอยู่ |
| `/fapi/v1/premiumIndex` | GET | ❌ | Funding rate ปัจจุบัน |
| `/fapi/v1/fundingRate` | GET | ❌ | Funding rate history |
| `/fapi/v1/openInterest` | GET | ❌ | Open interest |
| `/futures/data/globalLongShortAccountRatio` | GET | ❌ | Long/Short ratio |

---

## 11. Flow เปิด Order แบบสมบูรณ์

```
1. SET LEVERAGE
   POST /fapi/v1/leverage
   └─ symbol, leverage
          │
2. OPEN POSITION (Market Order)
   POST /fapi/v1/order
   └─ symbol, side, type=MARKET, quantity
          │
3. PLACE STOP LOSS
   POST /fapi/v1/algoOrder
   └─ type=STOP_MARKET, triggerPrice, closePosition=true
          │
4. PLACE TAKE PROFIT
   POST /fapi/v1/algoOrder
   └─ type=TAKE_PROFIT_MARKET, triggerPrice, closePosition=true
          │
5. MONITOR
   GET /fapi/v2/positionRisk  (วนเช็ค)
   GET /fapi/v1/ticker/price
          │
6. CLOSE POSITION (ถ้าต้องการปิดเอง)
   POST /fapi/v1/order  (side ตรงข้าม)
   DELETE /fapi/v1/allOpenOrders  (ล้าง SL/TP ที่ค้าง)
```

---

## 12. คำนวณ TP/SL

```python
# Long (BUY)
take_profit = entry_price * (1 + tp_percent / 100)
stop_loss   = entry_price * (1 - sl_percent / 100)

# Short (SELL)
take_profit = entry_price * (1 - tp_percent / 100)
stop_loss   = entry_price * (1 + sl_percent / 100)
```

---

## 13. Quantity Precision

| Symbol | Decimal Places | ตัวอย่าง Min |
|---|---|---|
| BTCUSDT | 3 | 0.001 |
| ETHUSDT | 3 | 0.001 |
| SOLUSDT | 0–2 | 1 |
| Altcoins ทั่วไป | 0–2 | แล้วแต่เหรียญ |

> ดู precision ที่แน่นอนได้จาก `GET /fapi/v1/exchangeInfo`

---

## 14. Common Errors

| Code | สาเหตุ | วิธีแก้ |
|---|---|---|
| `-1021` | Timestamp ไม่ sync | ตรวจ system clock |
| `-1022` | Signature ผิด | ตรวจ API Secret + ลำดับ params |
| `-2019` | Margin ไม่พอ | ลด quantity หรือ leverage |
| `-4003` | Quantity ต่ำกว่า minimum | เพิ่ม quantity |
| `-4014` | Price precision ผิด | ปรับ decimal places |

---

## 15. สรุป Demo vs Live

| หัวข้อ | Demo | Live |
|---|---|---|
| Base URL | `https://demo-fapi.binance.com` | `https://fapi.binance.com` |
| API Key | จาก Testnet portal | จาก Binance จริง |
| เงิน | จำลอง | จริง |
| Endpoint paths | **เหมือนกัน** | **เหมือนกัน** |
| Signature method | **เหมือนกัน** | **เหมือนกัน** |
| Order types | **เหมือนกัน** | **เหมือนกัน** |
