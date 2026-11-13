# ESP32 電燈開關

ESP32-C3 透過家用 Wi‑Fi 接收指令，再用 MG90S 舵機從外側按牆壁開關。你可以開 ESP32 的網頁，也可以在 Telegram 私聊對 Hermes 機器人說「開燈」、「關燈」或「狀態」。Hermes 跑在能連到 ESP32 的電腦上。

- [ESP32 韌體](firmware/WallSwitch/WallSwitch.ino)
- [Hermes Telegram 外掛](hermes/wall-switch-telegram/)

## 接線

本專案的接線都在 USB 低壓側。舵機固定在牆壁開關外側。先拔掉 USB 電源再接線。

| 電源或訊號 | 接到哪裡 |
| --- | --- |
| 綠色 USB 取電端子的 5 V 正極 | ESP32 的 `5V` 腳與舵機紅線，從正極 WAGO 各拉一條 |
| 綠色 USB 取電端子的負極 | ESP32 的 `GND` 與舵機棕線，從負極 WAGO 各拉一條 |
| ESP32 的 `GPIO4` | 舵機訊號線，MG90S 常見為橙色 |

ESP32 由 `5V` 排針供電時，板子的 USB‑C 留空。要用電腦 USB 上傳韌體，先拔掉綠色端子的電源，並取下板上 `5V` 的外接線。使用 WAGO 221‑413 時，請依原廠規格選線與剝線。這個裝置在換掉 WAGO 到 ESP32 的兩條細供電線後，才通過共用 5 V 電源的測試。

## 燒錄與連線

1. 在 Arduino IDE 安裝 Espressif 的 ESP32 板卡套件，開啟 `firmware/WallSwitch/WallSwitch.ino`。實測使用 3.3.12，板型選 **ESP32C3 Dev Module**，啟用 **USB CDC On Boot**。
2. 上傳後打開 115200 baud 的序列監控。首次啟動會顯示 `AP_SSID`、`SETUP_KEY` 和 `SETUP_URL`。將 `SETUP_KEY` 存在本機。
3. 手機加入 `AP_SSID`，用 `SETUP_KEY` 當密碼，再打開 `SETUP_URL`，填入家中 2.4 GHz Wi‑Fi 的名稱與密碼。
4. 配對後讓手機切回家中 Wi‑Fi，等約 90 秒讓設定熱點關閉，再開序列監控顯示的 `LAN_URL`。帳號是 `light`，密碼是 `SETUP_KEY`。
5. 舵臂離開牆壁開關時，先測對中、開燈、關燈。調整固定位置和行程，讓舵臂能按下開關並回中。

## 連接 Hermes 和 Telegram

1. 在能連到 ESP32 控制頁的電腦上設定 Hermes 的 Telegram 頻道。將你的 Telegram 使用者 ID 加到該設定檔的 `TELEGRAM_ALLOWED_USERS`。實測使用 Hermes Agent 0.23.0。
2. 在該設定檔執行 `hermes --profile wall-switch config path`，找出設定檔資料夾。把 `hermes/wall-switch-telegram` 複製到該資料夾的 `plugins` 下面。若設定檔名稱不同，指令和下方設定都換成實際名稱。
3. 把 `device-credential.example.txt` 複製成 `device-credential.txt`，只替換尖括號內的內容，保留「設定密碼」後的全形空格。
4. 把 `settings.example.json` 複製成 `settings.json`，改成自己的設定。

   | 欄位 | 填入內容 |
   | --- | --- |
   | `address` | `LAN_URL` 裡的家用區網 IPv4，只填位址 |
   | `credential_file` | `device-credential.txt` 的絕對路徑 |
   | `telegram_env_file` | 該 Hermes 設定檔的 `.env` 絕對路徑 |
   | `telegram_profile` | 實際使用的 Hermes 設定檔名稱 |

5. 執行 `hermes --profile wall-switch plugins enable wall-switch-telegram`。Gateway 運作後，在授權帳號和機器人的私聊傳「狀態」，再測「開燈」和「關燈」。也可以用 `/light status`、`/light on` 和 `/light off`。

「狀態」查的是控制頁能否連線。ESP32 可以回報舵機指令已完成，燈泡實際亮熄要以現場看到的為準。使用 Telegram 時，執行 Hermes Gateway 的電腦與 ESP32 都要保持上線。

## 連線穩定性與自動檢查

- 韌體用 `WiFi.setSleep(false)` 關掉 Wi‑Fi 省電，避免 ESP32 偶爾來不及回應。改韌體時不要拿掉。
- Hermes 外掛連不到 ESP32 時，會在約 15 秒內重試查詢控制頁；送出開關燈的指令只送一次，不會重送，避免舵機連按兩次。
- 已存好的 Wi‑Fi 連不上超過 2 分鐘，ESP32 會自己重開；程式卡住 30 秒，看門狗（自動重開保護）也會讓它重開。用設定熱點配網時不會自動重開。
- 在 Telegram 傳「狀態」，會顯示已連續運作多久、上次啟動原因（通電、電壓不足、斷線自動重開、程式卡住等），以及 Wi‑Fi 重連了幾次，方便查斷線原因。
- 修改後執行 `python -m pytest tests`（需要 `pytest`、`requests`），確認上述幾點和目前舵機行程沒有被改掉。GitHub 也會在每個 PR 自動跑測試並編譯韌體。

裝置密碼、Telegram Token 和實際設定留在本機。對外分享時使用 repo 裡的空白範本。
