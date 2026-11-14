#include <Arduino.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_random.h>
#include <esp_system.h>
#include <esp_task_wdt.h>

constexpr uint8_t ServoPin = 4;
constexpr uint8_t PwmBits = 14;
constexpr uint16_t OnPulseUs = 1180;
constexpr uint16_t CenterPulseUs = 1500;
constexpr uint16_t OffPulseUs = 1767;
constexpr uint32_t JoinTimeoutMs = 15000;
constexpr uint32_t SetupGraceMs = 90000;
constexpr uint32_t SetupPrintIntervalMs = 5000;
// Restart if saved Wi-Fi stays unreachable this long; a reboot clears a stuck radio.
constexpr uint32_t OfflineRestartMs = 120000;
// Restart if loop() stops running this long (longest normal wait is the 5 s HTTP read).
constexpr uint32_t LoopWatchdogMs = 30000;
constexpr char StorageNamespace[] = "wall-switch";
constexpr char SetupKeyEntry[] = "setup-key";
constexpr char WifiEntry[] = "wifi-v1";

struct WifiCredentials {
  char ssid[33];
  char password[65];
};

enum class NetworkState { Setup, Joining, Joined, Online };

const IPAddress setupIp(192, 168, 4, 1);
const IPAddress noIp(0, 0, 0, 0);
WebServer server(80);
NetworkState networkState = NetworkState::Setup;
WifiCredentials credentials{};
char apSsid[24];
char hostname[24];
char setupKey[25];
char setupNonce[33];
char controlNonce[33];
uint32_t stateSince;
uint32_t lastSetupPrint;
bool apActive;
bool mdnsActive;
bool servoReady;
bool storageReady;
const char *setupMessage = "";
uint32_t offlineSince;
uint32_t rejoinCount;

void sendPage(int status, const String &content, bool refresh = false);
void showSetup(int status = 200);

void randomHex(char *output, size_t digits) {
  constexpr char hex[] = "0123456789abcdef";
  for (size_t i = 0; i < digits; i += 2) {
    uint8_t byte;
    esp_fill_random(&byte, sizeof(byte));
    output[i] = hex[byte >> 4];
    output[i + 1] = hex[byte & 15];
  }
  output[digits] = '\0';
}

bool loadSetupKey() {
  Preferences store;
  if (!store.begin(StorageNamespace, false)) {
    return false;
  }
  bool valid = store.getString(SetupKeyEntry, setupKey, sizeof(setupKey)) == sizeof(setupKey);
  if (!valid) {
    randomHex(setupKey, sizeof(setupKey) - 1);
    valid = store.putString(SetupKeyEntry, setupKey) == sizeof(setupKey) - 1;
  }
  store.end();
  return valid;
}

bool loadWifi() {
  Preferences store;
  if (!store.begin(StorageNamespace, true)) {
    return false;
  }
  const size_t size = store.getBytesLength(WifiEntry);
  const bool loaded = size == sizeof(credentials) &&
                      store.getBytes(WifiEntry, &credentials, sizeof(credentials)) == size;
  store.end();
  return loaded && credentials.ssid[0] && credentials.ssid[32] == '\0' &&
         credentials.password[64] == '\0';
}

bool saveWifi() {
  Preferences store;
  if (!store.begin(StorageNamespace, false)) {
    return false;
  }
  const bool saved = store.putBytes(WifiEntry, &credentials, sizeof(credentials)) == sizeof(credentials);
  store.end();
  return saved;
}

void printSetupInfo() {
  Serial.printf("AP_SSID=%s\nSETUP_KEY=%s\nSETUP_URL=http://192.168.4.1/\n", apSsid, setupKey);
  lastSetupPrint = millis();
}

void startSetup(const char *message) {
  if (mdnsActive) {
    MDNS.end();
    mdnsActive = false;
  }
  WiFi.disconnect();
  credentials = {};
  networkState = NetworkState::Setup;
  stateSince = millis();
  setupMessage = message;
  if (apActive) {
    return;
  }
  apActive = WiFi.mode(WIFI_AP_STA) &&
             WiFi.softAPConfig(setupIp, setupIp, IPAddress(255, 255, 255, 0)) &&
             WiFi.softAP(apSsid, setupKey);
  if (apActive) {
    printSetupInfo();
  } else {
    WiFi.mode(WIFI_STA);
    Serial.println("ERROR=SETUP_AP");
  }
}

