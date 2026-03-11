#include <Wire.h>
#include <MPU6050.h>
#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>

MPU6050 mpu;

const char* ssid         = "Parthiv's S23";
const char* password     = "Parthiv@02";
const char* uploadURL    = "http://10.166.166.44:5000/upload";
const char* heartbeatURL = "http://10.166.166.44:5000/esp_heartbeat";

WiFiClient client;
HTTPClient http;

unsigned long lastSend = 0;

// Samples to average per upload (used always — fast enough at 100ms interval)
const int NUM_SAMPLES = 5;

void getAngles(float &angleDP, float &angleIE) {
  long sumAx = 0, sumAy = 0, sumAz = 0;
  for (int i = 0; i < NUM_SAMPLES; i++) {
    int16_t ax, ay, az;
    mpu.getAcceleration(&ax, &ay, &az);
    sumAx += ax;
    sumAy += ay;
    sumAz += az;
    delay(5); // 5ms x 5 samples = 25ms total — fast and still smoothed
  }
  // Cast to float FIRST before dividing — avoids integer truncation for small values
  float Ax = ((float)sumAx / NUM_SAMPLES) / 16384.0;
  float Ay = ((float)sumAy / NUM_SAMPLES) / 16384.0;
  float Az = ((float)sumAz / NUM_SAMPLES) / 16384.0;

  angleDP = (atan2(Ax, Az) * 180.0 / PI) + 90.0;
  angleIE = (atan2(Ay, Az) * 180.0 / PI) + 90.0;
}

void setup() {
  Serial.begin(9600);
  Wire.begin(4, 5);
  mpu.initialize();

  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("Connected");
}

void loop() {
  // Send every 100ms — fast enough for responsive calibration,
  // server handles whether it's a calibration read or session read
  if (millis() - lastSend >= 100) {
    lastSend = millis();

    float angleDP, angleIE;
    getAngles(angleDP, angleIE);

    // Classify and print all 4 movements
    String dpLabel = (angleDP >= 90.0) ? "Dorsiflexion  " : "Plantarflexion";
    String ieLabel = (angleIE >= 90.0) ? "Eversion  "     : "Inversion     ";

    Serial.println("------------------------------------");
    Serial.print(dpLabel); Serial.print("  : "); Serial.print(angleDP, 1); Serial.println(" deg");
    Serial.print(ieLabel); Serial.print("  : "); Serial.print(angleIE, 1); Serial.println(" deg");

    String json = "{\"angleDP\":" + String(angleDP, 1) +
                  ",\"angleIE\":" + String(angleIE, 1) + "}";

    http.begin(client, uploadURL);
    http.addHeader("Content-Type", "application/json");
    int code = http.POST(json);
    if (code > 0) Serial.println(http.getString());
    http.end();

    // Heartbeat is covered by upload — only send separately
    // every 1s to avoid flooding
    static unsigned long lastHB = 0;
    if (millis() - lastHB >= 1000) {
      lastHB = millis();
      http.begin(client, heartbeatURL);
      http.addHeader("Content-Type", "application/json");
      http.POST("{}");
      http.end();
    }
  }
}