void joinWifi() {
  WiFi.begin(credentials.ssid, credentials.password);
  networkState = NetworkState::Joining;
  stateSince = millis();
}

bool stationReady() {
  return WiFi.status() == WL_CONNECTED && WiFi.localIP() != noIp;
}

void announceStation() {
  mdnsActive = MDNS.begin(hostname);
  if (mdnsActive) {
    MDNS.addService("http", "tcp", 80);
    Serial.printf("MDNS_URL=http://%s.local/\n", hostname);
  }
  Serial.printf("LAN_URL=http://%s/\n", WiFi.localIP().toString().c_str());
}

void updateNetwork() {
  switch (networkState) {
    case NetworkState::Setup:
      if (!apActive && millis() - stateSince >= SetupPrintIntervalMs) {
        startSetup(setupMessage);
      }
      break;
    case NetworkState::Joining:
      if (stationReady()) {
        if (apActive && !saveWifi()) {
          startSetup("無法儲存 Wi-Fi 設定，請再試一次。");
          return;
        }
        announceStation();
        networkState = apActive ? NetworkState::Joined : NetworkState::Online;
        stateSince = millis();
      } else if (millis() - stateSince >= JoinTimeoutMs) {
        if (apActive) {
          startSetup("連線逾時。請確認是 2.4 GHz Wi-Fi 並檢查密碼後重試。");
        } else {
          WiFi.disconnect();
          joinWifi();
        }
      }
      break;
    case NetworkState::Joined:
    case NetworkState::Online:
      if (!stationReady()) {
        if (mdnsActive) {
          MDNS.end();
          mdnsActive = false;
        }
        rejoinCount++;
        WiFi.disconnect();
        joinWifi();
      } else if (networkState == NetworkState::Joined && millis() - stateSince >= SetupGraceMs) {
        if (WiFi.mode(WIFI_STA)) {
          apActive = false;
          networkState = NetworkState::Online;
          Serial.println("LAN_READY=1");
          Serial.printf("LAN_URL=http://%s/\n", WiFi.localIP().toString().c_str());
        }
      }
      break;
  }
}

void watchConnection() {
  // Only with saved Wi-Fi: never restart while someone is using the setup hotspot.
  const bool offline = !apActive && networkState != NetworkState::Setup && !stationReady();
  if (!offline) {
    offlineSince = 0;
  } else if (!offlineSince) {
    offlineSince = millis() | 1;
  } else if (millis() - offlineSince >= OfflineRestartMs) {
    Serial.println("RESTART=WIFI_OFFLINE");
    delay(100);
    ESP.restart();
  }
}

const char *resetReasonName() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SW: return "restart";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT: return "watchdog";
    case ESP_RST_EXT: return "button";
    default: return "other";
  }
}

bool fromSetupNetwork() {
  return apActive && server.client().localIP() == setupIp;
}

void sendPage(int status, const String &content, bool refresh) {
  String page = F("<!doctype html><html lang='zh-Hant'><meta charset='utf-8'>"
                  "<meta name='viewport' content='width=device-width, initial-scale=1'>");
  if (refresh) {
    page += F("<meta http-equiv='refresh' content='3;url=/'>");
  }
  page += F("<title>牆壁開關</title><style>"
            "body{font:1.1rem system-ui;max-width:28rem;margin:2rem auto;padding:0 1rem}"
            "label,input,button{display:block;margin:.6rem 0}"
            "input,button{box-sizing:border-box;font:inherit;padding:.7rem;width:100%}"
            "a{overflow-wrap:anywhere}</style><h1>牆壁開關</h1>");
  page += content;
  page += F("</html>");
  server.sendHeader("Cache-Control", "no-store");
  server.sendHeader("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'");
  server.send(status, "text/html; charset=utf-8", page);
}

String nonceField(const char *nonce) {
  return String(F("<input type='hidden' name='nonce' value='")) + nonce + F("'>");
}

String urlLink(const String &url) {
  return String(F("<p><a href='")) + url + F("'>") + url + F("</a></p>");
}

void showSetup(int status) {
  if (networkState == NetworkState::Joining) {
    sendPage(status, F("<p>正在連線，請先保持連在開關的設定 Wi-Fi。</p>"), true);
    return;
  }
  if (networkState == NetworkState::Joined) {
    String content = F("<p>已加入家中 Wi-Fi。請讓手機切回家中 Wi-Fi，"
                       "約 90 秒後即可用下方網址操作。帳號為 light，密碼與設定 Wi-Fi 相同。</p>");
    content += urlLink(String(F("http://")) + WiFi.localIP().toString() + "/");
    if (mdnsActive) {
      content += urlLink(String(F("http://")) + hostname + ".local/");
    }
    sendPage(status, content);
    return;
  }
  String content = String(F("<p>")) + setupMessage +
                   F("</p><p>輸入家中 2.4 GHz Wi-Fi 的名稱和密碼。</p>"
                     "<form method='post' action='/join'>");
  content += nonceField(setupNonce);
  content += F("<label>Wi-Fi 名稱<input name='ssid' maxlength='32' required></label>"
               "<label>Wi-Fi 密碼<input name='password' type='password' maxlength='64'></label>"
               "<button>連線</button></form>");
  sendPage(status, content);
}

bool validPassword(const String &password) {
  const size_t length = password.length();
  if (length == 0) {
    return true;
  }
  if (length < 8 || length > 64) {
    return false;
  }
  for (size_t i = 0; i < length; ++i) {
    const char c = password[i];
    const bool hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
                     (c >= 'A' && c <= 'F');
    if (length == 64 ? !hex : (c < 32 || c > 126)) {
      return false;
    }
  }
  return true;
}

void handleJoin() {
  if (!fromSetupNetwork() || server.arg("nonce") != setupNonce) {
    sendPage(403, F("<p>請從開關的設定 Wi-Fi 重新開啟設定頁。</p>"));
    return;
  }
  if (networkState != NetworkState::Setup) {
    showSetup(409);
    return;
  }
  const String ssid = server.arg("ssid");
  const String password = server.arg("password");
  if (!ssid.length() || ssid.length() > 32 || ssid.length() != strlen(ssid.c_str()) ||
      !server.hasArg("password") || !validPassword(password)) {
    setupMessage = "Wi-Fi 名稱或密碼格式有誤，請重新輸入。";
    showSetup(400);
    return;
  }
  credentials = {};
  memcpy(credentials.ssid, ssid.c_str(), ssid.length());
  memcpy(credentials.password, password.c_str(), password.length());
  sendPage(202, F("<p>正在連線。若手機切離設定網路，請重新連回來。</p>"), true);
  joinWifi();
}

bool allowControl() {
  if (apActive || networkState != NetworkState::Online ||
      !stationReady() || server.client().localIP() != WiFi.localIP()) {
    sendPage(403, F("<p>請等設定 Wi-Fi 關閉後，再從家中 Wi-Fi 開啟控制頁。</p>"));
    return false;
  }
  const uint32_t mask = WiFi.subnetMask();
  if (!mask || (uint32_t(server.client().remoteIP()) & mask) != (uint32_t(WiFi.localIP()) & mask)) {
    sendPage(403, F("<p>請從同一個家用區網連線。</p>"));
    return false;
  }
  if (!server.authenticate("light", setupKey)) {
    server.sendHeader("Cache-Control", "no-store");
    server.requestAuthentication(DIGEST_AUTH, "WallSwitch");
    return false;
  }
  return true;
}

void showControls(const char *message) {
  String content = String(F("<p>")) + message +
                   F("</p><p>開燈和關燈會按下實體開關的指定側。此裝置沒有燈況感測器。</p>"
                     "<form method='post' action='/move'>");
  char health[112];
  snprintf(health, sizeof(health), "<p class='health' data-uptime='%lu' data-reset='%s' data-rejoins='%lu'></p>",
           static_cast<unsigned long>(millis() / 1000), resetReasonName(), static_cast<unsigned long>(rejoinCount));
  content = health + content;
  content += nonceField(controlNonce);
  content += F("<button name='side' value='on'>開燈</button>"
               "<button name='side' value='off'>關燈</button>"
               "<button name='side' value='center'>對中</button></form>");
  sendPage(200, content);
}

// Plain-text nonce for iPhone Shortcuts: Shortcuts turns the HTML page into
// rich text and drops the hidden nonce field, so it cannot read it from "/".
void handleNonce() {
  if (!allowControl()) {
    return;
  }
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "text/plain", controlNonce);
}

uint32_t pulseDuty(uint16_t micros) {
  return (uint32_t(micros) * (1UL << PwmBits) + 10000) / 20000;
}

void handleMove() {
  if (!allowControl()) {
    return;
  }
  if (server.arg("nonce") != controlNonce) {
    sendPage(403, F("<p>操作憑證已失效。請重新開啟控制頁。</p>"));
    return;
  }
  const String side = server.arg("side");
  uint16_t pulse = CenterPulseUs;
  if (side == "on") {
    pulse = OnPulseUs;
  } else if (side == "off") {
    pulse = OffPulseUs;
  } else if (side != "center") {
    sendPage(400, F("<p>無效的操作。</p>"));
    return;
  }
  if (!servoReady) {
    sendPage(503, F("<p>舵機控制無法啟動。</p>"));
    return;
  }
  const bool centeredOnly = pulse == CenterPulseUs;
  const bool sentPress = centeredOnly || ledcWrite(ServoPin, pulseDuty(pulse));
  if (!centeredOnly && sentPress) {
    delay(350);
  }
  const bool sentCenter = ledcWrite(ServoPin, pulseDuty(CenterPulseUs));
  delay(400);
  const bool released = ledcWrite(ServoPin, 0);
  if (!released) {
    ledcDetach(ServoPin);
    pinMode(ServoPin, OUTPUT);
    digitalWrite(ServoPin, LOW);
    servoReady = false;
  }
  if (!sentPress || !sentCenter || !released) {
    sendPage(503, F("<p>舵機訊號輸出失敗。請檢查舵臂位置。</p>"));
    return;
  }
  server.sendHeader("Cache-Control", "no-store");
  server.sendHeader("Location", "/?moved=1");
  server.send(303);
}

void handleHome() {
  if (fromSetupNetwork()) {
    showSetup();
  } else if (allowControl()) {
    showControls(server.arg("moved") == "1" ? "舵機已完成指令。" : "");
  }
}

void handleSerial() {
  static char command[24];
  static size_t length = 0;
  static bool tooLong = false;
  while (Serial.available()) {
    const int next = Serial.read();
    if (next == '\r') {
      continue;
    }
    if (next == '\n') {
      command[length] = '\0';
      if (!tooLong && strcmp(command, "info") == 0) {
        printSetupInfo();
        if (stationReady()) {
          Serial.printf("LAN_URL=http://%s/\n", WiFi.localIP().toString().c_str());
        }
      } else if (!tooLong && strcmp(command, "wifi setup") == 0) {
        startSetup("請輸入新的 Wi-Fi 設定。");
        if (apActive) {
          printSetupInfo();
        }
      }
      length = 0;
      tooLong = false;
    } else if (length < sizeof(command) - 1) {
      command[length++] = char(next);
    } else {
      tooLong = true;
    }
  }
}

void setup() {
  pinMode(ServoPin, OUTPUT);
  digitalWrite(ServoPin, LOW);
  servoReady = ledcAttach(ServoPin, 50, PwmBits) && ledcWrite(ServoPin, 0);
  Serial.begin(115200);
  const esp_task_wdt_config_t watchdog = {LoopWatchdogMs, 0, true};
  esp_task_wdt_reconfigure(&watchdog);
  esp_task_wdt_add(nullptr);
  Serial.printf("RESET_REASON=%s\n", resetReasonName());
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  // Keep the radio awake so Hermes requests are answered right away.
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(false);
  const uint64_t mac = ESP.getEfuseMac();
  const auto idHigh = static_cast<unsigned int>(mac >> 32) & 0xffff;
  const auto idLow = static_cast<unsigned long>(mac);
  snprintf(hostname, sizeof(hostname), "wallswitch-%04x%08lx", idHigh, idLow);
  snprintf(apSsid, sizeof(apSsid), "WallSwitch-%04X%08lX", idHigh, idLow);
  storageReady = loadSetupKey();
  if (!storageReady) {
    Serial.println("ERROR=SETUP_KEY_STORAGE");
    return;
  }
  randomHex(setupNonce, sizeof(setupNonce) - 1);
  randomHex(controlNonce, sizeof(controlNonce) - 1);
  if (loadWifi()) {
    joinWifi();
  } else {
    startSetup("");
  }
  server.on("/", HTTP_GET, handleHome);
  server.on("/join", HTTP_POST, handleJoin);
  server.on("/move", HTTP_POST, handleMove);
  server.on("/nonce", HTTP_GET, handleNonce);
  server.onNotFound([] { sendPage(404, F("<p>請手動開啟裝置網址。</p>")); });
  server.begin();
}

void loop() {
  esp_task_wdt_reset();
  if (storageReady) {
    handleSerial();
    updateNetwork();
    watchConnection();
    server.handleClient();
    if (apActive && millis() - lastSetupPrint >= SetupPrintIntervalMs) {
      printSetupInfo();
    }
  }
  delay(2);
}
